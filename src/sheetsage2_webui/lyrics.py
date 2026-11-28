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
#: 对齐器声学置信度低于这个阈值时，才把该词时间视为不可信（走平台区、靠结构拉回）。
#: 取代旧的「时长 > 音节数×0.35s 就抹平距离」启发式——正常拖长音（0.5~1.5s）不该被抹平。
LOW_CONFIDENCE_THRESHOLD = 0.25
#: 音符间超过这个休止时长（秒）视为换气/断句：前一个字/词的延音不应轻易跨休止吞掉新句首音。
REST_SKIP_GAP = 0.25
#: 跳过一个紧跟在休止符（> REST_SKIP_GAP）之后的音符时的附加代价。
REST_SKIP_PENALTY = 0.25
#: 第二遍补延音标记时，超过词 end 且与前一音之间存在大于该时长的休止，停止继续补标记。
CONTINUATION_MAX_GAP = 0.50


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
    """后续音节用哪个标记：英文多音节词与未拆的多拍假名词写 `+`；中粤韩及单拍假名（か/きょ/ラ）写 `-`。"""
    if _is_latin(text):
        return CONTINUATION
    if _is_japanese(text) and _mora_count(text) > 1:
        return CONTINUATION
    return MELISMA


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
    """保留兼容：判断词窗口是否明显超过单音节典型时长（不再直接用于抹平距离梯度）。"""
    return (end - start) > syllables * HEAD_TRUST_SECONDS


def _is_low_confidence(char: Any) -> bool:
    """该字/词的对齐器声学置信度是否过低（仅在携带 `confidence` 时生效）。"""
    conf = getattr(char, "confidence", None)
    if conf is None:
        return False
    return float(conf) < LOW_CONFIDENCE_THRESHOLD


