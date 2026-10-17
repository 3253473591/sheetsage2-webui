"""旋律的**八度一致性**校正：重复段不许差一个八度。

## 为什么需要它

模型的音高是"**音级对、八度不可靠**"（证据见 `docs/WebUI问题清单.md` §1）：
`melody_full.lab` 的 pitch mod 12 与实测 F0 吻合，错的是整数个八度。
`pitch.py` 现在的对策是**整体固定 −12**，它能治"全曲统一偏高一个八度"，
但治不了"**某一段**比别人低一个八度"——那是**局部**的八度错误，
整体平移动不了它。

`samples/对齐/` 里那对 SVP（机扒 vs 人工修过）就是活证据：

| 同一段副歌出现的位置 | 机扒 | 人工 |
|---|---|---|
| 第 72~104 拍（section 0） | 66 | 66 |
| 第 168~201 拍（section 1） | **54** | **66** |
| 第 248~281 拍（section 2） | 66 | 66 |

381 个音里有 **76 个（20%）** 差整整 12 个半音，全部落在这两段里；
人工版把 section 1 的副歌和 section 0 的尾句**各抬一个八度**，
抬完之后它们与同一段旋律在别处的出现**逐音相同**（54/54 命中）。

## 靠什么判

**重复段必须同高。** 同一段旋律在歌里出现多次时，机扒给它们的音高只允许差 0，
不允许差一个八度。这是唯一**不需要外部真值**、又能自己自洽验证的不变量，
而且它是"宁可不改"的：没有重复段证据的音符一个都不动。

## 算法

1. **切乐句**：同一轨内，静音间隙 ≥ `REST_GAP` 秒就断开（默认 0.6s）。
2. **找重复**：两两乐句做**音级**（pitch mod 12）序列匹配
   （`difflib.SequenceMatcher`，容忍插入/删除），匹配块要够长，
   并且块内的**音程走向**也要一致，防止"音级碰巧一样"的假重复。
3. **切段**：一个匹配块里如果只有一部分差整八度（副歌头是对的、尾句低一个八度），
   就按"差值是常数"再切成几段，每段单独算一处重复；相邻且同差值的段会并回去。
4. **建约束**：每处重复把两段配对起来，约束 `p_i + 12k_i = p_j + 12k_j`
   （同一处重复里差值是常数）；差值不是整八度的、或者超过 ±2 个八度的，一律不认。
5. **解方程**：并查集把互相咬合的段连成一个**分量**，
   分量内部各段的相对偏移 `base` 由约束唯一确定；
   再由**所有重复段的端点**把音符切成最小段，保证重叠的匹配不会互相排斥。
6. **挑整体平移 K**（每个分量一个整数）：让**改动的音符数最少**——
   也就是"让证据最多的那一拨保持不动"。
   **两个候选一样多（平票）就不动手**：分不清哪一次才是错的，不猜。
7. **应用** `p_n += 12·k_n`，并记下"大跳次数"的变化供复核。

## 不做什么

- 不动时间（tick / 秒 / 时值 / 力度），只动 pitch；
- 不猜"音级也不对"的地方——只认整八度的差异；
- 不猜平票的情形；
- 不依赖音频（不需要干声），只用模型自己的音符；
- 没有重复段证据 → 零改动，行为与今天完全一样。
"""

from __future__ import annotations

import bisect
import difflib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence

#: 一个八度。
OCTAVE = 12

#: 乐句切分：同一轨内两个音之间静音 ≥ 这个秒数就断开。
REST_GAP = 0.6
#: 少于这么多音的乐句不参与重复判断。
MIN_PHRASE_NOTES = 6
#: 一次重复匹配至少要覆盖这么多音。
MIN_BLOCK = 8
#: 匹配块里"音程走向一致"的最低比例。
MIN_CONTOUR_RATE = 0.8
#: 音程比较容差（半音）：量化后的重复段可能差 1 个半音。
CONTOUR_TOL = 1
#: 最多允许差几个八度（超过就不认，避免离谱的匹配）。
MAX_OCTAVES = 2
#: 相邻两个音差到这个数就算"大跳"，只用来报告（不做判定）。
BIG_LEAP = 11
#: 按起始时间查表时的容差（秒）。MIDI 的时间是从 tick 换算的，和 lab 有 ~1e-4 的差。
TIME_TOLERANCE = 5e-3


