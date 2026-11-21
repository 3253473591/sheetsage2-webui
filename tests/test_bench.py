"""挂字评测的纯逻辑：人工重复字归一、offset 搜索、head/marker 指标、stress 分层。"""

from __future__ import annotations

import json
from pathlib import Path

from sheetsage2_webui.bench import (
    Char,
    Note,
    best_offset,
    delta_histogram,
    map_notes,
    normalize_heads,
    partition,
    pending,
    product_score,
    read_split,
    score_alignment,
    slice_rates,
    summarize,
    text_similarity,
    token_records,
)


def _note(start: float, end: float, lyric: str, pitch: int = 60) -> Note:
    return Note(start=start, end=end, pitch=pitch, lyric=lyric)


def test_normalize_heads_collapses_repeated_chars():
    assert normalize_heads(["九", "九", "乘", "-", "法"]) == [
        (0, "九"), (2, "乘"), (4, "法")]


def test_score_alignment_finds_exact_heads_after_offset():
    manual = [_note(1.0, 1.4, "你"), _note(1.5, 1.9, "好"),
              _note(2.0, 2.4, "吗")]
    chars = [Char("你", 0.8, 1.2), Char("好", 1.3, 1.7),
             Char("吗", 1.8, 2.2)]

    result = score_alignment(manual, chars, offset=0.2)
    assert result["heads"] == 3
    assert result["content"] == 3
    assert result["exact"] == 3
    assert result["pm1"] == 3
    assert result["missing"] == 0


def test_best_offset_searches_a_global_shift():
    manual = [_note(1.0, 1.4, "你"), _note(1.5, 1.9, "好"),
              _note(2.0, 2.4, "吗")]
    chars = [Char("你", 0.5, 0.6), Char("好", 1.0, 1.1),
             Char("吗", 1.5, 1.6)]

    offset, result = best_offset(manual, chars, max_abs=0.6, step=0.02)
    assert abs(offset - 0.5) <= 0.1
    assert result["content"] == 3


def test_marker_is_scored_separately_from_head():
    manual = [_note(1.0, 1.4, "你"), _note(1.5, 1.9, "-"),
              _note(2.0, 2.4, "好")]
    chars = [Char("你", 1.0, 1.9), Char("好", 2.0, 2.4)]

    result = score_alignment(manual, chars, offset=0.0)
    assert result["heads"] == 2
    assert result["markers"] == 1
    assert result["marker_hit"] == 1


# ---------------- 分层：stress 不进主指标 ----------------

