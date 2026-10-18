"""耗时预估（G20）：拖进一首 6 分钟的歌，到底要等 3 分钟还是 40 分钟。

**为什么需要它**：界面上只有一个进度条，用户在第一个 5% 待着的两分钟里没有任何参照物——
他不知道该去泡杯茶，还是该怀疑卡死了。给一句"预计 2~4 分钟"，等待就从"未知"变成"有数"。

**口径**（本机实测拟合，宁可粗一点也不要给假精确）：

| 阶段 | 显卡 | CPU | 说明 |
|---|---|---|---|
| `transcribe` | 音频秒数 × 0.6 | × 8 | `docs/健壮性保障评估.md` §G20；CPU 退路极慢，系数量级差就是事实 |
| `lyrics` | 音频秒数 × 0.5 | × 4 | 只跑唱段，所以比扒谱快 |

再加一项**固定开销**（模型加载：本机实测 15~30s，第一次更久），因为"3 分钟的歌要 4 分钟"
里有一分钟是加载。区间按 ±35% 给（音频长短、量化开关、显存抖动都能让它偏）。

**这是纯函数，不 import torch**：`device` 由调用方（`JobManager`）决定并传进来，
所以单测不需要显卡，也不会因为缺驱动就崩。
"""

from __future__ import annotations

from typing import Any, Mapping

#: 阶段 -> (显卡系数, CPU 系数)：每 1 秒音频要跑多久。
BASE_FACTOR: dict[str, tuple[float, float]] = {
    "transcribe": (0.6, 8.0),
    "lyrics": (0.5, 4.0),
}

#: 固定开销（秒）：扒谱要先把 SheetSage2 搬进显存，歌词要换 Qwen3-ASR。
OVERHEAD: dict[str, tuple[float, float]] = {
    "transcribe": (25.0, 45.0),
    "lyrics": (25.0, 45.0),
}

#: 区间的宽窄：低估 20%、高估 30%。不求准，只求"不会差得离谱"。
LOW_RATIO = 0.8
HIGH_RATIO = 1.3

#: 短于这个秒数就别给预估了（估算值的相对误差太大，说了等于没说）。
MIN_AUDIO_SECONDS = 3.0


def is_cpu(device: Any) -> bool:
    """`cpu` / `cuda` / `cuda:0` / `auto` -> 是不是按 CPU 口径算。

    只有明确写了 `cpu` 才算 CPU：`auto` 在一台有卡的机器上就是显卡跑，
    而**没卡**的机器由 `JobManager` 先把 `auto` 解析成 `cpu` 再传进来（见 `DeviceHint`）。
    """
    return str(device or "").strip().lower().startswith("cpu")


def range_seconds(options: Mapping[str, Any] | None = None) -> float | None:
    """「范围」参数选的是前 30 / 60 秒时，实际只跑那么长——预估也要跟着缩。"""
    value = str((options or {}).get("range") or "full")
    if value in ("30", "60"):
        return float(value)
    return None


def estimate(phase: str, audio_seconds: float | None, device: Any = "cuda",
             options: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """给一个阶段估个耗时；信息不够就返回 `None`（界面就不显示预估）。

    返回::

        {"seconds": 180.0, "low": 144.0, "high": 216.0,
         "label": "预计 2~4 分钟", "device": "cuda", "audio_seconds": 240.0}

    `seconds` 是**中位估计**（界面拿它算"还需要多久"），`low`/`high` 是给人看的区间。
    """
    factor = BASE_FACTOR.get(phase)
    if factor is None:
        return None
    if not audio_seconds or float(audio_seconds) < MIN_AUDIO_SECONDS:
        return None
    span = float(audio_seconds)
    if phase == "transcribe":
        limited = range_seconds(options)
        if limited is not None:
            span = min(span, limited)
    cpu = is_cpu(device)
    per_second, overhead = (factor[1], OVERHEAD[phase][1]) if cpu else (factor[0],
                                                                      OVERHEAD[phase][0])
    middle = overhead + span * per_second
    low = max(1.0, middle * LOW_RATIO)
    high = max(low + 1.0, middle * HIGH_RATIO)
    return {
        "seconds": round(middle, 1),
        "low": round(low, 1),
        "high": round(high, 1),
        "label": f"预计 {humanize(low, high)}",
        "device": "cpu" if cpu else "cuda",
        "audio_seconds": round(span, 2),
    }


def humanize(low: float, high: float) -> str:
    """区间 -> 人话（"40~60 秒" / "2~4 分钟" / "1.5~2.0 小时"）。

    刻意用"~"而不是"约 2 到 4 分钟"：一行字要能塞进阶段卡，不换行。
    """
    low = max(1.0, float(low))
    high = max(low, float(high))
    if high < 90:
        return f"{_round_int(low)}~{_round_int(high)} 秒"
    if high < 5400:
        minutes_low = max(1, _round_int(low / 60))
        minutes_high = max(minutes_low + 1, _round_int(high / 60))
        return f"{minutes_low}~{minutes_high} 分钟"
    return f"{low / 3600:.1f}~{high / 3600:.1f} 小时"


def _round_int(value: float) -> int:
    """向上取整到"看得懂的档"：40 秒、55 秒这种，别出现 43.7 秒。"""
    if value <= 10:
        return max(1, int(round(value)))
    if value <= 90:
        return max(1, int(round(value / 5.0)) * 5)
    return max(1, int(round(value)))


def remaining(estimate_dict: Mapping[str, Any] | None, elapsed_seconds: float) -> float | None:
    """跑了一半之后还剩多久（秒）：中位估计减去已用时间，不小于 0。

    估得比实际快时会归零——**不要**编一个"还需 0 分钟"，界面见 0 就只显示已用时间。
    """
    if not estimate_dict:
        return None
    total = float(estimate_dict.get("seconds") or 0.0)
    if total <= 0:
        return None
    return max(0.0, total - max(0.0, float(elapsed_seconds or 0.0)))