@dataclass(frozen=True)
class Note:
    """一个旋律音（只关心校正用得上的字段）。"""

    start: float
    end: float
    pitch: int
    track: int = 0


@dataclass
class OctaveStats:
    """一次八度校正的汇总（进 `project.json` / 报告）。"""

    enabled: bool = True
    tracks: int = 0
    phrases: int = 0
    links: int = 0          # 匹配上的重复块对数
    groups: int = 0         # 有重复段证据、且真的连成一片的分量数
    moved_notes: int = 0    # 被平移的音符数
    moved_octaves: int = 0  # 平移的八度总数（±1 累加）
    big_leaps: tuple[int, int] = (0, 0)   # (改之前, 改之后) 的大跳次数
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "tracks": self.tracks,
            "phrases": self.phrases,
            "links": self.links,
            "groups": self.groups,
            "moved_notes": self.moved_notes,
            "moved_octaves": self.moved_octaves,
            "big_leaps_before": self.big_leaps[0],
            "big_leaps_after": self.big_leaps[1],
            "warnings": list(self.warnings),
        }

    @property
    def changed(self) -> bool:
        return self.moved_notes > 0


# --------------------------------------------------------------------- 并查集
class _Offsets:
    """带偏移的并查集：维护 `base[x] - base[parent[x]]`。

    `union(a, b, delta)` 表示 `base[a] - base[b] = delta`。矛盾就不并。
    """

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n
        self.offset = [0] * n          # base[x] - base[parent]

    def find(self, x: int) -> tuple[int, int]:
        """返回 `(根, base[x] - base[根])`，顺手做路径压缩。"""
        root = x
        acc = 0
        while self.parent[root] != root:
            acc += self.offset[root]
            root = self.parent[root]
        # 第二趟：压缩
        cur = x
        cur_acc = 0
        while self.parent[cur] != cur:
            nxt = self.parent[cur]
            step = self.offset[cur]
            self.parent[cur] = root
            self.offset[cur] = acc - cur_acc
            cur_acc += step
            cur = nxt
        return root, acc

    def union(self, a: int, b: int, delta: int) -> bool:
        """断言 `base[a] - base[b] = delta`；与已有信息矛盾时返回 False。"""
        ra, oa = self.find(a)
        rb, ob = self.find(b)
        if ra == rb:
            return (oa - ob) == delta
        # base[a] = base[ra] + oa；base[b] = base[rb] + ob
        # 要 base[a] - base[b] = delta
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
            self.offset[ra] = delta - oa + ob
        else:
            self.parent[rb] = ra
            self.offset[rb] = oa - ob - delta
            if self.rank[ra] == self.rank[rb]:
                self.rank[ra] += 1
        return True


# --------------------------------------------------------------------- 乐句
def split_phrases(notes: Sequence[Note], rest_gap: float = REST_GAP
                  ) -> list[list[int]]:
    """把一条轨上的音符按下标切成乐句，返回每句的音符下标。"""
    phrases: list[list[int]] = []
    cur: list[int] = []
    for i, note in enumerate(notes):
        if cur and note.start - notes[cur[-1]].end >= rest_gap:
            phrases.append(cur)
            cur = []
        cur.append(i)
    if cur:
        phrases.append(cur)
    return phrases


def _contour(pitches: Sequence[int]) -> list[int]:
    return [pitches[i + 1] - pitches[i] for i in range(len(pitches) - 1)]


def _contour_agrees(a: Sequence[int], b: Sequence[int]) -> float:
    """两段同长音程序列的一致比例（容差 `CONTOUR_TOL`）。"""
    if not a or len(a) != len(b):
        return 0.0
    hit = sum(1 for x, y in zip(a, b) if abs(x - y) <= CONTOUR_TOL)
    return hit / len(a)


