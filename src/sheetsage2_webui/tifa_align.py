"""TIFA 强制对齐后端（可选，`aligner="tifa"` 时用）。

上游是 [openvpi/TIFA](https://github.com/openvpi/TIFA)：**源码 MIT**，模型权重
CC BY-NC-SA 4.0，约 40M 参数 / fp16 后 76MB。推理用的代码子集 vendored 在
`vendor/tifa/`（MIT，见那里的 LICENSE）。

和 Qwen3-ForcedAligner 的差别就在**边界从哪来**：TIFA 按帧做 Viterbi 解码，每个词
必然分到一段（实测零宽度 0），Qwen 那边是 13~17%。同一套音符、同一个贪心下
《爱情转移》hit 89.4% → 99.3%。

**硬限制：上下文约 1 分钟**（官方说明：更长会不稳），所以切片上限必须 ≤50s，
见 `MAX_ALIGN_SECONDS`。

这个模块只负责「一段音频 + 它的文本 → 一组 (text, start, end)」；
切片、偏移、与音符的匹配都在调用方（`asr.py` / `lyrics.py`）。
"""
from __future__ import annotations

import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Sequence

#: TIFA 官方建议的上下文上限（秒）。切片超过这个就不保证稳定。
MAX_ALIGN_SECONDS = 50.0

#: 我们的语言名 -> TIFA 的 G2P 语言代码。TIFA 1.0 只覆盖这四种。
LANGUAGE_TO_TIFA = {
    "Chinese": "zh",
    "English": "en",
    "Japanese": "ja",
    "Cantonese": "yue",
}

_VENDOR = Path(__file__).resolve().parent / "vendor" / "tifa"

#: TextGrid 里 `texts` 层的区间。
_INTERVAL = re.compile(
    r'xmin = ([\d.]+)\s*\n\s*xmax = ([\d.]+)\s*\n\s*text = "([^"]*)"'
)

_LOCK = threading.Lock()


def default_model_path() -> Path:
    """`models/TIFA-1.0/model.pt`（和 bootstrap 的约定一致）。"""
    root = Path(__file__).resolve().parents[2]
    return root / "models" / "TIFA-1.0" / "model.pt"


def tifa_language(language: str | None, *, default: str = "zh") -> str:
    """我们的语言名（或 TIFA 代码）-> TIFA 的代码。认不出来的退回 `default`。

    TIFA 1.0 只覆盖 zh / ja / en / yue，没有韩语。
    """
    if not language:
        return default
    value = str(language).strip()
    if value in ("zh", "ja", "en", "yue"):
        return value
    return LANGUAGE_TO_TIFA.get(value, default)


def guess_language(text: str) -> str:
    """按文字猜 TIFA 语言。**只在用户选了「自动」、而 ASR 又判错时兜底**。

    实测 Qwen3-ASR 对一首日文歌（假名+汉字全对）报的是 `Chinese`——照它走，
    TIFA 会拿中文 G2P 去处理假名，整段 OOV 被丢掉（一个字都出不来）。
    假名 / 拉丁字母能靠字符集认出来；粤语不行（和普通话一样是汉字，只能靠用户选）。
    """
    text = text or ""
    if any("\u3040" <= char <= "\u30ff" for char in text):
        return "ja"
    latin = sum(1 for char in text if char.isascii() and char.isalpha())
    hanzi = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    if latin > hanzi:
        return "en"
    return "zh"


def parse_text_grid(path: Path) -> list[tuple[str, float, float]]:
    """从 TextGrid 的 `texts` 层取 (文本, 起, 止)，跳过 `SP` 和零宽度。"""
    text = path.read_text(encoding="utf-8")
    block = re.search(r'name = "texts"(.*?)(?=\n\titem|\Z)', text, re.S)
    if block is None:
        return []
    spans: list[tuple[str, float, float]] = []
    for start, end, label in _INTERVAL.findall(block.group(1)):
        label = label.strip()
        if not label or label == "SP":
            continue
        begin, finish = float(start), float(end)
        if finish - begin <= 1e-6:
            continue
        spans.append((label, begin, finish))
    return spans


