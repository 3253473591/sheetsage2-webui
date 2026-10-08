"""音符时值量化：把落在任意 tick 上的音符搬回网格。

移植自 Vocal2Midi `inference/quant/smart_quantize.py`（Apache 2.0，
Copyright 2026 Xiantaidu）。**移植基准是 `reference/Vocal2Midi V2`，不是 V1**——
V1 把位移按"一小节"归一（4/4 下一小节 = 4 拍），位移代价被摊薄 16 倍，于是
`simplicity=0` 也压不住节拍阶梯：本来就在 16 分/32 分网格上的音符会被整体推走
半拍甚至一拍（实测《旷古回响》656 个音里 350 个被搬走、最大 1 拍；《光芒》
324/324 全被搬）。V2 把位移改成**按四分音符（一拍）归一**，位移代价恢复成
和节拍阶梯同一个量级，量化就只剩"就近吸附"。见 `docs/音符量化规格.md` §3。

相对 V2 参考实现，这一层只做两件事（算法本身逐位一致）：

1. 把"任意 PPQ → 引擎定点标度 → 任意 PPQ"的换算收进这一层。
2. 提供 `quantize_tracks()`：直接对 `project.json` 那种 `{"tick", "dur", "pitch"}`
   的音符表做量化，并给出人话摘要。

**网格步长 = 一拍 / 96**（PPQ1920 时 20 tick），与拍号无关，也不看小节。
V2 参考实现用的是"一拍 / 32"（只用 2 的幂）；本项目扩到 **96 = 32 × 3**，因为
只用 2 的幂的网格**永远表示不了三连音**（一拍/3 = 640、一拍/6 = 320 都不是它的整数倍），
配套把 `metric_bonus` 的阶梯也加了三连音两档 —— 否则 DP 会把三连音音挪到直线位置上。

算法要点（细节见 `docs/音符量化规格.md`）：

- 音符按时间排序，**相邻音符之间的静音也做成一条 gap 记录**，音符与静音串成
  一条头尾相接的链；重叠到摆不开会让链不可行 → 直接判失败，音符原样保留。
- 每条记录只有一个自由度：占几列 `j ∈ [max(1, dcols-16), dcols+16]`。
- 打分最大化：起点/终点用"节拍重要性阶梯 − 0.5·位移²"，时长用"−位移²"，
  **距离一律按四分音符（一拍）归一**，不是按小节。
- 节拍重要性阶梯 `M(p)` 只看 `p = 绝对位置 mod 一拍`（小节头与普通拍头同为 0 分，
  没有小节级层次）：拍头 0、八分 −0.05λ、十六分 −0.1λ、32 分 −0.2λ、64 分 −0.4λ、
  **三连音位（1/6、1/3 拍）−0.5λ**、其余 −0.8λ。
- 两条 DP 表 `s0[r][c]`（记录 r 从列 c 开始）/ `s1[r][c]`（到列 c 结束），
  **最后一条的终点由 DP 自己挑最优列**（V1 是强制在链跨度那一列），再回溯。

量化后的边界全部落在网格上，且每条记录的终点 = 下一条的起点
（**包括静音**，所以整条旋律是连续的，不会因为量化多出/少掉空拍）。

参考实现的常数（QUARTER = 705,600,000 tick，GRID = QUARTER/32 = 22,050,000 tick）在
**内部标度**上使用，不随输入 PPQ 改变，因此不同 PPQ 的结果逐 tick 一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence

# --------------------------------------------------------------- 定点标度
#: 内部标度里一个四分音符（一拍）的 tick 数。取自参考实现，保持逐位一致。
#: **注意**：V1 参考实现把这个数叫 `BAR`（当成 4/4 的一小节），V2 改名 `QUARTER`
#: 并明确它是**一拍**——这正是 V2 修掉的那个口径错误。本项目沿用 V2 的 `QUARTER`；
#: 数值没变，所以「任意 PPQ → 内部标度」的换算与老版本逐位相同。
QUARTER = 705_600_000
#: 内部标度的网格步长：一拍的 1/96（= 32 分 × 三连音，同时覆盖直线族与三连音族）。
GRID = QUARTER // 96             # 7_350_000
#: 时长/窗口的搜索带宽（±16 列）。
BAND = 16
#: **单音位移上限**（列）。量化只做"就近吸附"，不许把音搬家：每一列的可用窗口
#: 还会被收到 `原始列 ± MAX_SHIFT_COLS`。
#:
#: 参考实现（V1/V2）没有这个上限，只有 `BAND = ±16 列`——在"一拍/32"（60 tick）的
#: 老网格上是 ±16 个 32 分音符，在"一拍/96"（20 tick）的细网格上就是 **±320 tick**，
#: 太松了。松到会出事：每一档之间的位移代价只有 0.0009~0.014，而档位落差是
#: 0.05λ~0.4λ，DP 于是"挑最好的档"而不是"就近吸附"，整条链会被一起拖走。
#:
#: 取 2 列 = 40 tick（@PPQ1920），依据见 `docs/音符量化规格.md` §9：
#: - **够大**：实测真实工程的"量化前 onset 到最近网格点"距离 p100 = **10 tick**
#:   （中位数 0、p99 = 9），10 个工程 6927 个音里**没有一个**超过 40 tick。
#:   所以 40 tick 足够把真实音频吸回网格。
#: - **够小**：它小于"32 分位到 1/6 拍"的 80 tick，也远小于"16 分位到 1/3 拍"的
#:   160 tick，所以直线素材与三连音素材各自**原样通过**（判据见
#:   `tests/test_quantize.py::test_clip_criteria_*`）。
#:
#: 关掉它（传 `max_shift=None`）能让位移涨到 ±295 tick、平均 33.5 tick；
#: 收紧到 2 列是平均 6.3 tick、最大 59 tick，网格率仍然 100%。
MAX_SHIFT_COLS = 2
NEG_INF = -1.0e10
FAIL_THRESHOLD = -1.0e9
#: 内部标度下四分音符的 tick 数——算分时**所有距离都按它归一**。
TICKS_PER_QUARTER = QUARTER
#: V1 参考实现里的 `BAR`：数值等于本项目的 `QUARTER`（老文档/老口径的别名）。
#: 保留它是为了不破坏 `bar=` 关键字与既有的换算讨论，算分不再按"一小节"归一。
BAR = QUARTER                    # 705_600_000

#: 项目默认 PPQ（`timeline.PPQ`）。PPQ 是 32 的倍数时，网格步长就是整数 tick。
DEFAULT_PPQ = 1920
#: 网格步长 = 一拍的 1/`GRID_DIVISOR`。**32 × 3 = 96**，所以这个网格同时能表示
#: "直线族"（一拍/2^k：960、480、240…）和"三连音族"（一拍/3 = 640、一拍/6 = 320）——
#: 只用 2 的幂（32、64…）永远表示不了三连音。PPQ=1920 时为 20 tick。
GRID_DIVISOR = 96
#: 网格步长在项目标度下的 tick 数（PPQ=1920 时为 20 tick）。
GRID_TICKS_AT_DEFAULT_PPQ = DEFAULT_PPQ // GRID_DIVISOR


def lam_from_simplicity(s: float) -> float:
    """力度滑杆 `s ∈ [0,5]` → 权重 `λ = 0.5 × 10^(s/5)`（对数刻度）。"""
    return 0.5 * (10.0 ** (s / 5.0))


def metric_bonus(abs_pos: int, lam: float, beat: int = TICKS_PER_QUARTER) -> float:
    """节拍重要性阶梯 M(p)，`p = 绝对位置 mod 一拍`。

    **阶梯同时认"直线族"和"三连音族"**，因为这两族都是真实谱面上的位置：

    | 档 | p 的整除条件 | 分 |
    |---|---|---|
    | 拍头 | `beat` | 0 |
    | 8 分 | `beat/2` | −0.05λ |
    | 十六分 | `beat/4` | −0.1λ |
    | 32 分 | `beat/8` | −0.2λ |
    | 64 分 | `beat/16` | −0.4λ |
    | **8 分三连音（1/3、2/3 拍）** | `beat/3` | **−0.05λ** |
    | **16 分三连音（1/6、5/6 拍）** | `beat/6` | **−0.1λ** |
    | 其余 | — | −0.8λ |

    **三连音两档与网格是配套的**（网格 `GRID_DIVISOR = 96` 也覆盖这两族）。
    没有这两档时，1/3 拍落在兜底的 −0.8λ，DP 会花很小的位移代价把它挪到直线位置——
    实测《巴拉莱卡》569 个音里 479 个被搬走、126 个 1/3 拍与 167 个 2/3 拍**全部**
    被吸到 3/8 / 3/4 上，三连音节奏被抹平。

    档位取"与对应的直线档同级"：1/3 拍 ↔ 8 分（`beat/2`）、1/6 拍 ↔ 16 分（`beat/4`），
    都是 −0.05λ / −0.1λ。**这不会把直线位置反向吸过去**，因为网格够细（20 tick）：
    32 分位（240）想到 1/6 拍（320）要挪 80 tick、花 0.00087，而 1/6 拍档只比 32 分档
    好 0.05λ，收益 0.1λ 里还要扣掉"离 8 分档更远"的损失——净收益为正时才会移动，
    实测 32 分位的音**不动**（`test_triplets_do_not_pull_straight_positions`）。

    `beat` 是一拍的 tick 数（引擎标度下 = `TICKS_PER_QUARTER`）。V1 用的是
    "按小节判级"、V2 是"按一拍但只认直线"；三连音两档是本项目在 V2 之上的扩展。
    """
    p = abs_pos % beat
    if p == 0:
        return 0.0
    for div, weight in ((2, .05), (4, .1), (8, .2), (16, .4)):
        if p % (beat // div) == 0:
            return -weight * lam
    # 三连音档：先判"更大"的八分三连音（1/3 拍），再判十六分三连音（1/6 拍）
    if beat % 3 == 0 and p % (beat // 3) == 0:
        return -0.05 * lam
    if beat % 6 == 0 and p % (beat // 6) == 0:
        return -0.1 * lam
    return -0.8 * lam


def metric_level(abs_pos: int, beat: int = TICKS_PER_QUARTER) -> str:
    """调试用：位置落在哪一级拍点上（V2 命名，另加三连音两档）。"""
    p = abs_pos % beat
    if p == 0:
        return "beat"
    for div, name in ((2, "8th"), (4, "16th"), (8, "32nd"), (16, "64th")):
        if p % (beat // div) == 0:
            return name
    if beat % 3 == 0 and p % (beat // 3) == 0:
        return "8th-triplet"
    if beat % 6 == 0 and p % (beat // 6) == 0:
        return "16th-triplet"
    return "128th"


@dataclass
class Result:
    """量化结果。`ok=False` 时 `notes` 为空，调用方应保留原音符。"""

    ok: bool
    notes: list[tuple[int, int]] = field(default_factory=list)   # 新的 (start, end)
    detail: list[dict] = field(default_factory=list)             # 每条记录的调试信息
    message: str = ""
    moved: int = 0
    total: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(ok=self.ok, message=self.message, moved=self.moved, total=self.total)


# --------------------------------------------------------------- 核心算法
_OVERLAP_HINT = ("量化失败：相邻音符重叠（或时值异常）让节奏链在网格上摆不开，"
                 "音符保持原样。参考实现同样把重叠视为链不可行，而不是裁剪音符。")


def _why_failed(S0: Sequence[Mapping[int, float]],
                S1: Sequence[Mapping[int, float]]) -> str:
    """DP 判失败后说明"死在第几条记录上"。纯诊断，不改结果。"""
    for r in range(len(S0)):
        alive0 = any(v > NEG_INF for v in S0[r].values())
        alive1 = any(v > NEG_INF for v in S1[r].values())
        if not alive0 and not alive1:
            return f"{_OVERLAP_HINT}（第 {r + 1} 条记录无从落列）"
        if not alive1:
            return f"{_OVERLAP_HINT}（第 {r + 1} 条记录起点合法但终点放不下）"
    return _OVERLAP_HINT + "（末条记录没有任何可用的结束列）"


def smart_quantize(
    notes: Sequence[tuple[int, int]],
    simplicity: float = 0.0,
    *,
    beat: int = TICKS_PER_QUARTER,
    grid_divisor: int = GRID_DIVISOR,
    grid_span: int | None = None,
    bar: int | None = None,
    max_shift: int | None = MAX_SHIFT_COLS,
) -> Result:
    """核心算法。`notes` 是内部定点标度下的 `(start, end)`。

    `beat` 是**一拍**的 tick 数，决定节拍重要性阶梯 M(p) 的周期 —— 也就是"什么位置
    算强拍"。

    `grid_divisor` 是网格的分辨率：网格步长 = `beat / grid_divisor`，默认
    **96 = 32 × 3**，所以网格同时覆盖直线族（一拍/2^k）与三连音族（一拍/3、一拍/6）。

    `grid_span` 是另一种给网格的办法：**网格步长 = `grid_span / grid_divisor`**
    （默认 `beat * grid_divisor`，即"一拍"）。`bar` 是 V1 时代的旧关键字，语义等于
    `grid_span`；`grid_span` 与 `bar` 都给会报错。

    `max_shift` 是**单音位移上限（列）**，默认 `MAX_SHIFT_COLS = 4`（80 tick）。
    传 `None` 关掉上限，退回参考实现的行为（见 `MAX_SHIFT_COLS` 的注释：
    关掉它 DP 会"挑最好的档"而不是"就近吸附"，把整条链拖走）。

    **算分里的所有距离一律按 `TICKS_PER_QUARTER`（一拍）归一**，与网格无关。
    这是 V1→V2 唯一实质性的改动：V1 按"一小节"归一，4/4 下位移代价被摊薄 16 倍，
    节拍阶梯于是压过一切，把对好的音也推走。
    """
    if bar is not None:
        if grid_span is not None:
            raise ValueError("`bar` 与 `grid_span` 是同一个东西，只能给一个")
        grid_span = bar
    lam = lam_from_simplicity(simplicity)
    if not notes:
        return Result(ok=False, message="没有音符")

    beat = int(beat)
    grid_divisor = int(grid_divisor)
    if beat <= 0 or grid_divisor <= 0:
        raise ValueError(f"`beat` / `grid_divisor` 必须为正，收到 {beat} / {grid_divisor}")
    if beat % grid_divisor:
        raise ValueError(
            f"一拍必须能被网格分辨率整除（网格步长 = beat/{grid_divisor} 要是整数），"
            f"收到 beat={beat}")
    if grid_span is None:
        grid_span = beat
    grid_span = int(grid_span)
    if grid_span <= 0 or grid_span % grid_divisor:
        raise ValueError(
            f"网格跨度必须是 {grid_divisor} 的倍数，收到 {grid_span}")
    grid = grid_span // grid_divisor
    norm = float(TICKS_PER_QUARTER)          # 距离归一的基准：一拍
    band = BAND

    # ---- 1. 记录链（排序 + 静音记录） ----
    order = sorted(range(len(notes)), key=lambda i: notes[i][0])
    records: list[tuple[int, int, int | None]] = []
    for i in order:
        s, e = notes[i]
        e = max(s, e)                                   # 终点不早于起点
        if records and s > records[-1][1]:
            records.append((records[-1][1], s, None))   # gap record（静音）
        records.append((s, e, i))
    R = len(records)

    # ---- 2. 锚点与总列数 ----
    first = records[0][0]
    if first >= 0:
        anchor = (first + grid // 2) // grid * grid
    else:
        anchor = -(((-first) + grid // 2) // grid * grid)
    rel = [(s - anchor, e - anchor) for s, e, _ in records]
    span = rel[-1][1]
    if span > 0:
        col_count = (span + grid // 2) // grid            # 四舍五入（半数进位）
    elif span < 0:
        col_count = -((-span + grid // 2) // grid)
    else:
        col_count = 0
    if col_count <= 0:
        return Result(ok=False, message="跨度退化（全部音符挤在同一个网格点）")

    # ---- 合法列窗口：先按 ±band 列，再收到"原始列 ± max_shift 列" ----
    # 后者是"不许搬家"的硬保证：没有它，DP 会为了让音升一档而跨过整个 1/3 拍。
    lo = [max(0, r0 // grid - band) for r0, _ in rel]      # python // = 向下取整
    hi = [min(col_count, r1 // grid + band) for _, r1 in rel]
    if max_shift is not None and max_shift >= 0:
        for r in range(R):
            lo[r] = max(lo[r], rel[r][0] // grid - max_shift, 0)
            hi[r] = min(hi[r], rel[r][1] // grid + max_shift + 1)
            hi[r] = max(lo[r], hi[r])
    else:
        hi = [max(l, h) for l, h in zip(lo, hi)]

    # ---- 可行性预检 ----
    # 参考实现在窗口为空时 `for c in range(wlo, whi)` 一次都不进，那张 DP 表就是空的，
    # 后续回溯可能拿出"越界"的结果。这里先把死局挡掉并明确报原因：
    #   某条记录连一个合法列都没有（hi <= lo）；
    #   整条链摆不开的情形由后面的 DP 状态诊断给出。
    for r in range(R):
        if hi[r] <= lo[r]:
            what = "音符" if records[r][2] is not None else "静音"
            return Result(ok=False, total=len(notes),
                          message=f"量化失败：第 {r + 1} 条记录（{what}）没有合法的网格窗口")

    # ---- 3/4/5. DP ----
    S0: list[dict[int, float]] = [dict() for _ in range(R)]   # 记录 r 从列 c 开始的最优分
    S1: list[dict[int, float]] = [dict() for _ in range(R)]   # 记录 r 到列 c 结束的最优分
    BP: list[dict[int, int | None]] = [dict() for _ in range(R)]   # s1 回溯：记录 r 的起始列

    dur_ticks = [r1 - r0 for r0, r1 in rel]

    for r in range(R):
        wlo, whi = lo[r], hi[r]
        dcols = dur_ticks[r] // grid
        j_lo = max(1, dcols - band)
        j_hi = dcols + band                                     # 右开
        for c in range(wlo, whi):
            # ---- s0[r][c] ----
            # 距离一律按**一拍**归一（`norm`），不是按小节：这是 V1→V2 的关键改动。
            if r == 0:
                if c == 0:
                    d = abs(0 - rel[0][0])                      # 锚点取整的残差
                    s0 = metric_bonus(anchor, lam, beat) - 0.5 * (d / norm) ** 2
                else:
                    s0 = NEG_INF
            else:
                prev = S1[r - 1].get(c - 1, NEG_INF)
                pos = anchor + grid * c
                d = abs(grid * c - rel[r][0])
                s0 = prev + metric_bonus(pos, lam, beat) - 0.5 * (d / norm) ** 2
            S0[r][c] = s0 if s0 > NEG_INF else NEG_INF

            # ---- s1[r][c]：在 j 带里挑起始列 ----
            best, best_start = NEG_INF, None
            for j in range(j_lo, j_hi):
                scol = c - j + 1
                if scol < wlo:
                    break                                       # j 变大时起始列单调变小
                cand = S0[r].get(scol, NEG_INF)
                if cand <= NEG_INF:
                    continue
                dd = abs(grid * j - dur_ticks[r])
                cand -= (dd / norm) ** 2                        # 时长失真代价
                if cand > best:
                    best, best_start = cand, scol
            end_pos = anchor + grid * (c + 1)                   # 开区间终点
            d_end = abs(grid * (c + 1) - rel[r][1])
            s1 = best - 0.5 * (d_end / norm) ** 2 + metric_bonus(end_pos, lam, beat)
            S1[r][c] = s1 if s1 > NEG_INF else NEG_INF
            BP[r][c] = best_start

    # ---- 6. 末条记录的终点**由 DP 自己挑**（V2：不再强制在 colCount-1），回溯 ----
    if not S1[R - 1]:
        return Result(ok=False, total=len(notes), message=_why_failed(S0, S1))
    last_col, final = max(S1[R - 1].items(), key=lambda kv: kv[1])
    if final < FAIL_THRESHOLD:
        return Result(ok=False, total=len(notes), message=_why_failed(S0, S1))

    start_cols = [0] * R
    end_col = last_col
    for r in range(R - 1, -1, -1):
        sc = BP[r][end_col]
        if sc is None:
            return Result(ok=False, total=len(notes), message=_why_failed(S0, S1))
        start_cols[r] = sc
        end_col = sc - 1                                        # 上一条的结束列

    # ---- 7. 写回（头尾相接；最后一条的终点 = DP 挑的那一列） ----
    new_pairs: list[tuple[int, int]] = []
    for r in range(R):
        start = anchor + grid * start_cols[r]
        if r < R - 1:
            end = anchor + grid * start_cols[r + 1]
        else:
            end = anchor + grid * col_count
        new_pairs.append((start, end))

    out: list[tuple[int, int]] = [(0, 0)] * len(notes)
    detail = []
    for r, (_, _, idx) in enumerate(records):
        if idx is not None:
            out[idx] = new_pairs[r]
        detail.append({
            "record": r,
            "note_index": idx,                     # None = 静音记录
            "orig": records[r][:2],
            "new": new_pairs[r],
            "start_col": start_cols[r],
            "start_level": metric_level(new_pairs[r][0], beat),
        })

    moved = sum(1 for r, (_, _, idx) in enumerate(records)
                if idx is not None and new_pairs[r] != records[r][:2])
    return Result(
        ok=True,
        notes=out,
        detail=detail,
        moved=moved,
        total=len(notes),
        message=f"λ={lam_from_simplicity(simplicity):.4g}，{moved}/{len(notes)} 个音符被移动",
    )


def smart_quantize_musical(
    notes: Sequence[tuple[int, int]],
    simplicity: float = 0.0,
    ticks_per_quarter: int = DEFAULT_PPQ,
    *,
    bar_quarters: float | None = None,
    max_shift: int | None = MAX_SHIFT_COLS,
) -> Result:
    """任意 PPQ → 内部标度 → 量化 → 换回原 PPQ。

    量化后的边界落在网格上，所以只要 PPQ 是 32 的倍数，来回换算就是精确的。

    **网格步长 = 一拍 / 96**（PPQ1920 时 20 tick），与拍号无关 —— 拍号不参与量化，
    所以 `bar_quarters` 已经不用了，留作旧的兼容参数（给了也不影响结果）。

    `96 = 32 × 3` 是刻意的：只用 `2 的幂` 当网格永远表示不了三连音
    （一拍/3 = 640 tick、一拍/6 = 320 tick 都不是 60/240 的整数倍），
    而 96 同时覆盖直线族与三连音族。配套地，`metric_bonus` 的阶梯也加了三连音两档，
    否则 DP 仍旧会把三连音音挪到直线位置上（见 `metric_bonus` 的注释）。

    早先还踩过另一个坑：网格写成"**一小节**/32"，6/8 下会变成 180 tick，
    与模型 onset 的 60 tick 格子对不上，《相思》128/214 个音因此被搬 ≥180 tick。
    """
    if ticks_per_quarter <= 0:
        raise ValueError("ticks_per_quarter 必须为正")
    if ticks_per_quarter % GRID_DIVISOR:
        raise ValueError(
            f"PPQ 必须是 {GRID_DIVISOR} 的倍数（= 一拍的 1/{GRID_DIVISOR} 才是整数 tick），"
            f"收到 {ticks_per_quarter}")

    # `beat` 是一拍（阶梯周期），`grid_divisor` 给网格分辨率：
    # 两者都由 PPQ 决定，与拍号无关。
    scale = Fraction(TICKS_PER_QUARTER, ticks_per_quarter)
    same_scale = scale == 1
    if same_scale:
        eng = [(int(s), int(e)) for s, e in notes]
    else:
        eng = [(int(Fraction(s) * scale), int(Fraction(e) * scale)) for s, e in notes]

    res = smart_quantize(eng, simplicity=simplicity,
                         beat=TICKS_PER_QUARTER, grid_divisor=GRID_DIVISOR,
                         max_shift=max_shift)
    if res.ok:
        if same_scale:
            res.notes = [(int(s), int(e)) for s, e in res.notes]
        else:
            inv = Fraction(ticks_per_quarter, TICKS_PER_QUARTER)
            res.notes = [(int(Fraction(s) * inv), int(Fraction(e) * inv)) for s, e in res.notes]
            for d in res.detail:
                d["orig"] = tuple(int(Fraction(x) * inv) for x in d["orig"])
                d["new"] = tuple(int(Fraction(x) * inv) for x in d["new"])
    return res


# --------------------------------------------------------------- 项目适配
def bar_quarters_from_timeline(timeline: Any) -> float:
    """从 `Timeline` 取"一小节等于几个四分音符"（弱起小节按自己实际长度算）。

    小节表的 tick 跨度 = 拍号 × 全音符 tick / 分母，而四分音符 tick = PPQ，
    所以 `bar_quarters = 小节 tick / PPQ`。
    """
    measures = list(getattr(timeline, "measures", []) or [])
    if not measures:
        return 4.0
    ppq = int(getattr(timeline, "ppq", DEFAULT_PPQ) or DEFAULT_PPQ)
    full = [m for m in measures if not getattr(m, "is_pickup", False)] or measures
    span = max(m.ticks for m in full)
    if span <= 0:
        return 4.0
    return span / ppq


@dataclass
class QuantizeStats:
    """一次量化的汇总，进报告用。"""

    simplicity: float
    tracks: int = 0
    notes: int = 0
    moved: int = 0
    failed_tracks: int = 0
    #: 旧字段，只为兼容老的 `project.json` / `report.json` 读者：量化已经**不看拍号**，
    #: 这个值只剩"当时工程的小节长度"这一层记录意义。新代码别再用它做判断。
    picked_bar_quarters: float = 4.0
    #: 网格步长 = **一拍 / 96**（项目 tick）。PPQ1920 时 20 tick，同时覆盖
    #: 直线族与三连音族。
    grid_ticks: int = GRID_TICKS_AT_DEFAULT_PPQ
    messages: list[str] = field(default_factory=list)

    @property
    def moved_ratio(self) -> float:
        return self.moved / self.notes if self.notes else 0.0

    @property
    def placement_ticks(self) -> int:
        """落点分辨率。与 `grid_ticks` 是同一个数，保留它只是为了老读者的兼容。"""
        return self.grid_ticks

    def as_dict(self) -> dict[str, Any]:
        return dict(enabled=True, simplicity=self.simplicity, tracks=self.tracks,
                    notes=self.notes, moved=self.moved,
                    moved_ratio=round(self.moved_ratio, 4),
                    failed_tracks=self.failed_tracks,
                    bar_quarters=round(self.picked_bar_quarters, 4),
                    grid_ticks=self.grid_ticks,
                    placement_ticks=self.placement_ticks,
                    messages=self.messages)


def quantize_tracks(
    tracks: Iterable[Mapping[str, Any]],
    *,
    tick_length: int | None = None,
    simplicity: float = 0.0,
    ticks_per_quarter: int = DEFAULT_PPQ,
    bar_quarters: float | None = None,
) -> tuple[list[list[dict[str, Any]]], QuantizeStats]:
    """对 `project.json` 的分轨音符做量化。

    `tracks` 是 `[{"id": ..., "notes": [{"tick", "dur", "pitch"}, ...]}, ...]`。
    就地风格：返回新的音符表列表，**原数据不动**。

    单条轨道一条记录链，所以只对"单声部"轨道成立——`vocal` / `instrumental`
    各自是单声部，符合前提。轨道内音符重叠会让量化判失败，此时该轨保持原样。

    **网格步长 = 一拍的 1/96**（= `ticks_per_quarter / 96`，PPQ1920 时 20 tick），
    与拍号无关；`bar_quarters` 已经不用了，只是为了兼容老调用方仍然接受
    （给了也不影响结果）。
    """
    stats = QuantizeStats(simplicity=simplicity,
                          picked_bar_quarters=float(bar_quarters or 4.0))
    # 网格步长（项目 tick）= 一拍 / 96（32 × 3，直线族 + 三连音族），与拍号无关
    stats.grid_ticks = max(1, int(round(ticks_per_quarter / GRID_DIVISOR)))
    out_tracks: list[list[dict[str, Any]]] = []

    for track in tracks:
        notes = list(track.get("notes") or [])
        stats.tracks += 1
        stats.notes += len(notes)
        if len(notes) < 2:
            out_tracks.append([dict(n) for n in notes])
            continue

        # 定序：量化按起点排序，回来时按原下标写回
        order = sorted(range(len(notes)), key=lambda i: (notes[i]["tick"], notes[i]["pitch"]))
        pairs = [(int(notes[i]["tick"]), int(notes[i]["tick"]) + max(1, int(notes[i]["dur"])))
                 for i in order]
        result = smart_quantize_musical(pairs, simplicity=simplicity,
                                       ticks_per_quarter=ticks_per_quarter)
        if not result.ok:
            stats.failed_tracks += 1
            stats.messages.append(f"{track.get('id')}: {result.message}")
            out_tracks.append([dict(n) for n in notes])
            continue

        stats.moved += result.moved
        stats.messages.append(f"{track.get('id')}: {result.message}")
        new_notes = [dict(n) for n in notes]
        for slot, i in enumerate(order):
            start, end = result.notes[slot]
            old = new_notes[i]
            # 把**量化前的位置和时长**也留下来。小节表（秒 ↔ tick）是按模型自己的音符算
            # 出来的，所以"原始 tick"才是这个音在曲子里真实的位置；量化后的 tick 是为了
            # 落网格。填词（逐字时间戳）必须对着原始跨度匹配，否则字会被吸到错的音上——
            # 实测《光芒》量化后填词"显示不出来"的字从 29 涨到 60，《相思》18 涨到 42。
            old["raw_tick"] = int(old["tick"])
            old["raw_dur"] = int(old["dur"])
            old["tick"] = int(start)
            old["dur"] = int(max(1, end - start))
        if tick_length:
            for note in new_notes:                       # 不许越过工程末尾
                note["tick"] = max(0, min(note["tick"], tick_length - 1))
                note["dur"] = int(max(1, min(note["dur"], tick_length - note["tick"])))
        new_notes.sort(key=lambda n: (n["tick"], n["pitch"]))
        out_tracks.append(new_notes)

    return out_tracks, stats


def note_shift_seconds(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    timeline: Any,
) -> dict[str, float]:
    """量化把音符搬了多远（秒），用来判断"量化是否把谱子改得面目全非"。

    `before` 必须是**逐音对应**的原始表（调用方先按同一次排序把两轨拼起来，不要各排各的）。
    位移的实际上界：每条记录的搜索带宽是 ±16 列 = 半个 32 分音符，换算到秒就是
    `0.5 × 网格步长`（4/4、120 BPM 时约 0.06s，`朋友的酒` 实测上限 0.25s）。
    明显更大的值只可能来自"节拍阶梯让 DP 用位移换强拍点"。
    """
    if not before or len(before) != len(after):
        return {}
    shifts = []
    for old, new in zip(before, after):
        try:
            shifts.append(abs(timeline.tick_to_sec(new["tick"]) - timeline.tick_to_sec(old["tick"])))
        except Exception:                                # 小节表为空等：不值得为报告崩掉
            return {}
    if not shifts:
        return {}
    shifts.sort()
    return {"mean": round(sum(shifts) / len(shifts), 4),
            "median": round(shifts[len(shifts) // 2], 4),
            "p90": round(shifts[int(len(shifts) * 0.9)], 4),
            "max": round(shifts[-1], 4)}
