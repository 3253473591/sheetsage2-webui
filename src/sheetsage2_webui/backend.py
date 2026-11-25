"""显卡后端识别：CUDA / ROCm（HIP）/ CPU。

为什么要单独一个模块：同一份代码要在 **N 卡（CUDA）** 和 **A 卡（ROCm）** 上跑，而两者的
区别几乎只体现在"这个 torch 是哪种构建"上——ROCm 构建把 HIP 映射到 `torch.cuda.*`
命名空间，所以调用代码不用改，**需要区分的只有文案、诊断信息和少数数值细节**：

- 横幅写「NVIDIA CUDA」还是「AMD ROCm」；
- 诊断包 / 日志里记 `hip` 版本（出问题时省半小时排查）；
- **bf16 探测**：RDNA2 一类卡没有 bf16 指令，硬用 bf16 会直接报错，要落 fp16。

对外只用两个函数：:func:`detect_backend` 和 :func:`bf16_supported`。两者都接受可选的
`torch_module`（测试里塞假对象），**不在这里 import torch**——启动预检不该因为缺 torch 就崩。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

#: 后端的三种取值 + `unknown`（读不到 torch）。
BACKEND_CUDA = "cuda"
BACKEND_ROCM = "rocm"
BACKEND_CPU = "cpu"
BACKEND_UNKNOWN = "unknown"


def _torch(torch_module: Any | None) -> Any | None:
    """拿到 torch（或测试塞进来的假对象）；拿不到就返回 None。"""
    if torch_module is not None:
        return torch_module
    try:
        return importlib.import_module("torch")
    except Exception:                                   # noqa: BLE001 - 预检/诊断不该崩
        return None


def detect_backend(torch_module: Any | None = None) -> dict[str, Any]:
    """当前进程用的是哪种显卡后端。

    返回 ``{"kind", "name", "hip", "cuda"}``：

    - ``kind``：``cuda``（NVIDIA）/ ``rocm``（AMD）/ ``cpu``（没有可用显卡）/ ``unknown``（读不到 torch）
    - ``name``：显卡名（没有就是 None）
    - ``hip`` / ``cuda``：torch 编译时的版本号（另一个是 None）——诊断包直接打这两个

    判据只有一条：**`torch.version.hip` 有值就是 ROCm**（AMD 的构建一定带它，NVIDIA 的构建一定没有）。
    """
    torch = _torch(torch_module)
    if torch is None:
        return {"kind": BACKEND_UNKNOWN, "name": None, "hip": None, "cuda": None}
    try:
        available = bool(torch.cuda.is_available())
    except Exception:                                   # noqa: BLE001 - 驱动坏了也只是"没显卡"
        available = False
    version = getattr(torch, "version", None)
    hip = getattr(version, "hip", None)
    cuda = getattr(version, "cuda", None)
    if not available:
        return {"kind": BACKEND_CPU, "name": None, "hip": hip, "cuda": cuda}
    try:
        name = torch.cuda.get_device_name(0)
    except Exception:                                   # noqa: BLE001
        name = None
    return {
        "kind": BACKEND_ROCM if hip else BACKEND_CUDA,
        "name": name,
        "hip": hip,
        "cuda": cuda,
    }


def backend_label(kind: str) -> str:
    """给用户看的名字：`cuda` → `NVIDIA CUDA`，`rocm` → `AMD ROCm`。"""
    return {
        BACKEND_CUDA: "NVIDIA CUDA",
        BACKEND_ROCM: "AMD ROCm",
        BACKEND_CPU: "CPU",
        BACKEND_UNKNOWN: "未知",
    }.get(kind, kind)


def bf16_supported(torch_module: Any | None = None) -> bool:
    """这台机器/这个后端能不能用 bf16 算。

    - CPU：False（慢且精度可疑，`engine.py` 本来就走 fp32）
    - CUDA：torch 从 2.x 起有 `torch.cuda.is_bf16_supported()`（看算力 ≥ 8.0）
    - ROCm：同名字符串由 AMD 构建提供，但**RDNA2（gfx1030）等卡返回 False**
    - 任何异常一律按 False 处理——宁可用 fp16 慢一点，也别在加载模型时炸
    """
    torch = _torch(torch_module)
    if torch is None:
        return False
    try:
        if not torch.cuda.is_available():
            return False
        probe = getattr(torch.cuda, "is_bf16_supported", None)
        if probe is None:                               # 老 torch：保守认为不支持
            return False
        return bool(probe())
    except Exception:                                   # noqa: BLE001
        return False


def effective_dtype(requested: str, *, device: str,
                    torch_module: Any | None = None) -> str:
    """把"用户想要的精度"换成本机真能跑的精度。

    - CPU → `fp32`
    - 只有 bf16 不被支持时才降级到 `fp16`（**不是 fp32**：显存还是省一半，精度也够）
    - 其余原样返回（`fp32` / `fp16` 用户明确要求的都尊重）
    """
    if not str(device).startswith("cuda"):
        return "fp32"
    if requested == "bf16" and not bf16_supported(torch_module):
        return "fp16"
    return requested


# ---------------------------------------------------------------- 双环境
#: 每个后端用它自己的 venv 目录（包里塞不下两份 torch，见 docs/ROCm加速调研.md §1）。
VENV_DIRS = {BACKEND_CUDA: ".venv", BACKEND_ROCM: ".venv-rocm"}

#: 安装器把"这台机器用哪个后端"写在这里，启动器照着挑解释器。
BACKEND_MARKER = ".env-backend"


def read_backend_marker(root: Path | str) -> str | None:
    """读 `.env-backend`（安装器写的）；内容不是已知后端就返回 None。"""
    try:
        text = (Path(root) / BACKEND_MARKER).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    return text if text in (BACKEND_CUDA, BACKEND_ROCM) else None


def write_backend_marker(root: Path | str, backend: str) -> None:
    if backend not in (BACKEND_CUDA, BACKEND_ROCM):
        raise ValueError(f"未知后端 {backend}")
    (Path(root) / BACKEND_MARKER).write_text(backend + "\n", encoding="utf-8")


def venv_python(root: Path | str, backend: str | None = None) -> Path:
    """启动器该用哪个解释器：按 `.env-backend` 挑；对应环境下没装好就退回 `.venv`。

    ROCm 环境不存在时**不要**硬指向它（那会让一个装好 CUDA 的机器直接起不来）。
    """
    root = Path(root)
    chosen = backend or read_backend_marker(root) or BACKEND_CUDA
    candidate = root / VENV_DIRS.get(chosen, VENV_DIRS[BACKEND_CUDA]) / "Scripts" / "python.exe"
    if candidate.is_file():
        return candidate
    return root / VENV_DIRS[BACKEND_CUDA] / "Scripts" / "python.exe"
