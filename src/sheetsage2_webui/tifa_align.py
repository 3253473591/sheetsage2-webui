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
import os
import sys
import types
from typing import Any, Sequence

#: TIFA 官方建议的上下文上限（秒）。切片超过这个就不保证稳定。
MAX_ALIGN_SECONDS = 50.0

#: 我们的语言名 -> TIFA 的 G2P 语言代码。TIFA 1.0 只覆盖这四种。
LANGUAGE_TO_TIFA = {
    "Chinese": "zh",
    "English": "en",
    "Japanese": "ja",
    # 粤语：TIFA 的 yue 模型实测覆盖很差（《你唔爱我啦》LRC 只出 227/439 字），
    # 而粤语歌词本来就是汉字，用中文 G2P 反而 370 字。默认走 zh。
    "Cantonese": "zh",
    # TIFA 1.0 **没有韩语**：这里映射成 `ko`，让调用方能识别“TIFA 不支持、
    # 要走 Qwen 对齐器”。不映射的话 `tifa_language("Korean")` 会静默落回 `zh`，
    # 韩文被中文 G2P 处理 → 一个时间戳都出不来。
    "Korean": "ko",
}

_VENDOR = Path(__file__).resolve().parent / "vendor" / "tifa"

#: TextGrid 里 `texts` 层的区间。
_INTERVAL = re.compile(
    r'xmin = ([\d.]+)\s*\n\s*xmax = ([\d.]+)\s*\n\s*text = "([^"]*)"'
)

_LOCK = threading.Lock()

#: 推理真正需要的第三方包（用于「依赖缺失就退回 Qwen」，别让整条链路炸掉）。
_REQUIRED_IMPORTS = ("lightning", "omegaconf", "g2pflow", "einops", "h5py",
                     "colorednoise", "matplotlib")


def ensure_available() -> None:
    """快速检查 TIFA 的依赖在不在；缺了抛 ImportError（调用方负责退回 Qwen）。"""
    vendor = str(_VENDOR)
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    import importlib

    for name in _REQUIRED_IMPORTS:
        importlib.import_module(name)


def unidic_dir() -> Path | None:
    """本地 UniDic 词典目录（`sys.dic` + `mecabrc` 都在才算）。

    查找顺序：环境变量 `SHEETSAGE_UNIDIC_DIR` → `models/unidic` → `models/unidic/dicdir`。
    日文 G2P 需要它（g2pflow 的 `japanese-mecab` 转换器）。
    """
    env = os.environ.get("SHEETSAGE_UNIDIC_DIR")
    if env:
        # 显式指定就**只认它**：测试里用这个把"没有词典"钉死，
        # 也不会因为开发机上装了 models/unidic 而串味。
        candidates = [Path(env)]
    else:
        root = Path(__file__).resolve().parents[2]
        candidates = [root / "models" / "unidic", root / "models" / "unidic" / "dicdir"]
    for candidate in candidates:
        if not (candidate / "sys.dic").is_file():
            continue
        rc = candidate / "mecabrc"
        if not rc.is_file():
            # UniDic 官方 zip 里没有 mecabrc（MeCab 默认去系统目录找），
            # fugashi 又要求 `-r` 指向的文件存在，所以这里补一个最小的。
            try:
                rc.write_text(f"dicdir = {candidate.as_posix()}\n", encoding="utf-8")
            except OSError:
                return None
        return candidate
    return None


def ja_g2p_available() -> bool:
    """日文 G2P 能不能用：本地有 UniDic 词典且已安装 `fugashi`（不要求 pip 装 unidic）。"""
    import importlib.util

    return unidic_dir() is not None and importlib.util.find_spec("fugashi") is not None


def _ensure_unidic_module(dicdir: Path) -> None:
    """g2pflow 会 `import unidic` 只为拿 `DICDIR`；没装包时给一个只暴露 DICDIR 的桩。

    真正读词典用的是传进去的 `unidic_dir`，所以桩不影响对齐质量。
    """
    if "unidic" in sys.modules:
        return
    try:
        import unidic  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    module = types.ModuleType("unidic")
    module.DICDIR = str(dicdir)
    download = types.ModuleType("unidic.download")

    def download_version(*_args, **_kwargs):  # pragma: no cover - 桩不会被调用
        raise RuntimeError("UniDic 由 models/unidic 提供，不需要联网下载")

    download.download_version = download_version
    module.download = download
    sys.modules["unidic"] = module
    sys.modules["unidic.download"] = download


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
    if any("\uac00" <= char <= "\ud7a3" or "\u1100" <= char <= "\u11ff"
           or "\u3130" <= char <= "\u318f" for char in text):
        return "ko"
    if any("\u3040" <= char <= "\u30ff" for char in text):
        return "ja"
    latin = sum(1 for char in text if char.isascii() and char.isalpha())
    hanzi = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    if latin > hanzi:
        return "en"
    return "zh"