def _match_syllable_notes(parts: Sequence[float],
                          note_onsets: Sequence[float]) -> set[int]:
    """把词内 `M = len(parts)` 个后续音节起点，严格单调 1-to-1 匹配到 `K = len(note_onsets)` 个续音。

    替代旧的 `while parts[cursor] <= onset + 0.10` 贪心扫描：
    - 旧做法只要 `part` 比音符晚 0.12s（超容差 20ms）就彻底丢弃 `+`（实测 125 例）；
    - 或两个 `parts` 落在同一音容差内被一次吞光，后续音全变成 `-`（实测 35 例）。
    当 `M >= K` 时每个续音都是新音节（全 `+`）；当 `M < K` 时用 DP 选总时间差最小的 `M` 个音写 `+`。
    返回应写 `+` 的 `note_onsets` 下标集合。
    """
    m = len(parts)
    k = len(note_onsets)
    if m == 0 or k == 0:
        return set()
    if m >= k:
        return set(range(k))
    inf = float("inf")
    dp = [[inf] * (k + 1) for _ in range(m + 1)]
    take = [[False] * (k + 1) for _ in range(m + 1)]
    for col in range(k + 1):
        dp[0][col] = 0.0
    for row in range(1, m + 1):
        part_time = parts[row - 1]
        for col in range(row, k + 1):
            skip_cost = dp[row][col - 1]
            match_cost = dp[row - 1][col - 1] + abs(part_time - note_onsets[col - 1])
            if match_cost < skip_cost:
                dp[row][col] = match_cost
                take[row][col] = True
            else:
                dp[row][col] = skip_cost
    chosen: set[int] = set()
    row, col = m, k
    while row > 0 and col > 0:
        if take[row][col]:
            chosen.add(col - 1)
            row -= 1
            col -= 1
        else:
            col -= 1
    return chosen


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
    ends = [spans[note][1] for note in order]
    gaps_before = ([0.0] + [max(0.0, onsets[i] - ends[i - 1])
                            for i in range(1, len(onsets))]
                   if onsets else [])
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
            rest_pen = 0.0
            for mid in range(0, min(position, ideal + DP_RUN_CAP) + 1):
                u = position - mid
                if mid > 0 and step > 0 and gaps_before[u] > REST_SKIP_GAP:
                    rest_pen += REST_SKIP_PENALTY
                value = prev[u]
                if value == infinity:
                    continue
                shape = (DP_RUN_EXCESS_COST * max(0, mid - ideal)
                         + DP_RUN_DEFICIT_COST * max(0, ideal - mid)
                         + rest_pen)
                if value + shape < head_cost:
                    head_cost, head_from = value + shape, u
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
                      _is_low_confidence(char)))
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
        # 纯英文 或 未拆的多拍日语词（如 ありがとう）走词级 DP；
        # 已拆成单拍假名（か/きょ/ん）的日语（含日英混唱）与中/粤/韩同口径，走一拍一音的单调 DP
        # （内嵌英文段若带 TIFA 音节起点 `parts` 会由 `_realign_latin_runs` 自动切分）。
        has_multi_mora_kana = any(
            _is_japanese(text) and _mora_count(text) > 1 for text in texts
        )
        if any(texts) and (
            all(_is_latin(text) for text in texts if text)
            or has_multi_mora_kana
        ):
            dp_heads, skipped_words = _align_latin_words(order, spans, chars)
            dp_skipped = set(skipped_words)
        elif any(texts):
            # 中文/粤语/单拍日语（一字/一拍一音节）：逐字贪心会被 ±0.2~0.4s 的字时间抖动带成整段
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
        # 窗口终点：中英文统一延到下一个词/字的起点（无下一词则到轨尾）。
        # 此前仅对非英文放开，导致英文 TIFA end 偏短时 44 处真实续音被留空；
        # 同时加一条跨休止保护：已经越过本词 end 且与前一音之间有 > CONTINUATION_MAX_GAP
        # 的明显休止时停止延续，避免把间奏/新乐句孤立音错标成延音。
        next_start = (ordered_heads[position + 1][1][1]
                      if position + 1 < len(ordered_heads) else None)
        window_end = next_start if next_start is not None else end
        candidates: list[int] = []
        prev_end_sec = spans[head][1]
        for note in order:
            if note <= head or note >= limit or note in taken:
                continue
            note_start, note_end = spans[note]
            if note_start >= window_end:
                break     # 转音必须落在字的时间窗内；容差只用来找"头"，不放大窗口
            if (next_word_start is not None
                    and note_start >= next_word_start):
                break     # 已经唱到下一个词了：这个音留给它，不当延音
            if note_start >= end and (note_start - prev_end_sec) > CONTINUATION_MAX_GAP:
                break     # 已越过本词结束时间且出现明显休止：不再跨休止延续
            if only_empty and lyrics[note].strip():
                prev_end_sec = max(prev_end_sec, note_end)
                continue
            candidates.append(note)
            prev_end_sec = max(prev_end_sec, note_end)

        # 没有 `parts` 的英文词：按粗估音节数在"头 + 延音"上均分边界。
        # 实测人工谱里 `+` 被写成 `-` 的有 101 处、反向只有 9 处 —— 以前"保守写 `-`"
        # 是错的方向；now 只要音数 ≥ 2 就按音节均分（hello 2 音节 / 3 个音 → `- +`）。
        total = len(candidates) + 1
        boundaries: set[int] = set()
        plus_positions: set[int] = set()
        if parts and candidates:
            plus_positions = _match_syllable_notes(
                parts, [spans[note][0] for note in candidates])
        elif not parts and _is_latin(text):
            # 没有音节信息时按粗估音节数在「头 + 延音」上均分边界。
            # 注意：8 首人工谱的标注约定并不一致（`+` 占比 44.8%~89.5%），所以这里的
            # 取舍不可能对所有歌都对；严格 marker 要按"与写法无关"的 `marker_any` 一起看。
            syllables = max(1, min(total, _latin_syllables(text)))
            for index in range(1, syllables):
                boundaries.add(round(index * total / syllables))

        for idx_cand, note in enumerate(candidates):
            ordinal = idx_cand + 1
            new_syllable = False
            if parts:
                new_syllable = idx_cand in plus_positions
            elif _is_japanese(text):
                # 单拍假名（か / きょ / ラ）：只有 1 拍，拖音一定是 `-`，避免非法 `ラ + +`；
                # 未拆的多拍假名词（ありがとう = 5 拍）：前 mora-1 个音是下一拍 `+`，超出后是延音 `-`
                new_syllable = ordinal < _mora_count(text)
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


_LINE_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:['\u2019-][A-Za-z0-9]+)*|[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7a3]"
)


def _tokenize_lyric_line(text: str) -> list[str]:
    """把一行歌词切成对齐单元：拉丁词整词保留，中日韩方块字/假名逐字切开，忽略纯标点。"""
    tokens = _LINE_TOKEN_RE.findall(str(text or ""))
    if tokens:
        return tokens
    return [ch for ch in str(text or "") if ch.strip()]


def _norm_token(text: str) -> str:
    return str(text or "").strip().lower().replace("\u2019", "'")


