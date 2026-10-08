r"""`timeline` 模块的回归测试。

核心断言只有一条：**秒 ↔ tick 是无损的**，而且这件事与 BPM 无关。
另外覆盖弱起小节的两种处理和"没有小节表时用强拍兜底"。

    .\.venv\Scripts\python.exe -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.timeline import (  # noqa: E402
    PPQ,
    WHOLE_TICKS,
    from_beats,
    from_playback,
    notes_to_ticks,
    round_trip_error,
)


def playback(*measures):
    """构造一个最小可用的 playback.json 片段。"""
    rows = []
    for index, (start, end, score_start, score_end, leading, trailing) in enumerate(measures):
        rows.append(dict(index=index, start=start, end=end, score_start=score_start,
                         score_end=score_end, leading_rest=leading, trailing_rest=trailing))
    return {"measures": rows, "duration": measures[-1][1] if measures else 0.0}


def test_measure_table_maps_seconds_to_ticks_without_bpm():
    """4/4 两小节，每小节 2 秒：不需要任何 BPM 就能定位。"""
    timeline = from_playback(playback(
        (0.0, 2.0, 0.0, 1.0, 0.0, 0.0),
        (2.0, 4.0, 1.0, 2.0, 0.0, 0.0),
    ))
    assert [m.ticks for m in timeline.measures] == [WHOLE_TICKS, WHOLE_TICKS]
    assert timeline.sec_to_tick(1.0) == pytest.approx(WHOLE_TICKS / 2)
    assert timeline.sec_to_tick(2.0) == pytest.approx(WHOLE_TICKS)
    assert timeline.sec_to_tick(3.0) == pytest.approx(WHOLE_TICKS * 1.5)
    assert timeline.tick_to_sec(WHOLE_TICKS * 1.5) == pytest.approx(3.0)


def test_varying_tempo_uses_the_same_code_path():
    """变速曲：每小节秒数不同，映射照样精确，仍然不涉及 BPM。"""
    timeline = from_playback(playback(
        (0.0, 2.0, 0.0, 1.0, 0.0, 0.0),      # 120 BPM
        (2.0, 4.4, 1.0, 2.0, 0.0, 0.0),      # 100 BPM
        (4.4, 6.0, 2.0, 3.0, 0.0, 0.0),      # 150 BPM
    ))
    times = [0.0, 0.5, 1.9, 2.0, 3.3, 4.4, 5.0, 6.0]
    assert round_trip_error(timeline, times) < 1e-9
    bpms = [round(m.bpm, 2) for m in timeline.measures]
    assert bpms == [120.0, 100.0, 150.0]


def test_round_trip_is_lossless_on_a_dense_grid():
    timeline = from_playback(playback(
        (0.01, 2.65, 0.0, 1.0, 0.0, 0.0),
        (2.65, 4.96, 1.0, 2.0, 0.0, 0.0),
        (4.96, 7.27, 2.0, 3.0, 0.0, 0.0),
    ))
    times = [0.01 + 0.01 * i for i in range(700)]
    assert round_trip_error(timeline, times) < 1e-9


# --------------------------------------------------------------- 弱起
PICKUP = playback(
    (0.01, 0.28, 0.0, 1.0, 0.875, 0.0),      # 补成整小节的 1/8 弱起
    (0.28, 2.65, 1.0, 2.0, 0.0, 0.0),
)


def test_pickup_actual_shrinks_the_first_measure():
    timeline = from_playback(PICKUP, pickup="actual")
    first = timeline.measures[0]
    assert first.is_pickup is True
    assert first.padded_ticks == WHOLE_TICKS
    assert first.ticks == WHOLE_TICKS // 8          # 1/8 音符 = 960 tick
    assert first.numerator == 1 and first.denominator == 8
    assert timeline.measures[1].tick_start == WHOLE_TICKS // 8
    assert timeline.pickup_seconds() == pytest.approx(0.27)
    assert timeline.warnings


def test_pickup_padded_keeps_the_model_notation():
    timeline = from_playback(PICKUP, pickup="padded")
    assert timeline.measures[0].ticks == WHOLE_TICKS
    assert timeline.measures[1].tick_start == WHOLE_TICKS


def test_both_pickup_modes_stay_lossless():
    times = [0.01, 0.15, 0.27, 0.28, 1.0, 2.0, 2.64]
    for mode in ("actual", "padded"):
        assert round_trip_error(from_playback(PICKUP, pickup=mode), times) < 1e-9


def test_invalid_pickup_mode_is_rejected():
    with pytest.raises(ValueError):
        from_playback(PICKUP, pickup="whatever")


# --------------------------------------------------------------- 其他
def test_empty_measure_table_warns_instead_of_crashing():
    timeline = from_playback({"measures": []})
    assert timeline.measures == []
    assert timeline.warnings
    with pytest.raises(ValueError):
        timeline.sec_to_tick(1.0)


def test_notes_to_ticks_is_sorted_and_has_no_zero_durations():
    timeline = from_playback(playback((0.0, 2.0, 0.0, 1.0, 0.0, 0.0)))
    notes = [[1.5, 1.5, 60, 0], [0.0, 0.5, 62, 1], [0.25, 0.5, 64, 0]]
    ticks = notes_to_ticks(notes, timeline)

    assert [n["tick"] for n in ticks] == sorted(n["tick"] for n in ticks)
    assert all(n["dur"] >= 1 for n in ticks)
    assert ticks[0]["pitch"] == 62 and ticks[0]["tick"] == 0


def test_from_beats_fallback_builds_measures_from_downbeats():
    """没有 playback.json 时，用 beat.lab 的强拍兜底。"""
    rows = [
        ["0.00", "1", "4", "4"],
        ["0.50", "2", "4", "4"],
        ["1.00", "3", "4", "4"],
        ["1.50", "4", "4", "4"],
        ["2.00", "1", "4", "4"],
        ["2.50", "2", "4", "4"],
        ["3.00", "3", "4", "4"],
        ["3.50", "4", "4", "4"],
    ]
    timeline = from_beats(rows, duration=4.0, meter=(4, 4))
    assert timeline.source == "beats"
    assert len(timeline.measures) == 2
    assert timeline.measures[0].ticks == WHOLE_TICKS
    assert round_trip_error(timeline, [0.0, 1.0, 2.0, 3.0]) < 1e-9


def test_from_beats_makes_a_pickup_before_the_first_downbeat():
    rows = [
        ["0.00", "3", "4", "4"],          # 弱起（不是强拍）
        ["0.50", "1", "4", "4"],
        ["1.00", "2", "4", "4"],
        ["1.50", "3", "4", "4"],
        ["2.00", "4", "4", "4"],
        ["2.50", "1", "4", "4"],
    ]
    timeline = from_beats(rows, duration=3.0, meter=(4, 4), pickup="actual")
    assert timeline.measures[0].is_pickup is True
    assert timeline.measures[0].ticks < WHOLE_TICKS


def test_tempo_map_is_monotonic_in_ticks():
    timeline = from_playback(playback(
        (0.0, 2.0, 0.0, 1.0, 0.0, 0.0),
        (2.0, 4.4, 1.0, 2.0, 0.0, 0.0),
    ))
    ticks = [entry["tick"] for entry in timeline.tempo_map()]
    assert ticks == sorted(ticks)
    assert all(entry["bpm"] > 0 for entry in timeline.tempo_map())


def test_ppq_defaults_to_1920():
    assert PPQ == 1920
    assert from_playback(playback((0.0, 1.0, 0.0, 1.0, 0.0, 0.0))).ppq == 1920


# ----------------------------------------- 速度表与音符 tick 是解耦的
def test_per_measure_tempo_map_reproduces_the_original_duration():
    """逐小节速度表能把 tick 精确换回原来的秒——这是导出必须用它的原因。"""
    from sheetsage2_webui.pipeline import _played_seconds

    timeline = from_playback(playback(
        (0.0, 2.0, 0.0, 1.0, 0.0, 0.0),      # 2s / 全音符 -> 120 BPM
        (2.0, 4.4, 1.0, 2.0, 0.0, 0.0),      # 2.4s -> 100 BPM
        (4.4, 6.0, 2.0, 3.0, 0.0, 0.0),      # 1.6s -> 150 BPM
    ))
    played = _played_seconds(timeline.tempo_map(), timeline.tick_length)
    assert played == pytest.approx(6.0, abs=1e-9)


def test_single_tempo_does_not_reproduce_a_varying_duration():
    """换成单一速度，tick 不动但总时长会跑偏——所以"改 BPM 不用改音符"不等于"没影响"。"""
    from sheetsage2_webui.pipeline import _played_seconds

    timeline = from_playback(playback(
        (0.0, 2.0, 0.0, 1.0, 0.0, 0.0),
        (2.0, 4.4, 1.0, 2.0, 0.0, 0.0),
    ))
    flat = [{"tick": 0, "bpm": 120.0}]
    played = _played_seconds(flat, timeline.tick_length)
    # 两小节实际是 120 与 100 BPM，写成统一的 120 会播得比原曲短
    assert played != pytest.approx(4.4, abs=0.01)
    assert played == pytest.approx(4.0, abs=0.01)
