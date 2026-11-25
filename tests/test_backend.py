"""显卡后端识别（`backend.py`）的单测。

这里**不碰真的 torch**：用鸭子类型的假对象模拟三种构建（CUDA / ROCm / 无卡），
这样在 N 卡开发机上也能把 A 卡那条分支测到。
"""

from __future__ import annotations

import types

import pytest

from sheetsage2_webui import backend


class FakeCuda:
    def __init__(self, available: bool = True, name: str = "Fake GPU",
                 bf16: bool = True, raise_on_name: bool = False) -> None:
        self._available = available
        self._name = name
        self._bf16 = bf16
        self._raise_on_name = raise_on_name

    def is_available(self) -> bool:
        return self._available

    def get_device_name(self, index: int = 0) -> str:
        if self._raise_on_name:
            raise RuntimeError("driver 炸了")
        return self._name

    def is_bf16_supported(self) -> bool:
        return self._bf16


def fake_torch(*, hip: str | None = None, cuda_version: str | None = "13.0",
               available: bool = True, name: str = "Fake GPU",
               bf16: bool = True) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        version=types.SimpleNamespace(hip=hip, cuda=cuda_version),
        cuda=FakeCuda(available=available, name=name, bf16=bf16),
    )


# ---------------------------------------------------------------- detect_backend
def test_nvidia_build_is_cuda():
    info = backend.detect_backend(fake_torch(cuda_version="13.0"))
    assert info["kind"] == backend.BACKEND_CUDA
    assert info["name"] == "Fake GPU"
    assert info["cuda"] == "13.0"
    assert info["hip"] is None


def test_amd_build_is_rocm_and_reports_hip():
    info = backend.detect_backend(fake_torch(hip="7.2.1", cuda_version=None))
    assert info["kind"] == backend.BACKEND_ROCM
    assert info["hip"] == "7.2.1"
    assert info["cuda"] is None


def test_no_gpu_is_cpu():
    info = backend.detect_backend(fake_torch(available=False))
    assert info["kind"] == backend.BACKEND_CPU
    assert info["name"] is None


def test_missing_torch_is_unknown(monkeypatch):
    """`import torch` 直接失败（没装/装坏了）→ unknown，而不是把机器当无卡。"""
    def boom(name: str):
        raise ImportError("没有 torch")

    monkeypatch.setattr(backend.importlib, "import_module", boom)
    assert backend.detect_backend()["kind"] == backend.BACKEND_UNKNOWN


def test_device_name_failure_does_not_crash():
    torch = fake_torch()
    torch.cuda = FakeCuda(raise_on_name=True)
    # FakeCuda 没实现 raise_on_name 以外的东西？其实实现了——这里补一个会抛的
    class BrokenName(FakeCuda):
        def get_device_name(self, index: int = 0) -> str:
            raise RuntimeError("driver 炸了")

    torch.cuda = BrokenName()
    info = backend.detect_backend(torch)
    assert info["kind"] == backend.BACKEND_CUDA
    assert info["name"] is None


def test_backend_label():
    assert backend.backend_label(backend.BACKEND_CUDA) == "NVIDIA CUDA"
    assert backend.backend_label(backend.BACKEND_ROCM) == "AMD ROCm"


# ---------------------------------------------------------------- bf16
def test_bf16_true_on_supported_card():
    assert backend.bf16_supported(fake_torch(bf16=True)) is True


def test_bf16_false_on_rdna2_like_card():
    assert backend.bf16_supported(fake_torch(bf16=False)) is False


def test_bf16_false_without_cuda_or_probe():
    assert backend.bf16_supported(fake_torch(available=False)) is False
    torch = fake_torch()
    torch.cuda = types.SimpleNamespace(is_available=lambda: True)
    assert backend.bf16_supported(torch) is False       # 老 torch 没有探测函数


def test_effective_dtype_falls_back_to_fp16_without_bf16():
    torch = fake_torch(bf16=False)
    assert backend.effective_dtype("bf16", device="cuda", torch_module=torch) == "fp16"
    assert backend.effective_dtype("bf16", device="cuda", torch_module=fake_torch()) == "bf16"
    assert backend.effective_dtype("fp32", device="cuda", torch_module=torch) == "fp32"
    assert backend.effective_dtype("bf16", device="cpu", torch_module=torch) == "fp32"


# ---------------------------------------------------------------- 双环境
def test_marker_roundtrip(tmp_path):
    assert backend.read_backend_marker(tmp_path) is None
    backend.write_backend_marker(tmp_path, backend.BACKEND_ROCM)
    assert backend.read_backend_marker(tmp_path) == backend.BACKEND_ROCM
    backend.write_backend_marker(tmp_path, backend.BACKEND_CUDA)
    assert backend.read_backend_marker(tmp_path) == backend.BACKEND_CUDA


def test_marker_rejects_garbage(tmp_path):
    (tmp_path / backend.BACKEND_MARKER).write_text("weird\n", encoding="utf-8")
    assert backend.read_backend_marker(tmp_path) is None
    with pytest.raises(ValueError):
        backend.write_backend_marker(tmp_path, "weird")


def test_venv_python_follows_the_marker(tmp_path):
    for name in (".venv", ".venv-rocm"):
        (tmp_path / name / "Scripts").mkdir(parents=True)
        (tmp_path / name / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    backend.write_backend_marker(tmp_path, backend.BACKEND_ROCM)
    assert backend.venv_python(tmp_path).parent.parent.name == ".venv-rocm"


def test_venv_python_falls_back_when_rocm_env_missing(tmp_path):
    """标记写着 rocm、但环境没装好：退回 .venv，别让机器直接起不来。"""
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    backend.write_backend_marker(tmp_path, backend.BACKEND_ROCM)
    assert backend.venv_python(tmp_path).parent.parent.name == ".venv"
