"""把逐字时间戳填到音符上。

输入是两样东西：

- **音符**：来自 `project.json` 的 tick 域音符（只用到 `tick` / `dur`）
- **逐字时间戳**：来自 `asr.AsrEngine` 的 `AlignedChar`（秒，整首曲时间轴）

映射规则：

1. 一个字的范围是 `[s, e]`（零长度的退化字当成"发生在一瞬间"，给对称窗口）。它挂到一个
   **起点落在窗口里**的音；窗口里有多个音时，空的优先，再比"字是不是真的唱在它上面"。
2. 一个字唱了几个音：第一个音挂字，后面的音写转音标记（日文/英文 `+`——多音节词
   进下一拍/下一音节；中粤韩 `-`——一字一音的延音）。英文按 TIFA 的音节起点区分
   「延着当前音节」和「进下一个音节」；日文按拍唱，每个音都是下一拍。
3. 有多出来的字没有音位可放时，**记进 `absorbed_chars`**，不丢也不悄悄覆盖——
   根因是那段旋律的音比字少（模型没扒出那么细），需要人工核对。
4. 没分到字的音保留空串并在报告里标出来。
5. **全英文的歌不走逐字贪心，走词-音整体 DP 对齐**（`_align_latin_words`）：词头 =
   词开始后第一个音、延音不越过下一个词的开始、ASR 重复词自动跳过、TIFA 时间漂移
   的句子按序列结构拉回。规律全部对照 `samples/对齐/英文/` 的人工谱归纳——
   人工/自动对比脚本在 `tmp/score_fill.py`（词头命中 70% → 95%，转音标记 44% → 80%）。

时间轴统一用**秒**做匹配（容差是秒，更直观），再用 `timeline` 把结果写回音符。
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .timeline import Timeline

#: 默认容差（秒）。字的时间戳和音符起点常有几十毫秒的错位。
DEFAULT_TOLERANCE = 0.20

MELISMA = "-"
CONTINUATION = "+"

# --------------------------------------------------------------------- 英文词-音对齐
# 英文的词头/转音不是逐字贪心能做对的（对照 samples/对齐/英文/ 的人工谱归纳）：
#
# 1. **词头 = 词开始之后第一个空音**，不是"重叠最多的音"。TIFA 的词起点略早于音符起点
#    （实测 112/228 落在同一时刻、83 个晚 0.1s——音符在网格上，词时间在网格前一点），
#    按"重叠最多"挑会把词推到下一个音上，前面那个音变成占位音节。
# 2. **TIFA 的词时间会局部漂移**：一个词被 Viterbi 拉长到盖住后面几个词的音符
#    （实测 'you' 1.4s、'choice' 1.55s），后面的词整体晚 1~2s。人工修谱是按
#    "词序列对音符序列"听的，所以英文走同一条路：词-音整体 DP 对齐
#    （`_align_latin_words`），时间只在可信窗内说话，其余交给序列结构。
# 3. **延音不得越过下一个词的开始**（人工谱 0 反例）。
# 4. 音节边界判定容差：TIFA 的音节起点和音符起点各有 ±0.1s（八分音符网格）的抖动，
#    0.02s 的判据会把 "untrue" 的第二个音节判到没有音符可挂。

#: 词起点与音符起点差在这个值内 → TIFA 时间可信，按距离精确挑音。
HEAD_TRUST_SECONDS = 0.35
#: 词头不许比词的开始早这么多（人工谱实测词头落在 [−0.1, +0.2]s；但 TIFA 漂移区的
#: 词要靠结构往回拉，实测拉回最多 ~1.2s）。没有这条下界，延音段形状代价会把词拽到
#: 几秒前的音上（实测 'any' 被拽早 3.2s、'my' 被拽早 2.3s）；卡到 0.35s 又会把漂移区
#: 的词钉死在错误位置上（实测 'disassociate' 拉不回、整段跳词）。
HEAD_EARLY_LIMIT = 1.5
#: 差距超过这个值 → 这个词跟这段音符基本无关（多半是长休止里的幻觉词），
#: 才允许"跳过"。实测漂移区的词偏 1~2.2s，都要落进序列里，不许跳。
HEAD_ABANDON_SECONDS = 3.0
#: 可信窗之外、放弃线之内：距离代价按平台算（漂移区里距离没有信息量），
#: 只留一点残余斜率让同区域内仍然挑更近的音。
HEAD_RESIDUAL_WEIGHT = 0.05
#: 跳过一个词的记账代价。只在「没有合法词头」或「最近的合法位置超过放弃线」时
#: 才会走到，不与赋值代价竞争（否则长漂移区里 DP 会发现"整段跳过更便宜"，
#: 输出直接没词——实测踩过）。
SKIP_COST = 0.45
#: 词的延音段比「音节数−1」多出几个音的代价（每个，封顶 2 个）：
#: 单音节词占着一串音 = 前面的词时间漂了，把后面的词拉回来。
#: 封顶压在 2：更长的罚项会把"前一段没有词的音阶走句"硬塞给下一个词
#: （实测 'any' 被拽早 3.2s——走句的音没人认领，超额罚项逼着它去认领）。
RUN_EXCESS_COST = 0.4
RUN_EXCESS_CAP = 2
#: 延音段比「音节数−1」少几个音的代价（每个，封顶 2 个）：
#: 双音节词一个延音都没有 = 它的第二个音节被下一个词抢了音位。
RUN_DEFICIT_COST = 0.3
RUN_DEFICIT_CAP = 2
#: 第一个词之前的音（前奏尾音）每个的代价，防止首词飘走。
LEAD_ORPHAN_COST = 0.1
#: 音节起点判到音符上的容差（秒）。见上面第 4 条。
SYLLABLE_EPSILON = 0.1


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


#: 英文单词的音节数粗估只当**上限**用：数元音组（y 也算）。
_EN_VOWEL_GROUPS = re.compile(r"[aeiouy]+")


def _latin_syllables(text: str) -> int:
    """英文音节数的粗估（只当上限：元音组数，去掉结尾静音 e）。

    混唱的 `parts` 是空的（走中文对齐路径拿不到音节起点），补标记时拿它判断
    「一个音一个音节」是否成立。everywhere → 4（实际 3），所以只在
    `音节数 ≥ 音数` 时才敢把延音写成 `+`。
    """
    word = "".join(ch for ch in text.lower() if ch.isalpha())
    if not word:
        return 1
    groups = len(_EN_VOWEL_GROUPS.findall(word))
    if groups > 1 and word.endswith("e") and not word.endswith(("le", "ee")):
        groups -= 1
    return max(1, groups)


#: 假名（平假名/片假名）。出现假名 = 日语（汉字是中日共用的，假名才有区分度，
#: 与 `tifa_align.guess_language` 同一口径）。
_KANA_RANGES = (("\u3040", "\u30ff"),)
#: 与前一个假名拼成一拍的结合假名（きょ = 一拍）。促音 っ/ッ 与长音 ー 各占一拍。
_COMBINING_KANA = set("ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ")


def _is_japanese(text: str) -> bool:
    return any(lo <= ch <= hi for ch in text for lo, hi in _KANA_RANGES)


def _mora_count(text: str) -> int:
    """日语词的拍数（モーラ）：一个假名一拍，结合假名（ゃゅょ…）并入前一拍。

    ありがとう = 5 拍；きょ = 1 拍。日语按拍唱，一个词的拍数就是它的「音节数」，
    DP 靠它判断延音段是正常的一字一拍还是吞了后面的词。
    """
    return sum(1 for ch in text if ch not in _COMBINING_KANA)


def continuation_marker(text: str) -> str:
    """后续音节用哪个标记：英文/日文是多音节词，写 `+`；中粤韩一字一音，写 `-`。"""
    return CONTINUATION if _is_latin(text) or _is_japanese(text) else MELISMA


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _build_order(spans: Sequence[tuple[float, float]]) -> list[int]:
    """音符按起点排序后的下标序列。"""
    return sorted(range(len(spans)), key=lambda index: spans[index][0])


# --------------------------------------------------------------------- 英文词-音对齐
def _head_distance_cost(distance: float, *, trusted: bool) -> float:
    """词起点与音符起点的距离代价。

    可信窗内按真实距离算；之外进**平台区**——TIFA 的词时间在漂移区里本身就是错的，
    距离没有信息量，让位给延音段形状（结构）代价，只留一点残余斜率让同区域内仍然
    挑更近的音。窗口膨胀的词（见 `_is_inflated`）时间整体不可信，直接按平台算。
    超过放弃线（`HEAD_ABANDON_SECONDS`）恢复线性陡增：那么远的音八成跟这个词无关。
    """
    if not trusted:
        return HEAD_TRUST_SECONDS
    if distance <= HEAD_TRUST_SECONDS:
        return distance
    if distance <= HEAD_ABANDON_SECONDS:
        return (HEAD_TRUST_SECONDS
                + HEAD_RESIDUAL_WEIGHT * (distance - HEAD_TRUST_SECONDS))
    base = (HEAD_TRUST_SECONDS
            + HEAD_RESIDUAL_WEIGHT * (HEAD_ABANDON_SECONDS - HEAD_TRUST_SECONDS))
    return base + (distance - HEAD_ABANDON_SECONDS)


def _syllable_count(text: str, parts: Sequence[float]) -> int:
    """词的音节数：1 + TIFA 给出的第二、三…音节起点数。"""
    return 1 + len(tuple(parts))


def _is_inflated(start: float, end: float, syllables: int) -> bool:
    """词窗口是不是被对齐器**拉长**了（Viterbi 把一个词摊到后面几个词的音频上）。

    实测漂移区的词：'you' 1.4s / 'choice' 1.55s / 'i' 0.51s——都是单音节词占着
    几倍于一个音节的窗口。窗口超过「音节数 × 0.35s」就当不可信。
    """
    return (end - start) > syllables * HEAD_TRUST_SECONDS


class _RunShape:
    """延音段形状代价：长度偏离「音节数 − 1」（ideal）的罚项。

    多出（excess）= 前面的词时间漂了、把后面词的音吞了（漂移检测的主力）；
    少了（deficit）= 它的某个音节没抢到音位（下一个词起太早）。
    """

    def __init__(self, excess: float, deficit: float,
                 excess_cap: int, deficit_cap: int) -> None:
        self.excess = excess
        self.deficit = deficit
        self.excess_cap = excess_cap
        self.deficit_cap = deficit_cap

    def total(self, run_length: int, ideal: int) -> float:
        over = max(0, run_length - ideal)
        under = max(0, ideal - run_length)
        return (min(over, self.excess_cap) * self.excess
                + min(under, self.deficit_cap) * self.deficit)



# --------------------------------------------------------------------- 字级 DP
#: 字级 DP：跳过一个音的代价（中文的转音很常见，所以给得很小）。
DP_SKIP_NOTE_COST = 0.05
#: 字级 DP：英文词多占一个音的代价（对齐器把词时间拉长时用）。
DP_RUN_EXCESS_COST = 0.05
#: 字级 DP：英文词少占一个音的代价（比多占更值得罚：词被挤前一位正是混唱的病）。
DP_RUN_DEFICIT_COST = 0.10
#: 延音段长度偏离「预期」超过这么多音就不再细分（更长的一律按封顶算）。
DP_RUN_CAP = 4
#: 英文词「预期占几个音」的上限（窗口被对齐器拉长时别夸张）。
DP_MAX_SPAN = 6
#: 英文词的「预期延音段」上限：粗估音节数会多算（everywhere 数出 4，实际 3），
#: 封顶 2 免得把下一个词推得太晚。

#: 字级 DP：跳过一个字的代价（字比音多时才会用：absorbed/dropped 走这条）。
DP_SKIP_CHAR_COST = 1.0
#: 字级 DP：起点距离的封顶。字时间有 ±0.2~0.4s 抖动，超过这个距离的"匹配"没有信息量，
#: 封顶避免一个漂移字把整段拉走。
DP_DISTANCE_CAP = 0.5
#: 字级 DP：硬门限。离所有候选音都超过这个距离的字直接跳过（否则封顶会让 5 秒外的字
#: 也"挂"到最近的音上）；跳过的字按有没有音盖住它分成 absorbed / dropped。
DP_MAX_GAP = 1.0


def _refine_latin_runs(order: Sequence[int], spans: Sequence[tuple[float, float]],
                       chars: Sequence[Any], sequence: Sequence[int],
                       heads: dict[int, int], skipped: list[int]) -> None:
    """混唱：把连续的英文段交给词级 DP（`_align_latin_words`）重排。

    字级 DP 每个 token 只占一个音，`everywhere`（3 个音节）后两个音会被下一个词占走，
    `parts` 就没有落点。词级 DP 按音节数预留延音段；边界取**相邻中文头之间**的音
    `[上一个中文头 + 1, 下一个中文头 − 1]`，段落首尾则取全曲边界。

    只动英文段，中文头一个都不碰（中文结果已单独验证过）。
    """
    position_of = {note: index for index, note in enumerate(order)}
    flags = [_is_latin(str(getattr(chars[index], "text", ""))) for index in sequence]
    step = 0
    while step < len(sequence):
        if not flags[step]:
            step += 1
            continue
        end_step = step
        while end_step + 1 < len(sequence) and flags[end_step + 1]:
            end_step += 1
        run = list(sequence[step:end_step + 1])
        # 没有 `parts`（音节起点）时词级 DP 会把每个词都当单音节，反而比字级 DP 差
        # （《梦不落》英文 token 50% → 27%）。只重排真正拿到音节信息的段。
        if not any(getattr(chars[index], "parts", ()) for index in run):
            step = end_step + 1
            continue

        low = 0
        for previous in range(step - 1, -1, -1):
            note = heads.get(sequence[previous])
            if note is not None:
                low = position_of.get(note, 0) + 1
                break
        high = len(order) - 1
        for following in range(end_step + 1, len(sequence)):
            note = heads.get(sequence[following])
            if note is not None:
                high = position_of.get(note, len(order)) - 1
                break
        if high >= low:
            sub_order = list(order[low:high + 1])
            sub_heads, _ignored = _align_latin_words(
                sub_order, spans, [chars[index] for index in run])
            for index in run:
                heads.pop(index, None)
            # 词级 DP 返回的是**子序列内**的下标，要映回全局字下标
            for local_index, note in sub_heads.items():
                heads[run[local_index]] = note
            dropped = set(run)
            skipped[:] = [index for index in skipped if index not in dropped]
        step = end_step + 1


def _align_chars_dp(order: Sequence[int], spans: Sequence[tuple[float, float]],
                    chars: Sequence[Any], *,
                    skip_char_cost: float = DP_SKIP_CHAR_COST,
                    cap: float = DP_DISTANCE_CAP,
                    max_gap: float = DP_MAX_GAP
                    ) -> tuple[dict[int, int], list[int]]:
    """中文/粤语/混唱：token 序列 → 音符序列的**单调 DP**（一个 token 一个头）。

    逐字贪心在中文上有一个结构性毛病：字时间有 ±0.2~0.4s 抖动，而音符中位长只有
    0.25s，一旦某个字被放到后一个音上，**后面整段会跟着错位**，直到碰到一个空音才
    重新对上（实测 1123 个 ±1 的 token 只来自 267 段这样的错位）。所以这里改成整体
    最小代价对齐：

    - 头的音符下标**严格递增**（后一个字不能挂到更早的音上）；
    - 代价 = 起点距离（封顶）+ 与「预期延音段」的偏离 + 跳过的字数 × 大代价；
    - 预期延音段：中文恒 0；英文词取**它时间窗里的音数 − 1**（everywhere 占 3 个音
      就保 2 个），混唱里靠这个不让后面的词被挤前一位；
    - 允许跳音（转音）、允许跳字（字比音多）。

    DP 状态是「处理完当前字、下一个空闲音位置 u」，转移按 mid（延音段长度）展开，
    超出 `DP_RUN_CAP` 的用前缀最小值兜住，整体 O(字数×音数)。

    返回 `(头表 {字下标: 音符下标}, 跳过的字下标)`。
    """
    onsets = [spans[note][0] for note in order]
    note_count = len(onsets)
    if not note_count:
        return {}, list(range(len(chars)))
    sequence = [index for index, _char in
                sorted(enumerate(chars),
                       key=lambda item: (float(getattr(item[1], "start", 0.0)),
                                         item[0]))]

    #: 每个 token 的「预期延音段」。中文/混唱暂按 0：英文词的时间窗太粗
    #: （TIFA 给 everywhere 的窗口只有 0.12s，实际占 3 个音），按音节预留会推错方向
    #: （实测《梦不落》英文 token 50% → 40%）。等英文段拿到 `parts`（音节起点）再开。
    ideals = [0 for _index in sequence]
    infinity = float("inf")
    #: `prev[u]`：处理完前几个字、下一个空闲音是 u 的最小代价。
    prev = [0.0] * (note_count + 1)
    #: 每个字在每个状态下的决策：(前一状态, 占的音符下标或 None)
    backs: list[list[tuple[int, int | None]]] = []

    for step, char_index in enumerate(sequence):
        start = float(getattr(chars[char_index], "start", 0.0))
        # 这一段延音属于**前一个** token：它的预期长度决定跳几个音才免费。
        ideal = ideals[step - 1] if step else 0
        current = [infinity] * (note_count + 1)
        back: list[tuple[int, int | None]] = [(0, None)] * (note_count + 1)
        # 前缀最小：给超出封顶的长延音段兜底（形状是常数，不用逐个 mid 算）。
        prefix = [infinity] * note_count
        prefix_arg = [-1] * note_count
        best, best_u = infinity, -1
        for u in range(note_count + 1):
            if prev[u] < best:
                best, best_u = prev[u], u
            if u < note_count:
                prefix[u] = best
                prefix_arg[u] = best_u
        for position in range(note_count):
            if prefix[position] == infinity:
                continue
            gap = abs(onsets[position] - start)
            if gap > max_gap:
                continue                # 太远：宁可跳过这个字，也不硬挂到别的句子上
            distance = min(gap, cap)
            head_cost = infinity
            head_from = -1
            for mid in range(0, min(position, ideal + DP_RUN_CAP) + 1):
                value = prev[position - mid]
                if value == infinity:
                    continue
                shape = (DP_RUN_EXCESS_COST * max(0, mid - ideal)
                         + DP_RUN_DEFICIT_COST * max(0, ideal - mid))
                if value + shape < head_cost:
                    head_cost, head_from = value + shape, position - mid
            tail = position - (ideal + DP_RUN_CAP) - 1
            if tail >= 0 and prefix[tail] != infinity:
                value = prefix[tail] + DP_RUN_EXCESS_COST * DP_RUN_CAP
                if value < head_cost:
                    head_cost, head_from = value, prefix_arg[tail]
            if head_from >= 0:
                cost = head_cost + distance
                if cost < current[position + 1]:
                    current[position + 1] = cost
                    back[position + 1] = (head_from, position)
        # 跳过这个字（字比音多时才会选它）
        for u in range(note_count + 1):
            value = prev[u] + skip_char_cost
            if value < current[u]:
                current[u] = value
                back[u] = (u, None)
        prev = current
        backs.append(back)

    state = min(range(note_count + 1), key=lambda u: prev[u])
    heads: dict[int, int] = {}
    skipped: list[int] = []
    for step in range(len(sequence) - 1, -1, -1):
        previous_state, position = backs[step][state]
        if position is None:
            skipped.append(sequence[step])
        else:
            heads[sequence[step]] = order[position]
        state = previous_state
    skipped.reverse()
    # 混唱：连续的英文段用词级 DP 重排（按音节预留音，`parts` 才有落点）
    _refine_latin_runs(order, spans, chars, sequence, heads, skipped)
    return heads, skipped


def _align_latin_words(order: Sequence[int], spans: Sequence[tuple[float, float]],
                       chars: Sequence[Any]) -> tuple[dict[int, int], list[int]]:
    """英文：词序列 → 音符序列的整体对齐（DP）。

    逐字贪心对英文有两类系统性错误（对照 `samples/对齐/英文/` 的人工谱）：
    词头按"重叠最多"挑会晚一个音；TIFA 词时间漂移会让整句推迟、延音吞掉下一个词的
    头。人工修谱的做法是**按词序列对音符序列**听，这里把同一件事形式化成最小代价对齐：

    - 每个词占一个**词头音**，词头严格递增；
    - 词头的候选上界 = 两个约束的交集：
        1. onset ≥ 自己开始的音不能留给前一个词当延音（人工规则：延音都在下一个词
           开始之前，实测 0 反例）——词头 ≤ 第一个 onset ≥ 自己开始的音；
        2. onset ≥ 下一个词开始的音不能当自己的头——词头 < 第一个 onset ≥ 下一词
           开始的音。ASR 重复的词因此**自动没有合法词头**（候选区间为空 → 跳过）。
    - 词头与下一个词头之间的音 = 前一个词的延音段，长度偏离「音节数−1」要罚
      （见 `_RunShape`）。
    - 词头距离代价可信窗内线性、窗外汇入平台（见 `_head_distance_cost`）。

    返回 `(词头表 {字符下标: 音符下标}, 跳过的字符下标列表)`。
    """
    words: list[tuple[int, str, float, float, int, bool]] = []
    for index, char in enumerate(chars):
        text = str(getattr(char, "text", "") or "").strip()
        if not text:
            continue
        start = float(getattr(char, "start", 0.0))
        end = float(getattr(char, "end", start))
        parts = tuple(getattr(char, "parts", ()) or ())
        # 日语的「音节」是拍（モーラ）：Qwen 对齐器不给音节起点，按假名数拍；
        # 英文有 TIFA 的音节起点，没有就当单音节（Qwen 路径）。
        if _is_japanese(text):
            syllables = _mora_count(text)
        else:
            syllables = _syllable_count(text, parts)
        words.append((index, text, start, end, syllables,
                      _is_inflated(start, end, syllables)))
    if not words:
        return {}, []

    note_count = len(order)
    onsets = [spans[note][0] for note in order]
    # 按词开始时间排序处理（正常输入本来就有序；这里只是防御）
    sequence = sorted(range(len(words)), key=lambda k: (words[k][2], k))

    def first_ge(t: float) -> int:
        """第一个 onset >= t 的音符位置（onsets 升序）。"""
        return bisect.bisect_left(onsets, t - 1e-9)

    INF = float("inf")
    #: f[u]：处理完当前词、下一个空闲音符位置是 u 的最小代价。
    f = [LEAD_ORPHAN_COST * u for u in range(note_count + 1)]
    #: 每个词在状态 u 的决策：(前一状态 u，是否占头)。回溯用。
    choices: list[dict[int, tuple[int, bool]]] = []

    for step, word_key in enumerate(sequence):
        _idx, prev_check, start, _end, _syllables, inflated = words[word_key]
        trusted = not inflated
        # 延音段的属主是**前一个**词，形状按它的音节数算；
        # 第一个词之前没有词，前面的音是前奏，按 LEAD 线性算。
        if step:
            prev_word = words[sequence[step - 1]]
            if _is_japanese(prev_word[1]):
                # 日语词的延音段不罚：假名按拍唱多长都正常，汉字词的拍数
                # （夢 = ゆめ 两拍）从字面上无从得知，罚了只会把后面词拽早。
                ideal = 0
                shape = _RunShape(0.0, 0.0, RUN_EXCESS_CAP, RUN_DEFICIT_CAP)
            else:
                ideal = prev_word[4] - 1
                shape = _RunShape(RUN_EXCESS_COST, RUN_DEFICIT_COST,
                                  RUN_EXCESS_CAP, RUN_DEFICIT_CAP)
        else:
            ideal = 0
            shape = _RunShape(LEAD_ORPHAN_COST, 0.0, note_count, 0)
        # 下一个词真正更晚才开始时才约束词头上界（零长度字对儿同时刻，不互相约束）
        if step + 1 < len(sequence) and words[sequence[step + 1]][2] > start + 1e-6:
            hi = min(first_ge(start), first_ge(words[sequence[step + 1]][2]) - 1)
        else:
            hi = first_ge(start)
        hi = min(hi, note_count - 1)

        # 词头 position 的转移：min over u ∈ [0, position] of
        #   f[u] + shape(position − u) + 距离代价。
        # shape 只在 mid = position − u ∈ [0, ideal + CAPX] 内变化，之外饱和成常数
        # → 用前缀最小值兜住更长的延音段。
        reach = min(ideal + shape.excess_cap, note_count - 1)
        prefix = []                            # prefix[u] = min(f[0..u])
        prefix_arg = []                        # 以及取到最小值的那个 u（回溯要用）
        running = INF
        running_u = -1
        for u, value in enumerate(f):
            if value < running:
                running, running_u = value, u
            prefix.append(running)
            prefix_arg.append(running_u)
        nf = [INF] * (note_count + 1)
        pick: dict[int, tuple[int, bool]] = {}
        # 词头合法区间 [lo, hi]：lo 挡"拽到几秒前"（见 HEAD_EARLY_LIMIT），hi 挡
        # "占下一个词的头"。
        lo = first_ge(start - HEAD_EARLY_LIMIT)
        for position in range(note_count):
            best_cost = INF
            best_u = -1
            for mid in range(0, min(position, reach) + 1):
                u = position - mid
                value = f[u] + shape.total(mid, ideal)
                if value < best_cost:
                    best_cost, best_u = value, u
            tail = position - reach - 1
            if tail >= 0:                      # 延音段长到超出封顶：形状是常数，
                value = prefix[tail] + shape.total(reach + 1, ideal)
                if value < best_cost:          # 前驱是前缀最小值所在的 u，不是 tail
                    best_cost, best_u = value, prefix_arg[tail]
            if best_u >= 0 and lo <= position <= hi:
                distance = abs(onsets[position] - start)
                total = best_cost + _head_distance_cost(distance, trusted=trusted)
                if total < nf[position + 1]:
                    nf[position + 1] = total
                    pick[position + 1] = (best_u, True)
            # 跳过：只允许「这个词没有合法词头」（[lo, hi] 与当前状态不相交，ASR 重复词
            # 就是被这条拦下的）或「最近的合法位置远得离谱」（长休止里的幻觉词）。
            # 不给"跳过更便宜"这个选项——否则长漂移区里 DP 会发现整段跳过更划算，
            # 输出直接没词（实测踩过）。
            if max(lo, position) > hi:
                skip_allowed = True
            else:
                closest = min(max(first_ge(start), max(lo, position)), hi)
                skip_allowed = abs(onsets[closest] - start) > HEAD_ABANDON_SECONDS
            if skip_allowed and f[position] + SKIP_COST < nf[position]:
                nf[position] = f[position] + SKIP_COST
                pick[position] = (position, False)
        f = nf
        choices.append(pick)

    # 回溯
    state = min(range(note_count + 1), key=lambda u: f[u])
    heads: dict[int, int] = {}
    skipped: list[int] = []
    for step in range(len(sequence) - 1, -1, -1):
        previous, assigned = choices[step].get(state, (state, False))
        if assigned:
            heads[words[sequence[step]][0]] = order[state - 1]
        else:
            skipped.append(words[sequence[step]][0])
        state = previous
    skipped.reverse()
    return heads, skipped


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
    #: 音符下标 -> 这个英文词内部「第 2、3…个音节」的起点（`AlignedChar.parts`）。
    #: 有它才能区分延续标记 `-`（延长当前音节）和 `+`（进下一个音节）。
    parts_of: dict[int, tuple[float, ...]] = {}

    def available(note: int) -> bool:
        if note in taken:
            return False
        return not (only_empty and lyrics[note].strip())

    last_note = -1

    # ---------------- 第一遍：只决定每个字挂在哪个音上 ----------------
    # 日文/英文是**多音节词语言**：一个词（ありがとう / computer）唱在几个音上，
    # 词头落点、转音分界都要按词算 → 走整体 DP 对齐（`_align_latin_words`）。
    # 中文/粤语/韩语一个方块字就是一个音节，走原来的逐字贪心。
    # 判定看**整首**：歌词里出现假名（含汉字混排的日语歌词）就整首按词算；
    # 纯汉字（中粤）或韩文不会误入——它们没有假名也不全是拉丁字母。
    dp_heads: dict[int, int] | None = None
    dp_skipped: set[int] = set()
    dp_kind = "word"
    if not only_empty:
        # 整体重填（重新识别）：填词落点会变，旧词残留在不再是词头的音上会变成
        # "てる てる"这种重复、或"行头一字为名"这种上一轮的词（实测两次踩过，
        # DP/贪心两条路径都会）。清空再填，旧词一个不留。
        lyrics[:] = [""] * len(spans)
        texts = [str(getattr(char, "text", "") or "").strip() for char in chars]
        has_kana = any(_is_japanese(text) for text in texts)
        if any(texts) and all(
                _is_latin(text) or _is_japanese(text) or has_kana
                for text in texts if text):
            dp_heads, skipped_words = _align_latin_words(order, spans, chars)
            dp_skipped = set(skipped_words)
        elif any(texts):
            # 中文/粤语（一字一音节）：逐字贪心会被 ±0.2~0.4s 的字时间抖动带成整段
            # 错位，改用整体单调 DP（见 `_align_chars_dp`）。
            dp_heads, skipped_chars = _align_chars_dp(order, spans, chars)
            dp_skipped = set(skipped_chars)
            dp_kind = "char"

    for char_index, char in enumerate(chars):
        text = str(getattr(char, "text", "") or "").strip()
        if not text:
            continue
        start = float(getattr(char, "start", 0.0))
        end = float(getattr(char, "end", start))

        if dp_heads is not None:
            head = dp_heads.get(char_index)
            if head is not None:
                lyrics[head] = text
                taken.add(head)
                heads[head] = (text, start, max(end, start))
                parts = tuple(getattr(char, "parts", ()) or ())
                if parts:
                    parts_of[head] = parts
                fill.reasons["dp_head"] = fill.reasons.get("dp_head", 0) + 1
                fill.assigned += 1
            else:
                # DP 跳过的字/词：没有合法头位。登记出来让人核对，不悄悄丢。
                # 中文（字级 DP）再分一层：有音盖住这个字的起点 = 音符比字少（absorbed）；
                # 连盖住的音都没有 = 起点落在音符之间的间隙（dropped）。
                fill.reasons["dp_skip"] = fill.reasons.get("dp_skip", 0) + 1
                counted = True
                if char_index in dp_skipped:
                    covering = next((note for note in order
                                     if spans[note][0] <= start <= spans[note][1]),
                                    None)
                    if dp_kind == "char" and covering is None:
                        fill.dropped += 1
                        fill.dropped_chars.append(text)
                        fill.reasons["dropped"] = (
                            fill.reasons.get("dropped", 0) + 1)
                        counted = False
                    else:
                        fill.absorbed_chars.append(text)
                        if covering is not None:
                            fill.warnings.append(
                                f"字「{text}」({start:.2f}s) 落在已经占住的音上"
                                f"（音 {covering}）：那段音符比字少")
                        else:
                            fill.warnings.append(
                                f"字「{text}」({start:.2f}s) 没有可挂的音位"
                                f"（那段音符比字少，或识别重复）")
                if counted:
                    fill.assigned += 1
            continue

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
            # 起点比容差还早的音，只有一种情况仍然算候选：**这个字的起点落在它里面**。
            # 音符中位长只有 0.25s，而字时间有 ±0.2~0.4s 抖动，光靠「起点不早于容差」
            # 会把短音整片跳过，字被挤到后一个音上——实测《最炫民族风》「涯」晚
            # 0.213s（容差 0.20）就漏掉自己的音，后面整段跟着错位到下一个空隙为止。
            if (note_start < start - tolerance
                    and not (note_start <= start <= note_end)):
                continue
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
            parts = tuple(getattr(char, "parts", ()) or ())
            if parts:
                parts_of[best_note] = parts
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
            parts = tuple(getattr(char, "parts", ()) or ())
            if parts:
                parts_of[fallback] = parts
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
    all_onsets = sorted(spans[note][0] for note in order)
    for position, (head, (text, start, end)) in enumerate(ordered_heads):
        if end <= start + 1e-9:
            continue
        limit = (ordered_heads[position + 1][0]
                 if position + 1 < len(ordered_heads) else len(spans))
        parts = parts_of.get(head, ())
        # 人工规则：延音不得越过下一个词的开始（实测 0 反例）。DP 路径的词头已经被
        # 这个约束卡过；这里防的是老贪心路径（中日文/混合内容里的英文词）TIFA 的
        # end 拖得太长、把下一个词的头音吞成延音的情况——只对英文词生效。
        next_word_start = (ordered_heads[position + 1][1][1]
                           if position + 1 < len(ordered_heads)
                           and _is_latin(ordered_heads[position + 1][1][0]) else None)
        # 先把"属于这个词的延音音"挑出来，再决定哪些写 `+`。
        # 窗口终点：CJK 用**下一个字的起点**，而不是这个字自己的 end。
        # TIFA 对一部分字给出接近零的时长（实测粤语《你唔爱我啦》"系" 0.02s），
        # 按 end 截断会让一个字后面的延音全丢（marker 0/72）。字一直唱到下一个字
        # 开唱为止，所以用 next_start 才是对的。
        next_start = (ordered_heads[position + 1][1][1]
                      if position + 1 < len(ordered_heads) else None)
        window_end = (next_start if (next_start is not None and not _is_latin(text))
                      else end)
        candidates: list[int] = []
        for note in order:
            if note <= head or note >= limit or note in taken:
                continue
            if spans[note][0] >= window_end:
                break     # 转音必须落在字的时间窗内；容差只用来找"头"，不放大窗口
            if (next_word_start is not None
                    and spans[note][0] >= next_word_start):
                break     # 已经唱到下一个词了：这个音留给它，不当延音
            if only_empty and lyrics[note].strip():
                continue
            candidates.append(note)

        # 没有 `parts` 的英文词：按粗估音节数在"头 + 延音"上均分边界。
        # 实测人工谱里 `+` 被写成 `-` 的有 101 处、反向只有 9 处 —— 以前"保守写 `-`"
        # 是错的方向；now 只要音数 ≥ 2 就按音节均分（hello 2 音节 / 3 个音 → `- +`）。
        total = len(candidates) + 1
        boundaries: set[int] = set()
        if not parts and _is_latin(text):
            # 没有音节信息时按粗估音节数在「头 + 延音」上均分边界。
            # 注意：8 首人工谱的标注约定并不一致（`+` 占比 44.8%~89.5%），所以这里的
            # 取舍不可能对所有歌都对；严格 marker 要按"与写法无关"的 `marker_any` 一起看。
            syllables = max(1, min(total, _latin_syllables(text)))
            for index in range(1, syllables):
                boundaries.add(round(index * total / syllables))

        cursor = 0
        for ordinal, note in enumerate(candidates, start=1):
            new_syllable = False
            if parts:
                while (cursor < len(parts)
                       and parts[cursor] <= spans[note][0] + SYLLABLE_EPSILON):
                    cursor += 1
                    new_syllable = True
            elif _is_japanese(text):
                # 日语没有音节起点，按拍唱：每个音都是下一拍
                new_syllable = True
            elif ordinal in boundaries:
                new_syllable = True
            lyrics[note] = CONTINUATION if new_syllable else MELISMA
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


def retime_lines_by_chars(lines: Sequence[Any], chars: Sequence[Any]) -> list[Any]:
    """无时间戳的官方词（TXT）借用已对齐字符的时间：两串字符按比例位对位。

    识别文本靠不住但**时间靠得住**——TIFA 把识别文本对到音频上，每个被唱的
    音节都有时刻；误听不改音节数（中文尤甚）。官方词的总字数与识别字数按比例
    位对位（官方第 j 个字拿识别第 j×N/M 个字的时刻），字面换成官方词、
    时间保持识别的。识别多听/漏听一两个字时错位不超过那一两个字；
    但识别在某个区间**整段多听了内容**（桥段和声、说唱）时，官方词会整体
    后移——这是按位映射的原理上限，要逐行精确请用带时间戳的 LRC。
    """
    from .asr import AlignedChar

    spoken = [c for c in chars if str(c.text or "").strip()]
    official: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        for ch in line.text:
            if ch.strip():
                official.append((index, ch))
    if not official or not spoken:
        return list(chars)
    total = len(official)
    out: list[Any] = []
    for position, (index, ch) in enumerate(official):
        k = min(int(position / total * len(spoken)), len(spoken) - 1)
        spoken_char = spoken[k]
        out.append(AlignedChar(ch, spoken_char.start, spoken_char.end))
    return out


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
