r"""`engine` 里不依赖模型加载的逻辑（显存预算的参数拼装）。

    .\.venv\Scripts\python.exe -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.engine import (  # noqa: E402
    EngineConfig,
    engine_load_kwargs,
    weights_dtype_choice,
)


def test_without_a_budget_it_only_carries_the_usual_flags():
    kwargs = engine_load_kwargs(EngineConfig())
    assert kwargs == {"trust_remote_code": True}
    assert "device_map" not in kwargs


def test_flags_are_passed_through():
    kwargs = engine_load_kwargs(EngineConfig(local_files_only=True, cache_dir="D:/cache"))
    assert kwargs["local_files_only"] is True
    assert kwargs["cache_dir"] == "D:/cache"


def test_weights_dtype_is_the_only_vram_lever_for_the_engine():
    """**扒谱阶段没有 offload 这条路**：SheetSage2 的 from_pretrained 只接受单个设备
    （`device_map="auto"` 会被它强转成 `cuda`，多设备直接 ValueError），所以省显存只能靠
    把权重精度降到 bf16。这条测试锁住"我们不去给 device_map"。
    """
    assert "device_map" not in engine_load_kwargs(EngineConfig())

    assert "device_map" not in engine_load_kwargs(EngineConfig(weights_dtype="bf16"))
    assert weights_dtype_choice(EngineConfig(weights_dtype="bf16")) == "bf16"
    assert weights_dtype_choice(EngineConfig()) == "fp32"          # 默认 = 一直以来的行为
    with pytest.raises(ValueError):
        weights_dtype_choice(EngineConfig(weights_dtype="int8"))
