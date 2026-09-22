"""歌词填充：把用户提供的 LRC 或纯文本歌词分配到音符上。

本模块**不做歌词识别**（早期版本的 Qwen3-ASR 已移除）。歌词来源是用户：
可以拖入 ``.lrc`` 文件、粘贴带时间戳的 LRC，或直接粘贴纯文本。

三种输入的处理方式
------------------
====================  ==========================================================
输入                  处理
====================  ==========================================================
LRC（带 ``[mm:ss.xx]``）  每行有起始时间 → 把该行的字**按时间**分配到落在这一行
                      时间窗内的音符上。有参考时间，最准。
纯文本                没有时间信息 → **按顺序一字一音**铺开（从第一个音符开始）。
                      翻唱常用做法。
ABC 行内歌词（``w:``）   暂不支持。
====================  ==========================================================

关于延音符
----------
分配结果里，同一个**词的下标**延续到下一个音符时写延音符：

* ``-``：同一音节延续（CJK 一字多音、长音）；
* ``+``：同一个词的下一个音节（英文多音节词，如 ``open`` → ``open`` ``+``）。

判定依据是**词的下标**而不是文本，否则歌词里的连续重复字（「来来回回」）会被
误判成延音而被吞掉——这是实测修过的一个真 bug。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

__all__ = [
    "LyricWord",
    "CONTINUATION_SUSTAIN",
    "CONTINUATION_SYLLABLE",
    "continuation_mark",
    "parse_lrc",
    "looks_like_lrc",
    "words_from_lrc",
    "words_from_plain",
    "fill_lyrics",
    "assign_lyrics",
    "build_lines",
    "build_lrc",
]

#: LRC 时间戳：``[mm:ss.xx]`` / ``[mm:ss]`` / ``[mm:ss:xx]``，一行可有多个
_LRC_TIME_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")

#: 空白与常见分隔符（不参与歌词，只用来断句）
_SPACE_RE = re.compile(r"\s+")

CONTINUATION_SUSTAIN = "-"
CONTINUATION_SYLLABLE = "+"

#: 段落名 → LRC 标签（导出 LRC 时按原曲段落插入）
SECTION_LABELS = {
    "intro": "Intro",
    "verse": "Verse",
    "pre-chorus": "Pre-Chorus",
    "prechorus": "Pre-Chorus",
    "chorus": "Chorus",
    "bridge": "Bridge",
    "interlude": "Interlude",
    "solo": "Interlude",
    "instrumental": "Interlude",
    "outro": "Outro",
    "ending": "Outro",
}


@dataclass
class LyricWord:
    """一个对齐单元（一个汉字，或一个英文词）。"""

    text: str
    start: float
    end: float
    #: 来源 LRC 的行号（0 起）；非 LRC 输入为 -1。
    #: 有了它，导出的 LRC 才能保留用户原文的断行，而不是被重新按间隔合并成一大坨。
    line: int = -1

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "start": round(self.start, 3), "end": round(self.end, 3), "line": self.line}


# --------------------------------------------------------------------------
# LRC 解析
# --------------------------------------------------------------------------
def looks_like_lrc(text: str) -> bool:
    """粗略判断输入是不是 LRC（至少有一行带时间戳）。"""
    if not text:
        return False
    return any(_LRC_TIME_RE.search(line) for line in text.splitlines()[:80])


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """解析 LRC 文本，返回 ``[(起始秒, 该行歌词), ...]``（按时间排序）。

    一行可以有多个时间戳（``[00:01.00][00:05.00]同一句``），会展开成多条。
    没有时间戳的裸行会被忽略。
    """
    rows: list[tuple[float, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        stamps = list(_LRC_TIME_RE.finditer(line))
        if not stamps:
            continue
        # 去掉所有时间戳后剩下的就是歌词
        content = _LRC_TIME_RE.sub("", line).strip()
        if not content:
            continue
        for m in stamps:
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            frac = m.group(3) or "0"
            # 两位是百分秒，三位是毫秒
            frac_sec = int(frac) / (1000.0 if len(frac) == 3 else 100.0)
            rows.append((minutes * 60 + seconds + frac_sec, content))
    rows.sort(key=lambda r: r[0])
    return rows


def _split_units(text: str) -> list[str]:
    """把一行歌词切成对齐单元：CJK 逐字，拉丁按词。"""
    units: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            units.append("".join(buf))
            buf.clear()

    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in "'’-"):
            buf.append(ch)
        elif ch.isspace() or ch in "　":
            flush()
        elif ch.isalnum():
            # CJK 等：各自成一个单元
            flush()
            units.append(ch)
        else:
            # 标点：并入当前单元（英文）或单独丢弃（中文标点不参与演唱）
            if buf:
                buf.append(ch)
            else:
                continue
    flush()
    return units


def words_from_lrc(
    lrc_text: str,
    notes: Sequence[tuple[float, float]],
    *,
    fallback_span: float = 0.8,
) -> tuple[list[LyricWord], list[str]]:
    """由 LRC + 音符时间轴生成带时间的词。

    做法：对每一行，取其时间窗 ``[行起始, 下一行起始)``；把该行的单元**按落在
    窗内的音符数目平均分配**。这样即使 LRC 只有行级时间戳，也能得到逐音符的字。

    Returns:
        ``(词列表, 警告列表)``
    """
    warnings: list[str] = []
    rows = parse_lrc(lrc_text)
    if not rows:
        return [], ["LRC 里没有解析到任何带时间戳的歌词行"]

    spans = list(notes)
    words: list[LyricWord] = []
    used_notes = 0

    for idx, (start, content) in enumerate(rows):
        end = rows[idx + 1][0] if idx + 1 < len(rows) else start + fallback_span * max(1, len(content))
        units = _split_units(content)
        if not units:
            continue
        # 落在本行窗内的音符
        inside = [(s, e) for s, e in spans if s >= start - 0.02 and s < end - 0.02]
        if not inside:
            # 退化：按行时长均分（没有音符落在窗内时仍然给出词，供后续按重叠匹配）
            step = max(0.12, (end - start) / max(1, len(units)))
            for k, unit in enumerate(units):
                words.append(LyricWord(unit, start + k * step, start + (k + 1) * step))
            continue
        used_notes += len(inside)
        if len(units) == len(inside):
            # 一一对应：直接用音符自身的时间，最准
            for unit, (ns, ne) in zip(units, inside):
                words.append(LyricWord(unit, ns, ne, line=idx))
        else:
            # 数目不等：在行时间窗内均分，再让 assign_lyrics 按时间落位
            total = max(0.2, end - start)
            step = total / len(units)
            for k, unit in enumerate(units):
                words.append(LyricWord(unit, start + k * step, start + (k + 1) * step, line=idx))
    if used_notes == 0:
        warnings.append("没有任何音符落在 LRC 行时间窗内；已按行内均分处理，对齐可能不准")
    return words, warnings


def words_from_plain(
    text: str,
    notes: Sequence[tuple[float, float]],
) -> tuple[list[LyricWord], list[str]]:
    """纯文本 → 按顺序**一字一音**铺开（没有时间信息时的常用做法）。

    标点与空白不占音符。若字数多于音符数，多余的字被丢弃并给出警告。
    """
    warnings: list[str] = []
    spans = list(notes)
    if not spans:
        return [], ["还没有乐谱，无法把歌词铺到音符上"]

    units: list[str] = []
    for line in (text or "").splitlines():
        units.extend(_split_units(line))
    if not units:
        return [], ["没有解析到可用的歌词文字"]

    if len(units) > len(spans):
        warnings.append(
            f"歌词 {len(units)} 个字多于音符 {len(spans)} 个，多出的 {len(units) - len(spans)} 字已忽略"
        )
    elif len(units) < len(spans):
        warnings.append(
            f"歌词 {len(units)} 个字少于音符 {len(spans)} 个，其余音符将填占位歌词"
        )

    words = [
        LyricWord(unit, spans[i][0], spans[i][1])
        for i, unit in enumerate(units[: len(spans)])
    ]
    return words, warnings


# --------------------------------------------------------------------------
# 对齐到音符
# --------------------------------------------------------------------------
def continuation_mark(text: str, mode: str = "auto") -> str:
    """按词决定延音符记号。

    含拉丁字母的词（英文等）用 ``+``，CJK 单字用 ``-``；
    ``mode`` 传 ``"-"`` 或 ``"+"`` 可强制统一。
    """
    if mode in (CONTINUATION_SUSTAIN, CONTINUATION_SYLLABLE):
        return mode
    has_latin = any(ch.isascii() and ch.isalpha() for ch in (text or ""))
    return CONTINUATION_SYLLABLE if has_latin else CONTINUATION_SUSTAIN


def assign_lyrics(
    words: Sequence[LyricWord],
    notes: Sequence[tuple[float, float]],
    *,
    fallback: str = "la",
    min_overlap: float = 0.01,
    method: str = "sequence",
    tolerance: float = 0.35,
    continuation: str = "auto",
) -> tuple[list[str], dict[str, Any]]:
    """把词分配到音符上。

    ``method="sequence"``（默认，推荐）按时间顺序**单调**推进，对几十到几百毫秒的
    错位更稳健（实测 82% vs 68%）。``method="overlap"`` 每个音符独立取重叠最大的词。

    延音符只在**本音符与上一个音符落在同一个词的这一次出现**时才产生，判定依据是
    **词的下标**而非文本——否则「来来回回」会被写成 ``来 - 回 -``，吞掉重复字。
    """
    if method == "overlap":
        return _assign_overlap(
            words, notes, fallback=fallback, min_overlap=min_overlap, continuation=continuation
        )
    return _assign_sequence(
        words, notes, fallback=fallback, tolerance=tolerance, continuation=continuation
    )


def _assign_overlap(words, notes, *, fallback, min_overlap, continuation="auto"):
    out: list[str] = []
    matched = 0
    previous_idx: int | None = None
    for start, end in notes:
        best_idx: int | None = None
        best_overlap = 0.0
        for idx, word in enumerate(words):
            overlap = min(end, word.end) - max(start, word.start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_idx = idx
        if best_idx is None or best_overlap < min_overlap:
            out.append(fallback)
            previous_idx = None
            continue
        matched += 1
        if previous_idx == best_idx:
            out.append(continuation_mark(words[best_idx].text, continuation))
        else:
            out.append(words[best_idx].text)
            previous_idx = best_idx
    return out, _stats(notes, matched, fallback, "overlap", continuation)


def _assign_sequence(words, notes, *, fallback, tolerance, continuation="auto"):
    out: list[str] = []
    matched = 0
    wi = 0
    previous_idx: int | None = None
    total = len(words)
    for start, end in notes:
        while wi < total and words[wi].end < start - tolerance:
            wi += 1
        if wi >= total:
            out.append(fallback)
            previous_idx = None
            continue
        word = words[wi]
        if word.start <= end + tolerance and word.end >= start - tolerance:
            matched += 1
            if previous_idx == wi:
                out.append(continuation_mark(word.text, continuation))
            else:
                out.append(word.text)
                previous_idx = wi
            if word.end <= end + tolerance:
                wi += 1
        else:
            out.append(fallback)
            previous_idx = None
    return out, _stats(notes, matched, fallback, "sequence", continuation)


def _stats(notes, matched, fallback, method, continuation="auto"):
    return {
        "method": method,
        "continuation": continuation,
        "notes": len(notes),
        "matched": matched,
        "fallback": len(notes) - matched,
        "match_rate": round(matched / len(notes), 4) if notes else 0.0,
        "fallback_text": fallback,
    }


def fill_lyrics(
    text: str,
    notes: Sequence[tuple[float, float]],
    *,
    fmt: str = "auto",
    fallback: str = "la",
    continuation: str = "auto",
) -> dict[str, Any]:
    """一站式：解析输入 → 生成词 → 分配到音符。

    Args:
        fmt: ``auto`` / ``lrc`` / ``plain``。

    Returns:
        ``{"ok", "format", "words", "lyrics", "stats", "warnings"}``
    """
    spans = [(float(s), float(e)) for s, e in notes]
    use_lrc = fmt == "lrc" or (fmt == "auto" and looks_like_lrc(text))
    if use_lrc:
        words, warnings = words_from_lrc(text, spans)
        used = "lrc"
    else:
        words, warnings = words_from_plain(text, spans)
        used = "plain"

    if not words:
        return {
            "ok": False,
            "format": used,
            "words": [],
            "lyrics": [fallback] * len(spans),
            "stats": _stats(spans, 0, fallback, "sequence", continuation),
            "warnings": warnings or ["没有解析到可用的歌词"],
        }

    lyrics, stats = assign_lyrics(words, spans, fallback=fallback, continuation=continuation)
    return {
        "ok": True,
        "format": used,
        "words": [w.as_dict() for w in words],
        "lyrics": lyrics,
        "stats": stats,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# 断句与 LRC 导出
# --------------------------------------------------------------------------
def build_lines(
    words: Sequence[LyricWord],
    *,
    gap: float = 0.9,
    max_chars: int = 18,
) -> list[dict[str, Any]]:
    """把词断成句。

    如果词带有来源行号（LRC 输入），**优先沿用用户原文的断行**——否则间隔很小，
    四行歌词会被合并成一长条，导出的 LRC 跟用户给的完全不一样。
    没有行号时退化为「按静音间隔 + 单行字数上限」。格式对齐 ``asr.json`` 的 ``lines``。
    """
    if not words:
        return []
    if all(w.line >= 0 for w in words):
        out: list[dict[str, Any]] = []
        buffer: list[str] = []
        start: float | None = None
        current_line = words[0].line
        for word in words:
            if word.line != current_line and buffer:
                out.append(
                    {"start": round(start or 0.0, 3), "end": round(prev_end, 3), "text": "".join(buffer)}
                )
                buffer = []
                start = None
                current_line = word.line
            if start is None:
                start = word.start
            buffer.append(word.text)
            prev_end = word.end
        if buffer:
            out.append(
                {"start": round(start or 0.0, 3), "end": round(prev_end, 3), "text": "".join(buffer)}
            )
        return out

    out = []
    buffer = []
    start = None
    for i, word in enumerate(words):
        if start is None:
            start = word.start
        buffer.append(word.text)
        nxt = words[i + 1] if i + 1 < len(words) else None
        silence = nxt is None or (nxt.start - word.end) >= gap
        if silence or len(buffer) >= max_chars:
            out.append({"start": round(start, 3), "end": round(word.end, 3), "text": "".join(buffer)})
            buffer = []
            start = None
    return out


def build_lrc(
    words: Sequence[LyricWord],
    *,
    sections: Sequence[dict[str, Any]] | None = None,
    gap: float = 0.9,
    max_chars: int = 18,
    section_labels: dict[str, str] | None = None,
) -> str:
    """由词生成 LRC，并按原曲段落插入 ``[Verse]`` 等标签。"""
    lines = build_lines(words, gap=gap, max_chars=max_chars)
    if not lines:
        return ""
    labels = section_labels or {}
    marks: list[tuple[float, str]] = []
    for section in sections or []:
        label = str(section.get("label") or "")
        if not label:
            continue
        marks.append((float(section.get("start") or 0.0), f"[{labels.get(label.lower(), label)}]"))
    marks.sort(key=lambda x: x[0])

    out: list[str] = []
    mi = 0
    for line in lines:
        while mi < len(marks) and marks[mi][0] <= line["start"]:
            out.append(marks[mi][1])
            mi += 1
        out.append(f"{_lrc_time(line['start'])}{line['text']}")
    while mi < len(marks):
        out.append(marks[mi][1])
        mi += 1
    return "\n".join(out) + "\n"


def _lrc_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    rest = seconds - minutes * 60
    return f"[{minutes:02d}:{rest:05.2f}]"