@dataclass(frozen=True)
class Repeat:
    """一段被认出来的重复：`a[i]` 与 `b[i]` 是同一句旋律，前后只差整八度。

    存的是**全局音符下标**的元组（长度相等），不是区间——因为
    `melody_full.lab` 里两条轨的音是交错的，全局下标不连续，
    用 `起点+长度` 表示区间会算错。
    """

    a: tuple[int, ...]
    b: tuple[int, ...]
    octaves: int          # 对应音符相差几个八度（正的 = a 比 b 高）

    @property
    def size(self) -> int:
        return len(self.a)


def find_repeats(pitches: Sequence[int], phrases: Sequence[Sequence[int]], *,
                 min_block: int = MIN_BLOCK,
                 min_contour: float = MIN_CONTOUR_RATE) -> list[Repeat]:
    """在乐句两两之间找"只差整八度"的重复段。

    `phrases` 里装的是**全局音符下标**，返回的 `Repeat` 里也是全局下标。
    """
    out: list[Repeat] = []
    for i, pa in enumerate(phrases):
        if len(pa) < min_block:
            continue
        for pb in phrases[i + 1:]:
            if len(pb) < min_block:
                continue
            out.extend(_match(pitches, pa, pb, min_block=min_block,
                              min_contour=min_contour))
    return out


def _match(pitches: Sequence[int], pa: Sequence[int], pb: Sequence[int], *,
           min_block: int, min_contour: float) -> list[Repeat]:
    """一个乐句对内部的匹配。音级序列 + 音程走向双重把关。

    `SequenceMatcher` 的块是按**音级**对齐的，块里可能只有一部分差整八度
    （副歌头是对的、尾句低了一个八度），所以还要按"差值恒定"再切；
    切完之后**把相邻且同差值的段并回去**——不然一个 13 音的重复会被
    块边界切成 8+5，两段都够不上 `MIN_BLOCK`，白白丢掉。
    """
    toks_a = [pitches[i] % OCTAVE for i in pa]
    toks_b = [pitches[i] % OCTAVE for i in pb]
    matcher = difflib.SequenceMatcher(a=toks_a, b=toks_b, autojunk=False)

    # (a 起, a 止, b 起, b 止, 差值)——位置是**乐句内**的位置（半开区间）
    raw: list[tuple[int, int, int, int, int]] = []
    for block in matcher.get_matching_blocks():
        if block.size < 2:
            continue
        span_a = list(range(block.a, block.a + block.size))
        span_b = list(range(block.b, block.b + block.size))
        # 音程走向也要一致，挡掉"音级碰巧一样"的假重复
        if _contour_agrees(_contour([pitches[pa[i]] for i in span_a]),
                           _contour([pitches[pb[j]] for j in span_b])) < min_contour:
            continue
        deltas = [pitches[pa[i]] - pitches[pb[j]] for i, j in zip(span_a, span_b)]
        for start, end, delta in _constant_runs(deltas):
            raw.append((block.a + start, block.a + end,
                        block.b + start, block.b + end, delta))

    raw.sort()
    merged: list[tuple[int, int, int, int, int]] = []
    for a0, a1, b0, b1, delta in raw:
        last = merged[-1] if merged else None
        if (last is not None and last[4] == delta
                and last[1] == a0 and last[3] == b0):
            merged[-1] = (last[0], a1, last[2], b1, delta)
        else:
            merged.append((a0, a1, b0, b1, delta))

    found: list[Repeat] = []
    for a0, a1, b0, b1, delta in merged:
        size = a1 - a0
        if size < min_block:
            continue
        octaves = int(round(delta / OCTAVE))
        if octaves == 0 or abs(octaves) > MAX_OCTAVES:
            continue
        if abs(delta - octaves * OCTAVE) > 1:
            continue
        found.append(Repeat(a=tuple(pa[i] for i in range(a0, a1)),
                            b=tuple(pb[j] for j in range(b0, b1)),
                            octaves=octaves))
    return found


def _constant_runs(values: Sequence[int]) -> list[tuple[int, int, int]]:
    """把一串值切成"每段内部相等"的最大段，返回 `[(start, end, value)]`。"""
    runs: list[tuple[int, int, int]] = []
    i = 0
    while i < len(values):
        j = i
        # 差值可能是中间抖 1 个半音的（量化误差），允许 ±1 跟着走
        while j + 1 < len(values) and abs(values[j + 1] - values[i]) <= 1:
            j += 1
        runs.append((i, j + 1, values[i]))
        i = j + 1
    return runs


