"""模型持有者：进程内复用引擎、按工程参数重载、用完把显存还回去。

**为什么单独一层**：模型的加载/释放由显存和权重决定（几十秒一次、10G 卡上两个模型不能
同时留着），和"这一阶段跑什么、进度怎么翻译"是两类变化原因。`runner.py` 只管阶段，
这里只管"手上有没有那个模型、配置对不对"。

`Progress` 只用来写日志/进度，所以这一层也不依赖调度器。
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from ..asr import AsrConfig, AsrEngine
from ..engine import EngineConfig, SheetSage2Engine
from .job_models import Job, Progress
from .job_options import asr_model_label


class EngineProvider:
    """进程内复用一个 `SheetSage2Engine`（加载一次几十秒，之后每个任务都省掉）。

    页面上的「精度 / 设备」是**每个工程**自己的，所以这里按配置缓存：配置和手上这台不一样
    就把它放掉、按新配置重载。代价照实说——切一次要重新加载（本机实测 ~15s）。
    """

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig()
        self._engine: SheetSage2Engine | None = None
        self._key: tuple[Any, ...] | None = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    @property
    def device(self) -> str:
        return self._engine.device if self._engine is not None else self.config.device

    def describe(self) -> dict[str, Any]:
        return {"model": str(self.config.model_id), "device": self.device, "loaded": self.loaded}

    def config_for(self, job: Job) -> EngineConfig:
        """把工程的「精度 / 设备」叠到服务端基准配置上（权重精度不在这里，那是启动开关）。"""
        return replace(
            self.config,
            dtype=str(job.options.get("dtype") or self.config.dtype),
            device=str(job.options.get("device") or self.config.device),
        )

    @staticmethod
    def _key_of(config: EngineConfig) -> tuple[Any, ...]:
        return (str(config.model_id), config.device, config.dtype,
                config.weights_dtype, config.preset)

    def get(self, progress: Progress | None = None,
            config: EngineConfig | None = None) -> SheetSage2Engine:
        config = config or self.config
        key = self._key_of(config)
        with self._lock:
            if self._engine is not None and self._key != key:
                self._engine = None
                _free_cuda()
                if progress is not None:
                    progress.log("扒谱参数变了，按新参数重新加载模型")
            if self._engine is None:
                if progress is not None:
                    progress.set(0.01, "加载模型")
                engine = SheetSage2Engine(config)
                engine.load()
                self._engine = engine
                if progress is not None:
                    progress.log(f"模型就绪：device={engine.device}，"
                                 f"dtype={engine.effective_dtype}")
            self._key = key
            return self._engine

    def release(self) -> None:
        with self._lock:
            self._engine = None
            self._key = None
        _free_cuda()


class AsrProvider:
    """Qwen3-ASR 的懒加载持有者。用完 `release()` 把显存还回去。

    `choices` 是页面上「模型」那一栏的可选项（`{"1.7B": 路径, "0.6B": 路径}`），
    工程选了哪个就用哪个；换了模型同样要重载。
    """

    def __init__(self, config: AsrConfig,
                 choices: Mapping[str, Any] | None = None) -> None:
        self.config = config
        self.choices: dict[str, str] = {str(key): str(value)
                                        for key, value in (choices or {}).items()}
        if not self.choices:
            self.choices[asr_model_label(config.asr_model)] = str(config.asr_model)
        self._engine: AsrEngine | None = None
        self._key: tuple[Any, ...] | None = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    @property
    def model_name(self) -> str:
        return Path(str(self.config.asr_model)).name

    def describe(self) -> dict[str, Any]:
        return {"model": self.model_name, "device": self.config.device, "loaded": self.loaded,
                "models": dict(self.choices)}

    def config_for(self, job: Job) -> AsrConfig:
        """按工程选的「模型 / 对齐器」换一个权重目录；认不出的标签就用服务端默认。"""
        label = str(job.options.get("asr_model") or "")
        path = self.choices.get(label)
        config = self.config if path is None else replace(self.config, asr_model=path)
        aligner = str(job.options.get("aligner") or "").strip().lower()
        if aligner and aligner != config.aligner:
            config = replace(config, aligner=aligner)
        return config

    def get(self, progress: Progress | None = None,
            config: AsrConfig | None = None) -> AsrEngine:
        config = config or self.config
        key = (str(config.asr_model), config.device, config.max_vram)
        with self._lock:
            if self._engine is not None and self._key != key:
                engine, self._engine = self._engine, None
                if engine is not None:
                    engine.release()
                _free_cuda()
                if progress is not None:
                    progress.log("换了识别模型，重新加载")
            if self._engine is None:
                if progress is not None:
                    progress.set(0.06, f"加载 {Path(str(config.asr_model)).name}")
                engine = AsrEngine(config)
                engine.load()
                self._engine = engine
                if progress is not None:
                    progress.log(f"识别模型就绪：{Path(str(config.asr_model)).name} "
                                 f"@ {config.device}")
            self._key = key
            return self._engine

    def release(self) -> None:
        with self._lock:
            engine, self._engine = self._engine, None
            self._key = None
        if engine is not None:
            engine.release()
        _free_cuda()


def _free_cuda() -> None:
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:                                   # torch 没装/没卡都不该影响任务
        pass


#: 识别模型的兄弟目录怎么收成选项（`models/Qwen3-ASR-*`）。
def asr_model_choices(default_model: str | Path) -> dict[str, str]:
    """页面上能选的识别模型：默认权重目录的**兄弟目录**里所有 `Qwen3-ASR-*`。

    有什么权重就列什么——`/api/env` 的「模型」选项就是这么来的，所以前端不会给出一个
    本机根本不存在的选择。
    """
    path = Path(str(default_model))
    choices: dict[str, str] = {}
    if path.parent.is_dir():
        for child in sorted(path.parent.iterdir()):
            label = asr_model_label(child)
            if child.is_dir() and label != child.name:
                choices[label] = str(child)
    if str(default_model) not in choices.values():      # 兜底：至少认默认那个
        choices[asr_model_label(path)] = str(default_model)
    return choices


def pick_asr_model(models_root: Path) -> Path:
    """挑一个本机装了的识别模型：优先 1.7B，没有就退到最小集的 0.6B。

    以前这里写死 `Qwen3-ASR-1.7B`，而 `01_install.bat` 的默认档位（最小集）只下 0.6B
    ——新机器上第一次点「识别歌词」就会因为找不到权重而失败。
    """
    preferred = Path(models_root) / "Qwen3-ASR-1.7B"
    if (preferred / "config.json").is_file():
        return preferred
    for candidate in sorted(Path(models_root).glob("Qwen3-ASR-*")):
        if (candidate / "config.json").is_file():
            return candidate
    return preferred


def default_asr_config(models_root: Path | None = None) -> AsrConfig:
    """默认歌词识别配置：本机装了的识别模型 + TIFA（缺失时退回 Qwen）。"""
    from ..bootstrap import PROJECT_ROOT

    root = Path(models_root) if models_root is not None else PROJECT_ROOT / "models"
    aligner = root / "Qwen3-ForcedAligner-0.6B"
    return AsrConfig(
        asr_model=pick_asr_model(root),
        # 对齐器没装就别传：`from_pretrained` 会因为缺目录直接失败，
        # 而默认走的是随包的 TIFA，本来也不需要它（日语才用 Qwen 对齐器）。
        aligner_model=aligner if (aligner / "config.json").is_file() else None,
        language="Chinese",
        device="cuda:0",
    )
