r"""填词逻辑的回归测试（不需要模型）。

    .\.venv\Scripts\python.exe -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.lyrics import (  # noqa: E402
    CONTINUATION,
    MELISMA,
    assign_lyrics,
    assign_lyrics_split,
    continuation_marker,
    fill_project,
)
from sheetsage2_webui.timeline import from_playback  # noqa: E402


def playback(*measures):
    """最小可用的 playback.json 片段（每小节恰好一个全音符长）。"""
    return {"measures": [
        dict(index=index, start=start, end=end, score_start=float(index),
             score_end=float(index + 1), leading_rest=0.0, trailing_rest=0.0)
        for index, (start, end) in enumerate(measures)
    ], "duration": measures[-1][1] if measures else 0.0}


class Char:
    """替身：只要有 .text / .start / .end 就行。"""

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


def spans(*pairs) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in pairs]


# --------------------------------------------------------------- 基本
def test_one_char_per_note():
    notes = spans((0.0, 0.5), (0.5, 1.0), (1.0, 1.5))
    chars = [Char("春", 0.0, 0.5), Char("眠", 0.5, 1.0), Char("不", 1.0, 1.5)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["春", "眠", "不"]
    assert fill.assigned == 3 and fill.melisma == 0
    assert fill.unmatched_notes == 0


def test_char_spanning_several_notes_becomes_melisma():
    """一个字唱了几个音：第一个音挂字，后续写转音标记。"""
    notes = spans((0.0, 0.3), (0.3, 0.6), (0.6, 0.9))
    chars = [Char("长", 0.0, 0.9)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["长", MELISMA, MELISMA]
    assert fill.assigned == 1 and fill.melisma == 2


def test_english_word_uses_plus_for_later_syllables():
    notes = spans((0.0, 0.3), (0.3, 0.6), (0.6, 0.9))
    chars = [Char("hello", 0.0, 0.9)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["hello", CONTINUATION, CONTINUATION]


def test_a_following_char_is_not_eaten_by_the_previous_melisma():
    """真实 bug：处理第一个字时贪心把下一个音标成 `-`，结果下一个字被吃掉。

    实测《光芒》开头会变成"曾 - 天 空"，"经"丢失。修法是先定所有字的头，再补转音。
    """
    notes = spans((30.0, 30.17), (30.17, 30.33), (30.33, 30.65), (30.65, 30.89))
    chars = [Char("曾", 30.01, 30.17), Char("经", 30.17, 30.33),
             Char("天", 30.33, 30.65), Char("空", 30.65, 30.89)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["曾", "经", "天", "空"]
    assert fill.melisma == 0
    assert fill.assigned == 4


def test_melisma_stops_at_the_next_char():
    """转音只延伸到下一个字的头为止，不会越过它。"""
    notes = spans((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8))
    chars = [Char("长", 0.0, 0.4), Char("短", 0.6, 0.8)]
    fill = assign_lyrics(notes, chars, tolerance=0.0)
    # 长 -> note0 为头；note1 落在 [0, 0.4) 内且早于"短"的头(note3) -> 转音
    assert fill.lyrics == ["长", MELISMA, "", "短"]


def test_continuation_marker_picks_by_script():
    assert continuation_marker("你") == MELISMA
    assert continuation_marker("あ") == MELISMA
    assert continuation_marker("hello") == CONTINUATION
    assert continuation_marker("abc") == CONTINUATION


def test_zero_length_char_still_gets_a_note():
    """对齐器对约 20% 的字给出零长度时间戳（start == end = "没对齐上"）。

    `[start - 容差, end)` 这时会退化成空窗口，把它们全漏掉——实测《光芒》有 53/324 个字
    是这样。零长度就当"这个字发生在这一刻"，给它一个以它为中心的对称窗口。
    """
    notes = spans((58.900, 59.190), (59.190, 59.770), (60.055, 60.340))
    chars = [Char("雅", 59.130, 59.130), Char("山", 59.130, 59.130)]
    fill = assign_lyrics(notes, chars)
    # 两个点状的字分别落到最近的两个音上，而不是被丢掉
    assert fill.dropped == 0
    assert "雅" in fill.lyrics and "山" in fill.lyrics


def test_zero_length_char_uses_nearest_start_not_overlap():
    notes = spans((10.0, 10.5), (11.0, 11.5))
    fill = assign_lyrics(notes, [Char("点", 10.45, 10.45)], tolerance=0.05)
    # 10.45 离 10.0 更近（0.45）而不是 11.0（0.55）
    assert fill.lyrics == ["点", ""]


def test_zero_length_char_does_not_create_melisma():
    notes = spans((0.0, 0.5), (0.5, 1.0), (1.0, 1.5))
    fill = assign_lyrics(notes, [Char("点", 0.0, 0.0)])
    assert fill.lyrics[0] == "点"
    assert fill.melisma == 0


def test_assignment_is_monotonic_in_note_order():
    """后面的字不能挂到前面的音上。

    实测《光芒》"喜马拉雅山"会输出成"喜 马 拉 山 雅"——雅和山都是零长度、同一时刻，
    退路各自找最近的音就颠倒过来了。
    """
    notes = spans((58.90, 59.19), (59.19, 59.77), (60.05, 60.34), (60.34, 60.92))
    chars = [Char("雅", 59.13, 59.13), Char("山", 59.13, 59.13)]
    fill = assign_lyrics(notes, chars)
    assert fill.dropped == 0
    positions = [i for i, text in enumerate(fill.lyrics) if text in ("雅", "山")]
    assert positions == sorted(positions), "先出现的字必须落在更靠前的音上"
    assert fill.lyrics[positions[0]] == "雅"
    assert fill.lyrics[positions[1]] == "山"


# --------------------------------------------------------------- 下标 bug 回归
def test_chars_are_not_dropped_when_the_sorted_order_differs_from_note_order():
    """回归：`last_pos` 曾经同时被当成"排序后的位置"和"音符下标"用。

    两者只有在音符本来就按起点有序时才相等。一旦某个音更早开始，扫描起点就跳到了
    旋律中段，那个位置之前的音再也扫不到——实测《光芒》因此丢掉 29/324 个字，
    而且全被误报成"没有重叠"（真正原因是它们根本没被扫到）。

    这里的音符故意乱序（note2 比 note1 早），字按时间铺开，一个字都不该丢。
    """
    notes = spans((0.0, 0.25), (0.8, 1.05), (0.25, 0.5), (0.5, 0.8))
    chars = [Char("一", 0.01, 0.24), Char("二", 0.26, 0.49),
             Char("三", 0.51, 0.79), Char("四", 0.81, 1.04)]
    fill = assign_lyrics(notes, chars, tolerance=0.05)

    assert fill.dropped == 0, f"丢了 {fill.dropped_chars}"
    assert set(fill.lyrics) == {"一", "二", "三", "四"}
    # 每个字都挂到了时间上对应的那个音
    assert fill.lyrics == ["一", "四", "二", "三"]


def test_every_char_lands_somewhere_or_is_reported():
    """字只能有三种下场：显示出来、算作转音延续、或者被登记；绝不能悄悄消失。"""
    notes = spans((0.0, 0.4), (0.4, 0.8), (0.8, 1.2))
    chars = [Char("甲", 0.0, 0.2), Char("乙", 0.2, 0.4), Char("丙", 0.4, 0.6),
             Char("丁", 0.6, 0.8), Char("戊", 0.8, 1.0), Char("己", 1.0, 1.2)]
    fill = assign_lyrics(notes, chars)
    accounted = (fill.assigned + fill.dropped)
    assert accounted == len(chars)                       # 每个字都被处理过
    assert fill.dropped + len(fill.absorbed_chars) + fill.melisma <= len(chars)
    if fill.absorbed_chars:
        assert fill.warnings                            # 被挤掉必须出声


def test_chars_competing_for_one_note_are_registered_not_lost():
    """段落的音比字少时，多出来的字被登记（absorbed），而不是悄悄覆盖。"""
    notes = spans((0.0, 1.0))                            # 一个长音
    chars = [Char("一", 0.0, 0.3), Char("二", 0.3, 0.6), Char("三", 0.6, 0.9)]
    fill = assign_lyrics(notes, chars)

    assert fill.dropped == 0
    assert fill.lyrics[0] == "一"                        # 先来的占住音位
    assert set(fill.absorbed_chars) == {"二", "三"}
    assert fill.assigned == 3
    assert any("已经占住" in w for w in fill.warnings)


def test_note_spans_prefer_raw_tick_over_the_quantized_one():
    """填词要对着**量化前**的位置匹配。

    逐字时间戳是沿着模型自己的音符时间轴产生的；量化把 tick 吸到网格上之后，两者会差
    半个网格步长（实测最大 0.86s）。实测影响：《光芒》"显示不出来"的字 29 → 60，
    《相思》18 → 42。所以量化会把原位置存进 `raw_tick`，填词优先用它。
    """
    from sheetsage2_webui.lyrics import note_spans_seconds

    timeline = from_playback(playback((0.0, 2.0)))       # 一全音符 = 2s → 每 tick 1/960 s
    notes = [
        {"tick": 0, "dur": 960, "pitch": 60, "raw_tick": 0, "raw_dur": 960},
        # 量化把它从 940 挪到 960，时长也从 940 变成 1000
        {"tick": 960, "dur": 1000, "pitch": 62, "raw_tick": 940, "raw_dur": 940},
    ]
    spans = note_spans_seconds(timeline, notes)
    assert spans[1][0] == pytest.approx(timeline.tick_to_sec(940))
    assert spans[1][1] == pytest.approx(timeline.tick_to_sec(1880))
    assert spans[1][0] != pytest.approx(timeline.tick_to_sec(960))

    # 没有 raw_* 时退回 tick / dur（老工程 / 没开量化）
    legacy = [{"tick": 0, "dur": 960, "pitch": 60},
              {"tick": 960, "dur": 960, "pitch": 62}]
    assert note_spans_seconds(timeline, legacy)[1] == \
        (pytest.approx(timeline.tick_to_sec(960)),
         pytest.approx(timeline.tick_to_sec(1920)))


def test_fill_project_uses_raw_tick_so_quantization_does_not_break_alignment():
    """端到端：量化把音挪走之后，只要带着 raw_tick，填词结果不变。"""
    project = project_fixture()
    chars = [Char("一", 0.0, 0.25), Char("二", 0.25, 0.5),
             Char("三", 0.5, 0.75), Char("四", 0.75, 1.0)]
    baseline = fill_project(project_fixture(), chars)["lyrics"]

    moved = project_fixture()
    for index, note in enumerate(moved["tracks"][0]["notes"]):
        note["raw_tick"] = note["tick"]
        note["tick"] = note["tick"] + 120 * (index + 1)   # 假装量化把它们挪走了
    assert fill_project(moved, chars)["lyrics"] == baseline


def test_quantize_notes_carry_raw_position_and_keep_lyric():
    """量化留下的 `raw_tick` / `raw_dur` 必须等于量化前的值，并且不动已填的歌词。"""
    from sheetsage2_webui.quantize import quantize_tracks

    track = {"id": "vocal", "notes": [
        {"tick": 1730, "dur": 950, "pitch": 60, "lyric": "一"},
        {"tick": 2900, "dur": 900, "pitch": 62, "lyric": "二"},
    ]}
    new_tracks, stats = quantize_tracks([track], ticks_per_quarter=1920)
    assert stats.failed_tracks == 0
    assert [n["raw_tick"] for n in new_tracks[0]] == [1730, 2900]
    assert [n["raw_dur"] for n in new_tracks[0]] == [950, 900]
    assert [n["lyric"] for n in new_tracks[0]] == ["一", "二"]
    # 落网格的不变量是**网格步长**（一拍/96 = 20 tick）
    assert stats.placement_ticks == 1920 // 96
    assert all(n["tick"] % stats.placement_ticks == 0 for n in new_tracks[0])
    assert all((n["tick"] + n["dur"]) % stats.placement_ticks == 0 for n in new_tracks[0])


# --------------------------------------------------------------- 容差与退化
def test_tolerance_lets_a_slightly_early_char_land():
    """容差是往**前**放宽：字来得比音符晚一点时，仍然能挂到那个音符上。"""
    notes = spans((1.00, 1.10))
    chars = [Char("晚", 1.20, 1.30)]           # 字整个落在音符之后，没有重叠
    assert assign_lyrics(notes, chars, tolerance=0.20).lyrics == ["晚"]
    assert assign_lyrics(notes, chars, tolerance=0.05).lyrics == [""]


def test_char_with_no_note_is_dropped_and_reported():
    notes = spans((5.0, 5.5))
    chars = [Char("丢", 0.0, 0.3)]
    fill = assign_lyrics(notes, chars)
    assert fill.dropped == 1
    assert fill.dropped_chars == ["丢"]
    assert fill.warnings


def test_falls_back_to_the_most_overlapping_note():
    """没有音符起点落在窗口里时，挂到重叠最多的那个音。"""
    notes = spans((0.0, 1.0), (1.0, 2.0))
    chars = [Char("回", 0.85, 0.95)]           # 起点都不在窗口内
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["回", ""]           # 与第一个音重叠最多


def test_empty_inputs_are_safe():
    assert assign_lyrics([], []).lyrics == []
    fill = assign_lyrics(spans((0.0, 1.0)), [])
    assert fill.lyrics == [""]
    assert fill.unmatched_notes == 1


def test_blank_char_text_is_ignored():
    fill = assign_lyrics(spans((0.0, 1.0)), [Char("  ", 0.0, 1.0)])
    assert fill.lyrics == [""]


# --------------------------------------------------------------- 既有歌词
def test_only_empty_keeps_hand_written_lyrics():
    notes = spans((0.0, 0.5), (0.5, 1.0))
    chars = [Char("新", 0.0, 0.5), Char("词", 0.5, 1.0)]
    fill = assign_lyrics(notes, chars, existing=["手写", ""], only_empty=True)
    assert fill.lyrics == ["手写", "词"]


def test_existing_is_overwritten_by_default():
    notes = spans((0.0, 0.5))
    fill = assign_lyrics(notes, [Char("新", 0.0, 0.5)], existing=["旧"])
    assert fill.lyrics == ["新"]


# --------------------------------------------------------------- 工程
def project_fixture() -> dict:
    """两个小节、四个音符的极小工程。"""
    return {
        "version": 1,
        "ppq": 1920,
        "tracks": [{"id": "vocal", "role": "vocal-lead", "notes": [
            {"tick": 0, "dur": 960, "pitch": 60, "lyric": ""},
            {"tick": 960, "dur": 960, "pitch": 62, "lyric": ""},
            {"tick": 1920, "dur": 960, "pitch": 64, "lyric": ""},
            {"tick": 2880, "dur": 960, "pitch": 65, "lyric": ""},
        ]}],
        "timeline": {
            "measures": [
                {"index": 0, "start": 0.0, "end": 2.0, "tick_start": 0, "tick_end": 7680,
                 "numerator": 4, "denominator": 4, "is_pickup": False},
            ],
            "pickup_seconds": 0.0,
            "tick_length": 7680,
        },
    }


def test_fill_project_writes_lyrics_into_the_track():
    project = project_fixture()
    # 小节 0–2s 覆盖 0–7680 tick；每个音 960 tick = 0.25s
    chars = [Char("一", 0.0, 0.25), Char("二", 0.25, 0.5),
             Char("三", 0.5, 0.75), Char("四", 0.75, 1.0)]
    result = fill_project(project, chars)
    assert result["found"] is True
    notes = project["tracks"][0]["notes"]
    assert [n["lyric"] for n in notes] == ["一", "二", "三", "四"]
    assert result["fill"]["assigned"] == 4


def test_fill_project_reports_a_missing_track():
    result = fill_project(project_fixture(), [], track_id="nope")
    assert result["found"] is False
    assert "nope" in result["reason"]


def test_fill_project_respects_tick_to_second_mapping():
    """音符时间是从存下来的小节表反算的，不是按 BPM 猜的。"""
    project = project_fixture()
    # 把第二个音往后挪到 1920 tick（=0.5s），于是它不再是 0.25s 那个音
    project["tracks"][0]["notes"][1]["tick"] = 1920
    chars = [Char("甲", 0.0, 0.2), Char("乙", 0.4, 0.6)]
    fill_project(project, chars)
    lyrics = [n["lyric"] for n in project["tracks"][0]["notes"]]
    assert lyrics[0] == "甲"
    # 乙落在 0.25s 那个音上——证明是按小节表反算的秒在匹配
    assert lyrics[1] == "乙"


# --------------------------------------------------------------------- ③ 切分模式
def test_split_mode_gives_each_char_its_own_piece():
    """一音多字：一个长音上唱了两个字 -> 切成两段，两个字都看得见。"""
    spans = [(0.0, 2.0), (2.0, 3.0)]
    chars = [Char("一", 0.2, 1.0), Char("二", 1.2, 1.8)]
    fill = assign_lyrics_split(spans, chars)

    assert [note.text for note in fill.notes] == ["一", "二"]
    assert [note.source for note in fill.notes] == [0, 0]      # 都来自同一个原音
    assert fill.notes[0].start == 0.2 and fill.notes[0].end == 1.0
    assert fill.notes[1].start == 1.2 and fill.notes[1].end == 1.8
    assert all(not note.slur for note in fill.notes)           # 各自是头，不是延音
    assert fill.visible == 2 and fill.absorbed == 0
    # 代价：原音 2 个 -> 3 段；切开 1 个音
    assert fill.cut_notes == 1 and fill.added_notes == 1 and fill.original_notes == 2
    assert fill.ratio == pytest.approx(0.5)


def test_split_mode_marks_following_segments_as_slur():
    """一字多音：一个字跨两个音 -> 头 + 后续段（对应现在的转音标记）。"""
    spans = [(0.0, 1.0), (1.0, 2.0)]
    fill = assign_lyrics_split(spans, [Char("长", 0.2, 1.8)])

    assert [note.text for note in fill.notes] == ["长", ""]
    assert [note.slur for note in fill.notes] == [False, True]
    assert fill.visible == 1 and fill.added_notes == 0        # 没多出音符，只是重新分段
    assert fill.notes[0].end == 1.0 and fill.notes[1].start == 1.0


def test_split_mode_does_not_overlap_or_grow_total_duration():
    """切出来的段不能重叠，总时长也不能超过原音——否则等于凭空加时间。"""
    spans = [(0.0, 3.0)]
    chars = [Char("甲", 0.1, 0.9), Char("乙", 1.0, 2.0), Char("丙", 2.0, 2.8)]
    fill = assign_lyrics_split(spans, chars)

    assert [note.text for note in fill.notes] == ["甲", "乙", "丙"]
    for before, after in zip(fill.notes, fill.notes[1:]):
        assert before.end <= after.start + 1e-9
    assert fill.notes[0].start >= spans[0][0]
    assert fill.notes[-1].end <= spans[0][1] + 1e-9


def test_split_mode_falls_back_for_zero_length_timestamps():
    """零长度时间戳的字没有边界可切，但**不能丢**——退回"离得最近的音"。"""
    spans = [(0.0, 1.0), (1.0, 2.0)]
    # 空文本的字直接跳过，不算"零长度"（它根本不是字）
    assert assign_lyrics_split(spans, [Char("", 1.0, 1.0)]).degenerate == 0

    fill = assign_lyrics_split(spans, [Char("晚", 0.2, 0.8), Char("风", 1.0, 1.0)])
    assert fill.degenerate == 1
    assert fill.fallback == 1 and fill.visible == 2             # 兜底挂上了，没丢字


def test_split_mode_reports_chars_with_no_note_to_attach():
    spans = [(0.0, 1.0), (3.0, 4.0)]
    fill = assign_lyrics_split(spans, [Char("缝", 1.8, 2.2)])
    assert fill.dropped == 1 and fill.visible == 0
    assert fill.warnings and "间隙" in fill.warnings[0]


def test_split_mode_is_not_the_default_path():
    """切分模式目前只是"候选"：默认填词仍然走 onset（音符一个都不动）。

    这条测试是把决定锁住——真要以切分为默认，得先改这里，别悄悄换掉。
    """
    spans = [(0.0, 2.0)]
    chars = [Char("一", 0.2, 1.0), Char("二", 1.2, 1.8)]
    onset = assign_lyrics(spans, chars)
    assert len(onset.lyrics) == len(spans)                      # onset 不新增音符
    assert onset.lyrics == ["一"]                               # "二" 被挤占
    assert len(onset.absorbed_chars) == 1