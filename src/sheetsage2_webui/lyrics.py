"""把逐字时间戳填到音符上。

输入是两样东西：

- **音符**：来自 `project.json` 的 tick 域音符（只用到 `tick` / `dur`）
- **逐字时间戳**：来自 `asr.AsrEngine` 的 `AlignedChar`（秒，整首曲时间轴）

映射规则：

1. 一个字的范围是 `[s, e]`（零长度的退化字当成"发生在一瞬间"，给对称窗口）。它挂到一个
   **起点落在窗口里**的音；窗口里有多个音时，空的优先，再比"字是不是真的唱在它上面"。
2. 一个字唱了几个音：第一个音挂字，后面的音写转音标记（中文/日文 `-`，英文 `+`）。
3. 有多出来的字没有音位可放时，**记进 `absorbed_chars`**，不丢也不悄悄覆盖——
   根因是那段旋律的音比字少（模型没扒出那么细），需要人工核对。
4. 没分到字的音保留空串并在报告里标出来。

时间轴统一用**秒**做匹配（容差是秒，更直观），再用 `timeline` 把结果写回音符。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .timeline import Timeline

#: 默认容差（秒）。字的时间戳和音符起点常有几十毫秒的错位。
DEFAULT_TOLERANCE = 0.20

MELISMA = "-"
CONTINUATION = "+"


@dataclass
class LyricFill:
    """一次填词的结果。"""

    lyrics: list[str] = field(default_factory=list)   # 与传入音符一一对应
    assigned: int = 0        # 挂了字（含被挤掉但登记了的）的字数
    melisma: int = 0         # 写成转音标记的音符数
    dropped: int = 0         # 起点落在音符间隙里、真的没音可挂的字数
    unmatched_notes: int = 0  # 没分到字的音符数
    warnings: list[str] = field(default_factory=list)
    dropped_chars: list[str] = field(default_factory=list)
    #: 挂上去了、但和别的字落在同一个音上、歌词串里显示不出来的字。
    #: 不是"丢了"——那段旋律本来就只有一个音位，是**模型没扒出那么细的音**。
    #: 例：《光芒》"流进了梦想"6 个字只有 4 个音，进/了 挤在同一个长音上。
    absorbed_chars: list[str] = field(default_factory=list)
    #: 每个字是走哪条路挂上去的（诊断用）：window / fallback / absorbed / dropped
    reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(assigned=self.assigned, melisma=self.melisma, dropped=self.dropped,
                    unmatched_notes=self.unmatched_notes,
                    absorbed=len(self.absorbed_chars),
                    absorbed_chars=self.absorbed_chars,
                    dropped_chars=self.dropped_chars, reasons=self.reasons,
                    warnings=self.warnings)


def _is_latin(text: str) -> bool:
    return any("a" <= ch.lower() <= "z" for ch in text)


def continuation_marker(text: str) -> str:
    """后续音节用哪个标记：英文词写 `+`，其余（中日文）写 `-`。"""
    return CONTINUATION if _is_latin(text) else MELISMA


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _build_order(spans: Sequence[tuple[float, float]]) -> list[int]:
    """音符按起点排序后的下标序列。"""
    return sorted(range(len(spans)), key=lambda index: spans[index][0])


# --------------------------------------------------------------------- 切分模式
@dataclass
class SplitNote:
    """切分模式产出的**新音符**。它是"一个字的一段"，不再等于模型的某个音。"""

    source: int          # 来自哪个原音（下标）；-1 表示没有对应的音
    start: float
    end: float
    text: str            # 这一段属于哪个字（"" = 只是延音段）
    slur: bool           # 是不是同一个字的后续段（对应现在的转音标记）

    @property
    def duration(self) -> float:
        return self.end - self.start

    def as_dict(self) -> dict[str, Any]:
        return dict(source=self.source, start=round(self.start, 4),
                    end=round(self.end, 4), text=self.text, slur=self.slur)


@dataclass
class SplitFill:
    """切分模式的结果与代价统计。"""

    notes: list[SplitNote] = field(default_factory=list)
    visible: int = 0         # 有音可挂、切出了自己那一段的字数
    absorbed: int = 0        # 仍然挤在同一个音上（这一段没有可切的边界）
    dropped: int = 0         # 起点落在音符间隙里，真的没音可挂
    degenerate: int = 0      # 零长度时间戳（对齐器给不出时间）：没有窗口，走兜底挂法
    fallback: int = 0        # 其中靠"离得最近的音"挂上去的字数
    cut_notes: int = 0       # 被切开的原音数
    added_notes: int = 0     # 净新增的音符数（新音符表 - 原音符表）
    untouched_notes: int = 0 # 没被任何字碰到的原音（原样保留）
    original_notes: int = 0  # 原音符表大小（切分前的）
    warnings: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        """谱面被改写的比例（新增音符 / 原音符）。"""
        return self.added_notes / self.original_notes if self.original_notes else 0.0

    def as_dict(self) -> dict[str, Any]:
        return dict(visible=self.visible, absorbed=self.absorbed, dropped=self.dropped,
                    degenerate=self.degenerate, fallback=self.fallback,
                    cut_notes=self.cut_notes,
                    added_notes=self.added_notes, untouched_notes=self.untouched_notes,
                    notes_before=self.original_notes,
                    notes_after=self.original_notes + self.added_notes,
                    ratio=round(self.ratio, 4),
                    warnings=self.warnings)


def assign_lyrics_split(
    spans: Sequence[tuple[float, float]],
    chars: Sequence[Any],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> SplitFill:
    """**切分模式**（对照 Vocal2Midi 的中文路径）。

    和 :func:`assign_lyrics`（onset 模式，对应他们的英文路径）的区别：

    - onset：音符是槽，字往音上挂，**音符一个都不动**。一段旋律只有 4 个音却唱了 6 个字时，
      多出来的字显示不出来，只能记进 `absorbed_chars`。
    - 切分：以**字**为单位，按字的时间边界把音符切开——扫过与字的时间窗相交的音，每段取
      交集时长；同一个字的后续段打 `slur`（等价于现在写 `-`）。于是"一音多字"看得见了，
      代价是**音符表被改写**：新增的音符是模型没扒出来的音位，是我们按时间戳补的。

    出处：`reference/Vocal2Midi/inference/game/alignment_utils.py:205-251`。

    两条保护：

    1. **同一个音产出的段不重叠**（用 `consumed` 记录每个音已经切到哪），总时长不会超过原音；
    2. **零长度时间戳的字走兜底**：对齐器给不出时间就没有边界可切，这些字按"离得最近的音"
       挂一段（`fallback`），跟现在的行为一致——不然切分模式反而会**丢掉**这部分字（实测
       《光芒》53/324 = 16%）。
    """
    fill = SplitFill()
    if not spans:
        return fill

    order = _build_order(spans)
    consumed: dict[int, float] = {}
    per_source: dict[int, int] = {}
    touched: set[int] = set()

    def place(note: int, segment_start: float, segment_end: float, text: str,
              first: bool) -> None:
        fill.notes.append(SplitNote(source=note, start=segment_start, end=segment_end,
                                    text=text, slur=not first))
        consumed[note] = segment_end
        per_source[note] = per_source.get(note, 0) + 1
        touched.add(note)

    for char in chars:
        text = str(getattr(char, "text", "") or "").strip()
        if not text:
            continue
        start = float(getattr(char, "start", 0.0))
        end = float(getattr(char, "end", start))
        if end <= start + 1e-9:
            fill.degenerate += 1
            # 兜底：找"离这个时间点最近、并且那一段还没被切走"的音，给它一小段
            best, best_gap = None, None
            for note in order:
                note_start, note_end = spans[note]
                gap = 0.0 if note_start <= start <= note_end else min(
                    abs(note_start - start), abs(note_end - start))
                if gap > tolerance and best is not None:
                    break
                if gap > tolerance:
                    continue
                segment_start = max(start, note_start, consumed.get(note, note_start))
                segment_end = min(note_end, segment_start + tolerance)
                if segment_end - segment_start <= 1e-9:
                    continue
                if best_gap is None or gap < best_gap:
                    best, best_gap = note, gap
            if best is not None:
                note_start, note_end = spans[best]
                segment_start = max(start, note_start, consumed.get(best, note_start))
                segment_end = min(note_end, segment_start + tolerance)
                place(best, segment_start, segment_end, text, True)
                fill.fallback += 1
                fill.visible += 1
            else:
                fill.absorbed += 1
            continue

        pieces: list[SplitNote] = []
        for note in order:
            note_start, note_end = spans[note]
            if note_end <= start + 1e-9:
                continue                        # 这个字开始之前就响完了
            if note_start >= end - 1e-9:
                break                           # 起点单调，后面的音只会更晚
            segment_start = max(start, note_start, consumed.get(note, -1e18))
            segment_end = min(end, note_end)
            if segment_end - segment_start <= 1e-9:
                continue                        # 这一段已经被前面的字切走了
            place(note, segment_start, segment_end, text if not pieces else "",
                  not pieces)
            pieces.append(fill.notes[-1])

        if pieces:
            fill.visible += 1
            continue
        # 切不出来：要么时间窗落在音符之间的空隙里（真的没音），要么那一段已经被占了
        if any(note_start <= start <= note_end for note_start, note_end in spans):
            fill.absorbed += 1
        else:
            fill.dropped += 1

    # 没被任何字碰到的音原样保留（切分只动有关系的那几个音）
    fill.original_notes = len(spans)
    fill.untouched_notes = len(spans) - len(touched)
    fill.cut_notes = sum(1 for count in per_source.values() if count >= 2)
    fill.added_notes = len(fill.notes) - len(touched)
    fill.notes.sort(key=lambda item: (item.start, item.source))
    if fill.absorbed:
        fill.warnings.append(
            f"{fill.absorbed} 个字切不出自己的一段（窗口落在已被切走的区间里），仍然显示不出来")
    if fill.degenerate:
        fill.warnings.append(
            f"{fill.degenerate} 个字是零长度时间戳，没有边界可切（对齐器给不出时间）")
    if fill.dropped:
        fill.warnings.append(f"{fill.dropped} 个字落在音符之间的间隙里，没挂上")
    return fill


def assign_lyrics(
    spans: Sequence[tuple[float, float]],
    chars: Sequence[Any],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    existing: Sequence[str] | None = None,
    only_empty: bool = False,
) -> LyricFill:
    """把逐字时间戳填到音符上。

    参数
    ----
    spans       每个音符的 `(start_seconds, end_seconds)`，按轨道顺序
    chars       逐字时间戳，需要 `.text` / `.start` / `.end`
    tolerance   字的时间窗向前放宽多少秒去找音符起点
    existing    已有歌词；给了就在它基础上填
    only_empty  只填目前为空的音符（不覆盖用户已填的）

    两遍：**先给每个字定下它挂的那个音（头），再回头补转音标记**。反过来做会被贪心
    吃掉下一个字（实测开头会变成"曾 - 天 空"，"经"丢失）。

    **单调性**：后面出现的字不能挂到更早开始的音上。零长度字（对齐器给不出时间）
    只能取最近的音，没有这条约束就会出现"喜 马 拉 山 雅"这种前后颠倒。

    **这个函数原来有一个下标 bug**：`last_pos` 记的是"按起点排序后的位置"，却被当成
    音符下标用（`range(last_pos + 1, ...)`）。两者只有在音符本来就有序时才相等，于是从
    第一个"起点乱序"的音符之后，扫描起点就跳到了旋律中段，**那个位置之前的音再也扫
    不到**。实测《光芒》因此丢掉 29/324 个字，而且全被误报成"没有重叠"——真正的原因是
    它们根本没被扫到。
    """
    lyrics = list(existing) if existing else [""] * len(spans)
    fill = LyricFill(lyrics=lyrics)
    if not spans:
        return fill
    if not chars:
        fill.unmatched_notes = len(spans)
        return fill

    order = _build_order(spans)
    taken: set[int] = set()
    #: 音符下标 -> (字, 字的 start, 字的 end)。第二遍补转音时要用。
    heads: dict[int, tuple[str, float, float]] = {}

    def available(note: int) -> bool:
        if note in taken:
            return False
        return not (only_empty and lyrics[note].strip())

    last_note = -1

    # ---------------- 第一遍：只决定每个字挂在哪个音上 ----------------
    for char in chars:
        text = str(getattr(char, "text", "") or "").strip()
        if not text:
            continue
        start = float(getattr(char, "start", 0.0))
        end = float(getattr(char, "end", start))

        # 对齐器对约 16% 的字给出零长度时间戳（start == end），意思是"这个字没对齐上"。
        # 此时 `[start − 容差, end)` 会退化成空窗口，把它们全漏掉（实测《光芒》53/324）。
        degenerate = end <= start + 1e-9
        window_end = start + tolerance if degenerate else end

        # 第一步：这个字是不是"前一个字的延续"？两个条件都要满足：
        #   1. 上一个字占着的那个音**现在还在响**；
        #   2. 这个字是在上**一个字结束之前**就开始了的（"长"0.0-0.9 唱在 [0,0.3)+[0.3,0.6)
        #      两个音上时，0.3 处那个字才是"长"的延续）。
        # 在上一个字结束**之后**才开始的字是下一个字的起音，不该当成延续。
        # 这一步要放在选音之前——属于前一个字的转音，不该抢一个新音。
        head_note = max(heads) if heads else None
        head_sounding = (head_note is not None
                         and spans[head_note][0] <= start <= spans[head_note][1])
        prev_end = heads[max(heads, key=lambda n: heads[n][2])][2] if heads else -1e18
        if head_sounding and start < prev_end - 1e-9:
            fill.reasons["continuation"] = fill.reasons.get("continuation", 0) + 1
            text0, start0, end0 = heads[head_note]
            heads[head_note] = (text0, start0, max(end0, max(end, start)))
            fill.assigned += 1
            continue

        # 第二步：在窗口里挑一个能放字的音。空的音一定优先于已经有主的音；同为空的
        # （或同为主）时再比"字是不是真的唱在它上面"（包含、重叠）。
        best_note: int | None = None
        best_key: tuple[int, int, int, float] | None = None
        for note in order:
            note_start, note_end = spans[note]
            if note_start < start - tolerance:
                continue                        # 已经响过、或者比容差还早
            if note_start >= window_end:
                break                           # 起点单调，后面的音只会更远
            if not available(note):
                continue
            contained = 1 if (note_start <= start <= note_end) else 0
            overlap = _overlap(note_start, note_end, start, end)
            key = (contained, 1 if overlap > 1e-9 else 0, overlap)
            if best_key is None or key > best_key:
                best_key, best_note = key, note

        if best_note is not None:
            fill.reasons["window"] = fill.reasons.get("window", 0) + 1
            lyrics[best_note] = text
            taken.add(best_note)
            heads[best_note] = (text, start, max(end, start))
            last_note = max(last_note, best_note)
            fill.assigned += 1
            continue

        # 第三步：窗口里没有空音了。找一个"和这个字重叠最多、还没主"的音（可以超出窗口）。
        # 同时记住：这个字的起点是不是落在某个**已经在响**的音里（那个音已经被占了）。
        fallback: int | None = None
        best_value: float | None = None
        covering: int | None = None
        for note in order:
            note_start, note_end = spans[note]
            if covering is None and note_start <= start <= note_end:
                covering = note
            if note < last_note or not available(note):
                continue
            value = (-abs(note_start - start) if degenerate
                     else _overlap(note_start, note_end, start, end))
            if best_value is None or value > best_value + 1e-9:
                best_value, fallback = value, note
        if fallback is not None and (degenerate or (best_value or 0.0) > 1e-9):
            fill.reasons["fallback"] = fill.reasons.get("fallback", 0) + 1
            lyrics[fallback] = text
            taken.add(fallback)
            heads[fallback] = (text, start, max(end, start))
            last_note = max(last_note, fallback)
            fill.assigned += 1
            continue

        # 第四步：这个字确实唱在那个音的时间里，只是那个音位被别的字占了。
        # **不丢字，也不假装显示得出来**——记进 absorbed，让人能核对。根因是那段旋律的
        # 音比字少（模型没扒那么细），例：《光芒》"流进了梦想"6 个字只有 4 个音。
        if covering is not None:
            fill.reasons["absorbed"] = fill.reasons.get("absorbed", 0) + 1
            fill.absorbed_chars.append(text)
            fill.warnings.append(
                f"字「{text}」({start:.2f}s) 落在已经占住的音上"
                f"（音 {covering}，[{spans[covering][0]:.2f}-{spans[covering][1]:.2f}]s）："
                f"那段音符比字少")
            fill.assigned += 1
            continue

        # 剩下的就是起点落在音符之间的间隙里：真的没音可挂。
        fill.reasons["dropped"] = fill.reasons.get("dropped", 0) + 1
        fill.dropped += 1
        fill.dropped_chars.append(text)

    # ---------------- 第二遍：补转音标记 ----------------
    # 一个字挂到某个音之后，从这个音往后、直到下一个字的"头"之前，落在该字时间窗内的
    # 音符都算这个字的转音。零长度的字没有窗口，不参与这一遍。
    ordered_heads = sorted(heads.items(), key=lambda item: item[0])
    for position, (head, (text, start, end)) in enumerate(ordered_heads):
        if end <= start + 1e-9:
            continue
        limit = (ordered_heads[position + 1][0]
                 if position + 1 < len(ordered_heads) else len(spans))
        marker = continuation_marker(text)
        for note in order:
            if note <= head or note >= limit or note in taken:
                continue
            if spans[note][0] >= end:
                break     # 转音必须落在字的时间窗内；容差只用来找"头"，不放大窗口
            if only_empty and lyrics[note].strip():
                continue
            lyrics[note] = marker
            taken.add(note)
            fill.melisma += 1

    fill.unmatched_notes = sum(1 for index in range(len(spans))
                               if index not in taken and not lyrics[index].strip())
    if fill.dropped:
        fill.warnings.append(f"{fill.dropped} 个字落在音符之间的间隙里，没挂上")
    if fill.absorbed_chars:
        fill.warnings.append(
            f"{len(fill.absorbed_chars)} 个字和别的字落在同一个音上（那段音符比字少，"
            f"歌词串里显示不出来）：{''.join(fill.absorbed_chars)}")
    return fill


def note_spans_seconds(timeline: Timeline, notes: Sequence[Mapping[str, Any]]
                       ) -> list[tuple[float, float]]:
    """把 tick 域音符换算成秒跨度。

    **优先用 `raw_tick` / `raw_dur`（量化前的位置和时长）**：逐字时间戳是对着模型自己的
    音符时间轴产生的，而量化后的 tick 是被吸到网格上的位置，两者可能差半个网格步长
    （再加节拍阶梯换来的位移，实测最大 0.86s）。用量化后的位置去匹配，字会被吸到错的
    音上——实测《光芒》"显示不出来"的字从 29 涨到 60，《相思》从 18 涨到 42。
    没有 `raw_*`（比如老工程、或没开量化）时退回 `tick` / `dur`。
    """
    spans: list[tuple[float, float]] = []
    for note in notes:
        tick = float(note.get("raw_tick", note.get("tick", 0)))
        end = tick + float(note.get("raw_dur", note.get("dur", 0)))
        spans.append((timeline.tick_to_sec(tick), timeline.tick_to_sec(end)))
    return spans


def fill_project(project: Mapping[str, Any], chars: Sequence[Any], *,
                 track_id: str = "vocal", tolerance: float = DEFAULT_TOLERANCE,
                 only_empty: bool = False) -> dict[str, Any]:
    """把逐字时间戳填进 `project.json` 的某条轨道，原地返回 lyrics 与统计。

    只改内存里的 dict，落盘由调用方决定。
    """
    from .timeline import from_stored_measures

    timeline = from_stored_measures(project["timeline"]["measures"])
    track = next((t for t in project.get("tracks", []) if t.get("id") == track_id), None)
    if track is None:
        return {"found": False, "reason": f"没有轨道 {track_id}"}

    notes = track.get("notes", [])
    spans = note_spans_seconds(timeline, notes)
    existing = [str(note.get("lyric") or "") for note in notes]
    fill = assign_lyrics(spans, chars, tolerance=tolerance, existing=existing,
                         only_empty=only_empty)
    for note, lyric in zip(notes, fill.lyrics):
        note["lyric"] = lyric
    return {"found": True, "track": track_id, "notes": len(notes),
            "fill": fill.as_dict(), "lyrics": fill.lyrics}