def _median(values: Sequence[int]) -> float:
    xs = sorted(values)
    n = len(xs)
    if n == 0:
        return 0.0
    mid = n // 2
    return float(xs[mid]) if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


# --------------------------------------------------------------------- 计划
@dataclass
class OctavePlan:
    """校正计划：把每个源音符的平移按 `(轨道, 起始秒)` 记下来。

    之所以不按"时间段"记，是因为下游产物（lab / MIDI / playback）的时间
    精度不一样（MIDI 是从 tick 换回来的，和 lab 差 ~1e-4 秒），
    按**源音符的起始时间最近邻**查表最稳。
    """

    shifts: dict[int, tuple[list[float], list[int]]] = field(default_factory=dict)
    stats: OctaveStats = field(default_factory=OctaveStats)

    def shift_for(self, seconds: float, track: int | None = None) -> int:
        """这个时刻的音要平移几个半音；查不到就是 0。"""
        order = [track] if track is not None else [0, 1]
        best: tuple[float, int] | None = None
        for tid in order:
            entry = self.shifts.get(tid)
            if not entry:
                continue
            starts, values = entry
            i = bisect.bisect_left(starts, seconds)
            for j in (i - 1, i):
                if 0 <= j < len(starts):
                    gap = abs(starts[j] - seconds)
                    if gap <= TIME_TOLERANCE and (best is None or gap < best[0]):
                        best = (gap, values[j])
        return best[1] if best else 0

    @property
    def empty(self) -> bool:
        return not any(values for _, values in self.shifts.values())


@dataclass
class Structure:
    """一次分析的中间结果：段、约束分量、相邻音对。测试与复核脚本会直接看它。"""

    tracks: int = 0
    phrases: int = 0
    links: int = 0
    segments: list[list[int]] = field(default_factory=list)
    seg_of: dict[int, int] = field(default_factory=dict)
    #: 每个分量 = `[(段号, base, 该段几个音)]`，只有 ≥2 段的（有对照的）才留
    groups: list[list[tuple[int, int, int]]] = field(default_factory=list)
    repeats: list[Repeat] = field(default_factory=list)

    @classmethod
    def build(cls, notes: Sequence[Note], *, rest_gap: float = REST_GAP) -> "Structure":
        out = cls()
        if not notes:
            return out
        by_track: dict[int, list[int]] = defaultdict(list)
        for i, note in enumerate(notes):
            by_track[note.track].append(i)
        pitches = [n.pitch for n in notes]

        # --- 切段：由**所有**重复段的端点共同切出来的最小片段。这样重叠的匹配
        # （同一段旋律被不止一对重复引用）不会互相排斥，而是把同一批音连到一起。
        for _track, indices in sorted(by_track.items()):
            out.tracks += 1
            local = [notes[i] for i in indices]
            # 乐句里换成**全局下标**，后面的约束就不用再折算
            phrases = [[indices[pos] for pos in phrase]
                       for phrase in split_phrases(local, rest_gap)]
            out.phrases += len(phrases)
            found = find_repeats(pitches, phrases)
            out.repeats.extend(found)
            position = {g: n for n, g in enumerate(indices)}
            cuts = {0, len(indices)}
            for repeat in found:
                for seq in (repeat.a, repeat.b):
                    cuts.add(position[seq[0]])
                    cuts.add(position[seq[-1]] + 1)
            ordered = sorted(cuts)
            for lo, hi in zip(ordered, ordered[1:]):
                if hi <= lo:
                    continue
                members = indices[lo:hi]
                out.seg_of.update({g: len(out.segments) for g in members})
                out.segments.append(list(members))
        out.links = len(out.repeats)
        if not out.segments:
            return out

        # --- 解约束
        world = _Offsets(len(out.segments))
        for repeat in out.repeats:
            deltas = [pitches[x] - pitches[y] for x, y in zip(repeat.a, repeat.b)]
            delta = int(round(-_median(deltas) / OCTAVE))
            for x, y in zip(repeat.a, repeat.b):
                world.union(out.seg_of[x], out.seg_of[y], delta)

        members: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
        for n, group in enumerate(out.segments):
            root, base = world.find(n)
            members[root].append((n, base, len(group)))
        # 有对照（≥2 段）的分量才动；按证据多少从大到小，确定性好
        out.groups = [sorted(group, key=lambda item: (-item[2], item[0]))
                      for group in members.values() if len(group) >= 2]
        out.groups.sort(key=lambda group: (-sum(w for _, _, w in group), group[0][0]))
        return out