def _write_meta(folder: Path, split: str | None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    payload = {} if split is None else {"split": split}
    (folder / "meta.json").write_text(json.dumps(payload), encoding="utf-8")


def _row(name: str, exact: int, heads: int, split: str) -> dict:
    return {"name": name, "split": split, "heads": heads, "exact": exact,
            "content": heads, "pm1": heads, "markers": 0, "marker_hit": 0}


def test_read_split_defaults_to_unassigned_without_meta(tmp_path):
    folder = tmp_path / "无 meta"
    folder.mkdir()
    assert read_split(folder) == "unassigned"


def test_read_split_reads_meta_and_survives_broken_json(tmp_path):
    folder = tmp_path / "带 meta"
    _write_meta(folder, "stress")
    assert read_split(folder) == "stress"

    (folder / "meta.json").write_text("{不是 json", encoding="utf-8")
    assert read_split(folder) == "unassigned"


def test_partition_keeps_stress_out_of_the_main_metric():
    rows = [_row("正常", 9, 10, "unassigned"), _row("对唱", 1, 10, "stress")]

    main, stress, text_off = partition(rows)
    assert [row["name"] for row in main] == ["正常"]
    assert [row["name"] for row in stress] == ["对唱"]
    assert text_off == []

    main_all, stress_all, _ = partition(rows, include_stress=True)
    assert len(main_all) == 2
    assert stress_all == []


def test_partition_separates_samples_with_a_different_lyric_text():
    rows = [_row("文本一致", 9, 10, "unassigned"),
            _row("文本不一致", 9, 10, "unassigned")]
    rows[0]["text_similarity"] = 0.98
    rows[1]["text_similarity"] = 0.52

    main, stress, text_off = partition(rows)
    assert [row["name"] for row in main] == ["文本一致"]
    assert [row["name"] for row in text_off] == ["文本不一致"]
    assert stress == []


def test_partition_keeps_mild_text_difference_in_the_main_metric():
    # 70%~90% 只告警、仍进主指标：元若蓝 87.8%、蒋大为 89.8% 这类样本不该被踢掉
    row = _row("轻微差异", 9, 10, "unassigned")
    row["text_similarity"] = 0.88

    main, stress, text_off = partition([row])
    assert [item["name"] for item in main] == ["轻微差异"]
    assert text_off == []


def test_text_similarity_is_zero_when_text_is_not_shared():
    manual = [_note(1.0, 1.4, "轰"), _note(1.5, 1.9, "鸣"),
              _note(2.0, 2.4, "声")]
    same = [Char("轰", 1.0, 1.4), Char("鸣", 1.5, 1.9), Char("声", 2.0, 2.4)]
    other = [Char("后", 1.0, 1.4), Char("面", 1.5, 1.9), Char("是", 2.0, 2.4)]

    assert text_similarity(manual, same) == 1.0
    assert text_similarity(manual, other) < 0.9


# ---------------- 诊断：Δ 分布 / 切片 / product ----------------

def test_token_records_exposes_delta_and_context():
    manual = [_note(1.0, 1.4, "你"), _note(1.5, 1.9, "-"),
              _note(2.0, 2.4, "好")]
    chars = [Char("你", 1.0, 1.9), Char("好", 2.0, 2.4)]

    records = token_records(manual, chars)
    assert [record["text"] for record in records] == ["你", "好"]
    assert all(record["delta"] == record["generated"] - record["manual"]
               for record in records)
    assert records[0]["next_marker"] is True      # 下一个音是延音 `-`
    assert records[1]["first_note"] is False


def test_delta_histogram_buckets_by_offset():
    records = [{"delta": 0}, {"delta": 1}, {"delta": 1}, {"delta": -1},
               {"delta": 4}, {"delta": -3}]

    assert delta_histogram(records) == {
        "<=-3": 1, "-2": 0, "-1": 1, "0": 1, "1": 2, "2": 0, ">=3": 1}


def test_slice_rates_counts_context():
    records = [
        {"delta": 0, "prev_marker": True, "next_marker": False,
         "duration": 0.1, "gap_before": 0.0, "first_note": False,
         "density": 1.0},
        {"delta": 1, "prev_marker": True, "next_marker": True,
         "duration": 1.5, "gap_before": 0.9, "first_note": True,
         "density": 4.0},
    ]

    rates = slice_rates(records)
    assert rates["上一音是延音"] == (1, 2)
    assert rates["音长 <0.25s"] == (1, 1)
    assert rates["音长 >1.0s"] == (0, 1)
    assert rates["密集字 ≥3/s"] == (0, 1)
    assert rates["全曲首音"] == (0, 1)


def test_map_notes_matches_onsets_monotonically():
    auto = [_note(1.0, 1.4, ""), _note(1.5, 1.9, ""), _note(2.0, 2.4, "")]
    manual = [_note(1.02, 1.4, ""), _note(1.52, 1.9, ""),
              _note(2.6, 3.0, "")]      # 第 3 个差了 0.6s，超出容差

    mapping = map_notes(auto, manual, tolerance=0.3)
    assert mapping == {0: 0, 1: 1}


def test_product_score_penalises_wrong_notes():
    manual = [_note(1.0, 1.4, "你"), _note(1.5, 1.9, "好")]
    auto = [_note(1.0, 1.4, ""), _note(1.5, 1.9, "")]
    chars = [Char("你", 1.0, 1.1), Char("好", 1.5, 1.6)]

    product = product_score(manual, auto, chars)
    assert product["exact"] == 2
    assert product["note_match_rate"] == 1.0



def test_adlib_tokens_absent_from_the_text_are_excluded():
    """人工谱写了 `oh`、但 lyrics.json 里没有：不算挂字错，从分母剔掉并报数。"""
    manual = [_note(1.0, 1.4, "oh"), _note(1.5, 1.9, "you")]
    chars = [Char("you", 1.5, 1.9)]

    result = score_alignment(manual, chars)
    assert result["heads"] == 1
    assert result["adlib_excluded"] == 1

    # 文本源里本来就有 oh：不算 ad-lib，正常参与评分
    result = score_alignment(manual, [Char("oh", 1.0, 1.4), Char("you", 1.5, 1.9)])
    assert result["adlib_excluded"] == 0
    assert result["heads"] == 2


def test_summarize_reports_pooled_and_song_mean():
    # 大歌 100 个 head 全错、小歌 2 个 head 全对：pooled 低，歌级均值是 50%
    rows = [_row("大歌", 0, 100, "unassigned"), _row("小歌", 2, 2, "unassigned")]

    stats = summarize(rows)
    assert stats["songs"] == 2
    assert stats["exact_ratio"] == 2 / 102
    assert stats["exact_song_mean"] == 0.5
    assert stats["exact_song_min"] == 0.0


def test_pending_lists_only_dirs_missing_manual(tmp_path):
    done = tmp_path / "已完成"
    done.mkdir()
    for name in ("project.json", "lyrics.json", "manual.svp"):
        (done / name).write_text("{}", encoding="utf-8")

    waiting = tmp_path / "待补"
    waiting.mkdir()
    for name in ("project.json", "lyrics.json"):
        (waiting / name).write_text("{}", encoding="utf-8")

    (tmp_path / "空目录").mkdir()

    assert pending(tmp_path) == ["待补"]


def test_read_midi_notes_reads_lyrics_and_seconds(tmp_path):
    """`ref.mid` 金标：tick 要按全局 tempo 换成秒，歌词按时间配到音符上。"""
    import mido
    from sheetsage2_webui.bench import read_midi_notes

    midi = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0))
    track.append(mido.MetaMessage("lyrics", text="He", time=0))
    track.append(mido.Message("note_on", note=60, velocity=100, time=0))
    track.append(mido.Message("note_off", note=60, velocity=0, time=480))
    track.append(mido.MetaMessage("lyrics", text="llo", time=0))
    track.append(mido.Message("note_on", note=62, velocity=100, time=480))
    track.append(mido.Message("note_off", note=62, velocity=0, time=480))
    path = tmp_path / "ref.mid"
    midi.save(str(path))

    notes = read_midi_notes(path)
    assert [note.lyric for note in notes] == ["He", "llo"]
    # 120 BPM、480 PPQ：第 2 个音在 1.0s
    assert abs(notes[0].start) < 1e-3
    assert abs(notes[1].start - 1.0) < 1e-3
