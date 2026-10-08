"""兼容性探针：Qwen3-ASR + ForcedAligner 能否和 SheetSage2 共处一个环境。"""
import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import torch

import sheetsage2_webui  # noqa: F401  先让它把 ffmpeg / OMP 准备好
from sheetsage2_webui.engine import EngineConfig, SheetSage2Engine

PROJECT = Path(__file__).resolve().parents[1]
MODELS = PROJECT / "models"


def vram() -> str:
    return f"{torch.cuda.memory_allocated() / 1024**3:.2f}GB / 峰值 {torch.cuda.max_memory_allocated() / 1024**3:.2f}GB"


print("=== 1. 版本 ===")
import transformers
import accelerate
print(f"transformers {transformers.__version__}  accelerate {accelerate.__version__}  torch {torch.__version__}")

print("\n=== 2. SheetSage2 是否仍可用 ===")
started = time.monotonic()
engine = SheetSage2Engine(EngineConfig(device="cuda", dtype="bf16")).load()
print(f"加载 {time.monotonic() - started:.1f}s   device={engine.device}   {vram()}")
del engine
torch.cuda.empty_cache()
torch.cuda.reset_peak_memory_stats()

print("\n=== 3. qwen_asr 导入 ===")
from qwen_asr import Qwen3ASRModel
print("qwen_asr OK")

print("\n=== 4. 加载 Qwen3-ASR-0.6B（不带对齐器） ===")
started = time.monotonic()
asr = Qwen3ASRModel.from_pretrained(
    str(MODELS / "Qwen3-ASR-0.6B"),
    dtype=torch.bfloat16,
    device_map="cuda:0",
    max_inference_batch_size=1,
    max_new_tokens=256,
)
print(f"加载 {time.monotonic() - started:.1f}s   {vram()}")