def _align_token_sequences(official: Sequence[str],
                           spoken: Sequence[str]) -> list[tuple[int, int]]:
    """官方词序列与识别词序列的全局编辑距离对齐，返回精确匹配的 `(官方下标, 识别下标)` 锚点。"""
    n, m = len(official), len(spoken)
    if not n or not m:
        return []
    # 只在完全相等时给 0 代价，替换给 1.5（宁可跳过错听字，也不乱配成假锚点）
    direction = [[0] * (m + 1) for _ in range(n + 1)]
    prev = [float(j) for j in range(m + 1)]
    for i in range(1, n + 1):
        curr = [float(i)] + [0.0] * m
        o_tok = official[i - 1]
        row = direction[i]
        for j in range(1, m + 1):
            diag = prev[j - 1] + (0.0 if o_tok == spoken[j - 1] else 1.5)
            up = prev[j] + 1.0
            left = curr[j - 1] + 1.0
            if diag <= up and diag <= left:
                curr[j] = diag
                row[j] = 0
            elif up <= left:
                curr[j] = up
                row[j] = 1
            else:
                curr[j] = left
                row[j] = 2
        prev = curr
    pairs: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        step = direction[i][j]
        if step == 0:
            if official[i - 1] == spoken[j - 1]:
                pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif step == 1:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def _retime_official_with_lines(lines: Sequence[Any], chars: Sequence[Any]
                                ) -> tuple[list[Any], list[int]]:
    """核心实现：返回 `(对齐后的 AlignedChar 列表, 每个字所属的原始行下标)`。"""
    from .asr import AlignedChar, MAX_REPAIR_CHAR_SECONDS

    spoken = [c for c in chars if str(getattr(c, "text", "") or "").strip()]
    official: list[tuple[int, str]] = []
    for line_index, line in enumerate(lines):
        for token in _tokenize_lyric_line(getattr(line, "text", "")):
            official.append((line_index, token))
    if not official or not spoken:
        return list(chars), [0] * len(chars)

    o_norm = [_norm_token(tok) for _, tok in official]
    s_norm = [_norm_token(c.text) for c in spoken]
    anchors = _align_token_sequences(o_norm, s_norm)

    total = len(official)
    line_ids = [line_index for line_index, _ in official]

    if not anchors:
        # 完全没有共同字（极端情况）：退回按比例位对位，不崩
        out: list[Any] = []
        for pos, (_line_idx, tok) in enumerate(official):
            k = min(int(pos / total * len(spoken)), len(spoken) - 1)
            sc = spoken[k]
            out.append(AlignedChar(tok, sc.start, sc.end,
                                   tuple(getattr(sc, "parts", ()) or ()),
                                   getattr(sc, "confidence", None)))
        return out, line_ids

    # 估计单字平均时长，用于首尾锚点外推兜底
    durs = [max(0.0, float(c.end) - float(c.start)) for c in spoken
            if float(c.end) > float(c.start) + 1e-6]
    avg_dur = (min(max(sum(durs) / len(durs), 0.15), 0.50) if durs else 0.25)

    out_items: list[Any] = [None] * total

    def _fill_gap(o_start: int, o_end: int, t_left: float, t_right: float) -> None:
        count = o_end - o_start
        if count <= 0:
            return
        if t_right <= t_left + 1e-6:
            t_right = t_left + min(avg_dur, 0.15) * count
        step = min((t_right - t_left) / count, MAX_REPAIR_CHAR_SECONDS)
        for offset in range(count):
            tok = official[o_start + offset][1]
            s = max(0.0, t_left + step * offset)
            e = max(s, t_left + step * (offset + 1))
            out_items[o_start + offset] = AlignedChar(tok, s, e)

    # 先填精确锚点
    for o_idx, s_idx in anchors:
        sc = spoken[s_idx]
        out_items[o_idx] = AlignedChar(
            official[o_idx][1], float(sc.start), float(sc.end),
            tuple(getattr(sc, "parts", ()) or ()),
            getattr(sc, "confidence", None),
        )

    # 首锚点之前
    first_o, first_s = anchors[0]
    if first_o > 0:
        t_right = float(spoken[first_s].start)
        if first_s > 0:
            t_left = float(spoken[0].start)
        else:
            t_left = max(0.0, t_right - avg_dur * first_o)
        _fill_gap(0, first_o, t_left, t_right)

    # 相邻锚点之间
    for (o_a, s_a), (o_b, s_b) in zip(anchors, anchors[1:]):
        if o_b <= o_a + 1:
            continue
        t_left = float(spoken[s_a].end)
        t_right = float(spoken[s_b].start)
        if t_right <= t_left + 1e-6:
            t_left = float(spoken[s_a].start)
            t_right = float(spoken[s_b].end)
        _fill_gap(o_a + 1, o_b, t_left, t_right)

    # 末锚点之后
    last_o, last_s = anchors[-1]
    if last_o + 1 < total:
        t_left = float(spoken[last_s].end)
        if last_s + 1 < len(spoken):
            t_right = float(spoken[-1].end)
        else:
            t_right = t_left + avg_dur * (total - last_o - 1)
        _fill_gap(last_o + 1, total, t_left, t_right)

    return out_items, line_ids


