"""对照实验：现有贪心 vs 「句首/句末锚定 + 中间调整」 vs 全局单调 DP。

不跑模型、不碰 GPU。只用 output/<工程>/ 里已有的 project.json + lyrics.json。

三种结局，缺一不可：

    hit     挂上了，而且那个音和这个字在时间上真的重叠（看得见 + 位置对）
    miss    挂上了，但那个音在字唱完之后才开始 / 在字开始之前就唱完了（看得见 + 位置错）
    hidden  根本没挂上（absorbed / dropped）

为什么不能只看 hidden：现有实现在窗口里找不到音时会走 fallback（"离得最近、还没主"），
它能"挂上"一个几秒之后才响的音——hidden 好看，位置全错。所以 hit / miss 必须一起看。

六个变体：

| 名字 | 做法 |
|---|---|
| `greedy` | 现在线上的 `lyrics.assign_lyrics`（逐行复刻，用来验口径） |
| `clip`   | 不锚定，只把每句的搜索范围裁到本句时间跨度内的音 |
| `anchor` | 提案：钉死句首字 + 句末字，中间单调 DP，句间不许相连 |
| `head`   | 只钉句首字 |
| `sdp`    | 按句切段 + 硬边界，句内单调 DP（用**句的时间跨度**当边界，不钉字） |
| `dp`     | 不分句的全局单调 DP（理论上界参照） |

结论见 `docs/对齐-锚定方案评估.md`。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from sheetsage2_webui.lyrics import (  # noqa: E402
    DEFAULT_TOLERANCE, assign_lyrics, continuation_marker, note_spans_seconds,
)
from sheetsage2_webui.timeline import from_stored_measures  # noqa: E402

TOL = DEFAULT_TOLERANCE
LINE_GAP = 1.0
LINE_MAX_CHARS = 18


@dataclass
class Char:
    text: str
    start: float
    end: float

    @property
    def degenerate(self) -> bool:
        return self.end <= self.start + 1e-9


@dataclass
class Fill:
    lyrics: list[str] = field(default_factory=list)
    heads: dict[int, int] = field(default_factory=dict)   # note -> char index
    absorbed: int = 0
    dropped: int = 0
    melisma: int = 0
    unmatched: int = 0


# ------------------------------------------------------------------ 载入与工具
def load_case(directory: Path):
    project_path, lyrics_path = directory / "project.json", directory / "lyrics.json"
    if not (project_path.is_file() and lyrics_path.is_file()):
        return None
    project = json.loads(project_path.read_text(encoding="utf-8"))
    lyrics = json.loads(lyrics_path.read_text(encoding="utf-8"))
    chars = [Char(str(i["text"]), float(i["start"]), float(i["end"]))
             for i in lyrics.get("chars") or [] if str(i.get("text") or "").strip()]
    track = next((t for t in project.get("tracks") or [] if t.get("id") == "vocal"), None)
    if not chars or not track or not track.get("notes"):
        return None
    timeline = from_stored_measures(project["timeline"]["measures"])
    return {"name": directory.name, "chars": chars,
            "spans": note_spans_seconds(timeline, track["notes"])}


def sentences(chars, gap=LINE_GAP, max_chars=LINE_MAX_CHARS):
    """和 runner.group_lyric_lines 同一套断句口径。"""
    groups, current, prev_end = [], [], None
    for char in chars:
        if current and (char.start - prev_end > gap or len(current) >= max_chars):
            groups.append(current)
            current = []
        current.append(char)
        prev_end = char.end if prev_end is None else max(prev_end, char.end)
    if current:
        groups.append(current)
    return groups


def window(char: Char):
    return (char.start - TOL, char.start + TOL if char.degenerate else char.end)


def overlap(a, b, c, d):
    return max(0.0, min(b, d) - max(a, c))


def best_note(spans, char, *, lo, hi):
    start, end = window(char)
    best, best_key = None, None
    for index in range(lo, hi):
        note_start, note_end = spans[index]
        if note_start < start:
            continue
        if note_start >= end:
            break
        contained = 1 if (note_start <= char.start <= note_end) else 0
        o = overlap(note_start, note_end, char.start, char.end)
        key = (contained, 1 if o > 1e-9 else 0, o)
        if best_key is None or key > best_key:
            best, best_key = index, key
    return best


def build(spans, chars, heads, *, absorbed, dropped, line_of=None):
    """给一个 note -> char 的挂法，补转音与统计。"""
    result = Fill(lyrics=[""] * len(spans), heads=dict(heads),
                  absorbed=absorbed, dropped=dropped)
    used = set(heads)
    for note, index in heads.items():
        result.lyrics[note] = chars[index].text
    ordered = sorted(heads.items())
    for position, (head, index) in enumerate(ordered):
        char = chars[index]
        if char.degenerate:
            continue
        limit = ordered[position + 1][0] if position + 1 < len(ordered) else len(spans)
        if line_of is not None:
            line = line_of[index]
            later = [note for note, ci in ordered if ci > index and line_of[ci] != line]
            if later:
                limit = min(limit, min(later))
        marker = continuation_marker(char.text)
        for note in range(head + 1, limit):
            if note in used:
                continue
            if spans[note][0] >= char.end:
                break
            result.lyrics[note] = marker
            used.add(note)
            result.melisma += 1
    result.unmatched = sum(1 for i in range(len(spans))
                           if i not in used and not result.lyrics[i].strip())
    return result


# ------------------------------------------------------------------ 变体
def variant_greedy(spans, chars, **kw):
    """线上 `lyrics.assign_lyrics` 的逐行复刻，唯一区别是把 heads 记下来。

    `check_greedy()` 会验证它产出的 lyrics 与线上实现逐音相同。
    """
    fill = Fill(lyrics=[""] * len(spans))
    if not spans or not chars:
        fill.unmatched = len(spans)
        return fill
    order = sorted(range(len(spans)), key=lambda index: spans[index][0])
    taken: set[int] = set()
    heads: dict[int, tuple[str, float, float]] = {}
    head_index: dict[int, int] = {}
    last_note = -1

    for index, char in enumerate(chars):
        text, start, end = char.text, char.start, char.end
        degenerate = char.degenerate
        window_end = start + TOL if degenerate else end

        head_note = max(heads) if heads else None
        head_sounding = (head_note is not None
                         and spans[head_note][0] <= start <= spans[head_note][1])
        prev_end = heads[max(heads, key=lambda n: heads[n][2])][2] if heads else -1e18
        if head_sounding and start < prev_end - 1e-9:
            text0, start0, end0 = heads[head_note]
            heads[head_note] = (text0, start0, max(end0, max(end, start)))
            continue

        best_index, best_key = None, None
        for note in order:
            note_start, note_end = spans[note]
            if note_start < start - TOL:
                continue
            if note_start >= window_end:
                break
            if note in taken:
                continue
            contained = 1 if (note_start <= start <= note_end) else 0
            o = overlap(note_start, note_end, start, end)
            key = (contained, 1 if o > 1e-9 else 0, o)
            if best_key is None or key > best_key:
                best_key, best_index = key, note
        if best_index is not None:
            fill.lyrics[best_index] = text
            taken.add(best_index)
            heads[best_index] = (text, start, max(end, start))
            head_index[best_index] = index
            last_note = max(last_note, best_index)
            continue

        fallback, best_value, covering = None, None, None
        for note in order:
            note_start, note_end = spans[note]
            if covering is None and note_start <= start <= note_end:
                covering = note
            if note < last_note or note in taken:
                continue
            value = (-abs(note_start - start) if degenerate
                     else overlap(note_start, note_end, start, end))
            if best_value is None or value > best_value + 1e-9:
                best_value, fallback = value, note
        if fallback is not None and (degenerate or (best_value or 0.0) > 1e-9):
            fill.lyrics[fallback] = text
            taken.add(fallback)
            heads[fallback] = (text, start, max(end, start))
            head_index[fallback] = index
            last_note = max(last_note, fallback)
            continue
        if covering is not None:
            fill.absorbed += 1
            continue
        fill.dropped += 1

    ordered = sorted(heads.items())
    for position, (head, (text, start, end)) in enumerate(ordered):
        if end <= start + 1e-9:
            continue
        limit = ordered[position + 1][0] if position + 1 < len(ordered) else len(spans)
        marker = continuation_marker(text)
        for note in order:
            if note <= head or note >= limit or note in taken:
                continue
            if spans[note][0] >= end:
                break
            fill.lyrics[note] = marker
            taken.add(note)
            fill.melisma += 1
    fill.unmatched = sum(1 for i in range(len(spans))
                         if i not in taken and not fill.lyrics[i].strip())
    fill.heads = head_index
    return fill


def monotone_dp(spans, chars, char_ids, note_ids, *, absorb_cost=0.9):
    """单调序列对齐：字按顺序、音按顺序，可跳音、可弃字。返回 {char_id: note}。"""
    n, m = len(char_ids), len(note_ids)
    INF = float("inf")
    dp = [[INF] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        dp[0][j] = 0.0
        back[0][j] = ("skip", None)
    for i in range(1, n + 1):
        dp[i][0] = i * absorb_cost
        back[i][0] = ("absorb", None)
    for i in range(1, n + 1):
        char = chars[char_ids[i - 1]]
        start, end = window(char)
        for j in range(1, m + 1):
            note_start, note_end = spans[note_ids[j - 1]]
            best, move = dp[i][j - 1], ("skip", None)
            if dp[i - 1][j] + absorb_cost < best:
                best, move = dp[i - 1][j] + absorb_cost, ("absorb", None)
            if start <= note_start < end:
                exact = 0.0 if overlap(note_start, note_end, char.start, char.end) > 0 else 0.05
                cost = dp[i - 1][j - 1] + abs(note_start - char.start) + exact
                if cost < best:
                    best, move = cost, ("assign", note_ids[j - 1])
            dp[i][j], back[i][j] = best, move
    picked = {}
    i, j = n, m
    while i > 0 and j > 0:
        move = back[i][j]
        if move is None:
            break
        kind, note = move
        if kind == "assign":
            picked[char_ids[i - 1]] = note
            i, j = i - 1, j - 1
        elif kind == "absorb":
            i -= 1
        else:
            j -= 1
    return picked


def variant_global_dp(spans, chars, *, absorb_cost=0.9, **kw):
    picked = monotone_dp(spans, chars, list(range(len(chars))), list(range(len(spans))),
                         absorb_cost=absorb_cost)
    return build(spans, chars, {note: ci for ci, note in picked.items()},
                 absorbed=len(chars) - len(picked), dropped=0)


def variant_sentence_clip(spans, chars, *, absorb_cost=0.9, **kw):
    """便宜做法：不锚定具体字，只把每句的搜索范围裁到本句时间跨度内的音。"""
    groups = sentences(chars)
    line_of = {}
    for number, sentence in enumerate(groups):
        for char in sentence:
            line_of[id(char)] = number
    mapping = {index: line_of[id(chars[index])] for index in range(len(chars))}
    heads, absorbed, dropped = {}, 0, 0
    cursor, offset = 0, 0
    for sentence in groups:
        base = offset
        offset += len(sentence)
        hi = cursor
        while hi < len(spans) and spans[hi][0] <= sentence[-1].end + TOL:
            hi += 1
        hi = min(len(spans), max(hi, cursor + 1))
        if cursor >= len(spans):
            dropped += len(sentence)
            continue
        for k, char in enumerate(sentence):
            note = best_note(spans, char, lo=cursor, hi=hi)
            if note is None:
                if any(spans[i][0] <= char.start <= spans[i][1] for i in range(cursor, hi)):
                    absorbed += 1
                else:
                    dropped += 1
                continue
            heads[note] = base + k
            cursor = note + 1
    return build(spans, chars, heads, absorbed=absorbed, dropped=dropped,
                 line_of=mapping)


def variant_anchored(spans, chars, *, absorb_cost=0.9, anchor_last=True,
                     skip_degenerate_anchor=True, **kw):
    """句首 + 句末锚定，中间单调 DP；句间不许相连（下一句从上一句末字之后开始）。

    `skip_degenerate_anchor=True` 时，句末字若是零长度时间戳（对齐器没给时间），
    **不拿它当锚点**——那等于用一个编出来的位置去锁整句的右边界。
    """
    groups = sentences(chars)
    line_of = {}
    for number, sentence in enumerate(groups):
        for char in sentence:
            line_of[id(char)] = number
    mapping = {index: line_of[id(chars[index])] for index in range(len(chars))}
    heads, absorbed, dropped = {}, 0, 0
    cursor, offset = 0, 0
    for sentence in groups:
        base = offset
        offset += len(sentence)
        last_index = base + len(sentence) - 1
        a = best_note(spans, sentence[0], lo=cursor, hi=len(spans))
        if a is None and not (skip_degenerate_anchor and sentence[0].degenerate):
            a = next((i for i in range(cursor, len(spans))
                      if spans[i][0] >= sentence[0].start - TOL), None)
        if a is None:
            dropped += len(sentence)
            continue
        b = a
        if anchor_last and last_index > base:
            if skip_degenerate_anchor and sentence[-1].degenerate:
                b = max(a, min(len(spans) - 1,
                               next((i for i in range(len(spans) - 1, a, -1)
                                     if spans[i][0] <= sentence[-1].start + TOL), a)))
            else:
                b = best_note(spans, sentence[-1], lo=a + 1, hi=len(spans))
                if b is None:
                    b = a
        heads[a] = base
        if b > a:
            heads[b] = last_index
        interior = list(range(base + 1, last_index)) if b > a else []
        if interior:
            picked = monotone_dp(spans, chars, interior, list(range(a + 1, b)),
                                 absorb_cost=absorb_cost)
            heads.update({note: ci for ci, note in picked.items()})
            absorbed += len(interior) - len(picked)
        cursor = b + 1
    return build(spans, chars, heads, absorbed=absorbed, dropped=dropped, line_of=mapping)


# ------------------------------------------------------------------ 评分
def anchor_stats(chars, spans) -> dict:
    """纯结构统计：如果严格按「句首字挂它自己的音、句末字挂它自己的音」会怎样。

    - conflict：本句末字的天然音 >= 下一句首字的天然音——两句抢同一个音，
      「不相连」这条约束在这里**必须**牺牲一边的说法。
    - infeasible：句末字天然音找不到，或者落在句首字之前。
    - squeeze：本句 [首锚, 末锚] 之间的音数 < 中间字数（中间的字必然要被挤掉一些）。
    """
    groups = sentences(chars)
    anchors = []
    for number, sentence in enumerate(groups):
        base = sum(len(g) for g in groups[:number])
        a = best_note(spans, sentence[0], lo=0, hi=len(spans))
        b = best_note(spans, sentence[-1], lo=0, hi=len(spans)) if len(sentence) > 1 else a
        anchors.append((a, b, base, len(sentence)))
    conflict = infeasible = squeeze = 0
    for number, (a, b, base, size) in enumerate(anchors):
        if a is None or b is None or b < a:
            infeasible += 1
            continue
        if size > 2:
            middle_chars = size - 2
            middle_notes = max(0, b - a - 1)
            if middle_notes < middle_chars:
                squeeze += 1
        if number + 1 < len(anchors):
            nxt = anchors[number + 1][0]
            if nxt is not None and b >= nxt:
                conflict += 1
    return {"conflict": conflict, "infeasible": infeasible, "squeeze": squeeze}


HUMAN_SVP = (PROJECT / "samples" / "对齐"
             / "王朝1982,朱旭BooBoo - 我本将心向明月（人工）.svp")
HUMAN_PROJECT = "王朝1982_朱旭BooBoo_-_我本将心向明月-2"
SVP_QUARTER = 705600000


def load_human():
    """人工修过的 SVP -> [(start, end, lyric)]，秒。"""
    raw = HUMAN_SVP.read_bytes().rstrip(b"\x00")
    data = json.loads(raw.decode("utf-8"))
    notes = data["tracks"][0]["mainGroup"]["notes"]
    bpm = data["time"]["tempo"][0]["bpm"]
    rows = []
    for note in notes:
        start = note["onset"] / SVP_QUARTER * 60.0 / bpm
        end = (note["onset"] + note["duration"]) / SVP_QUARTER * 60.0 / bpm
        rows.append((start, end, note.get("lyrics") or ""))
    return rows


def human_agreement(fill: Fill, spans) -> dict:
    """把我们的音槽按时间对到人工版，看字对不对，以及是不是"错一格"。"""
    human = load_human()
    matched = []          # (our_note, human_index)
    used = set()
    j = 0
    for note, (start, _end) in enumerate(spans):
        best, best_gap = None, None
        for k in range(max(0, j - 1), min(len(human), j + 3)):
            if k in used:
                continue
            gap = abs(human[k][0] - start)
            if gap <= 0.08 and (best_gap is None or gap < best_gap):
                best, best_gap = k, gap
        if best is not None:
            matched.append((note, best))
            used.add(best)
            j = best + 1
    same = off_by_one = other = real_slots = 0
    for note, k in matched:
        ours = (fill.lyrics[note] or "").strip()
        theirs = human[k][2].strip()
        if theirs and theirs != "-":
            real_slots += 1
        if ours == theirs:
            same += 1
            continue
        neighbours = []
        for offset in (-1, 1):
            if 0 <= k + offset < len(human):
                neighbours.append(human[k + offset][2].strip())
        if ours and ours not in ("-", "+") and ours in neighbours:
            off_by_one += 1
        else:
            other += 1
    return {"slots": len(matched), "same": same, "off_by_one": off_by_one,
            "other": other, "real_slots": real_slots}


def score(fill: Fill, spans, chars):
    index_of = {id(char): index for index, char in enumerate(chars)}
    hit = miss_late = miss_early = 0
    deviation = []
    for note, index in fill.heads.items():
        char, (note_start, note_end) = chars[index], spans[note]
        if overlap(note_start, note_end, char.start, char.end) > 1e-9:
            hit += 1
        elif note_start > char.end:
            miss_late += 1
        else:
            miss_early += 1
        deviation.append(abs(note_start - char.start))
    deviation.sort()
    boundary_fail = 0
    head_of_char = {index: note for note, index in fill.heads.items()}
    for sentence in sentences(chars):
        for char in (sentence[0], sentence[-1]):
            note = head_of_char.get(index_of[id(char)])
            if note is None:
                boundary_fail += 1
                continue
            note_start, note_end = spans[note]
            if not (overlap(note_start, note_end, char.start, char.end) > 1e-9):
                boundary_fail += 1
    return {
        "hit": hit, "miss_late": miss_late, "miss_early": miss_early,
        "hidden": fill.absorbed + fill.dropped,
        "absorbed": fill.absorbed, "dropped": fill.dropped,
        "melisma": fill.melisma, "unmatched": fill.unmatched,
        "median": deviation[len(deviation) // 2] if deviation else 0.0,
        "p90": deviation[int(len(deviation) * 0.9)] if deviation else 0.0,
        "max": deviation[-1] if deviation else 0.0,
        "over_1s": sum(1 for d in deviation if d > 1.0),
        "boundary_fail": boundary_fail,
    }


def variant_head_only(spans, chars, *, absorb_cost=0.9, **kw):
    """只锚句首字：首字钉在它自己的音上，其余在"到本句末字时间为止"的音里跑 DP。"""
    groups = sentences(chars)
    line_of = {}
    for number, sentence in enumerate(groups):
        for char in sentence:
            line_of[id(char)] = number
    mapping = {index: line_of[id(chars[index])] for index in range(len(chars))}
    heads, absorbed, dropped = {}, 0, 0
    cursor, offset = 0, 0
    for sentence in groups:
        base = offset
        offset += len(sentence)
        a = best_note(spans, sentence[0], lo=cursor, hi=len(spans))
        if a is None:
            dropped += len(sentence)
            continue
        heads[a] = base
        hi = a + 1
        while hi < len(spans) and spans[hi][0] <= sentence[-1].end + TOL:
            hi += 1
        rest = list(range(base + 1, base + len(sentence)))
        if rest and hi > a + 1:
            picked = monotone_dp(spans, chars, rest, list(range(a + 1, hi)),
                                 absorb_cost=absorb_cost)
            heads.update({note: ci for ci, note in picked.items()})
            absorbed += len(rest) - len(picked)
        elif rest:
            absorbed += len(rest)
        used = [note for note, ci in heads.items() if base <= ci < base + len(sentence)]
        cursor = max(used) + 1 if used else a + 1
    return build(spans, chars, heads, absorbed=absorbed, dropped=dropped, line_of=mapping)


def variant_sentence_dp(spans, chars, *, absorb_cost=0.9, **kw):
    """按句切段：每句的音范围 = [上一句用掉的最后一个音之后, 本句末字时间之前]，句内跑单调 DP。

    这是"锚定"里真正有用的那一半：**用句的时间跨度当边界**，而不是钉死某个字。
    """
    groups = sentences(chars)
    line_of = {}
    for number, sentence in enumerate(groups):
        for char in sentence:
            line_of[id(char)] = number
    mapping = {index: line_of[id(chars[index])] for index in range(len(chars))}
    heads, absorbed, dropped = {}, 0, 0
    cursor, offset = 0, 0
    for sentence in groups:
        base = offset
        offset += len(sentence)
        hi = cursor
        while hi < len(spans) and spans[hi][0] <= sentence[-1].end + TOL:
            hi += 1
        if hi <= cursor:
            hi = min(len(spans), cursor + 1)
        if cursor >= len(spans):
            dropped += len(sentence)
            continue
        char_ids = list(range(base, base + len(sentence)))
        picked = monotone_dp(spans, chars, char_ids, list(range(cursor, hi)),
                             absorb_cost=absorb_cost)
        heads.update({note: ci for ci, note in picked.items()})
        absorbed += len(char_ids) - len(picked)
        # 下一句从本句真正用到的最后一个音之后开始（硬边界：句间不共用音）
        used = list(picked.values())
        cursor = (max(used) + 1) if used else cursor
    return build(spans, chars, heads, absorbed=absorbed, dropped=dropped, line_of=mapping)


def variant_anchored_strict(spans, chars, *, absorb_cost=0.9, **kw):
    """提案的**忠实**实现：先把所有句首锚点定下来，再让每句末锚点给下一句首锚让位。

    和 `variant_anchored` 的区别就是"谁让谁"：那里是末锚点先占、下一句被推后；
    这里是下一句首锚先行、末锚点被压回 `a_next - 1`——这才是"保证两句不相连"的做法。
    """
    groups = sentences(chars)
    line_of = {}
    for number, sentence in enumerate(groups):
        for char in sentence:
            line_of[id(char)] = number
    mapping = {index: line_of[id(chars[index])] for index in range(len(chars))}
    offsets, running = [], 0
    for sentence in groups:
        offsets.append(running)
        running += len(sentence)

    # 第一遍：句首锚点（单调递增）
    firsts: list[int | None] = []
    cursor = 0
    for sentence in groups:
        a = best_note(spans, sentence[0], lo=cursor, hi=len(spans))
        firsts.append(a)
        if a is not None:
            cursor = a + 1
    # 第二遍：句末锚点，必须严格小于下一句首锚
    heads, absorbed, dropped = {}, 0, 0
    cursor = 0
    for number, sentence in enumerate(groups):
        base = offsets[number]
        a = firsts[number]
        if a is None:
            dropped += len(sentence)
            continue
        if a < cursor:                      # 理论上不会（firsts 单调），防御
            a = cursor
        if a >= len(spans):                 # 前面的句子把音用光了
            dropped += len(sentence)
            continue
        limit = firsts[number + 1] if number + 1 < len(groups) else len(spans)
        if limit is None:
            limit = len(spans)
        heads[a] = base
        last_index = base + len(sentence) - 1
        b = a
        if last_index > base:
            natural = best_note(spans, sentence[-1], lo=a + 1, hi=len(spans))
            if natural is not None and natural < limit:
                b = natural
            elif limit - 1 > a:
                b = limit - 1
            if b > a:
                heads[b] = last_index
        interior = list(range(base + 1, last_index)) if b > a else []
        if interior:
            picked = monotone_dp(spans, chars, interior, list(range(a + 1, b)),
                                 absorb_cost=absorb_cost)
            heads.update({note: ci for ci, note in picked.items()})
            absorbed += len(interior) - len(picked)
        cursor = b + 1
    return build(spans, chars, heads, absorbed=absorbed, dropped=dropped, line_of=mapping)


def variant_sentence_dp_fill(spans, chars, *, absorb_cost=0.9, **kw):
    """`sdp` + **中间再调一次**：DP 之后还有字没挂上（多半是零长度时间戳），
    就按顺序把它们摊到本句还没用掉的音上——这正是"锚定之后调整中间"的一半价值。
    """
    groups = sentences(chars)
    line_of = {}
    for number, sentence in enumerate(groups):
        for char in sentence:
            line_of[id(char)] = number
    mapping = {index: line_of[id(chars[index])] for index in range(len(chars))}
    heads, absorbed, dropped = {}, 0, 0
    cursor, offset = 0, 0
    for sentence in groups:
        base = offset
        offset += len(sentence)
        hi = cursor
        while hi < len(spans) and spans[hi][0] <= sentence[-1].end + TOL:
            hi += 1
        if hi <= cursor:
            hi = min(len(spans), cursor + 1)
        if cursor >= len(spans):
            dropped += len(sentence)
            continue
        char_ids = list(range(base, base + len(sentence)))
        picked = monotone_dp(spans, chars, char_ids, list(range(cursor, hi)),
                             absorb_cost=absorb_cost)
        # 补：没挂上的字按顺序摊到本句剩余的空音上（单调、不抢已用的音）
        used = set(picked.values())
        lower = cursor - 1
        for position, char_id in enumerate(char_ids):
            if char_id in picked:
                lower = picked[char_id]
                continue
            upper = hi
            for later in char_ids[position + 1:]:
                if later in picked:
                    upper = picked[later]
                    break
            chosen = next((n for n in range(lower + 1, upper)
                           if n not in used and n < len(spans)), None)
            if chosen is None:
                absorbed += 1
                continue
            picked[char_id] = chosen
            used.add(chosen)
            lower = chosen
        heads.update({note: ci for ci, note in picked.items()})
        used_notes = list(picked.values())
        cursor = (max(used_notes) + 1) if used_notes else cursor
    return build(spans, chars, heads, absorbed=absorbed, dropped=dropped, line_of=mapping)


VARIANTS = {
    "greedy": variant_greedy,
    "clip": variant_sentence_clip,
    "anchor": variant_anchored,
    "astrict": variant_anchored_strict,
    "head": variant_head_only,
    "sdp": variant_sentence_dp,
    "sdpfill": variant_sentence_dp_fill,
    "dp": variant_global_dp,
}


def greedy_vs_sdp(chars, spans) -> dict:
    """两个补充数：贪心把末字挂出本句的句子数；sdp 让出的字在贪心那边挂得对不对。"""
    groups = sentences(chars)
    greedy = variant_greedy(spans, chars)
    sdp = variant_sentence_dp(spans, chars)
    head_of_char = {ci: n for n, ci in greedy.heads.items()}
    sdp_chars = set(sdp.heads.values())
    spill = 0
    offset = 0
    for sentence in groups:
        base = offset
        offset += len(sentence)
        a = best_note(spans, sentence[0], lo=0, hi=len(spans))
        b = best_note(spans, sentence[-1], lo=0, hi=len(spans))
        placed = [head_of_char[base + k] for k in range(len(sentence))
                  if (base + k) in head_of_char]
        if a is None or b is None or not placed:
            continue
        if max(placed) > b:
            spill += 1
    given_up = greedy_late = greedy_early = greedy_right = 0
    recovered = 0
    for index, char in enumerate(chars):
        note = head_of_char.get(index)
        if note is None:
            if index in sdp_chars:
                recovered += 1
            continue
        if index in sdp_chars:
            continue
        given_up += 1
        start = spans[note][0]
        if start > char.end:
            greedy_late += 1
        elif start < char.start:
            greedy_early += 1
        else:
            greedy_right += 1
    return {"sentences": len(groups), "spill": spill, "given_up": given_up,
            "greedy_late": greedy_late, "greedy_early": greedy_early,
            "greedy_right": greedy_right, "recovered": recovered}


def check_greedy(rows) -> list[str]:
    """复刻版必须和线上的 `assign_lyrics` 逐音一致，否则这组对照不作数。"""
    problems = []
    for row in rows:
        case = load_case(PROJECT / "output" / row["name"])
        official = assign_lyrics(case["spans"], case["chars"])
        copied = variant_greedy(case["spans"], case["chars"])
        if official.lyrics != copied.lyrics:
            diff = sum(1 for a, b in zip(official.lyrics, copied.lyrics) if a != b)
            problems.append(f"{row['name']}: {diff} 个音不一致")
        if (len(official.absorbed_chars) != copied.absorbed
                or official.dropped != copied.dropped):
            problems.append(f"{row['name']}: absorbed/dropped 计数不一致")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="填词：锚定 / 分句 / 全局 DP 对照（离线，不跑模型）")
    parser.add_argument("--cost", type=float, default=0.9, help="弃字的代价（秒当量）")
    parser.add_argument("--variants", nargs="*", default=list(VARIANTS))
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--json", default=None)
    parser.add_argument("--no-report", action="store_true",
                        help="不写 reports/锚定对齐对照.md")
    args = parser.parse_args(argv)

    root = PROJECT / "output"
    rows = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or (args.only and
                                      not any(k in directory.name for k in args.only)):
            continue
        case = load_case(directory)
        if not case:
            continue
        chars, spans = case["chars"], case["spans"]
        groups = sentences(chars)
        row = {"name": case["name"], "notes": len(spans), "chars": len(chars),
               "sentences": len(groups),
               "zero": sum(1 for c in chars if c.degenerate),
               "zero_last": sum(1 for g in groups if g[-1].degenerate),
               "zero_first": sum(1 for g in groups if g[0].degenerate),
               "short": sum(1 for g in groups if len(g) <= 2),
               "results": {}}
        for name in args.variants:
            fill = VARIANTS[name](spans, chars, absorb_cost=args.cost)
            row["results"][name] = score(fill, spans, chars)
        row.update(anchor_stats(chars, spans))
        row["spill"] = greedy_vs_sdp(chars, spans)
        rows.append(row)

    out: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        out.append(line)

    total = sum(r["chars"] for r in rows)
    problems = check_greedy(rows) if "greedy" in args.variants else []
    say("自检（复刻版 vs 线上 assign_lyrics）："
        + ("通过" if not problems else "；".join(problems)))
    say(f"字 {total} / 工程 {len(rows)} / absorb_cost {args.cost}")
    names = args.variants
    header = f"{'工程':24s} {'字':>4s} {'句':>3s} {'末零':>4s} " + " ".join(
        f"{n:>12s}" for n in names)
    say(header + "   hit%/miss/hidden")
    for row in rows:
        cells = []
        for name in names:
            res = row["results"][name]
            cells.append(f"{res['hit'] / max(1, row['chars']):5.1%}/"
                         f"{res['miss_late'] + res['miss_early']:3d}/{res['hidden']:3d}")
        say(f"{row['name'][:22]:24s} {row['chars']:4d} {row['sentences']:3d} "
            f"{row['zero_last']:4d} " + " ".join(f"{c:>12s}" for c in cells))
    say()
    sentences_total = sum(r["sentences"] for r in rows)
    say(f"句 {sentences_total}：锚点冲突 {sum(r['conflict'] for r in rows)}  "
        f"锚点不可行 {sum(r['infeasible'] for r in rows)}  "
        f"中段音不够 {sum(r['squeeze'] for r in rows)}")
    say()
    for name in names:
        hit = sum(r["results"][name]["hit"] for r in rows)
        late = sum(r["results"][name]["miss_late"] for r in rows)
        early = sum(r["results"][name]["miss_early"] for r in rows)
        hidden = sum(r["results"][name]["hidden"] for r in rows)
        mel = sum(r["results"][name]["melisma"] for r in rows)
        over = sum(r["results"][name]["over_1s"] for r in rows)
        bf = sum(r["results"][name]["boundary_fail"] for r in rows)
        bound = sum(2 * r["sentences"] for r in rows)
        say(f"{name:7s} hit {hit:5d} ({hit / total:5.1%})  late {late:4d}  "
            f"early {early:4d}  hidden {hidden:5d} ({hidden / total:5.1%})  "
            f"|Δ|>1s {over:4d}  转音 {mel:4d}  句首末挂歪 {bf:4d}/{bound}")

    # 对人工版（唯一有 ground truth 的一首）
    if HUMAN_SVP.is_file() and any(r["name"] == HUMAN_PROJECT for r in rows):
        case = load_case(PROJECT / "output" / HUMAN_PROJECT)
        say()
        say(f"对人工版（{HUMAN_PROJECT}）：")
        for name in names:
            fill = VARIANTS[name](case["spans"], case["chars"], absorb_cost=args.cost)
            stats = human_agreement(fill, case["spans"])
            say(f"  {name:7s} 槽 {stats['slots']:3d}  一致 {stats['same']:3d} "
                f"({stats['same'] / max(1, stats['slots']):5.1%})  "
                f"错一格 {stats['off_by_one']:3d}  其它 {stats['other']:3d}")

    if "greedy" in names and "sdp" in names:
        spill = sum(r["spill"]["spill"] for r in rows)
        given = sum(r["spill"]["given_up"] for r in rows)
        late = sum(r["spill"]["greedy_late"] for r in rows)
        early = sum(r["spill"]["greedy_early"] for r in rows)
        right = sum(r["spill"]["greedy_right"] for r in rows)
        recovered = sum(r["spill"]["recovered"] for r in rows)
        say()
        say(f"贪心有字被挂到「末字天然锚」之外的句子：{spill}/{sentences_total}")
        say(f"sdp 没挂上、贪心挂上了的字 {given}：贪心 late {late} / early {early} / 其实对 {right}")
        say(f"sdp 挂上、贪心没挂上的字：{recovered}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    if not args.no_report:
        report = PROJECT / "reports" / "锚定对齐对照.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            "# 填词：锚定 / 分句 / 全局 DP 对照\n\n"
            "由 `scripts/anchor_align_probe.py` 机械生成。拿 `output/<工程>/lyrics.json` 里\n"
            "已经识别好的逐字时间戳，对同一条人声轨跑八种挂字策略（greedy / clip /\n"
            "anchor / astrict / head / sdp / sdpfill / dp，定义见脚本 docstring）。\n\n"
            "```\n" + "\n".join(out) + "\n```\n\n"
            "口径：\n"
            "- **hit**：挂上了，而且那个音的时间区间和这个字的时间窗真的重叠。\n"
            "- **miss**：挂上了，但那个音在字唱完之后才开始（late）/ 字开始前就唱完了（early）。\n"
            "- **hidden**：没挂上（absorbed + dropped）。现有实现的 hidden 之所以低，是因为\n"
            "  fallback 会把字丢给「离得最近、还没主」的音，哪怕它在几秒之后。\n"
            "- **错一格**：和人工版对比，我们挂在某个音上的字，正好是人工版相邻音上的字——\n"
            "  这是贪心一路推过去的典型签名。\n\n"
            "分析与结论见 `docs/对齐-锚定方案评估.md`。\n",
            encoding="utf-8")
        print(f"\n写入 {report.relative_to(PROJECT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