class TifaAligner:
    """懒加载的 TIFA 对齐器。一个实例只加载一次模型，可以连续对齐很多段。"""

    def __init__(self, model: str | Path | None = None, *,
                 language: str | None = None,
                 precision: str = "16-mixed",
                 batch_size: int = 4) -> None:
        self.model = Path(model) if model else default_model_path()
        self.language = tifa_language(language)
        self.precision = precision
        self.batch_size = batch_size
        self._backend = None
        self._vocab = None
        self._g2p = None

    @property
    def loaded(self) -> bool:
        return self._backend is not None

    # ------------------------------------------------------------------ 加载
    def load(self) -> "TifaAligner":
        if self._backend is not None:
            return self
        if not self.model.is_file():
            raise FileNotFoundError(f"找不到 TIFA 权重：{self.model}")
        vendor = str(_VENDOR)
        if vendor not in sys.path:
            sys.path.insert(0, vendor)
        from inference.api import load_inference_model

        with _LOCK:
            # scope=ConfigurationScope.FA（0x1）：不传就按 0 加载，模型配置会缺字段
            backend, vocabulary, config = load_inference_model(self.model, scope=0x1)
        g2p = config.g2p
        if g2p is None:
            raise RuntimeError("TIFA 模型没有带 G2P 配置（config.yaml 缺 inference.g2p）")
        self._backend, self._vocab, self._g2p = backend, vocabulary, g2p
        return self

    def release(self) -> None:
        self._backend = self._vocab = self._g2p = None
        import gc

        gc.collect()
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:      # noqa: BLE001 - 没有 CUDA 也无所谓
            pass

    # ------------------------------------------------------------------ 对齐
    def align_many(self, items: Sequence[tuple[Path, str]]
                   ) -> list[list[tuple[str, float, float]]]:
        """给多段 (音频, 文本) 一次对齐完，返回每段的 (文本, 起, 止)。"""
        if not items:
            return []
        self.load()
        from inference.api import run_inference
        from inference.callbacks import SaveTextGridCallback
        from inference.data import AudioTextDataset

        results: list[list[tuple[str, float, float]]] = [[] for _ in items]
        # 文本要写成同名 .txt 放在音频旁边，AudioTextDataset 靠同名查找
        filemap: dict[str, Path] = {}
        owners: dict[str, int] = {}
        for index, (audio, text) in enumerate(items):
            audio = Path(audio)
            audio.with_suffix(".txt").write_text(text, encoding="utf-8")
            key = f"s{index:03d}_{audio.stem}"
            filemap[key] = audio
            owners[key] = index

        with tempfile.TemporaryDirectory(prefix="tifa-grid-") as grid_dir:
            dataset = AudioTextDataset(
                filemap=filemap, g2p_config=self._g2p, g2p_root=self.model.parent,
                vocabulary=self._vocab, audio_sample_rate=self._backend.sample_rate,
                language=[self.language],
                # 默认的 "discard" 会因为**一个**没见过的音素丢掉整段
                # （实测粤语《富士山下》「调」的 eu 就让整个 50s 窗口归零）。
                # 丢掉那条发音路径就行，其余照常对齐。
                oov_handling="force",
            )
            callbacks = [SaveTextGridCallback(
                output_dir=Path(grid_dir), language=self.language,
                timestep=self._backend.timestep, skip_handling="omit",
                save_pfml=False, fill="SP")]
            with _LOCK:
                run_inference(backend=self._backend, dataset=dataset,
                              callbacks=callbacks, batch_size=self.batch_size,
                              num_workers=0, precision=self.precision, mode="predict")
            for key, owner_index in owners.items():
                grid = Path(grid_dir) / f"{key}.TextGrid"
                if grid.is_file():
                    results[owner_index] = parse_text_grid(grid)
        return results
