"""速度表（tempo map）：让变速曲的 SVP / MIDI 拥有正确的速度与网格。

背景（交接文档 §16）
------------------
SheetSage2 **能跟住变速** —— 它自己的 `playback.json` 小节表里，逐小节算出来的
BPM 就是 110 → 75 → 110（实测《百战成诗》：m0–m44 ≈110、m45–m101 = 75.0、
m103+ ≈110）。但我们的导出只用了**一个** BPM（`overview` 推出来的 109.1），
于是占全曲 45% 的 75 BPM 段落，小节网格和速度全是错的。

SVP 153 本身支持多速度（205 个真实样本里 22 个是多速度，最多 88 条），
格式是 `time.tempo = [{"position": <blick>, "bpm": ...}, ...]`。

这里做两件事
------------
1. :func:`derive_tempo_map` —— 从 `playback.json` 的小节表派生速度表；
2. :func:`sec_to_blick` / :func:`blick_to_sec` —— **分段线性**的秒↔blick 换算。
   单速度时与原来的 ``秒 × bpm/60 × BLICK`` 完全等价，所以老路径行为不变。
"""
from __future__ import annotations

from typing import Any, Sequence

__all__ = [
    "BLICK_PER_QUARTER",
    "derive_tempo_map",
    "sec_to_blick",
    "blick_to_sec",
    "normalize_tempo_map",
]

#: 谱面 1 个四分音符对应的 blick 数（SVP 153；与 app.svpw 共用同一定义）
BLICK_PER_QUARTER = 705_600_000


def _median(vals: Sequence[float], i: int, half: int) -> float:
    lo, hi = max(0, i - half), min(len(vals), i + half + 1)
    win = sorted(vals[lo:hi])
    return win[len(win) // 2]


def derive_tempo_map(
    measures: Sequence[dict[str, Any]],
    *,
    tolerance: float = 0.04,
    median_window: int = 5,
    min_segment_seconds: float = 0.4,
) -> list[dict[str, Any]]:
    """从 ``playback.json`` 的小节表派生速度表。

    做法：逐小节算 BPM（拍数用 ``score_start/score_end`` 推，兼容非 4/4）→
    中值滤波压掉过渡/退化小节 → 按容差合并成若干段 → 每段用
    ``总拍数 / 总秒数`` 精确算 BPM → 太短的段并入前一段。

    Returns:
        ``[{"t": 起始秒, "bpm": 速度, "measures": 小节数}, ...]``，首项恒为 ``t=0``。
        单速度时长度 1（此时行为与旧的单一 bpm 完全一致）。
    """
    pts: list[tuple[float, float, float, float]] = []   # (start, end, seconds, quarters)
    for m in measures or ():
        try:
            start, end = float(m["start"]), float(m["end"])
            s0, s1 = float(m["score_start"]), float(m["score_end"])
        except (KeyError, TypeError, ValueError):
            continue
        seconds = end - start
        quarters = (s1 - s0) * 4.0
        if seconds <= 1e-6 or quarters <= 1e-6:
            continue
        pts.append((start, end, seconds, quarters))

    if not pts:
        return []

    raw_bpm = [q / s * 60.0 for _a, _b, s, q in pts]
    half = max(0, median_window // 2)
    smooth = [_median(raw_bpm, i, half) for i in range(len(raw_bpm))]

    # 按容差合并成段
    segments: list[dict[str, float]] = []
    for (start, _end, seconds, quarters), bpm in zip(pts, smooth):
        if segments and abs(bpm - segments[-1]["_bpm_ref"]) <= tolerance * segments[-1]["_bpm_ref"]:
            seg = segments[-1]
            seg["seconds"] += seconds
            seg["quarters"] += quarters
            seg["measures"] += 1
        else:
            segments.append({
                "t": start, "seconds": seconds, "quarters": quarters,
                "measures": 1, "_bpm_ref": bpm,
            })

    # 精确 BPM + 丢掉辅助字段
    for seg in segments:
        seg["bpm"] = round(seg["quarters"] / seg["seconds"] * 60.0, 3)

    # 太短的段并入前一段（否则会产出 400+ BPM 那种退化速度）
    merged: list[dict[str, Any]] = []
    for seg in segments:
        if merged and seg["seconds"] < min_segment_seconds:
            prev = merged[-1]
            prev["seconds"] += seg["seconds"]
            prev["quarters"] += seg["quarters"]
            prev["measures"] += seg["measures"]
            prev["bpm"] = round(prev["quarters"] / prev["seconds"] * 60.0, 3)
            continue
        merged.append({k: v for k, v in seg.items() if not k.startswith("_")})

    if merged and merged[0]["t"] > 1e-9:
        merged[0]["t"] = 0.0
    return merged


def normalize_tempo_map(
    tempo_map: Sequence[dict[str, Any]] | None,
    bpm: float,
) -> list[dict[str, Any]]:
    """把速度表规整成可用形式；为空或非法时退化成「单速度」的一条。"""
    out: list[dict[str, Any]] = []
    for item in tempo_map or ():
        try:
            t = float(item["t"])
            b = float(item["bpm"])
        except (KeyError, TypeError, ValueError):
            continue
        if b > 0:
            out.append({"t": max(0.0, t), "bpm": b})
    out.sort(key=lambda d: d["t"])
    if not out:
        return [{"t": 0.0, "bpm": float(bpm)}]
    if out[0]["t"] > 1e-9:
        out[0]["t"] = 0.0
    return out


def sec_to_blick(seconds: float, tempo_map: Sequence[dict[str, Any]]) -> int:
    """秒 → blick（**分段线性**：按速度表逐段累加）。

    单条速度时结果与 ``秒 × bpm/60 × BLICK_PER_QUARTER`` 完全相同。
    """
    seconds = float(seconds)
    if seconds <= 0:
        return 0
    tempo_map = normalize_tempo_map(tempo_map, 120.0)
    blick = 0.0
    prev_t = 0.0
    prev_bpm = tempo_map[0]["bpm"]
    for item in tempo_map[1:]:
        t = item["t"]
        if seconds <= t:
            break
        blick += (t - prev_t) * (prev_bpm / 60.0) * BLICK_PER_QUARTER
        prev_t, prev_bpm = t, item["bpm"]
    blick += (seconds - prev_t) * (prev_bpm / 60.0) * BLICK_PER_QUARTER
    return int(round(blick))


def blick_to_sec(blick: float, tempo_map: Sequence[dict[str, Any]]) -> float:
    """blick → 秒（:func:`sec_to_blick` 的逆；主要用于校验往返误差）。"""
    blick = float(blick)
    if blick <= 0:
        return 0.0
    tempo_map = normalize_tempo_map(tempo_map, 120.0)
    remain = blick
    prev_t = 0.0
    prev_bpm = tempo_map[0]["bpm"]
    for item in tempo_map[1:]:
        seg = (item["t"] - prev_t) * (prev_bpm / 60.0) * BLICK_PER_QUARTER
        if remain <= seg:
            break
        remain -= seg
        prev_t, prev_bpm = item["t"], item["bpm"]
    return prev_t + remain / ((prev_bpm / 60.0) * BLICK_PER_QUARTER)