def plan(notes: Sequence[Note], *, rest_gap: float = REST_GAP,
         max_octaves: int = MAX_OCTAVES) -> tuple[list[int], OctavePlan]:
    """算出每个音符该平移几个半音，返回 `(每个音的半音数, 计划)`。

    `notes` 必须按 `start` 升序（同一个轨内自然也是升序），
    返回的 `shifts` 与 `notes` 逐位对应。

    **决策单位是"重复段"，不是单个音**：一次匹配命中的一整段旋律共用同一个
    八度平移——否则每个音各动各的，会把旋律拆碎。段与段之间用约束
    `k_段A − k_段B = Δ八度` 连起来，同一连通分量里再挑一个整体平移 K，
    让**被改动的音符数最少**（等价于"让证据最多的那一拨保持不动"）。
    """
    stats = OctaveStats()
    shifts = [0] * len(notes)
    if not notes:
        return shifts, OctavePlan(stats=stats)

    structure = Structure.build(notes, rest_gap=rest_gap)
    stats.tracks = structure.tracks
    stats.phrases = structure.phrases
    stats.links = structure.links
    if not structure.groups:
        return shifts, OctavePlan(stats=stats)

    pitches = [n.pitch for n in notes]
    moved_notes = 0
    moved_octaves = 0
    quiet = 0
    for group in structure.groups:
        best_k = _best_offset(group)
        if best_k is None:              # 平票：不动手
            quiet += 1
            continue
        for n, base, weight in group:
            value = base + best_k
            if not value:
                continue
            for i in structure.segments[n]:
                shifts[i] = value * OCTAVE
            moved_notes += weight
            moved_octaves += abs(value) * weight

    stats.groups = len(structure.groups) - quiet
    stats.moved_notes = moved_notes
    stats.moved_octaves = moved_octaves
    stats.big_leaps = (_count_leaps(notes, [0] * len(notes)),
                       _count_leaps(notes, shifts))
    if not moved_notes:
        if quiet:
            stats.warnings.append(
                f"{quiet} 处重复段两边一样长（平票），分不清哪一次是错的，没有动手")
        return [0] * len(notes), OctavePlan(stats=stats)

    stats.warnings.append(
        f"八度一致性校正：{moved_notes} 个音被平移（共 {moved_octaves} 个八度），"
        "依据是同一段旋律在歌里重复出现时音高不一致")
    if stats.big_leaps[1] > stats.big_leaps[0]:
        stats.warnings.append(
            f"校正后大跳从 {stats.big_leaps[0]} 处变成 {stats.big_leaps[1]} 处——"
            "主歌/副歌本来就有的音区对比会被还原成这样，复核时对着原音频听一遍")

    shifts_by_track: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for i, note in enumerate(notes):
        if shifts[i]:
            shifts_by_track[note.track].append((note.start, shifts[i]))
    packed: dict[int, tuple[list[float], list[int]]] = {}
    for track, items in shifts_by_track.items():
        items.sort()
        packed[track] = ([s for s, _ in items], [v for _, v in items])
    return shifts, OctavePlan(shifts=packed, stats=stats)


def _best_offset(group: Sequence[tuple[int, int, int]]) -> int | None:
    """挑这个分量的整体平移 K；**平票就返回 `None`（不动手）**。

    只有一个判据：**改动的音符数最少**（等价于"让证据最多的那一拨保持不动"）。
    `group` 是 `[(段号, base, 该段几个音)]`。

    如果两个候选 K 改动的音一样多，就说明"抬前一次"和"压后一次"代价相同——
    这时分不清哪一次才是错的，所以不猜。`光芒` 的器乐就是这种情况（两次各 48 个音），
    不猜比猜错好。
    """
    candidates = {-base for _, base, _ in group} | {0}
    ranked = sorted((sum(weight for _, base, weight in group if base + k), abs(k), k)
                    for k in candidates)
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][2]


