"""分析概览汇总（对应任务文档 3.6.5）。

数据来源全部是 SheetSage2 的真实产物，**不做前端假数据**：

===============  ==================================================
概览字段          来源
===============  ==================================================
调性             ``key.lab``（``D#:major``），直接保留后端的音名拼写
速度 BPM         ``beat.lab`` 相邻拍间隔的中位数 → ``60 / median``
拍号             ``beat.lab`` 第 3/4 列（``4/4``）
时长             ``result.json: duration_seconds``
音符 人声/器乐     ``result.json: vocal_notes / instrumental_notes``
段落结构          ``structure.lab``，相邻同名段落自动合并
和弦进行          ``chord.lab``
用时             ``result.json: elapsed_seconds``
峰值显存          ``result.json: peak_gpu_mib``
===============  ==================================================

关于 BPM 的两个来源（任务文档 3.6.5 明确要求「把两者都写进诊断信息」）：

* ``bpm``         —— 概览显示值，由 ``beat.lab`` 实测拍点推导，反映真实律动；
* ``score_bpm``   —— 谱面速度标记，取自 ``score.abc`` 的 ``Q:1/4=N``。

两者允许不一致（SheetSage2 可能按半拍记谱，此时会相差 2 倍）。谱面渲染用
``score_bpm``，SVP 生成用 ``bpm``，差异规则写入 ``diagnostics``。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

__all__ = [
    "parse_lab_intervals",
    "parse_lab_beats",
    "parse_abc_header",
    "merge_sections",
    "key_to_chinese",
    "section_to_chinese",
    "summarize",
    "SHEETSAGE2_OUTPUT_FILES",
]

#: SheetSage2 会产出的文件（用于「输出文件」页与文件清单）
SHEETSAGE2_OUTPUT_FILES = (
    "score.abc",
    "transcription.mid",
    "melody.mid",
    "melody_vocal.mid",
    "melody_instrumental.mid",
    "chords.mid",
    "events.json",
    "events.tsv",
    "playback.json",
    "result.json",
    "tokens.json",
    "tokens.txt",
    "beat.lab",
    "downbeat.lab",
    "key.lab",
    "structure.lab",
    "chord.lab",
    "rhythm_events.lab",
    "melody_vocal.lab",
    "melody_instrumental.lab",
    "melody_full.lab",
)

_ABC_Q_RE = re.compile(r"^Q:\s*(?:1/4=)?(\d+(?:\.\d+)?)", re.M)
_ABC_K_RE = re.compile(r"^K:\s*(\S+)", re.M)
_ABC_M_RE = re.compile(r"^M:\s*(\S+)", re.M)
_ABC_L_RE = re.compile(r"^L:\s*(\S+)", re.M)

#: 段落名 → 中文（文档 3.6.5 的段落条标注）
_SECTION_CN = {
    "intro": "前奏",
    "outro": "尾奏",
    "verse": "主歌",
    "chorus": "副歌",
    "bridge": "桥段",
    "pre-chorus": "预副歌",
    "prechorus": "预副歌",
    "pre_chorus": "预副歌",
    "solo": "间奏",
    "instrumental": "间奏",
    "break": "间奏",
    "breakdown": "间奏",
    "refrain": "副歌",
    "hook": "副歌",
    "ending": "尾奏",
    "coda": "尾奏",
    "post-chorus": "后副歌",
    "postchorus": "后副歌",
    "drop": "高潮",
    "build": "推进",
    "build-up": "推进",
    "interlude": "间奏",
    "outro.": "尾奏",
}


def parse_lab_intervals(text: str) -> list[tuple[float, float, str]]:
    """解析 ``start\\tend\\tlabel`` 形式的 .lab（key / structure / chord 共用）。"""
    rows: list[tuple[float, float, str]] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            rows.append((float(parts[0]), float(parts[1]), parts[2].strip()))
        except ValueError:
            continue
    return rows


def parse_lab_beats(text: str) -> list[tuple[float, int, int, int]]:
    """解析 ``beat.lab``：``start\\tbeat_in_bar\\tnumerator\\tdenominator``。"""
    rows: list[tuple[float, int, int, int]] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            rows.append((float(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])))
        except ValueError:
            continue
    return rows


def parse_abc_header(abc: str) -> dict[str, Any]:
    """从 ABC 文本头提取速度标记、调号、拍号、音符时值基准。"""
    info: dict[str, Any] = {"q": None, "key": None, "meter": None, "unit": None}
    if not abc:
        return info
    m = _ABC_Q_RE.search(abc)
    if m:
        try:
            info["q"] = float(m.group(1))
        except ValueError:
            pass
    m = _ABC_K_RE.search(abc)
    if m:
        info["key"] = m.group(1)
    m = _ABC_M_RE.search(abc)
    if m:
        info["meter"] = m.group(1)
    m = _ABC_L_RE.search(abc)
    if m:
        info["unit"] = m.group(1)
    return info


def merge_sections(rows: list[tuple[float, float, str]]) -> list[dict[str, Any]]:
    """合并相邻同名段落，输出段落条需要的结构。"""
    merged: list[dict[str, Any]] = []
    for start, end, label in sorted(rows, key=lambda r: r[0]):
        if end <= start:
            continue
        if merged and merged[-1]["label"] == label and abs(merged[-1]["end"] - start) < 1e-3:
            merged[-1]["end"] = end
            continue
        merged.append({"label": label, "start": start, "end": end})
    for item in merged:
        item["duration"] = round(item["end"] - item["start"], 3)
        item["label_cn"] = section_to_chinese(item["label"])
    return merged


def section_to_chinese(label: str) -> str:
    """段落名 → 中文标注；未知段落原样返回（前端用调色板循环取色）。"""
    if not label:
        return "—"
    return _SECTION_CN.get(label.strip().lower(), label.strip())


def key_to_chinese(key: str) -> str:
    """``D#:major`` → ``D# 大调``；``F:minor`` → ``F 小调``。

    音名拼写**原样保留**后端输出（文档 3.6.5：不做 A#/B♭ 二次换算）。
    """
    if not key:
        return "—"
    raw = key.strip()
    if ":" not in raw:
        return raw
    root, _, mode = raw.partition(":")
    mode = mode.strip().lower()
    if mode.startswith("maj"):
        return f"{root} 大调"
    if mode.startswith("min"):
        return f"{root} 小调"
    return raw


def _derive_bpm(beats: list[tuple[float, int, int, int]]) -> float | None:
    """由实测拍点间隔中位数推导 BPM。"""
    if len(beats) < 3:
        return None
    times = sorted(b[0] for b in beats)
    diffs = [b - a for a, b in zip(times, times[1:]) if b - a > 1e-6]
    if not diffs:
        return None
    diffs.sort()
    mid = len(diffs) // 2
    median = diffs[mid] if len(diffs) % 2 else (diffs[mid - 1] + diffs[mid]) / 2.0
    if median <= 0:
        return None
    return 60.0 / median


def summarize(
    run_dir: str | Path,
    *,
    audio: str | Path | None = None,
    extra_diagnostics: list[str] | None = None,
) -> dict[str, Any]:
    """汇总一个 SheetSage2 输出目录，产出概览 + 诊断信息。"""
    run = Path(run_dir)
    result: dict[str, Any] = {}
    rj = run / "result.json"
    if rj.is_file():
        try:
            result = json.loads(rj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = {}

    def read(name: str) -> str:
        p = run / name
        if not p.is_file():
            return ""
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    beats = parse_lab_beats(read("beat.lab"))
    bpm = _derive_bpm(beats)

    meter = None
    if beats:
        meter = f"{beats[0][2]}/{beats[0][3]}"

    key_rows = parse_lab_intervals(read("key.lab"))
    key_raw = key_rows[0][2] if key_rows else None

    sections = merge_sections(parse_lab_intervals(read("structure.lab")))
    chords = [
        {"start": s, "end": e, "label": lab}
        for s, e, lab in parse_lab_intervals(read("chord.lab"))
        if lab and lab.upper() != "N"
    ]
    downbeats = []
    for line in read("downbeat.lab").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            downbeats.append(float(line.split("\t")[0]))
        except ValueError:
            continue

    abc = read("score.abc")
    header = parse_abc_header(abc)

    files = []
    for p in sorted(run.rglob("*")):
        if p.is_file():
            files.append(
                {
                    "name": p.name,
                    "rel": str(p.relative_to(run)).replace("\\", "/"),
                    "size": p.stat().st_size,
                    "path": str(p),
                }
            )

    diagnostics = list(result.get("diagnostics") or [])
    diagnostics.extend(extra_diagnostics or [])
    if bpm is not None and header.get("q"):
        if abs(bpm - header["q"]) > 1.0:
            diagnostics.append(
                f"概览 BPM({bpm:.1f}) 与谱面速度标记 Q:1/4={header['q']:.0f} 不一致，"
                "可能为半拍记谱；谱面按 Q: 渲染，SVP 按实测 BPM 生成。"
            )

    duration = result.get("duration_seconds")
    if duration is None and sections:
        duration = max(s["end"] for s in sections)

    return {
        "dir": str(run),
        "audio": str(audio) if audio else result.get("audio"),
        # --- 概览五格 ---
        "key": key_raw,
        "key_cn": key_to_chinese(key_raw or ""),
        "bpm": round(bpm, 1) if bpm is not None else None,
        "bpm_int": int(round(bpm)) if bpm is not None else None,
        "meter": meter,
        "duration": duration,
        "vocal_notes": result.get("vocal_notes"),
        "instrumental_notes": result.get("instrumental_notes"),
        "melody_notes": result.get("melody_notes"),
        # --- 段落 / 和弦 / 拍 ---
        "sections": sections,
        "chords": chords,
        "downbeats": downbeats,
        "beat_count": len(beats),
        # --- 谱面同源字段 ---
        "score_bpm": header.get("q"),
        "score_key": header.get("key"),
        "score_meter": header.get("meter"),
        "score_unit": header.get("unit"),
        # --- 诊断 ---
        "dtype": result.get("dtype"),
        "preset": result.get("preset"),
        "melody_only": result.get("melody_only"),
        "elapsed": result.get("elapsed_seconds"),
        "peak_gpu_mib": result.get("peak_gpu_mib"),
        "windows": result.get("windows") or [],
        "warnings": result.get("warnings") or [],
        "diagnostics": diagnostics,
        "abc_error": result.get("abc_error"),
        "events": result.get("events"),
        "abc": abc,
        "files": files,
    }