def retime_lines_by_chars(lines: Sequence[Any], chars: Sequence[Any]) -> list[Any]:
    """无时间戳的官方词（TXT）借用已对齐字符的时间：**序列锚定 + 锚间插值**。

    旧实现用全局字数比例 `k = j * N / M` 硬映射：只要 ASR 在桥段多听和声或漏听一段 rap，
    整首歌的官方词都会整体漂移。现在先做官方词 ↔ 识别词的全局编辑距离对齐，把精确匹配的字
    当硬锚点直接继承真实时间戳，只在相邻锚点之间做局部时间插值；同时英文整词保留不再拆成字母。
    """
    retimed, _line_ids = _retime_official_with_lines(lines, chars)
    return retimed


def anchor_lyric_lines(lines: Sequence[Any], chars: Sequence[Any]) -> list[Any]:
    """把无时间戳的官方 TXT 行锚定成带行首时间的 `LyricLine` 列表（供 TIFA 二次逐行精对齐）。"""
    from .asr import LyricLine

    retimed, line_ids = _retime_official_with_lines(lines, chars)
    if not retimed or not lines:
        return []
    first_start: dict[int, float] = {}
    for item, line_index in zip(retimed, line_ids):
        if line_index not in first_start:
            first_start[line_index] = float(item.start)
    anchored: list[LyricLine] = []
    last_time = 0.0
    for idx, line in enumerate(lines):
        text = str(getattr(line, "text", "") or "").strip()
        if not text:
            continue
        start = first_start.get(idx, last_time)
        start = max(last_time, start)
        anchored.append(LyricLine(start=start, text=text))
        last_time = start
    return anchored


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


#: 日语声学修补（方案 B）的护栏参数：
#: - 置信度门限：过滤 TIFA 漂移/低置信噪声，只修补真实唱出来的拍子；
#: - 切分最小时长：切开后前后两颗音符各自至少这么长（避免切出 30ms 碎片）；
#: - 短合并并字：总长太短切不开时，把拨音/元音/无声化短拍并进前音（SynthV 日语原生支持 `ない`/`べん`）；
#: - 补短音：仅针对孤立漏检的极短音拍（连续 ≥3 拍缺失视为整句漏扒，绝不凭空造音高）。
JA_REPAIR_MIN_CONFIDENCE = 0.25
JA_SPLIT_MIN_HALF_SECONDS = 0.065
JA_INSERT_MIN_SECONDS = 0.055
JA_INSERT_MAX_SECONDS = 0.28
JA_INSERT_MAX_GAP_SECONDS = 0.25
_JA_MERGEABLE_MORA = frozenset({
    "ん", "あ", "い", "う", "え", "お",
    "く", "き", "し", "す", "ひ", "ふ", "つ", "ち", "ぷ", "ぴ",
})


def _snap_sv_tick(tick: float) -> int:
    """对齐到 4 tick（SVP 的 480 PPQ = 我们 1920 PPQ ÷ 4），确保导出零叠音。"""
    return max(0, int(round(tick / 4.0)) * 4)


def _undo_repaired_notes(notes: list[dict[str, Any]]) -> None:
    """重填歌词前还原上一次日语声学修补改动，保证多次「重新识别」完全幂等。"""
    if not any(n.get("repaired") or "orig_dur" in n for n in notes):
        return
    restored: list[dict[str, Any]] = []
    for note in notes:
        kind = note.get("repaired")
        if kind == "insert":
            continue
        if kind == "split" and restored:
            parent = restored[-1]
            if "orig_dur" in parent:
                parent["dur"] = int(parent.pop("orig_dur"))
            else:
                parent["dur"] = int(parent.get("dur", 0)) + int(note.get("dur", 0))
            if "orig_raw_dur" in parent:
                parent["raw_dur"] = int(parent.pop("orig_raw_dur"))
            elif "raw_dur" in parent:
                parent["raw_dur"] = int(parent["raw_dur"]) + int(note.get("raw_dur", note.get("dur", 0)))
            continue
        note.pop("repaired", None)
        restored.append(note)
    for note in restored:
        note.pop("orig_dur", None)
        note.pop("orig_raw_dur", None)
    notes[:] = restored