def _count_leaps(notes: Sequence[Note], shifts: Sequence[int]) -> int:
    """**同一轨内**相邻音之间大跳的次数（幅度够就算，上下都算）。

    必须按轨分开数：`melody_full.lab` 里人声与器乐是交错的，
    按全局下标数会把两条轨的音当成前后相邻，数出一堆假大跳。
    """
    by_track: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for note, delta in zip(notes, shifts):
        by_track[note.track].append((note.start, note.pitch + delta))
    total = 0
    for items in by_track.values():
        items.sort()
        for (_, a), (_, b) in zip(items, items[1:]):
            if abs(a - b) >= BIG_LEAP:
                total += 1
    return total


# --------------------------------------------------------------------- 文本
def parse_lab_rows(text: str) -> list[Note]:
    """读 `start end pitch [track]`；解析不出或列数不对的行跳过。"""
    notes: list[Note] = []
    for line in text.splitlines():
        fields = line.replace("\t", " ").split()
        if len(fields) < 3:
            continue
        try:
            start, end, pitch = float(fields[0]), float(fields[1]), int(round(float(fields[2])))
        except ValueError:
            continue
        track = 0
        if len(fields) >= 4:
            try:
                track = int(float(fields[3]))
            except ValueError:
                track = 0
        notes.append(Note(start=start, end=end, pitch=pitch, track=track))
    return notes


def normalize_lab_text(text: str, plan_: OctavePlan, *,
                       track: int | None = None) -> tuple[str, int]:
    """按计划把 lab 文本第 3 列（pitch）平移。

    `track=None` 时用行里第 4 列的轨道号；给了 `track` 就强制用它。
    行列数 / 分隔符 / 其它列都原样保留。只改 pitch。
    """
    out: list[str] = []
    moved = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        use_tab = "\t" in line
        fields = stripped.split("\t") if use_tab else stripped.split()
        if len(fields) < 3:
            out.append(line)
            continue
        try:
            start = float(fields[0])
            pitch = int(round(float(fields[2])))
        except ValueError:
            out.append(line)
            continue
        row_track = track
        if row_track is None and len(fields) >= 4:
            try:
                row_track = int(float(fields[3]))
            except ValueError:
                row_track = 0
        delta = plan_.shift_for(start, row_track)
        if delta:
            fields[2] = str(pitch + delta)
            moved += 1
        out.append("\t".join(fields) if use_tab else " ".join(fields))
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), moved


def plan_from_labs(labs: dict[str, str]) -> OctavePlan:
    """从 melody 的 lab 文本集算出计划。

    `melody_full.lab`（带轨道列，人声 + 器乐都在里面）是首选；
    它缺了就退回 `melody_vocal.lab` + `melody_instrumental.lab` 两份。

    **返回的计划按时间查表**（`OctavePlan.shift_for`），所以对同一句话生成的
    其它产物（MIDI / playback）也能用同一份计划，不用逐音对齐。
    """
    rows = parse_lab_rows(labs.get("melody_full") or "")
    if rows:
        return plan(rows)[1]
    return _merge_plans(plan(parse_lab_rows(labs.get("melody_vocal") or ""))[1],
                        plan(parse_lab_rows(labs.get("melody_instrumental") or ""))[1])


def _merge_plans(a: OctavePlan, b: OctavePlan) -> OctavePlan:
    shifts = {track: (list(s), list(v)) for track, (s, v) in a.shifts.items()}
    for track, (starts, values) in b.shifts.items():
        if track in shifts:
            continue
        shifts[track] = (list(starts), list(values))
    return OctavePlan(shifts=shifts, stats=a.stats)


def track_of_name(name: str) -> int:
    """MIDI / playback 的轨名 → 轨道号（和弦轨由调用方先筛掉）。"""
    return 1 if "ins" in str(name or "").lower() else 0
