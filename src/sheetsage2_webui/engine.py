"""SheetSage2 模型加载与转写调用。

这一层只负责三件事：把模型稳定地加载起来、把一次 `transcribe` 调用包装成
可观测的形式、在显存出问题时按需退回 CPU。产物解析交给 `result`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .bootstrap import PROJECT_ROOT, prepare, sync_remote_code_cache

DEFAULT_MODEL_ID = "m-a-p/SheetSage2"

#: `weights_dtype` 里的名字。真正的 `torch.dtype` 在 `load()` 里填（这样
#: `engine_load_kwargs` / `weights_dtype_choice` 是纯函数，测试不需要 import torch）。
WEIGHTS_DTYPE_NAMES = ("fp32", "bf16", "fp16")
WEIGHTS_DTYPES: dict[str, Any] = {}


def resolve_model_path(model_id: str) -> str:
    """`models/<名字>/` 存在就用本地目录，否则交给 huggingface_hub 按仓库 id 取。

    本地权重优先，是为了让工作区能脱网复现；仓库 id 仍然是兜底。
    """
    local = PROJECT_ROOT / "models" / Path(str(model_id).replace("\\", "/")).name
    if (local / "config.json").is_file():
        return str(local)
    return str(model_id)

def local_backbone_path() -> Path | None:
    """本机有没有 MERT 骨架（SheetSage2 是 LoRA 适配器格式时要它）。

    模型代码默认按 `base_model_name_or_path` 的仓库 id 走 `cached_file`，
    **脱网就炸**；我们把权重下在 `models/MERT-v2-FullSong`，找到就用它。
    """
    backbone = PROJECT_ROOT / "models" / "MERT-v2-FullSong"
    return backbone if (backbone / "model.safetensors").is_file() else None


ProgressFn = Callable[[Mapping[str, Any]], None]


def _is_out_of_memory(exc: BaseException) -> bool:
    if type(exc).__name__ in {"OutOfMemoryError", "CudaOutOfMemoryError"}:
        return True
    text = str(exc).lower()
    return "out of memory" in text or "cuda error: out of memory" in text


class TranscriptionError(RuntimeError):
    """转写失败，附带已完成的部分结果（如果有）。"""

    def __init__(self, message: str, partial: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.partial = dict(partial or {})


@dataclass
class EngineConfig:
    model_id: str = DEFAULT_MODEL_ID
    device: str = "auto"          # auto | cuda | cpu
    dtype: str = "bf16"           # bf16 | fp32（仅在 CUDA 上生效，CPU 强制 fp32）
    local_files_only: bool = False
    cache_dir: str | None = None
    cpu_fallback: bool = True     # GPU 显存不足时自动退回 CPU 重跑
    preset: str = "default"       # default | paper
    #: 权重的加载精度。`fp32`（默认，= 一直以来的行为）或 `bf16`/`fp16`。
    #:
    #: **为什么需要它**：SheetSage2 自己的 `from_pretrained` 明确只接受**单个设备**
    #: （`device_map="auto"` 会被它强转成 `cuda`，多设备会直接报错），所以
    #: ASR 那套"把一部分层放内存"的 offload **在扒谱阶段用不了**。
    #: 但它认 `torch_dtype`：把权重从 fp32 降到 bf16，显存大约减半
    #: （实测扒谱阶段峰值 3379MiB -> 见 scripts/probe_asr_offload.py）。小显存卡的唯一杠杆。
    weights_dtype: str = "fp32"


def engine_load_kwargs(config: EngineConfig) -> dict[str, Any]:
    """把 `EngineConfig` 折成 `AutoModel.from_pretrained` 的参数（只有 hub 相关的）。

    **两个"不要"**：
    1. **不要 `device_map`**：SheetSage2 的 `from_pretrained` 把 `auto` 强转成单个设备、
       多设备直接 `ValueError`——扒谱阶段没有 offload 这条路。
    2. **不要 `torch_dtype`**：它会让 transformers 在加载期就把默认 dtype 换掉，而模型要先
       在 fp32 下 `merge_lora()`（"Merge adapters on CPU with float32 parameters."），
       直接给 bf16 会加载失败。省显存的做法是**加载完之后自己 `.to(dtype=...)`**
       （模型自身的 `requested_dtype` 也是这个顺序），见 :meth:`SheetSage2Engine.load`。
    """
    kwargs: dict[str, Any] = {"trust_remote_code": True}
    if config.local_files_only:
        kwargs["local_files_only"] = True
    if config.cache_dir:
        kwargs["cache_dir"] = config.cache_dir
    return kwargs


def weights_dtype_choice(config: EngineConfig) -> str:
    """校验并返回权重精度名（`fp32` / `bf16` / `fp16`）。"""
    value = str(config.weights_dtype or "fp32").lower()
    if value not in WEIGHTS_DTYPE_NAMES:
        raise ValueError(f"未知的权重精度 {config.weights_dtype!r}；"
                         f"可选 {'/'.join(WEIGHTS_DTYPE_NAMES)}")
    return value


@dataclass
class RunRecord:
    """一次转写的调用记录，用于日志和稳定性验证。"""

    audio: str
    device: str
    dtype: str
    output_dir: str | None
    seconds: float
    peak_gpu_mib: float = 0.0
    warnings: list[str] = field(default_factory=list)
    fallback_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(
            audio=self.audio, device=self.device, dtype=self.dtype,
            output_dir=self.output_dir, seconds=round(self.seconds, 2),
            peak_gpu_mib=round(self.peak_gpu_mib, 1),
            warnings=list(self.warnings), fallback_reason=self.fallback_reason,
        )


class SheetSage2Engine:
    """持有已加载模型的推理引擎。一个进程里建一个就够。"""

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig()
        self._model = None
        self._device = "unloaded"
        self.records: list[RunRecord] = []
        self.notes: list[str] = []

    # ---------------------------------------------------------------- 加载
    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def device(self) -> str:
        return self._device

    @property
    def model(self):
        return self._model

    def load(self) -> "SheetSage2Engine":
        if self._model is not None:
            return self

        prepare()
        import torch
        from transformers import AutoModel

        if not WEIGHTS_DTYPES:                      # 懒填，避免模块导入期就拉 torch
            WEIGHTS_DTYPES.update({"bf16": torch.bfloat16, "fp16": torch.float16,
                                   "fp32": torch.float32})
        torch.set_num_threads(min(4, torch.get_num_threads()))
        wanted = self.config.device
        if wanted == "auto":
            wanted = "cuda" if torch.cuda.is_available() else "cpu"

        kwargs: dict[str, Any] = engine_load_kwargs(self.config)
        weights_dtype = weights_dtype_choice(self.config)

        model_path = resolve_model_path(self.config.model_id)
        if Path(model_path).is_dir():
            # 本地目录要把 remote code 补全，否则 transformers 只复制一半
            synced = sync_remote_code_cache(Path(model_path))
            if synced:
                self.notes.append(f"补齐 {synced} 个 remote code 文件到模块缓存")

        # SheetSage2 是 LoRA 适配器格式时，要另外把 MERT 骨架读进来（它默认按
        # `base_model_name_or_path` 的 HF 仓库 id 走 `cached_file`，**脱网就炸**）。
        # 我们本来就把那份权重下到 `models/MERT-v2-FullSong`，直接告诉它路径。
        backbone = local_backbone_path()
        if backbone is not None:
            kwargs.setdefault("base_model_path", str(backbone))
            self.notes.append(f"MERT 骨架走本地：{backbone.name}")

        try:
            model = AutoModel.from_pretrained(model_path, **kwargs)
        except Exception as exc:  # 权重缺失 / remote code 不兼容
            raise TranscriptionError(
                f"加载 {self.config.model_id} 失败：{type(exc).__name__}: {exc}"
            ) from exc

        # 降精度必须在 `from_pretrained` **之后**：模型要先在 fp32 下合并 LoRA 适配器
        # （"Merge adapters on CPU with float32 parameters."），加载期就把默认 dtype 换掉会失败。
        # 这也是模型自身 `requested_dtype` 的顺序。
        if weights_dtype != "fp32":
            model = model.to(dtype=WEIGHTS_DTYPES[weights_dtype])

        try:
            model = model.eval().to(wanted)
        except Exception as exc:
            if wanted == "cuda" and self.config.cpu_fallback:
                model = model.eval().to("cpu")
                wanted = "cpu"
            else:
                raise TranscriptionError(f"模型搬到 {wanted} 失败：{exc}") from exc

        if weights_dtype != "fp32":
            self.notes.append(f"权重以 {weights_dtype} 加载（省显存，小显存卡用）")
        self._model = model
        self._device = wanted
        return self

    # ---------------------------------------------------------------- 推理
    def transcribe(
        self,
        audio: str | Path,
        output_dir: str | Path | None = None,
        *,
        progress: ProgressFn | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        """跑一次转写，返回模型原始 result 字典（含 abc / midi / events / labs）。"""
        if self._model is None:
            self.load()

        options = dict(options)
        options.setdefault("preset", self.config.preset)
        if progress is not None:
            options["progress"] = progress

        out = Path(output_dir) if output_dir is not None else None
        if out is not None:
            out.mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        try:
            result = self._invoke(audio, out, options)
        except Exception as exc:
            if not self._can_fall_back(exc):
                partial = getattr(exc, "result", None)
                raise TranscriptionError(f"转写失败：{type(exc).__name__}: {exc}", partial) from exc
            reason = f"CUDA OOM 退回 CPU 重跑（{type(exc).__name__}: {str(exc)[:160]}）"
            self._move_to_cpu()
            result = self._invoke(audio, out, options)
            self.records.append(
                RunRecord(str(audio), self._device, self._effective_dtype(), str(out) if out else None,
                          time.monotonic() - started,
                          peak_gpu_mib=float(result.get("peak_gpu_mib") or 0.0),
                          warnings=list(result.get("warnings") or []),
                          fallback_reason=reason)
            )
            return result

        self.records.append(
            RunRecord(
                audio=str(audio),
                device=self._device,
                dtype=self._effective_dtype(),
                output_dir=str(out) if out else None,
                seconds=time.monotonic() - started,
                peak_gpu_mib=float(result.get("peak_gpu_mib") or 0.0),
                warnings=list(result.get("warnings") or []),
            )
        )
        return result

    # ---------------------------------------------------------------- 内部
    def _invoke(self, audio, out: Path | None, options: Mapping[str, Any]) -> dict[str, Any]:
        call = dict(options)
        call["dtype"] = self._effective_dtype()
        return self._model.transcribe(str(audio), output_dir=out, **call)

    def _effective_dtype(self) -> str:
        # bf16 只在 CUDA 上有实际收益；CPU 上走 fp32，避免慢且精度可疑。
        return self.config.dtype if self._device.startswith("cuda") else "fp32"

    @property
    def effective_dtype(self) -> str:
        """这一趟实际用的计算精度（`fp32` / `bf16`）——日志与产物统计要报它。"""
        return self._effective_dtype()

    def _can_fall_back(self, exc: BaseException) -> bool:
        return (
            self.config.cpu_fallback
            and self._device.startswith("cuda")
            and _is_out_of_memory(exc)
        )

    def _move_to_cpu(self) -> None:
        import torch

        torch.cuda.empty_cache()
        self._model = self._model.to("cpu")
        self._device = "cpu"
