"""A 卡（ROCm）相关的回归：错误归类 + 精度回落。

不需要真 A 卡：错误归类看字符串，精度回落把 `bf16_supported` 换掉。
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------- 错误归类
@pytest.mark.parametrize("message, expected", [
    ("HIP error: out of memory", "E_VRAM_OOM"),
    ("RuntimeError: HIP out of memory. Tried to allocate 2.00 GiB", "E_VRAM_OOM"),
    ("miopenStatusNotInitialized: MIOpen failed", "E_VRAM_OOM"),
    ("hsa_status_t error 4104", "E_VRAM_OOM"),
    ("no hip devices found", "E_DEVICE"),
])
def test_hip_errors_are_classified(message, expected):
    """AMD 上报的是 HIP/MIOpen 字样；不认这些词，用户看到的就是「未知错误」。"""
    from sheetsage2_webui.webui.errors import describe

    assert describe(RuntimeError(message)).code == expected


def test_cuda_messages_still_classified():
    from sheetsage2_webui.webui.errors import describe

    assert describe(RuntimeError("CUDA error: an illegal memory access")).code == "E_VRAM_OOM"
    assert describe(RuntimeError("CUDA is not available")).code == "E_DEVICE"


# ---------------------------------------------------------------- 精度回落
def test_engine_effective_dtype_falls_back_to_fp16(monkeypatch):
    from sheetsage2_webui import backend
    from sheetsage2_webui.engine import EngineConfig, SheetSage2Engine

    engine = SheetSage2Engine(EngineConfig(model_id="x", device="cuda", dtype="bf16"))
    engine._device = "cuda"
    monkeypatch.setattr(backend, "bf16_supported", lambda *a, **k: False)
    assert engine.effective_dtype == "fp16"

    monkeypatch.setattr(backend, "bf16_supported", lambda *a, **k: True)
    assert engine.effective_dtype == "bf16"


def test_engine_dtype_on_cpu_is_fp32(monkeypatch):
    from sheetsage2_webui import backend
    from sheetsage2_webui.engine import EngineConfig, SheetSage2Engine

    monkeypatch.setattr(backend, "bf16_supported", lambda *a, **k: True)
    engine = SheetSage2Engine(EngineConfig(model_id="x", device="cpu", dtype="bf16"))
    engine._device = "cpu"
    assert engine.effective_dtype == "fp32"