def parse_text_grid(path: Path) -> list[tuple[str, float, float]]:
    """从 TextGrid 的 `texts` 层取 (文本, 起, 止)，跳过 `SP` 和零宽度。"""
    spans, _phones = parse_grid_tiers(path)
    return spans


def parse_grid_tiers(path: Path) -> tuple[list[tuple[str, float, float]],
                                          list[tuple[float, float, str]]]:
    """同时读 `texts`（词）和 `phones`（音素）。音素用来切音节。"""
    text = path.read_text(encoding="utf-8")

    def tier(name: str) -> list[tuple[float, float, str]]:
        block = re.search(rf'name = "{name}"(.*?)(?=\n\titem|\Z)', text, re.S)
        if block is None:
            return []
        rows = _INTERVAL.findall(block.group(1))
        return [(float(a), float(b), c.strip()) for a, b, c in rows if c.strip()]

    words = [(label, start, end) for start, end, label in tier("texts")
             if label != "SP" and end - start > 1e-6]
    phones = [(start, end, label) for start, end, label in tier("phones")
              if label != "SP" and end - start > 1e-6]
    return words, phones


#: ARPAbet 里能当音节核的音素（TIFA 的英文音素就是这一套）。
#: `ax` 是弱读的 schwa——「captain」的第二个音节就是 `t ax n`，漏了它就会把
#: 双音节词当成单音节。
_VOWELS = frozenset({"aa", "ae", "ah", "ao", "aw", "ay", "ax", "eh", "er", "ey",
                     "ih", "iy", "ow", "oy", "uh", "uw", "ix", "ux", "el", "em", "en"})


def _phone_name(text: str) -> str:
    """TIFA 的 phones 标签带语言前缀（`en/eh`、`zh/ao`），去掉前缀再判断。

    混唱切片里英文词的音素长这样：`en/eh` `en/v` `en/r` `en/iy` …；
    不剥前缀就永远匹配不到 `_VOWELS`，`parts` 会一直是空。
    """
    head, _, tail = text.partition("/")
    return tail or head


def syllable_onsets(phones: Sequence[tuple[float, float, str]],
                    start: float, end: float) -> tuple[float, ...]:
    """一个英文词内部「第二、三…个音节」的起点时刻。

    用的是最大开头原则的简化版：两个元音之间
      - 没有辅音 → 分界就在后一个元音；
      - 一个辅音 → 那个辅音归后一个音节；
      - 两个以上 → 只有最后一个辅音归后一个音节（captain 的 p|t、metal 的 e|t）。
    够用来决定「这个音是接着上一个音节唱，还是进下一个音节」。
    """
    inside = [(a, b, label) for a, b, label in phones
              if start - 1e-6 <= a < end - 1e-6]
    vowel_index = [i for i, (_a, _b, label) in enumerate(inside)
                   if _phone_name(label).lower().rstrip("012") in _VOWELS]
    onsets: list[float] = []
    for previous, current in zip(vowel_index, vowel_index[1:]):
        consonants = inside[previous + 1:current]
        if not consonants:
            onsets.append(inside[current][0])
        else:
            onsets.append(consonants[-1][0])
    return tuple(onsets)


def word_syllable_onsets(phones: Sequence[tuple[float, float, str]],
                         label: str, start: float, end: float, *,
                         language: str = "zh") -> tuple[float, ...]:
    """这个词内部的「第二、三…个音节」起点；汉字/假名词一律返回空。

    整首是英文时（`language == "en"`）全部词都切；**中文歌里夹的英文词也切**——
    TIFA 的 `language=[zh, en]` 让拉丁词走英文 G2P，phones 层里本来就是 ARPAbet。
    但中文词是拼音（`ao`/`en`/`er` 和 ARPAbet 撞名），所以只能按**词面**判断，
    不能按音素判断，不然汉字会被切出假的音节边界。
    """
    latin = any("a" <= ch.lower() <= "z" for ch in label)
    if language != "en" and not latin:
        return ()
    return syllable_onsets(phones, start, end)


