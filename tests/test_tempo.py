r"""`tempo` 模块的回归测试。

核心是钉住两个曾经踩过的坑：
1. `beat.lab` 时间戳只有 0.01s 精度，用中位间隔反推会把 104 BPM 读成 103.45；
2. 歌曲结尾散拍会让全局拟合跑偏，必须先找稳定段。

    .\.venv\Scripts\python.exe -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.tempo import (  # noqa: E402
    TempoSegment,
    abc_meter,
    abc_tempo,
    compare_to_constant,
    constant_beat_times,
    estimate_meter,
    estimate_tempo,
    local_tempo,
    reconcile,
    retime,
    sectional_tempo,
    segments_to_tempo_map,
    snap_bpm,
    suggest_grid_recovery,
)


def make_rows(period: float, count: int, denominator: int = 4, numerator: int = 4,
              offset: float = 0.01, quantize: float = 0.01):
    """按固定拍长生成 beat.lab 风格的行，时间戳可按给定步长量化。"""
    rows = []
    for index in range(count):
        time = offset + index * period
        if quantize:
            time = round(time / quantize) * quantize
        rows.append([f"{time:.3f}", index % numerator + 1, numerator, denominator])
    return rows


# --------------------------------------------------------------- 量化噪声
def test_median_interval_is_biased_by_quantization_but_lsq_is_not():
    """104 BPM 的真拍长 0.576923s 会被存成 0.58s，中位法因此偏 0.55。"""
    rows = make_rows(60 / 104, 339)
    estimate = estimate_tempo(rows)

    assert estimate.beat_unit_bpm == pytest.approx(104.0, abs=0.05)
    assert estimate.bpm_median_interval is not None
    assert estimate.bpm_median_interval < 103.9      # 朴素方法确实偏了
    assert estimate.residual_std < 0.02


def test_exact_round_tempo_still_recovered():
    estimate = estimate_tempo(make_rows(0.5, 200))
    assert estimate.bpm == pytest.approx(120.0, abs=0.01)
    assert estimate.is_constant


# --------------------------------------------------------------- 稳定段
def test_broken_tail_does_not_drag_the_estimate():
    """结尾散拍（淡出/丢拍）不能把整首曲的速度带偏。"""
    rows = make_rows(0.3, 501, denominator=8, numerator=6)
    last = 0.01 + 500 * 0.3
    for index in range(1, 21):
        time = round(last + 0.45 * index, 2)
        rows.append([f"{time:.3f}", index % 6 + 1, 6, 8])

    estimate = estimate_tempo(rows)
    assert estimate.denominator == 8
    assert estimate.beat_unit_bpm == pytest.approx(200.0, abs=0.05)
    assert estimate.bpm == pytest.approx(100.0, abs=0.05)   # 归一化到四分音符
    assert estimate.regular_coverage < 1.0
    assert estimate.regular_coverage > 0.9
    assert estimate.is_constant


def test_estimate_reports_nothing_for_a_single_beat():
    estimate = estimate_tempo([["0.01", "1", "4", "4"]])
    assert estimate.beats_total == 1
    assert estimate.bpm is None
    assert estimate.warnings


def test_compound_meter_is_normalized_to_quarter_notes():
    rows = make_rows(0.3, 64, denominator=8, numerator=6)
    estimate = estimate_tempo(rows)
    assert estimate.beat_unit_bpm == pytest.approx(200.0, abs=0.1)
    assert estimate.bpm == pytest.approx(100.0, abs=0.1)
    assert estimate.meter == "6/8"


# --------------------------------------------------------------- 吸附
@pytest.mark.parametrize("value,mode,expected", [
    (103.45, "none", 103.45),
    (103.45, "integer", 103.0),
    (103.62, "integer", 104.0),
    (103.45, "half", 103.5),
])
def test_snap_bpm(value, mode, expected):
    assert snap_bpm(value, mode) == pytest.approx(expected)


def test_snap_bpm_candidates_picks_nearest():
    assert snap_bpm(103.8, "candidates", candidates=[100, 104, 120]) == 104
    assert snap_bpm(103.8, "candidates", candidates=[100, 120]) == 100


def test_snap_bpm_none_passthrough():
    assert snap_bpm(None, "integer") is None
    with pytest.raises(ValueError):
        snap_bpm(120, "nonsense")


# --------------------------------------------------------------- 干预
def test_constant_beat_times_uses_quarter_note_bpm():
    grid = constant_beat_times(0.0, 120, 4, 5)
    assert grid == pytest.approx([0.0, 0.5, 1.0, 1.5, 2.0])
    # 6/8：四分音符 100 BPM -> 八分音符 0.3s
    grid = constant_beat_times(0.0, 100, 8, 3)
    assert grid == pytest.approx([0.0, 0.3, 0.6])


def test_compare_to_constant_measures_drift():
    times = constant_beat_times(0.0, 120, 4, 10)
    shifted = [t + 0.1 for t in times]
    # anchor 跟着走：整体平移不算漂移
    info = compare_to_constant(shifted, 0.1, 120, 4)
    assert info["drift_max"] == pytest.approx(0.0, abs=1e-9)

    # anchor 不跟着走：平移量会如实体现在漂移里
    info = compare_to_constant(shifted, 0.0, 120, 4)
    assert info["drift_max"] == pytest.approx(0.1, abs=1e-9)

    drifting = list(shifted)
    drifting[-1] += 1.0
    info = compare_to_constant(drifting, 0.1, 120, 4)
    assert info["drift_max"] == pytest.approx(1.0, abs=1e-9)


def test_retime_scales_around_anchor_and_reports_shift():
    times = [0.0, 1.0, 2.0, 3.0]
    moved, shift = retime(times, 0.5, anchor=0.0)
    assert moved == pytest.approx([0.0, 0.5, 1.0, 1.5])
    assert shift["shift_max"] == pytest.approx(1.5)
    assert shift["shift_mean"] == pytest.approx(0.75)


def test_retime_rejects_non_positive_ratio():
    with pytest.raises(ValueError):
        retime([1.0], 0)


# --------------------------------------------------- 模型自带的 Q 值与校验
def test_abc_tempo_reads_model_q_field():
    abc = "X:1\nT:\nM:4/4\nL:1/32\nQ:1/4=104\nK:F\n"
    assert abc_tempo(abc) == 104.0
    assert abc_tempo("X:1\nM:6/8\nQ: 1/4 = 98\n") == 98.0
    assert abc_tempo("X:1\nM:4/4\n") is None
    assert abc_tempo(None) is None


def test_reconcile_agrees_when_values_match():
    estimate = estimate_tempo(make_rows(60 / 104, 339))
    result = reconcile(104.0, estimate)
    assert result["agreement"] is True
    assert result["source"] == "beat_robust"
    assert result["chosen_bpm"] == pytest.approx(104.0, abs=0.05)
    assert abs(result["delta"]) < 0.1


def test_reconcile_flags_disagreement_and_still_prefers_the_beat_grid():
    """模型 Q 值被结尾散拍拖偏时，应优先采用节拍网格拟合值并如实报警。"""
    rows = make_rows(0.3, 501, denominator=8, numerator=6)
    last = 0.01 + 500 * 0.3
    for index in range(1, 21):
        rows.append([f"{round(last + 0.45 * index, 2):.3f}", index % 6 + 1, 6, 8])
    estimate = estimate_tempo(rows)

    result = reconcile(98.0, estimate)          # 模型端点法给出的错误值
    assert result["agreement"] is False
    assert result["chosen_bpm"] == pytest.approx(100.0, abs=0.05)
    assert result["source"] == "beat_robust"
    assert result["notes"]


def test_reconcile_falls_back_to_abc_when_grid_is_unusable():
    estimate = estimate_tempo([["0.0", "1", "4", "4"]])   # 只有一拍
    result = reconcile(96.0, estimate)
    assert result["chosen_bpm"] == 96.0
    assert result["source"] == "abc_q"


# --------------------------------------------------------------- 拍号
def test_meter_without_pickup_is_uniform():
    rows = make_rows(0.5, 32, numerator=6, denominator=8)
    meter = estimate_meter(rows)
    assert meter.primary == "6/8"
    assert meter.denominator == 8
    assert meter.consistent is True
    assert meter.pickup is None
    assert meter.changes == []


def test_short_first_bar_is_recognized_as_pickup():
    """光芒/朋友的酒 开头那个短小节是弱起，不是噪声。"""
    rows = [[0.01, 1, 1, 8]]                       # 1/8 弱起，1 拍
    rows += make_rows(0.5769, 40, numerator=4, denominator=4, offset=0.28)
    meter = estimate_meter(rows)

    assert meter.primary == "4/4"
    assert meter.consistent is True                # 弱起不算"转拍号"
    assert meter.changes == []
    assert meter.pickup is not None
    assert meter.pickup["meter"] == "1/8"
    assert meter.pickup["beats"] == 1
    assert meter.pickup["start"] == pytest.approx(0.01)
    assert meter.pickup["end"] == pytest.approx(0.28)
    assert meter.pickup["seconds"] == pytest.approx(0.27)


def test_pickup_detection_only_applies_to_the_first_run():
    """开头若是完整小节，就不该被当成弱起，而应记为转拍号。"""
    rows = make_rows(0.5, 8, numerator=4, denominator=4, offset=0.01)     # 4/4 两小节
    rows += make_rows(0.5, 20, numerator=3, denominator=4, offset=4.01)   # 3/4 更长，成为主拍号
    meter = estimate_meter(rows)

    assert meter.primary == "3/4"
    assert meter.pickup is None                    # 8 拍 >= 主拍号分子 3，不是弱起
    assert meter.consistent is False
    assert len(meter.changes) == 1
    assert meter.changes[0]["meter"] == "4/4"
    assert meter.changes[0]["start"] == pytest.approx(0.01)


def test_main_meter_is_the_longest_run_not_the_mode():
    """主拍号取最长段：短的弱起段即使重复出现也不该压过主拍号。"""
    rows = [[0.01, 1, 1, 8]]
    rows += make_rows(0.5, 60, numerator=4, denominator=4, offset=0.28)
    meter = estimate_meter(rows)
    assert meter.primary == "4/4"
    assert meter.distribution == {"1/8": 1, "4/4": 60}


def test_estimate_meter_without_rows_warns():
    meter = estimate_meter([["0.0"]])
    assert meter.primary is None
    assert meter.warnings


def test_abc_meter_reads_model_signature():
    assert abc_meter("X:1\nM:6/8\nL:1/32\n") == "6/8"
    assert abc_meter("X:1\nM: 4 / 4\n") == "4/4"
    assert abc_meter("X:1\nK:C\n") is None
    assert abc_meter(None) is None


# --------------------------------------------------------------- 变速曲
def two_section_rows(first_period=0.5, first_count=60, second_period=0.6, second_count=60):
    """前半一个速度、后半另一个速度：模拟 `萧敬腾 - 皮囊` 那种变速曲。"""
    rows = make_rows(first_period, first_count)
    offset = 0.01 + first_count * first_period
    rows += make_rows(second_period, second_count, offset=offset)
    return rows


def test_constant_song_has_a_flat_local_tempo_curve():
    rows = make_rows(0.5, 120)
    estimate = estimate_tempo(rows)
    assert estimate.tempo_range is not None
    assert estimate.tempo_range["max"] - estimate.tempo_range["min"] < 1.0
    assert estimate.is_constant is True


def test_varying_song_is_flagged_and_reports_a_range():
    estimate = estimate_tempo(two_section_rows())

    assert estimate.is_constant is False
    assert estimate.regular_coverage < 0.9
    assert estimate.tempo_range is not None
    assert estimate.tempo_range["min"] == pytest.approx(100.0, abs=2.0)
    assert estimate.tempo_range["max"] == pytest.approx(120.0, abs=2.0)
    assert any("不是单一速度" in w for w in estimate.warnings)


def test_local_tempo_normalizes_by_meter_denominator():
    """6/8 的局部速度也要归一化到四分音符，否则会显示成两倍。"""
    rows = make_rows(0.3, 64, numerator=6, denominator=8)
    times = sorted(float(r[0]) for r in rows)

    assert local_tempo(times, denominator=4)[0]["bpm"] == pytest.approx(200.0, abs=1.0)
    assert local_tempo(times, denominator=8)[0]["bpm"] == pytest.approx(100.0, abs=1.0)


def test_local_tempo_needs_enough_beats():
    assert local_tempo([0.0, 0.5, 1.0], window=32) == []
    assert local_tempo([], window=4) == []


def test_reconcile_marks_varying_song_as_not_confident():
    estimate = estimate_tempo(two_section_rows())
    result = reconcile(110.0, estimate)

    assert result["confident"] is False
    assert result["constant"] is False
    assert result["source"] == "abc_q"          # 非恒速时采用模型自己的全局值
    assert result["chosen_bpm"] == 110.0
    assert any("不是恒速曲" in note for note in result["notes"])


# --------------------------------------------------------- 分段速度（按段落）
def grid(bpm: float, beats: int, offset: float = 0.0) -> list[float]:
    """按给定四分音符速度生成拍点时间。"""
    period = 60.0 / bpm
    return [offset + i * period for i in range(beats)]


def sections_from(spans):
    return [{"start": start, "end": end, "label": label} for start, end, label in spans]


def test_sectional_tempo_collapses_a_constant_song():
    times = grid(120.0, 600)                       # 300 秒恒速
    sections = sections_from([(i * 30.0, (i + 1) * 30.0, "verse") for i in range(10)])
    segments = sectional_tempo(times, sections)

    assert len(segments) == 1
    assert segments[0].bpm == pytest.approx(120.0, abs=0.05)
    assert segments[0].beats == 600
    assert segments[0].reliable is True


def test_sectional_tempo_finds_the_slow_section():
    """复刻 `萧敬腾 - 皮囊`：124 → 110 → 124，边界落在段落上。"""
    first = grid(124.0, 200)
    slow = grid(110.0, 100, offset=first[-1] + 60.0 / 124.0)
    last = grid(124.0, 200, offset=slow[-1] + 60.0 / 110.0)
    times = first + slow + last
    sections = sections_from([
        (times[0], slow[0], "intro"),
        (slow[0], last[0], "verse"),
        (last[0], times[-1] + 1.0, "chorus"),
    ])
    segments = sectional_tempo(times, sections)

    assert len(segments) == 3
    assert [round(s.bpm) for s in segments] == [124, 110, 124]
    assert segments[1].bpm == pytest.approx(110.0, abs=0.05)
    assert segments[1].start == pytest.approx(slow[0], abs=1e-6)
    assert segments[1].label == "verse"


def test_sectional_tempo_absorbs_an_unreliable_segment():
    """残差超过一整拍的段落不是恒速区，应并进邻居，而不是造出一个假的速度变化点。

    中段有 6 秒的断裂，最小二乘残差 2.33s > 拍长 1.33s，判定为不可信。
    """
    head = grid(120.0, 200)                                  # 0–99.5s
    middle = [100.0, 100.5, 101.0, 101.5, 102.0,
              108.0, 108.5, 109.0, 109.5, 110.0]             # 中间断了 6 秒
    tail = grid(120.0, 200, offset=112.0)
    times = head + middle + tail
    sections = sections_from([(0.0, 100.0, "a"), (100.0, 110.5, "b"), (110.5, 212.0, "c")])

    segments = sectional_tempo(times, sections)
    assert len(segments) == 1
    assert segments[0].bpm == pytest.approx(120.0, abs=0.2)


def test_unreliable_flag_is_reported():
    """不可信段即便被合并，也应该能从残差/一致性上看出来。"""
    from sheetsage2_webui.tempo import _fit_segment

    bpm, _, residual, period, consistency = _fit_segment(
        [100.0, 100.5, 101.0, 101.5, 102.0, 108.0, 108.5, 109.0, 109.5, 110.0], 4)
    assert residual > period                       # 残差超过一整拍
    assert bpm > 0

    # 另一类不可信：残差不大，但拍间隔压根不一致（缓慢起伏的自由速度）
    wobble: list[float] = []
    t = 0.0
    for i in range(40):
        wobble.append(t)
        t += 0.5 + 0.12 * (i % 2)
    _, _, residual2, period2, consistency2 = _fit_segment(wobble, 4)
    assert residual2 <= period2 * 2                # 残差没大到离谱
    assert consistency2 < 0.6                      # 但间隔一致性把它拦下来了


def test_rubato_region_is_not_split_into_fake_segments():
    """`壁上观` 的中段：一次 rubato 不能被切成好几个"恒速区"。

    速度连续起伏时，最小二乘残差并不大（能被直线穿过），但拍间隔一致性很低。
    """
    times: list[float] = []
    t = 0.0
    for i in range(400):
        times.append(t)
        t += 0.5 + 0.12 * ((i // 20) % 2)          # 在 0.5 与 0.62 之间来回摆
    sections = sections_from([(times[i * 40], times[min((i + 1) * 40, len(times) - 1)] + 1.0,
                               f"s{i}") for i in range(10)])

    segments = sectional_tempo(times, sections)
    # 不许切成一堆"恒速段"
    assert len(segments) <= 2
    # 而且必须如实标注出来，不能拿子段的自洽度给整段背书
    assert any(not s.reliable for s in segments)


def test_large_unstable_region_is_reported_not_swallowed():
    """一大片网格不可靠的区域必须单独报出来，不能并进相邻的恒速段。

    真实案例：`壁上观` 71.5–249.5s 占全曲 69% 的拍点，模型在这一段没锁住速度。
    把它并进前面的 90 BPM 段，就会得出"整首 0–249.5s 都是 90"这种假结论。
    """
    steady = grid(120.0, 200)                      # 0–99.5s，干净
    wobble: list[float] = []
    t = 100.0
    for i in range(200):                           # 网格前后摆，锁不住
        wobble.append(t)
        t += 0.5 + 0.12 * (i % 2)
    tail = grid(120.0, 200, offset=t + 0.5)
    times = steady + wobble + tail
    sections = sections_from([(0.0, 100.0, "a"),
                              (100.0, t + 0.5, "b"),
                              (t + 0.5, t + 110.0, "c")])

    segments = sectional_tempo(times, sections)

    assert len(segments) == 3
    assert segments[1].reliable is False           # 中间那一段如实标注
    assert segments[1].interval_consistency < 0.6
    # 首尾仍然是干净的 120，没有被中间那段拖走
    assert segments[0].bpm == pytest.approx(120.0, abs=0.3)
    assert segments[0].interval_consistency >= 0.9
    assert segments[2].bpm == pytest.approx(120.0, abs=0.3)


def test_small_unstable_region_folds_into_its_neighbour():
    """太小的一片不可靠区不值得单独报告，直接并进邻居。"""
    steady = grid(120.0, 300)
    wobble = [200.0, 200.9, 201.1, 202.2, 202.4, 203.5, 203.8, 204.9, 205.2, 206.3]
    times = steady + wobble
    sections = sections_from([(0.0, 200.0, "a"), (200.0, 210.0, "b")])

    segments = sectional_tempo(times, sections)
    assert len(segments) == 1
    assert segments[0].bpm == pytest.approx(120.0, abs=0.3)


# ----------------------------------------------------- 网格重建的可信度
def test_grid_recovery_accepts_a_clean_region():
    times = grid(120.0, 200)
    recovery = suggest_grid_recovery(times, 0.0, times[-1] + 1.0)
    assert recovery.bpm == pytest.approx(120.0, abs=0.2)
    assert recovery.hit_ratio >= 0.95
    assert recovery.tight_ratio >= 0.6
    assert recovery.confident is True


def test_grid_recovery_rejects_a_scrambled_region():
    """`壁上观` 71.5–249.5s：拍点本身错了，不是相位猜错，所以不能自动重建。"""
    times: list[float] = []
    t = 0.0
    for i in range(300):
        times.append(t)
        t += 0.5 if i % 2 else 0.74          # 在两档之间乱跳
    recovery = suggest_grid_recovery(times, 0.0, times[-1] + 1.0)
    assert recovery.confident is False
    assert recovery.hit_ratio < 0.95


def test_grid_density_constraint_blocks_absurdly_short_periods():
    """不加密度约束，拍长越短命中率越高，会选出 240 BPM 这种假阳性。"""
    times = grid(90.0, 200)
    recovery = suggest_grid_recovery(times, 0.0, times[-1] + 1.0, min_bpm=40, max_bpm=240)
    # 网格线不能明显多于拍点，否则等于"到处都算命中"
    assert recovery.bpm == pytest.approx(90.0, abs=1.0)


# ----------------------------------------------------- BPM 的有效位数
def test_bpm_decimals_follow_the_measured_uncertainty():
    """三位小数不是被时间戳量化限制的（那只贡献 ±0.0004 BPM），而是被拍点抖动限制的。"""
    steady = TempoSegment(start=0.0, end=100.0, bpm=58.999, beats=155,
                          anchor=0.0, period_seconds=1.0172, bpm_sigma=0.0031)
    assert steady.bpm_decimals == 3
    assert steady.bpm_rounded == 58.999
    assert steady.bpm_uncertain is False

    shaky = TempoSegment(start=0.0, end=100.0, bpm=57.503, beats=68,
                         anchor=0.0, period_seconds=1.0434, bpm_sigma=1.4993)
    assert shaky.bpm_decimals == 1
    assert shaky.bpm_rounded == 57.5
    assert shaky.bpm_uncertain is True


def test_bpm_sigma_is_measured_from_sliding_windows():
    """段内速度有起伏时，sigma 要能反映出来；恒速段则接近 0。"""
    steady = grid(120.0, 200)
    wobble = []
    t = 0.0
    for i in range(200):
        wobble.append(t)
        t += 0.5 + 0.1 * ((i // 10) % 2)          # 每 10 拍在 0.5/0.6 之间换档
    times = steady + [x + 200.0 for x in wobble]

    from sheetsage2_webui.tempo import _tempo_sigma

    assert _tempo_sigma(steady, 4) < 0.01
    assert _tempo_sigma(times, 4) > 1.0


def test_tempo_map_uses_the_rounded_value():
    """速度表里不该写 124.311 这种暗示了不存在精度的值。"""
    from sheetsage2_webui.timeline import from_playback

    timeline = from_playback({"measures": [
        dict(index=0, start=0.0, end=100.0, score_start=0.0, score_end=1.0,
             leading_rest=0.0, trailing_rest=0.0)]})
    segment = TempoSegment(start=0.0, end=100.0, bpm=124.311, beats=356,
                           anchor=0.0, period_seconds=0.482666, bpm_sigma=0.3025)
    entries = segments_to_tempo_map([segment], timeline)
    assert entries[0]["bpm"] == 124.3
    assert entries[0]["bpm_sigma"] == 0.3025


def test_short_segment_cannot_stand_alone():
    """拍点太少的一段不足以支撑"这里变速了"。

    真实案例：`门尼 - 巴拉莱卡` 结尾只有 9 拍、拟合出 57.59 对 59.00，经人工核对是噪声。
    """
    head = grid(120.0, 200)                                  # 0–99.5s
    short = grid(100.0, 9, offset=100.0)                     # 9 拍、100 BPM，明显不同
    tail = grid(120.0, 200, offset=short[-1] + 0.6)
    times = head + short + tail
    sections = sections_from([(0.0, 100.0, "a"),
                              (100.0, short[-1] + 0.6, "b"),
                              (short[-1] + 0.6, 210.0, "c")])

    # 默认要求至少 16 拍才允许独立成段
    assert len(sectional_tempo(times, sections)) == 1

    # 把门槛调低，它就会独立出来——证明是这条规则在起作用，而不是拟合失败
    loose = sectional_tempo(times, sections, min_segment_beats=1)
    assert len(loose) == 3
    assert round(loose[1].bpm) == 100


def test_sectional_tempo_needs_sections():
    assert sectional_tempo(grid(120.0, 100), []) == []
    assert sectional_tempo([1.0], sections_from([(0.0, 10.0, "x")])) == []


def test_segments_to_tempo_map_lands_on_measure_ticks():
    from sheetsage2_webui.timeline import from_playback

    timeline = from_playback({"measures": [
        dict(index=0, start=0.0, end=10.0, score_start=0.0, score_end=1.0,
             leading_rest=0.0, trailing_rest=0.0),
        dict(index=1, start=10.0, end=20.0, score_start=1.0, score_end=2.0,
             leading_rest=0.0, trailing_rest=0.0),
    ]})
    segments = [TempoSegment(start=0.0, end=10.0, bpm=120.0, beats=20, anchor=0.0),
                TempoSegment(start=10.0, end=20.0, bpm=100.0, beats=20, anchor=10.0)]
    entries = segments_to_tempo_map(segments, timeline)

    assert [e["tick"] for e in entries] == [0, timeline.measures[1].tick_start]
    assert [e["bpm"] for e in entries] == [120.0, 100.0]


def test_segments_to_tempo_map_dedupes_same_tick():
    from sheetsage2_webui.timeline import from_playback

    timeline = from_playback({"measures": [
        dict(index=0, start=0.0, end=10.0, score_start=0.0, score_end=1.0,
             leading_rest=0.0, trailing_rest=0.0)]})
    segments = [TempoSegment(start=0.0, end=5.0, bpm=120.0, beats=10, anchor=0.0),
                TempoSegment(start=5.0, end=10.0, bpm=100.0, beats=10, anchor=5.0)]
    assert len(segments_to_tempo_map(segments, timeline)) == 1
