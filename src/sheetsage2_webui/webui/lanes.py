"""卷帘数据：把 tick 域的工程翻译成**秒**，前端只负责按比例画。

为什么不把这些换算搬到前端：秒 ↔ tick 的唯一真源是小节表（分段线性映射，见
`docs/BPM算法规格.md` §0）。那套逻辑已经在 `timeline.Timeline` 里验证过了，TS 里再抄一份
就等于埋第二个真相——BPM 一变就会对不上。所以这里算好秒再给出去，前端只做线性缩放。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..timeline import Timeline, from_stored_measures
from .runner import group_lyric_lines


def _read(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _interval_rows(rows: list[dict[str, Any]], timeline: Timeline,
                   duration: float) -> list[dict[str, Any]]:
    """把 ``{tick, dur, label}`` 搬进秒域，并裁到曲长以内。"""
    out: list[dict[str, Any]] = []
    for row in rows or []:
        start = max(0.0, timeline.tick_to_sec(float(row.get("tick") or 0)))
        end = timeline.tick_to_sec(float(row.get("tick") or 0) + float(row.get("dur") or 0))
        if duration:
            end = min(end, duration)
        if end <= start:
            continue
        out.append({"start": round(start, 3), "end": round(end, 3),
                    "label": str(row.get("label") or "")})
    return out


def build_lanes(job_out: Path, duration_hint: float = 0.0) -> dict[str, Any]:
    """``output/`` 目录 -> 卷帘要画的全部区间（秒）。"""
    project = _read(job_out / "project.json")
    if project is None:
        raise FileNotFoundError("project.json 还没写出来")
    timeline_block = project.get("timeline") or {}
    measures = timeline_block.get("measures") or []
    if not measures:
        raise FileNotFoundError("工程里没有小节表，无法换算时间")
    timeline = from_stored_measures(measures)
    duration = float(project.get("duration_seconds") or duration_hint or 0.0) \
        or max((timeline.tick_to_sec(float(m.get("tick_end") or 0)) for m in measures),
               default=0.0)

    sections = _interval_rows(project.get("sections") or [], timeline, duration)
    chords = _interval_rows(project.get("chords") or [], timeline, duration)

    vocal: list[dict[str, Any]] = []
    for track in project.get("tracks") or []:
        if track.get("id") != "vocal":
            continue
        for note in track.get("notes") or []:
            start = timeline.tick_to_sec(float(note.get("tick") or 0))
            end = timeline.tick_to_sec(float(note.get("tick") or 0) + float(note.get("dur") or 0))
            lyric = str(note.get("lyric") or "")
            if lyric in ("-", "+"):
                lyric = ""                       # 转音标记不占"字"，但要保留音符
            vocal.append({"start": round(start, 3), "end": round(end, 3),
                          "pitch": int(note.get("pitch") or 0), "lyric": lyric})

    bar_rows = []
    for measure in measures:
        start = timeline.tick_to_sec(float(measure.get("tick_start") or 0))
        end = timeline.tick_to_sec(float(measure.get("tick_end") or 0))
        bar_rows.append({"start": round(start, 3), "end": round(end, 3),
                         "bpm": measure.get("bpm"), "numerator": measure.get("numerator"),
                         "denominator": measure.get("denominator"),
                         "is_pickup": bool(measure.get("is_pickup"))})

    lyrics = _read(job_out / "lyrics.json") or {}
    lines = lyrics.get("lines")
    if not lines and lyrics.get("chars"):
        lines = group_lyric_lines(lyrics["chars"])

    return {
        "duration_seconds": round(duration, 3),
        "pickup_seconds": round(float(timeline_block.get("pickup_seconds") or 0.0), 3),
        "measures": bar_rows,
        "sections": sections,
        "chords": chords,
        "vocal_notes": vocal,
        "lyric_lines": lines or [],
        "lyrics_ready": bool(lines),
        "keys": [f"{item.get('tonic', 'C')}:{item.get('mode', 'major')}"
                 for item in project.get("keys") or []],
    }
