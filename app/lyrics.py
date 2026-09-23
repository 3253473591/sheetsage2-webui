"""歌词对齐：把**带时间的词**分配到音符上。

本模块**不做歌词识别**（早期版本的 Qwen3-ASR 已移除），也不负责"从 LRC/纯文本
生成词"——那部分（``parse_lrc`` / ``words_from_lrc`` / ``words_from_plain`` /
``fill_lyrics`` / ``build_lrc``）随 ④ 歌词填充卡片一起删除了。

现在的分工
----------
* **歌词的唯一来源是 ③ 钢琴卷帘**：用户在卷帘上逐音编辑，或用 Ctrl+L 批量填词；
  时间由音符本身给定，导出时直接取 ``AbcNote.lyric``（见 ``app.rebuild``）。
* 本模块剩下的能力只有两件，供那条**旧的** ABC 路径（``lyrics.json`` 存在时）使用：
  1. :class:`LyricWord` —— 带时间的词；
  2. :func:`assign_lyrics` —— 把词按时间对齐到音符，并给出延音符记号。

关于延音符
----------
同一个**词的下标**延续到下一个音符时写延音符：

* ``-``：同一音节延续（CJK 一字多音、长音）；
* ``+``：同一个词的下一个音节（英文多音节词，如 ``open`` → ``open`` ``+``）。

判定依据是**词的下标**而不是文本，否则歌词里的连续重复字（「来来回回」）会被
误判成延音而被吞掉——这是实测修过的一个真 bug。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "LyricWord",
    "CONTINUATION_SUSTAIN",
    "CONTINUATION_SYLLABLE",
    "continuation_mark",
    "assign_lyrics",
]

CONTINUATION_SUSTAIN = "-"
CONTINUATION_SYLLABLE = "+"


@dataclass
class LyricWord:
    """一个对齐单元（一个汉字，或一个英文词）。"""

    text: str
    start: float
    end: float
    #: 来源行的行号（0 起）。**历史字段**：当初 LRC 输入靠它保留用户原文的断行，
    #: 现在只剩读旧 ``lyrics.json`` 时带进来，没有消费者了。保留是为了不改动
    #: 那个 JSON 契约的形状（老任务的 lyrics.json 仍能被读出来）。
    line: int = -1

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "start": round(self.start, 3), "end": round(self.end, 3), "line": self.line}


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

    ⚠️ **卷帘路径不要走这里**：它是按时间窗对齐的，会把用户逐音填好的词冲掉。
    卷帘的音符自带歌词，导出时直接用（见 ``rebuild.rebuild_exports`` 的 ``pre_timed`` 分支）。
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
