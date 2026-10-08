r"""`quantize` 模块的回归测试（P0：音符时值量化）。

量化要同时满足三件事，测试就盯这三条：

1. **落到网格**：所有边界都是网格步长的整数倍（PPQ=1920、4/4 时 240 tick）。
2. **改动最小**：本来就在网格上的旋律，量化后逐 tick 不变（幂等）。
   这一条在 V1 口径下**不成立**——V1 把位移按"一小节"归一，位移代价被摊薄 256 倍，
   节拍阶梯于是把对好的 16 分/32 分音符也推走（见 `test_v1_scale_...` 那组反例）。
3. **退化输入安全**：摆不开的输入 → 判失败、原样保留，而不是给出坏结果。

    .\.venv\Scripts\python.exe -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.quantize import (  # noqa: E402
    BAR,
    BAND,
    GRID,
    GRID_DIVISOR,
    MAX_SHIFT_COLS,
    QUARTER,
    TICKS_PER_QUARTER,
    bar_quarters_from_timeline,
    lam_from_simplicity,
    metric_bonus,
    metric_level,
    quantize_tracks,
    smart_quantize,
    smart_quantize_musical,
)
from sheetsage2_webui.timeline import from_playback  # noqa: E402

PPQ = 1920
GRID_TICKS = PPQ // GRID_DIVISOR   # 网格步长 = 一拍/96 = 20 tick（32 分 × 三连音）


def playback(*measures):
    rows = []
    for index, (start, end) in enumerate(measures):
        rows.append(dict(index=index, start=start, end=end,
                         score_start=float(index), score_end=float(index + 1),
                         leading_rest=0.0, trailing_rest=0.0))
    return {"measures": rows, "duration": measures[-1][1] if measures else 0.0}


# --------------------------------------------------------------- 引擎常数
def test_engine_constants_match_the_reference():
    """定点标度不能改；网格分辨率 = 96 = 32 × 3（直线族 + 三连音族）。

    V1 把这个数叫 `BAR`（当成 4/4 的一小节），V2 改名 `QUARTER` 并明确它是**一拍**。
    数值没变（705,600,000），所以「任意 PPQ → 内部标度」的换算与老版本逐位相同；
    变的是**算分时拿谁做归一**——见 `test_v1_scale_...`。
    """
    assert QUARTER == 705_600_000
    assert BAR == QUARTER            # V1 的旧别名，数值相同
    assert GRID_DIVISOR == 96        # 32 × 3：只用 2 的幂表示不了三连音
    assert GRID == QUARTER // GRID_DIVISOR == 7_350_000
    assert TICKS_PER_QUARTER == QUARTER
    assert BAND == 16
    # PPQ 1920 → 网格步长 20 tick
    assert GRID_TICKS == 20


def test_grid_represents_both_straight_and_triplet_positions():
    """网格必须同时落得下"直线族"和"三连音族"——这正是 `96 = 32 × 3` 的理由。

    一拍 1920 tick 时：直线族（1/2^k）在 960/480/240/120…，
    三连音族（n/3、n/6）在 640/1280、320/1600。两者都要是 20 的整数倍。
    """
    beat = PPQ
    straight = [beat // d for d in (1, 2, 4, 8, 16, 32)]
    triplet = [beat * n // 3 for n in (1, 2)] + [beat * n // 6 for n in (1, 5)]
    for pos in straight + triplet:
        assert pos % GRID_TICKS == 0, f"{pos} tick 不在网格上"
    # 反例：只有 2 的幂的网格（一拍/32 = 60）落不下三连音
    assert (beat // 3) % (beat // 32) != 0


def test_lambda_is_the_documented_log_scale():
    assert lam_from_simplicity(0.0) == pytest.approx(0.5)
    assert lam_from_simplicity(5.0) == pytest.approx(5.0)
    assert lam_from_simplicity(2.5) == pytest.approx(0.5 * 10 ** 0.5)


def test_metric_ladder_ranks_the_beat_above_everything():
    """阶梯按**一拍**判级：拍头 0 > 8 分 > 16 分 > 32 分 > 64 分 > 其余。"""
    lam = 1.0
    q = TICKS_PER_QUARTER
    assert metric_bonus(0, lam) == 0.0
    assert metric_bonus(q // 2, lam) == pytest.approx(-0.05)
    assert metric_bonus(q // 4, lam) == pytest.approx(-0.1)
    assert metric_bonus(q // 8, lam) == pytest.approx(-0.2)
    assert metric_bonus(q // 16, lam) == pytest.approx(-0.4)
    assert metric_bonus(q // 32, lam) == pytest.approx(-0.8)
    assert metric_bonus(q + q // 4, lam) == pytest.approx(-0.1)      # 跨拍仍按 mod 判级
    # 每一拍的拍头都是 0 分：阶梯**没有小节层次的额外加权**
    assert metric_bonus(q * 3, lam) == 0.0
    assert [metric_level(x) for x in (0, q // 2, q // 4, q // 8, q // 16, GRID)] == \
        ["beat", "8th", "16th", "32nd", "64th", "128th"]
    # 三连音两档：1/3 拍 ↔ 8 分（同为 −0.05λ）、1/6 拍 ↔ 16 分（同为 −0.1λ）。
    # 与网格（一拍/96）配套——没有它们，三连音会被当成"最差档"而吸到直线位置。
    assert metric_bonus(q // 3, lam) == pytest.approx(-0.05)
    assert metric_bonus(2 * q // 3, lam) == pytest.approx(-0.05)
    assert metric_bonus(q // 6, lam) == pytest.approx(-0.1)
    assert metric_bonus(5 * q // 6, lam) == pytest.approx(-0.1)
    assert [metric_level(x) for x in (q // 3, 2 * q // 3, q // 6)] == \
        ["8th-triplet", "8th-triplet", "16th-triplet"]


# --------------------------------------------------------------- 基本性质
def test_quantized_boundaries_all_land_on_the_grid():
    notes = [(0, 1920), (1920, 2880), (3000, 5000), (5000, 5000), (6000, 7680)]
    result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)

    assert result.ok
    assert len(result.notes) == len(notes)
    for start, end in result.notes:
        assert start % GRID_TICKS == 0 and end % GRID_TICKS == 0
        assert end > start


def test_melody_already_on_a_regular_grid_is_left_untouched():
    """幂等性：网格规整的旋律，量化不该动手（这是"重跑流水线结果稳定"的前提）。

    注意这里的音符都恰好落在小节内的 2/4/8 分位置上——这是"规整"的强条件。
    只是"落在一个小结的网格上"不够，DP 还会拿节拍阶梯去换位置。
    """
    notes = [(0, 1920), (1920, 3840), (3840, 5760), (5760, 6720), (6720, 7680),
             (7680, 9600), (9600, 11520), (11520, 13440), (13440, 15360),
             (15360, 17280), (17280, 19200), (19200, 21120)]
    first = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    second = smart_quantize_musical(first.notes, simplicity=0.0, ticks_per_quarter=PPQ)

    assert first.ok and second.ok
    assert first.notes == notes                                  # 一格都没动
    assert second.notes == first.notes                            # 再跑一遍还是不动
    assert first.moved == 0 and second.moved == 0


def test_eighth_note_grid_is_also_left_untouched():
    """只落在**八分**网格（不是 16 分）上的旋律也不该被动。

    这是 V1→V2 修掉的主症状：V1 下这 8 个音有 6 个被搬走、最大挪 1 拍；
    V2 口径下位移代价与节拍阶梯同量级，"已经在网格上"就真的不动。
    """
    notes = [(0, 2880), (2880, 5760), (5760, 8640), (8640, 11520),
             (11520, 14400), (14400, 17280)]
    result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok
    assert result.notes == notes, "本来就在八分网格上的旋律被搬走了"
    assert result.moved == 0


def test_v1_scale_made_displacement_cheap_enough_to_be_swamped():
    """把 V1 的口径（按一小节归一）反过来钉住：位移代价被摊薄 16 倍，阶梯就压过它。

    这条测试不跑 V1 的代码，只算代价量级——一旦有人把归一改回 `BAR`（4/4 下 = 4 拍，
    于是代价再除 16），这里的数量关系立刻不成立。

    为什么 16 倍就足以出问题：节拍阶梯是 O(0.1~0.3) 的**分级落差**，而位移代价是
    O((位移/一拍)²)。4/4 的一小节 = 4 拍，所以 V1 的代价正好是 V2 的 1/16；
    λ=0.5 时"从其余位置升到八分"值 0.3，挪一个 16 分音符在 V1 下只花 0.0039——
    收益是代价的 77 倍，DP 当然会去换。V2 下同样的移动要花 0.0625，量级才对得上。
    """
    shift = PPQ // 4                                  # 挪一个 16 分音符 = 480 tick
    cost_v2 = 0.5 * ((shift * (QUARTER / PPQ)) / QUARTER) ** 2            # 按一拍归一
    cost_v1 = 0.5 * ((shift * (QUARTER / PPQ)) / (QUARTER * 4)) ** 2      # 按 4/4 一小节归一
    assert cost_v1 == pytest.approx(cost_v2 / 16)

    gain = lam_from_simplicity(0.0) * (0.8 - 0.2)     # λ=0.5：其余 → 八分
    assert gain > cost_v2 * 4, "V2 口径下阶梯收益与位移代价应当同量级"
    assert gain > cost_v1 * 60, "V1 口径下收益压倒代价——正是它把对好的音推走的原因"


def test_free_final_end_does_not_stretch_a_short_last_note():
    """末条记录的终点由 DP 自己挑，不再被"链跨度"顶长。

    V1 把末条终点强制在 `round(总跨度/网格)`，于是时值不整齐的收尾音会被
    拉长/截短，甚至把压缩量摊回整条链。V2 让 DP 自由收尾。
    """
    # 最后一个音原长 240，V1 会把它顶到网格边界
    notes = [(0, 480), (480, 960), (960, 1440), (1440, 1920), (1920, 2160)]
    result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok
    assert result.notes == notes
    assert result.notes[-1][1] - result.notes[-1][0] == 240


def test_clip_criteria_straight_and_triplet_grids_pass_through_unchanged():
    """**核心判据**：靠格子的素材必须原样通过——直线族与三连音族都要。

    这两族的最小音型是量化的"不能碰"清单：

    - 直线：八分 / 十六分 / 三十二分跑动
    - 三连音：1/3 拍循环、以及"三连音夹四分音符"的混合

    去掉 `MAX_SHIFT_COLS` 时它们全都会被改动：DP 会为了让音升一档而跨过 1/3 拍
    （挪 80~160 tick），实测《巴拉莱卡》479/569 个音被搬走、三连音全被抹平。
    """
    q = PPQ
    eighth, sixteenth, thirtysecond = q // 2, q // 4, q // 8
    triplet8 = q // 3                       # 八分三连音（1/3 拍）

    def seq(onsets):
        return [(s, onsets[i + 1] if i + 1 < len(onsets) else s + thirtysecond)
                for i, s in enumerate(onsets)]

    cases = {
        "直线 8 分": seq([0, eighth, 2 * eighth, 3 * eighth]),
        "直线 16 分": seq([0, sixteenth, eighth, eighth + sixteenth, 2 * eighth]),
        "直线 32 分": seq(list(range(0, q, thirtysecond))),
        "三连音 1/3": seq([0, triplet8, 2 * triplet8]),
        "三连音 + 四分": seq([0, triplet8, 2 * triplet8,
                              q, q + triplet8, q + 2 * triplet8]),
    }
    for name, notes in cases.items():
        result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
        assert result.ok, f"{name}: {result.message}"
        assert list(result.notes) == notes, f"{name} 被改动了：{result.notes}"
        assert result.moved == 0, f"{name} 有 {result.moved} 个音被移动"


def test_max_shift_cap_is_what_keeps_the_clip_criteria():
    """把单音上限关掉，同样的素材就会被搬走——证明上限是必需的，不是装饰。

    关掉后 DP 退回参考实现的行为：它去"挑最好的档"，而不是"就近吸附"。
    """
    q = PPQ
    notes = [(s, s + q // 8) for s in range(0, q, q // 8)]      # 32 分跑动
    capped = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    uncapped = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ,
                                      max_shift=None)
    assert capped.ok and uncapped.ok
    assert list(capped.notes) == notes                     # 有上限：原样
    assert list(uncapped.notes) != notes                   # 没上限：被搬走
    assert uncapped.moved > 0


def test_single_note_snaps_to_the_nearest_grid_column():
    """只有一个音、没有强拍可换时，目标函数只剩"位移²"，于是就是就近吸附。

    网格步长是 20 tick（一拍的 1/96），已经落在这套网格上的音**一个都不动**。
    """
    cases = {
        (0, 1900): (0, 1900),          # 1900 = 20 × 95，本来就在网格上
        (2000, 3900): (2000, 3900),    # 同上
        (100, 340): (100, 340),        # 同上
        (1990, 2010): (2000, 2020),    # 1990 不在网格上 → 两个边界各挪 10 tick
    }
    for notes, expected in cases.items():
        result = smart_quantize_musical([notes], simplicity=0.0, ticks_per_quarter=PPQ)
        assert result.ok, result.message
        assert result.notes == [expected]


def test_off_grid_note_snaps_to_the_nearest_column():
    """偏离网格的音各自就近吸附（每个边界最多挪半个网格步）。"""
    result = smart_quantize_musical([(7, 341)], simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok, result.message
    start, end = result.notes[0]
    assert start % GRID_TICKS == 0 and end % GRID_TICKS == 0
    assert abs(start - 7) <= GRID_TICKS // 2
    assert abs(end - 341) <= GRID_TICKS // 2


def test_slightly_off_grid_melody_snaps_to_the_grid():
    """偏离网格的音各自就近吸附，**位移不超过单音上限**（`MAX_SHIFT_COLS` 列）。

    上限是"不许搬家"的硬保证：没有它，DP 会为了让音升一档而跨过整个 1/3 拍，
    把整条链拖走（实测《巴拉莱卡》479/569 个音被搬、三连音全被抹平）。
    """
    notes = [(0, 1920), (1960, 2900), (3000, 4560)]
    result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok
    # 三个音的偏移都 ≤ 20 tick（不到一格），所以起点一个都没动，只把终点吸到网格
    assert result.notes == [(0, 1920), (1960, 2880), (3000, 4560)]
    for start, end in result.notes:
        assert start % GRID_TICKS == 0 and end % GRID_TICKS == 0
    # 单条记录的边界最多挪一个单音上限
    cap = MAX_SHIFT_COLS * GRID_TICKS
    for (old_start, old_end), (new_start, new_end) in zip(notes, result.notes):
        assert abs(new_start - old_start) <= cap
        assert abs(new_end - old_end) <= cap
    # 单条记录的边界最多挪一个搜索带宽（±16 列 = ±16 个网格步）
    for (old_start, _), (new_start, _) in zip(notes, result.notes):
        assert abs(new_start - old_start) <= 16 * GRID_TICKS


def test_simplicity_zero_keeps_the_total_span_and_ordering():
    notes = [(37, 997), (1050, 2401), (2503, 3999), (4100, 5999)]
    result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok
    assert [s for s, _ in result.notes] == sorted(s for s, _ in result.notes)
    # 不引入重叠：第 i 条的终点 ≤ 第 i+1 条的起点
    for (_, end), (start, _) in zip(result.notes, result.notes[1:]):
        assert end <= start


def test_higher_simplicity_snaps_further_to_strong_beats():
    """力度滑杆：同样的输入，s 越大越敢往强拍上贴。"""
    notes = [(180, 1700), (1900, 3600), (3800, 5600)]
    soft = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    hard = smart_quantize_musical(notes, simplicity=5.0, ticks_per_quarter=PPQ)
    assert soft.ok and hard.ok
    assert hard.moved >= soft.moved
    assert [metric_level(s, QUARTER) for s, _ in hard.notes].count("beat") >= \
        [metric_level(s, QUARTER) for s, _ in soft.notes].count("beat")


def test_bar_length_follows_ppq_so_results_are_scale_free():
    """换成 PPQ 960，量化结果应当是 1920 结果的一半（同一算法的另一种刻度）。"""
    at_1920 = smart_quantize_musical([(0, 1920), (2000, 4000)], ticks_per_quarter=1920)
    at_960 = smart_quantize_musical([(0, 960), (1000, 2000)], ticks_per_quarter=960)
    assert at_1920.ok and at_960.ok
    assert [(s * 2, e * 2) for s, e in at_960.notes] == at_1920.notes


def test_quantization_is_meter_independent():
    """网格步长 = 一拍 / 32，**与拍号无关**：传不传 `bar_quarters` 结果必须一样。

    这是"动网格"（写成"一小节 / 32" → "一拍 / 32"）之后的核心性质。
    写成"一小节 / 32"时，6/8 的曲子网格会是 180 tick，而模型给的 onset 本来就在
    60 的格子上，两者对不上，128/214 个音因此被搬了 ≥180 tick（《相思》实测）。
    """
    notes = [(0, 960), (960, 1920), (3000, 4200), (5100, 6000)]
    four_four = smart_quantize_musical(notes, ticks_per_quarter=PPQ, bar_quarters=4.0)
    six_eight = smart_quantize_musical(notes, ticks_per_quarter=PPQ, bar_quarters=3.0)
    two_four = smart_quantize_musical(notes, ticks_per_quarter=PPQ, bar_quarters=2.0)
    default = smart_quantize_musical(notes, ticks_per_quarter=PPQ)
    assert four_four.ok and six_eight.ok and two_four.ok and default.ok
    assert four_four.notes == six_eight.notes == two_four.notes == default.notes

    # 而且落点是 20 tick（一拍/96），不是任何"小节/32"
    assert GRID_TICKS == PPQ // GRID_DIVISOR
    for start, end in six_eight.notes:
        assert start % GRID_TICKS == 0 and end % GRID_TICKS == 0


def test_note_spans_are_kept_within_one_grid_step_of_their_origin():
    """量化是"就近吸附"：每个边界最多挪 BAND 列。"""
    notes = [(0, 1920), (2000, 3900)]
    result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok
    for (os_, _), (ns, _) in zip(notes, result.notes):
        assert abs(ns - os_) <= BAND * GRID_TICKS


# --------------------------------------------------------------- 退化输入
def test_empty_selection_fails_loudly():
    result = smart_quantize([], simplicity=0.0)
    assert result.ok is False and result.message


def test_zero_length_selection_is_degenerate():
    result = smart_quantize_musical([(1000, 1000)], ticks_per_quarter=PPQ)
    assert result.ok is False
    assert "退化" in result.message


def test_overlapping_notes_are_either_tamed_or_rejected_but_never_corrupt():
    """重叠输入：链要么把重叠吸收成"头尾相接"，要么明确判失败。

    参考实现的态度是**不裁剪**——重叠靠记录链消化。网格细化到 20 tick 之后，
    能否摆得开取决于重叠有多深，所以这里对两条路都做自洽性断言。
    """
    for notes in ([(0, 2000), (1000, 3000), (3500, 5000)],
                  [(0, 2000), (1000, 3000)],
                  [(0, 3000), (500, 6000)]):
        result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
        if result.ok:
            starts = [s for s, _ in result.notes]
            assert starts == sorted(starts)
            for start, end in result.notes:
                assert start % GRID_TICKS == 0 and end % GRID_TICKS == 0
                assert end > start
            for (_, prev_end), (start, _) in zip(result.notes, result.notes[1:]):
                assert start >= prev_end                     # 头尾相接，不再重叠
        else:
            assert result.notes == []
            assert result.message


def test_extreme_overlap_is_either_tamed_or_rejected_but_never_corrupt():
    """把第二条整个塞进第一条里面。

    网格步长 20 tick 时这种输入通常摆不开；无论走哪条路，输出都必须自洽：
    要么整条成功且头尾相接、全落网格，要么判失败且一个音都不给。
    """
    notes = [(0, 1000), (10, 100)]
    result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    if result.ok:
        starts = [s for s, _ in result.notes]
        assert starts == sorted(starts)
        for start, end in result.notes:
            assert start % GRID_TICKS == 0 and end % GRID_TICKS == 0
            assert end > start
        for (_, prev_end), (start, _) in zip(result.notes, result.notes[1:]):
            assert start >= prev_end
    else:
        assert result.notes == []
        assert result.message


def test_zero_span_overlap_is_still_infeasible():
    """整条链的跨度四舍五入成 0 列 → 判失败（安全结果，不是坏结果）。"""
    notes = [(0, 1000), (10, 15)]
    result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok is False
    assert result.notes == []
    assert result.message


def test_short_already_on_grid_notes_pass_through_untouched():
    """十六分音符（240 tick = 4 列）本来就在网格上，就不该被改动。"""
    result = smart_quantize_musical([(0, 240), (240, 480)], simplicity=0.0,
                                    ticks_per_quarter=PPQ)
    assert result.ok
    assert result.notes == [(0, 240), (240, 480)]
    assert result.moved == 0


def test_a_note_shorter_than_one_grid_column_is_degenerate():
    """比半个网格列还短的音符：全链跨度四舍五入成 0 列 → 明确失败。

    网格步长 20 tick，所以跨度 < 10 tick 才会退化（跨度 = 10 会就近进位成 1 列）。
    这是已知的粒度下限：更细的音符要么用更细的网格，要么不量化。
    """
    result = smart_quantize_musical([(0, 4)], simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok is False
    assert result.notes == []
    assert "退化" in result.message


def test_a_note_exactly_one_grid_column_long_survives():
    """正好一个网格列（20 tick）的音符不算退化，原样通过。"""
    result = smart_quantize_musical([(0, 20)], simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok
    assert result.notes == [(0, 20)]


def test_gap_records_absorb_silence_so_boundaries_stay_contiguous():
    """有静音的旋律：静音也进链，量化后音符之间的空拍跟着一起被整理。"""
    notes = [(0, 1000), (2000, 3000)]
    result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok
    assert result.notes[0][1] < result.notes[1][0]          # 静音还在
    for start, end in result.notes:
        assert start % GRID_TICKS == 0 and end % GRID_TICKS == 0
    gaps = [d for d in result.detail if d["note_index"] is None]
    assert len(gaps) == 1
    assert gaps[0]["orig"] == (1000, 2000)


def test_invalid_arguments_are_rejected():
    # 网格跨度必须是 32 的倍数（网格步长 = grid_span/32）
    with pytest.raises(ValueError):
        smart_quantize([(0, 100)], grid_span=GRID + 1)
    # bar= 是 grid_span 的旧别名，两者都给要报错
    with pytest.raises(ValueError):
        smart_quantize([(0, 100)], bar=BAR, grid_span=BAR)
    with pytest.raises(ValueError):
        smart_quantize_musical([(0, 100)], ticks_per_quarter=0)
    # PPQ 必须能被 32 整除，否则"一拍的 1/32"不是整数 tick
    with pytest.raises(ValueError):
        smart_quantize_musical([(0, 100)], ticks_per_quarter=100)


def test_negative_positions_round_the_anchor_symmetrically():
    """起点为负（合并多段素材时会出现）：锚点就近取整，结果仍然落网格。

    -20.25 格的锚点必须落在 -20 格（就近），不能上下取整成 -21 格。
    用两个带空隙的音符把链拉长到若干格——单个短音会因跨度四舍五入成 0 列而判退化，
    那是粒度下限，不是这条要测的东西。
    """
    q = GRID
    notes = [(-q * 20 - q // 4, -q * 20 + q),      # 起点 -20.25 格，锚点应就近落到 -20 格
             (-q * 20 + q, -q * 20 + 4 * q)]
    result = smart_quantize(notes, simplicity=0.0, bar=BAR)

    assert result.ok, result.message
    assert result.notes[0][0] == -q * 20                # 就近取整：-20.25 → -20，而不是 -21
    for start, end in result.notes:
        assert start % q == 0 and end % q == 0
        assert end > start


def test_engine_never_emits_off_grid_output_even_for_hostile_input():
    """兜底性质：不管输入多离谱，成功的结果一定落网格、失败则一个音符都不给。"""
    hostile = [
        [(-1_000_000, 1000), (2000, 5000)],
        [(0, 1000), (10, 100)],
        [(0, 0), (240, 240)],
        [(5, 5), (7, 9), (11, 4000)],
        [(0, 100_000_000), (10, 200)],
    ]
    for notes in hostile:
        result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
        if result.ok:
            assert list(result.notes) == sorted(result.notes)
            for start, end in result.notes:
                assert start % GRID_TICKS == 0 and end % GRID_TICKS == 0
                assert end > start
        else:
            assert result.notes == [] and result.message


# --------------------------------------------------------------- 项目适配
def test_bar_quarters_reads_the_meter_from_the_timeline():
    four_four = from_playback(playback((0.0, 2.0), (2.0, 4.0)))
    assert bar_quarters_from_timeline(four_four) == pytest.approx(4.0)


def test_bar_quarters_ignores_a_short_pickup_measure():
    rows = [
        dict(index=0, start=0.0, end=0.27, score_start=0.0, score_end=1.0,
             leading_rest=0.875, trailing_rest=0.0),
        dict(index=1, start=0.27, end=2.27, score_start=1.0, score_end=2.0,
             leading_rest=0.0, trailing_rest=0.0),
    ]
    timeline = from_playback({"measures": rows}, pickup="actual")
    assert timeline.measures[0].is_pickup is True
    assert bar_quarters_from_timeline(timeline) == pytest.approx(4.0)


def test_quantize_tracks_snaps_project_notes_and_keeps_order():
    tracks = [{"id": "vocal", "notes": [
        {"tick": 1960, "dur": 940, "pitch": 60},
        {"tick": 0, "dur": 1920, "pitch": 62},
    ]}]
    new_tracks, stats = quantize_tracks(tracks, simplicity=0.0, ticks_per_quarter=PPQ)

    assert stats.tracks == 1 and stats.notes == 2 and stats.failed_tracks == 0
    assert stats.grid_ticks == GRID_TICKS
    ticks = [n["tick"] for n in new_tracks[0]]
    assert ticks == sorted(ticks)
    assert all(n["tick"] % GRID_TICKS == 0 for n in new_tracks[0])
    assert all(n["dur"] % GRID_TICKS == 0 for n in new_tracks[0])
    # 音高跟着音符走：先响的仍是 62
    assert new_tracks[0][0]["pitch"] == 62
    # 原数据没有被动过
    assert tracks[0]["notes"][0]["tick"] == 1960


def test_quantize_tracks_keeps_a_failing_track_unchanged():
    """量化失败时整条轨道原样返回，绝不留半量化状态。

    用"整条链跨度退化"的输入触发失败（网格细化到 60 tick 后，一般重叠已经能摆开，
    见 `test_extreme_overlap_is_either_tamed_or_rejected_but_never_corrupt`）。
    """
    tracks = [{"id": "vocal", "notes": [
        {"tick": 0, "dur": 1000, "pitch": 60},
        {"tick": 10, "dur": 15, "pitch": 62},
    ]}]
    new_tracks, stats = quantize_tracks(tracks, simplicity=0.0, ticks_per_quarter=PPQ)
    assert stats.failed_tracks == 1
    assert stats.moved == 0
    assert new_tracks[0] == tracks[0]["notes"]
    assert stats.messages and "vocal" in stats.messages[0]


def test_quantize_tracks_never_leaves_a_zero_length_note():
    tracks = [{"id": "vocal", "notes": [
        {"tick": 0, "dur": 1, "pitch": 60},
        {"tick": 240, "dur": 1, "pitch": 62},
        {"tick": 480, "dur": 240, "pitch": 64},
    ]}]
    new_tracks, _ = quantize_tracks(tracks, simplicity=0.0, ticks_per_quarter=PPQ)
    assert all(n["dur"] >= 1 and n["tick"] + n["dur"] <= 7680 for n in new_tracks[0])


def test_quantize_tracks_respects_tick_length():
    tracks = [{"id": "instrumental", "notes": [
        {"tick": 0, "dur": 1000, "pitch": 60},
        {"tick": 5000, "dur": 4000, "pitch": 62},
    ]}]
    new_tracks, _ = quantize_tracks(tracks, tick_length=6000, ticks_per_quarter=PPQ)
    for note in new_tracks[0]:
        assert note["tick"] + note["dur"] <= 6000


def test_quantize_tracks_handles_single_note_and_empty_track():
    tracks = [{"id": "a", "notes": [{"tick": 100, "dur": 500, "pitch": 60}]},
              {"id": "b", "notes": []}]
    new_tracks, stats = quantize_tracks(tracks, ticks_per_quarter=PPQ)
    assert new_tracks[0] == tracks[0]["notes"]
    assert new_tracks[1] == []
    assert stats.failed_tracks == 0


def test_note_shift_seconds_reports_the_move_in_seconds():
    from sheetsage2_webui.quantize import note_shift_seconds

    timeline = from_playback(playback((0.0, 2.0), (2.0, 4.0)))     # 一全音符 = 2s
    before = [{"tick": 1960, "dur": 940, "pitch": 60}]
    after = [{"tick": 1920, "dur": 960, "pitch": 60}]
    shift = note_shift_seconds(before, after, timeline)
    assert shift["max"] == pytest.approx(abs(timeline.tick_to_sec(1960)
                                             - timeline.tick_to_sec(1920)), abs=1e-4)
    assert shift["mean"] == shift["median"] == shift["p90"] == shift["max"]
    assert note_shift_seconds([], [], timeline) == {}
    assert note_shift_seconds(before, [], timeline) == {}


def test_stats_serialize_for_the_report():
    _, stats = quantize_tracks([{"id": "vocal", "notes": [
        {"tick": 0, "dur": 1920, "pitch": 60},
        {"tick": 2000, "dur": 1920, "pitch": 62},
    ]}], simplicity=2.5, ticks_per_quarter=PPQ)
    payload = stats.as_dict()
    assert payload["simplicity"] == 2.5
    assert 0.0 <= payload["moved_ratio"] <= 1.0
    assert payload["grid_ticks"] == GRID_TICKS
    assert payload["bar_quarters"] == pytest.approx(4.0)


def test_stats_report_the_grid_and_ignore_the_meter():
    """网格步长 = **一拍 / 96**（PPQ1920 → 20 tick），与拍号无关。

    `bar_quarters` 已经退化成纯记录字段（老 `project.json` 读者还在看它），
    传不同的值**不得**改变网格或结果。
    """
    _, stats = quantize_tracks([{"id": "vocal", "notes": [
        {"tick": 0, "dur": 1920, "pitch": 60},
        {"tick": 2000, "dur": 1920, "pitch": 62},
    ]}])
    assert stats.grid_ticks == PPQ // GRID_DIVISOR     # 20
    assert stats.placement_ticks == PPQ // GRID_DIVISOR
    payload = stats.as_dict()
    assert payload["grid_ticks"] == PPQ // GRID_DIVISOR
    assert payload["placement_ticks"] == PPQ // GRID_DIVISOR

    # 2/4 与 6/8：网格**不**跟着变（写在"一小节/32"时这两个会变成 120 / 180，
    # 正是《相思》出问题的原因）
    for bar_q in (2.0, 3.0):
        _, st = quantize_tracks([{"id": "vocal", "notes": [
            {"tick": 0, "dur": 960, "pitch": 60},
            {"tick": 1000, "dur": 960, "pitch": 62},
        ]}], bar_quarters=bar_q)
        assert st.grid_ticks == PPQ // GRID_DIVISOR, f"bar_quarters={bar_q} 时网格变了"