def _is_mora_split_japanese(chars: Sequence[Any]) -> bool:
    texts = [str(getattr(c, "text", "") or "").strip() for c in chars]
    has_kana = any(_is_japanese(t) for t in texts)
    if not has_kana:
        return False
    return not any(_is_japanese(t) and _mora_count(t) > 1 for t in texts)


def _repair_japanese_notes(
    timeline: Timeline,
    notes: list[dict[str, Any]],
    chars: Sequence[Any],
    fill: LyricFill,
) -> None:
    """方案 B（混合修补）：用 TIFA 10ms 声学拍边界修补 SheetSage2 在日语上的两类漏检：

    1. **同音高合拍**（如 `ず+ん`、`む+ひ`、`な+い`、`べ+ん`）：
       - 音符够长（切后两侧均 ≥ 65ms）→ 按 TIFA 第二拍起点一刀切成两颗同音高音符；
       - 音符太短切不开 → 并字到该音符（`ない` / `べん` / `ずん`），不丢拍也不碎谱。
    2. **孤立漏检的极短无声化拍**（如 `く` / `し` / `ひ` / `つ` 落在音符紧邻空档）：
       - 仅修补置信度达标（≥ 0.25）、非整句缺失（连续跳过 < 3 拍）、距邻音 ≤ 0.25s 的短拍；
       - 在空档处补一颗继承相邻音高的短音符（严格截断不叠音）。
    """
    if not notes or not chars or not _is_mora_split_japanese(chars):
        return

    spans = note_spans_seconds(timeline, notes)
    order = _build_order(spans)
    dp_heads, skipped_list = _align_chars_dp(order, spans, chars)
    if not skipped_list:
        return
    skipped_set = set(skipped_list)

    # 按时间序建邻接音符查询，确保切分/补音绝不越界叠音
    pos_in_order = {note_idx: pos for pos, note_idx in enumerate(order)}

    def _next_track_start(note_idx: int) -> float | None:
        pos = pos_in_order.get(note_idx)
        if pos is None or pos + 1 >= len(order):
            return None
        return spans[order[pos + 1]][0]

    def _prev_track_end(note_idx: int) -> float | None:
        pos = pos_in_order.get(note_idx)
        if pos is None or pos <= 0:
            return None
        return spans[order[pos - 1]][1]

    # 识别「连续 ≥3 拍缺失」的大段空白（如整句漏扒/轻声念白），绝不凭空造音符
    long_missing: set[int] = set()
    run_buf: list[int] = []
    for idx in sorted(skipped_set):
        if run_buf and idx == run_buf[-1] + 1:
            run_buf.append(idx)
        else:
            if len(run_buf) >= 3:
                long_missing.update(run_buf)
            run_buf = [idx]
    if len(run_buf) >= 3:
        long_missing.update(run_buf)

    # 每个原始音符最多切 1 刀，避免碎片化；记录新增音符统一合并回列表
    split_done: set[int] = set()
    merged_notes: set[int] = set()
    added_notes: list[dict[str, Any]] = []
    repaired_absorbed: list[str] = []
    repaired_dropped: list[str] = []

    for ci in sorted(skipped_set):
        if ci in long_missing:
            continue
        char = chars[ci]
        text = str(getattr(char, "text", "") or "").strip()
        if not (_is_japanese(text) and _mora_count(text) == 1):
            continue
        conf = getattr(char, "confidence", None)
        if conf is not None and float(conf) < JA_REPAIR_MIN_CONFIDENCE:
            continue
        c_start = float(getattr(char, "start", 0.0))
        c_end = max(c_start, float(getattr(char, "end", c_start)))
        c_dur = c_end - c_start

        prev_note_idx = dp_heads.get(ci - 1)
        next_note_idx = dp_heads.get(ci + 1)

        # ---- 情况 1：落在前一个已挂字音符内（或紧贴其尾端 ≤ 60ms） → 切分或并字 ----
        if prev_note_idx is not None and prev_note_idx not in split_done:
            p_start, p_end = spans[prev_note_idx]
            p_note = notes[prev_note_idx]
            p_lyric = str(p_note.get("lyric") or "").strip()
            inside_prev = p_start - 1e-6 <= c_start <= p_end + 1e-6
            tail_touch = (p_end < c_start <= p_end + 0.06) and c_dur <= JA_INSERT_MAX_SECONDS
            if inside_prev and _is_japanese(p_lyric) and _mora_count(p_lyric) == 1:
                next_t = _next_track_start(prev_note_idx)
                right_end_sec = p_end
                if c_end > p_end:
                    right_end_sec = min(c_end, next_t) if next_t is not None else c_end
                    right_end_sec = max(right_end_sec, p_end)
                left_dur_sec = c_start - p_start
                right_dur_sec = right_end_sec - c_start
                if (left_dur_sec >= JA_SPLIT_MIN_HALF_SECONDS
                        and right_dur_sec >= JA_SPLIT_MIN_HALF_SECONDS):
                    orig_tick = int(p_note.get("tick", 0))
                    orig_dur = int(p_note.get("dur", 0))
                    orig_raw_tick = int(p_note.get("raw_tick", orig_tick))
                    orig_raw_dur = int(p_note.get("raw_dur", orig_dur))
                    cut_tick = _snap_sv_tick(timeline.sec_to_tick(c_start))
                    end_tick = _snap_sv_tick(timeline.sec_to_tick(right_end_sec))
                    # 严格落在父音符起点之后、不越过下一音符起点
                    next_tick_limit = (
                        int(notes[order[pos_in_order[prev_note_idx] + 1]].get("tick", end_tick))
                        if pos_in_order[prev_note_idx] + 1 < len(order) else None
                    )
                    if next_tick_limit is not None:
                        end_tick = min(end_tick, next_tick_limit)
                    if cut_tick > orig_tick and end_tick > cut_tick:
                        p_note["orig_dur"] = orig_dur
                        p_note["orig_raw_dur"] = orig_raw_dur
                        p_note["dur"] = cut_tick - orig_tick
                        if "raw_dur" in p_note:
                            p_note["raw_dur"] = max(4, cut_tick - orig_raw_tick)
                        new_note: dict[str, Any] = {
                            "tick": cut_tick,
                            "dur": end_tick - cut_tick,
                            "pitch": int(p_note.get("pitch", 60)),
                            "raw_tick": cut_tick,
                            "raw_dur": end_tick - cut_tick,
                            "lyric": text,
                            "repaired": "split",
                        }
                        added_notes.append(new_note)
                        split_done.add(prev_note_idx)
                        fill.reasons["ja_split"] = fill.reasons.get("ja_split", 0) + 1
                        repaired_absorbed.append(text)
                        continue
                # 切开太碎 → 走同音并字（仅允许 1+1=2 拍，如 ない / べん / ずん / おく）
                if prev_note_idx not in merged_notes and (
                    text in _JA_MERGEABLE_MORA or c_dur <= 0.16
                ):
                    p_note["lyric"] = p_lyric + text
                    merged_notes.add(prev_note_idx)
                    fill.reasons["ja_merge"] = fill.reasons.get("ja_merge", 0) + 1
                    repaired_absorbed.append(text)
                    continue
            elif tail_touch and prev_note_idx not in merged_notes and _is_japanese(p_lyric) and _mora_count(p_lyric) == 1:
                # 紧贴前音尾端但落进空档：尝试优先补短音（走下面情况 2），不够空档再并字
                pass

        # ---- 情况 2：落在相邻音符之间的空档里（孤立短拍 / 无声化音） → 补一颗同音高短音符 ----
        if not (JA_INSERT_MIN_SECONDS <= c_dur <= JA_INSERT_MAX_SECONDS):
            continue
        anchor_idx: int | None = None
        min_gap = float("inf")
        if prev_note_idx is not None:
            gap_prev = abs(c_start - spans[prev_note_idx][1])
            if gap_prev < min_gap:
                min_gap, anchor_idx = gap_prev, prev_note_idx
        if next_note_idx is not None:
            gap_next = abs(spans[next_note_idx][0] - c_end)
            if gap_next < min_gap:
                min_gap, anchor_idx = gap_next, next_note_idx
        if anchor_idx is None or min_gap > JA_INSERT_MAX_GAP_SECONDS:
            continue

        # 计算该空档严格不叠音的合法区间 [left_bound, right_bound]
        left_bound = 0.0
        right_bound: float | None = None
        for note_i in order:
            ns, ne = spans[note_i]
            if ne <= c_start + 0.02:
                left_bound = max(left_bound, ne)
            elif ns >= c_start - 0.02:
                right_bound = ns if right_bound is None else min(right_bound, ns)
                break
        slot_start = max(c_start, left_bound)
        slot_end = min(c_end, right_bound) if right_bound is not None else c_end
        if slot_end - slot_start < JA_INSERT_MIN_SECONDS:
            # 空档略窄但前面有余量时轻微前移（≤ 40ms），仍严格不叠音
            if right_bound is not None and right_bound - left_bound >= JA_INSERT_MIN_SECONDS:
                slot_end = min(c_end, right_bound)
                slot_start = max(left_bound, slot_end - max(c_dur, JA_INSERT_MIN_SECONDS))
        if slot_end - slot_start >= JA_INSERT_MIN_SECONDS:
            s_tick = _snap_sv_tick(timeline.sec_to_tick(slot_start))
            e_tick = _snap_sv_tick(timeline.sec_to_tick(slot_end))
            # 再做一次 tick 域严格不重叠校验
            for note_i in order:
                nt = int(notes[note_i].get("tick", 0))
                ne_t = nt + int(notes[note_i].get("dur", 0))
                if ne_t <= s_tick:
                    continue
                if nt >= e_tick:
                    break
                # 与某现有音符有 tick 交叉 → 截断
                if nt <= s_tick < ne_t:
                    s_tick = ne_t
                if s_tick < nt < e_tick:
                    e_tick = nt
            if e_tick - s_tick >= 4:
                pitch = int(notes[anchor_idx].get("pitch", 60))
                added_notes.append({
                    "tick": s_tick,
                    "dur": e_tick - s_tick,
                    "pitch": pitch,
                    "raw_tick": s_tick,
                    "raw_dur": e_tick - s_tick,
                    "lyric": text,
                    "repaired": "insert",
                })
                fill.reasons["ja_insert"] = fill.reasons.get("ja_insert", 0) + 1
                repaired_dropped.append(text)
                continue
        # 空档确实放不下且紧邻前音：退回并字（如 1+1=2 拍）
        if (prev_note_idx is not None and prev_note_idx not in merged_notes
                and prev_note_idx not in split_done and text in _JA_MERGEABLE_MORA):
            p_note = notes[prev_note_idx]
            p_lyric = str(p_note.get("lyric") or "").strip()
            if _is_japanese(p_lyric) and _mora_count(p_lyric) == 1:
                p_note["lyric"] = p_lyric + text
                merged_notes.add(prev_note_idx)
                fill.reasons["ja_merge"] = fill.reasons.get("ja_merge", 0) + 1
                repaired_dropped.append(text)

    if added_notes:
        notes.extend(added_notes)
        notes.sort(key=lambda n: (int(n.get("tick", 0)), int(n.get("pitch", 0))))
    # 同步刷新 fill 统计与 lyrics 列表
    if repaired_absorbed:
        for ch in repaired_absorbed:
            if ch in fill.absorbed_chars:
                fill.absorbed_chars.remove(ch)
    if repaired_dropped:
        for ch in repaired_dropped:
            if ch in fill.dropped_chars:
                fill.dropped_chars.remove(ch)
                fill.dropped = max(0, fill.dropped - 1)
                fill.assigned += 1
    fill.lyrics = [str(n.get("lyric") or "") for n in notes]