def _to_list(value: Any) -> list[Any]:
    return value.tolist() if hasattr(value, "tolist") else list(value)


def _preserve_zero_spans(
    spans: Sequence[tuple[float, float]],
    groups: Sequence[int],
    total_duration: float,
) -> list[tuple[float, float]]:
    """在允许的空档内给 0 宽跳过音素保留 1ms 区间，避免静默丢字。"""
    original = [(int(round(a * 1000)), int(round(b * 1000))) for a, b in spans]
    duration_ms = int(round(total_duration * 1000))
    intervals: list[tuple[int, int]] = []
    n = len(original)
    i = 0
    while i < n:
        left = intervals[-1][1] if intervals else 0
        onset, offset = original[i]
        if offset > onset:
            onset = max(onset, left)
            intervals.append((onset, max(offset, onset + 1)))
            i += 1
            continue
        end = i + 1
        while end < n and original[end][0] >= original[end][1]:
            end += 1
        right = max(left, original[end][0] if end < n else duration_ms)
        gap_index = i
        while gap_index <= end:
            if gap_index == 0 or gap_index == n or groups[gap_index - 1] != groups[gap_index]:
                break
            gap_index += 1
        left_count = min(gap_index, end) - i
        right_count = end - i - left_count
        for k in range(left_count):
            intervals.append((left + k, left + k + 1))
        right_start = max(left + left_count, right - right_count)
        for k in range(right_count):
            intervals.append((right_start + k, right_start + k + 1))
        i = end
    return [(a / 1000.0, b / 1000.0) for a, b in intervals]


def extract_alignment_from_result(
    result: dict[str, Any],
    *,
    timestep: float,
    skip_handling: str = "preserve",
) -> tuple[list[tuple[str, float, float, float | None]],
           list[tuple[float, float, str]]]:
    """直接从 TIFA 内存推理输出提取 `(词列表[(label, start, end, confidence)], phones)`。

    相比写临时 TextGrid 再正则回读：
    1. 保留 `result["similarity"]` 计算出的逐词声学置信度（跳过音素置信度为 0.0）；
    2. `skip_handling="preserve"` 不再静默丢弃零宽词，避免漏字及 `_labels_for` 长度错配；
    3. 零磁盘 TextGrid I/O。
    """
    raw_spans = _to_list(result["spans"])
    n = len(raw_spans)
    if n == 0:
        return [], []
    words_ids = [int(v) for v in _to_list(result["words"])]
    groups = [int(v) for v in _to_list(result["groups"])]
    phonemes = [str(p) for p in result["phonemes"]]
    texts = [str(t) for t in result["texts"]]

    # 逐音素声学置信度：按预测帧区间对 similarity[:, idx] 取均值（与 TIFA compute_confidence 同口径）
    similarity = result.get("similarity")
    phone_confs: list[float | None] = [None] * n
    if similarity is not None:
        t_max = int(similarity.shape[0])
        for idx in range(n):
            f0 = max(0, int(round(raw_spans[idx][0])))
            f1 = min(t_max, int(round(raw_spans[idx][1])))
            if f1 > f0:
                col = similarity[f0:f1, idx]
                val = float(col.float().mean().item()) if hasattr(col, "float") else (
                    sum(float(x) for x in col) / len(col))
                phone_confs[idx] = val
            else:
                phone_confs[idx] = 0.0

    sec_spans = [(round(float(a) * timestep, 3), round(float(b) * timestep, 3))
                 for a, b in raw_spans]
    spec = result.get("spectrogram")
    total_duration = (round(int(spec.shape[0]) * timestep, 3)
                      if spec is not None else max((b for _, b in sec_spans), default=0.0))

    if skip_handling == "omit":
        keep = [i for i in range(n) if sec_spans[i][1] > sec_spans[i][0]]
        if not keep:
            return [], []
        sec_spans = [sec_spans[i] for i in keep]
        words_ids = [words_ids[i] for i in keep]
        groups = [groups[i] for i in keep]
        phonemes = [phonemes[i] for i in keep]
        phone_confs = [phone_confs[i] for i in keep]
        n = len(keep)
    else:
        sec_spans = _preserve_zero_spans(sec_spans, groups, total_duration)

    eps = 0.001
    phone_intervals: list[tuple[float, float, str]] = []
    for idx in range(n):
        onset, offset = sec_spans[idx]
        if phone_intervals and onset < phone_intervals[-1][1]:
            onset = phone_intervals[-1][1]
        if offset <= onset:
            offset = onset + eps
        phone_intervals.append((onset, offset, phonemes[idx]))

    word_rows: list[tuple[str, float, float, float | None]] = []
    i = 0
    while i < n:
        owner = words_ids[i]
        j = i + 1
        while j < n and words_ids[j] == owner:
            j += 1
        label = texts[owner - 1].strip()
        onset = phone_intervals[i][0]
        offset = phone_intervals[j - 1][1]
        if word_rows and onset < word_rows[-1][2]:
            onset = word_rows[-1][2]
        if offset <= onset:
            offset = onset + eps
        confs = [phone_confs[k] for k in range(i, j) if phone_confs[k] is not None]
        word_conf = (round(sum(confs) / len(confs), 4) if confs else None)
        if label and label != "SP" and offset - onset > 1e-6:
            word_rows.append((label, onset, offset, word_conf))
        i = j

    phones_clean = [(a, b, p) for a, b, p in phone_intervals
                    if p.strip() and p.strip() != "SP" and b - a > 1e-6]
    return word_rows, phones_clean