#: 中/粤语安全长合拍切分（不动空档、不并字）的护栏参数：
#: - 仅当被吞汉字落在前一个已挂字汉字音符内、TIFA 置信度达标且切后两侧均 ≥ 80ms 时才切一刀；
#: - 绝不在音符空档凭空补音（中/粤无日语母音无声化），也绝不把两个汉字并到同一音符。
CJK_SPLIT_MIN_CONFIDENCE = 0.35
CJK_SPLIT_MIN_HALF_SECONDS = 0.080
CJK_SPLIT_MIN_CHAR_DURATION = 0.050


def _is_cjk_hanzi(text: str) -> bool:
    return len(text) == 1 and "\u4e00" <= text <= "\u9fff"


def _repair_cjk_merged_notes(
    timeline: Timeline,
    notes: list[dict[str, Any]],
    chars: Sequence[Any],
    fill: LyricFill,
) -> None:
    """中/粤语安全规则：仅对高置信度的同音高长合拍（如快歌/说唱中被 SheetSage2 合并的连字）
    按 TIFA 边界切分为两颗同音高音符；不动空档，不并字，每颗原始音符最多切 1 刀。
    """
    if not notes or not chars:
        return
    texts = [str(getattr(c, "text", "") or "").strip() for c in chars]
    # 日语走上面的方案 B，纯英文不切音符
    if any(_is_japanese(t) for t in texts):
        return
    if not any(_is_cjk_hanzi(t) for t in texts):
        return

    spans = note_spans_seconds(timeline, notes)
    order = _build_order(spans)
    dp_heads, skipped_list = _align_chars_dp(order, spans, chars)
    if not skipped_list:
        return

    split_done: set[int] = set()
    added_notes: list[dict[str, Any]] = []
    repaired_absorbed: list[str] = []

    for ci in sorted(set(skipped_list)):
        char = chars[ci]
        text = str(getattr(char, "text", "") or "").strip()
        if not _is_cjk_hanzi(text):
            continue
        conf = getattr(char, "confidence", None)
        if conf is not None and float(conf) < CJK_SPLIT_MIN_CONFIDENCE:
            continue
        c_start = float(getattr(char, "start", 0.0))
        c_end = max(c_start, float(getattr(char, "end", c_start)))
        if c_end - c_start < CJK_SPLIT_MIN_CHAR_DURATION:
            continue

        prev_note_idx = dp_heads.get(ci - 1)
        if prev_note_idx is None or prev_note_idx in split_done:
            continue
        p_start, p_end = spans[prev_note_idx]
        p_note = notes[prev_note_idx]
        p_lyric = str(p_note.get("lyric") or "").strip()
        if not _is_cjk_hanzi(p_lyric):
            continue
        if not (p_start - 1e-6 <= c_start <= p_end + 1e-6):
            continue

        left_dur_sec = c_start - p_start
        right_dur_sec = p_end - c_start
        if (left_dur_sec < CJK_SPLIT_MIN_HALF_SECONDS
                or right_dur_sec < CJK_SPLIT_MIN_HALF_SECONDS):
            continue

        orig_tick = int(p_note.get("tick", 0))
        orig_dur = int(p_note.get("dur", 0))
        orig_end_tick = orig_tick + orig_dur
        orig_raw_tick = int(p_note.get("raw_tick", orig_tick))
        orig_raw_dur = int(p_note.get("raw_dur", orig_dur))
        cut_tick = _snap_sv_tick(timeline.sec_to_tick(c_start))
        if not (orig_tick < cut_tick < orig_end_tick):
            continue

        p_note["orig_dur"] = orig_dur
        p_note["orig_raw_dur"] = orig_raw_dur
        p_note["dur"] = cut_tick - orig_tick
        if "raw_dur" in p_note:
            p_note["raw_dur"] = max(4, cut_tick - orig_raw_tick)
        added_notes.append({
            "tick": cut_tick,
            "dur": orig_end_tick - cut_tick,
            "pitch": int(p_note.get("pitch", 60)),
            "raw_tick": cut_tick,
            "raw_dur": orig_end_tick - cut_tick,
            "lyric": text,
            "repaired": "split",
        })
        split_done.add(prev_note_idx)
        fill.reasons["cjk_split"] = fill.reasons.get("cjk_split", 0) + 1
        repaired_absorbed.append(text)

    if added_notes:
        notes.extend(added_notes)
        notes.sort(key=lambda n: (int(n.get("tick", 0)), int(n.get("pitch", 0))))
    if repaired_absorbed:
        for ch in repaired_absorbed:
            if ch in fill.absorbed_chars:
                fill.absorbed_chars.remove(ch)
    fill.lyrics = [str(n.get("lyric") or "") for n in notes]


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
    if not only_empty:
        _undo_repaired_notes(notes)
    spans = note_spans_seconds(timeline, notes)
    existing = [str(note.get("lyric") or "") for note in notes]
    fill = assign_lyrics(spans, chars, tolerance=tolerance, existing=existing,
                         only_empty=only_empty)
    for note, lyric in zip(notes, fill.lyrics):
        note["lyric"] = lyric
    if not only_empty:
        _repair_japanese_notes(timeline, notes, chars, fill)
        _repair_cjk_merged_notes(timeline, notes, chars, fill)
    return {"found": True, "track": track_id, "notes": len(notes),
            "fill": fill.as_dict(), "lyrics": fill.lyrics}