#: 小写假名：跟在前一个假名后面，算同一モーラ。
_SMALL_KANA = set("ゃゅょぁぃぅぇぉゎゕゖャュョァィゥェォヮヵヶ")


def _kata_to_hira(text: str) -> str:
    out = []
    for char in text:
        code = ord(char)
        if 0x30A1 <= code <= 0x30F6:
            out.append(chr(code - 0x60))
        else:
            out.append(char)
    return "".join(out)


def split_mora(kana: str) -> list[str]:
    """把假名串切成モーラ：小写假名（ゃゅょ…）并到前一拍；っ/ん/ー 各自算一拍。"""
    mora: list[str] = []
    for char in kana:
        if mora and char in _SMALL_KANA:
            mora[-1] += char
        else:
            mora.append(char)
    return mora


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
        self._tagger = None

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

    def _expand_items(self, words, phones):
        """TIFA 的 word 层 -> 我们要的粒度。

        - 日语：MeCab 给的是词（`語れ`），但挂字要**モーラ**（か/た/れ）。
          用 fugashi 取读音、切モーラ、把词的时间按モーラ均分。
        - 其它语言：原样返回（汉字本来就是一字一项），并透传 `confidence`。
        """
        if str(self.language) not in ("ja", "jpn"):
            out = []
            for row in words:
                label, start, end = row[0], row[1], row[2]
                conf = row[3] if len(row) > 3 else None
                out.append((label, start, end,
                            word_syllable_onsets(phones, label, start, end,
                                                 language=self.language),
                            conf))
            return out
        items = []
        for row in words:
            label, start, end = row[0], row[1], row[2]
            conf = row[3] if len(row) > 3 else None
            # 日英混唱里的纯拉丁词保持整词 + 音节起点，不要交给 fugashi 按字母拼读成假名或剁成单字母
            if any("a" <= ch.lower() <= "z" for ch in label) and all(
                ord(ch) < 0x3000 for ch in label
            ):
                items.append((label, start, end,
                              word_syllable_onsets(phones, label, start, end, language="en"),
                              conf))
                continue
            mora = self._mora_of(label)
            if not mora:
                items.append((label, start, end, (), conf))
                continue
            step = (end - start) / len(mora)
            for index, kana in enumerate(mora):
                items.append((kana, start + index * step, start + (index + 1) * step,
                              (), conf))
        return items

    def _mora_of(self, word: str) -> list[str]:
        """词的假名读音 -> モーラ列表；拿不到读音就原样切。"""
        reading = ""
        try:
            reading = self._reading(word)
        except Exception:                  # noqa: BLE001 - 读音失败就退回原字
            reading = ""
        if not reading:
            reading = _kata_to_hira(word)
        return split_mora(reading)

    def _reading(self, word: str) -> str:
        if self._tagger is None:
            dicdir = unidic_dir()
            if dicdir is None:
                return ""
            import fugashi
            rc = dicdir / "mecabrc"
            self._tagger = fugashi.Tagger(f'-r "{rc.as_posix()}" -d "{dicdir.as_posix()}"')
        tokens = self._tagger(word)
        if not tokens:
            return ""
        parts = []
        for token in tokens:
            feature = getattr(token, "feature", None)
            reading = (getattr(feature, "pron", None) or getattr(feature, "kana", None)
                       or getattr(token, "surface", ""))
            parts.append(str(reading))
        return _kata_to_hira("".join(parts))

    def _apply_unidic(self) -> None:
        """日语：有本地 UniDic 就把 `unidic_dir` 塞给 japanese-mecab 转换器。"""
        if str(self.language) not in ("ja", "jpn"):
            return
        dicdir = unidic_dir()
        if dicdir is None:
            return
        for converter in getattr(self._g2p, "converters", ()):
            if getattr(converter, "id", "") == "japanese-mecab":
                converter.kwargs.setdefault("unidic_dir", str(dicdir))
        _ensure_unidic_module(dicdir)

    def release(self) -> None:
        self._backend = self._vocab = self._g2p = None
        self._tagger = None
        import gc

        gc.collect()
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:      # noqa: BLE001 - 没有 CUDA 也无所谓
            pass

    # ------------------------------------------------------------------ 对齐
    def align_many(self, items: Sequence[tuple[Path, str]]
                   ) -> list[list[tuple[str, float, float, tuple[float, ...], float | None]]]:
        """给多段 (音频, 文本) 一次对齐完。

        每段返回 `(文本, 起, 止, 音节起点, 声学置信度)`；第四项只对**拉丁词**有意义
        （用来区分「接着上一个音节唱」和「进下一个音节」），第五项来自 TIFA 的
        `similarity` 帧相似度均值（直接内存回调采集，不再写临时 TextGrid）。
        """
        if not items:
            return []
        self.load()
        self._apply_unidic()
        import lightning.pytorch.callbacks as _pl_callbacks
        from inference.api import run_inference
        from inference.data import AudioTextDataset

        results: list[list[tuple[str, float, float, tuple[float, ...], float | None]]] = [
            [] for _ in items]
        # 文本要写成同名 .txt 放在音频旁边，AudioTextDataset 靠同名查找
        filemap: dict[str, Path] = {}
        owners: dict[str, int] = {}
        for index, (audio, text) in enumerate(items):
            audio = Path(audio)
            audio.with_suffix(".txt").write_text(text, encoding="utf-8")
            key = f"s{index:03d}_{audio.stem}"
            filemap[key] = audio
            owners[key] = index

        collected: dict[str, tuple[list[tuple[str, float, float, float | None]],
                                   list[tuple[float, float, str]]]] = {}
        timestep = self._backend.timestep

        class _CollectAlignmentCallback(_pl_callbacks.Callback):
            def on_predict_batch_end(self, trainer, pl_module, outputs, batch,
                                     *args, **kwargs) -> None:
                for item in outputs or ():
                    ident = str(item.get("identifier") or "")
                    if ident:
                        collected[ident] = extract_alignment_from_result(
                            item, timestep=timestep, skip_handling="preserve")

        dataset = AudioTextDataset(
            filemap=filemap, g2p_config=self._g2p, g2p_root=self.model.parent,
            vocabulary=self._vocab, audio_sample_rate=self._backend.sample_rate,
            # 英文 LSTM 转换器一并激活：中文/粤语切片混着英文词（"Oh my god"）时，
            # 只传 zh 会让 G2P 抛 G2PConversionError，而数据集把异常吞成 skip——
            # 整个切片静默出不了对齐结果（实测《爱x无限大》5 段里 3 段全灭）。
            # 模型 config 里本来就配了 en 转换器，只是被语言过滤挡住了；
            # 拉丁词由它认领，汉字照走拼音，纯中文不受影响。
            language=[self.language, "en"],
            # 默认的 "discard" 会因为**一个**没见过的音素丢掉整段
            # （实测粤语《富士山下》「调」的 eu 就让整个 50s 窗口归零）。
            # 丢掉那条发音路径就行，其余照常对齐。
            oov_handling="force",
        )
        callbacks = [_CollectAlignmentCallback()]
        with _LOCK:
            run_inference(backend=self._backend, dataset=dataset,
                          callbacks=callbacks, batch_size=self.batch_size,
                          num_workers=0, precision=self.precision, mode="predict")
        for key, owner_index in owners.items():
            entry = collected.get(key)
            if entry is not None:
                words, phones = entry
                results[owner_index] = self._expand_items(words, phones)
        return results
