r"""八度一致性校正（`octave.py`）的单元测试。

规则一句话：**同一段旋律在歌里出现多次时，音高只允许差 0，不允许差一个八度**；
按"改动最少"挑哪一次是错的；两个候选一样多（平票）就不动手。

    .\.venv\Scripts\python.exe -m pytest tests/test_octave.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui import octave  # noqa: E402

#: 每个音 0.5 秒（= 90 BPM 的八分音符），远短于 REST_GAP，所以一段是一句。
STEP = 0.5
#: 段与段之间的静音，远长于 REST_GAP，所以一定断句。
REST = 1.0

#: 一段 12 个音的旋律：音级各不相同，重复时不会被"碰巧一样"骗到。
A = [60, 62, 64, 65, 67, 69, 71, 72, 71, 69, 67, 65]


def down(pitches, semitones=12):
    return [p - semitones for p in pitches]


def melody(*parts, tracks=None, step=STEP):
    """把几段旋律铺成一条时间轴；段间留 `REST` 秒静音。"""
    notes, t = [], 0.0
    for index, pitches in enumerate(parts):
        track = 0 if tracks is None else tracks[index]
        for pitch in pitches:
            notes.append(octave.Note(start=t, end=t + step, pitch=pitch,
                                     track=track))
            t += step
        t += REST
    return notes


# --------------------------------------------------------------- 基本不变量
def test_a_song_without_repeats_is_never_touched():
    """没有重复段 → 一个音都不动（这条保证新加的校正不会改坏别的歌）。"""
    notes = melody([60, 62, 64, 65, 67, 69, 71, 73])
    shifts, plan_ = octave.plan(notes)
    assert shifts == [0] * len(notes)
    assert plan_.empty
    assert plan_.stats.moved_notes == 0


def test_a_short_repeat_is_ignored():
    """重复太短（< `MIN_BLOCK` 个音）不算证据——宁可漏，不可错。"""
    short = [60, 62, 64, 65, 67, 69]
    notes = melody(short, down(short))
    shifts, _plan = octave.plan(notes)
    assert shifts == [0] * len(notes)


def test_a_phrase_an_octave_low_is_raised_back():
    """同一段旋律出现三次，中间那次低一个八度 → 把中间那次抬回来。

    少数服从多数（两次 66 / 一次 54）——和 `samples/对齐` 里人工修的一模一样。
    """
    notes = melody(A, down(A), A)
    shifts, plan_ = octave.plan(notes)
    assert len(notes) == 36
    assert shifts[:12] == [0] * 12
    assert shifts[12:24] == [12] * 12
    assert shifts[24:] == [0] * 12
    assert plan_.stats.moved_notes == 12
    assert plan_.stats.links >= 1


def test_a_tie_moves_nothing():
    """两次出现一样长（平票）→ 分不清哪次是错的，不动手。"""
    notes = melody(A, down(A))
    shifts, plan_ = octave.plan(notes)
    assert shifts == [0] * len(notes)
    assert plan_.stats.moved_notes == 0


def test_a_difference_that_is_not_a_whole_octave_is_left_alone():
    """差 7 个半音不是八度错误，不猜（只认整八度的差异）。"""
    notes = melody(A, down(A, 7), A)
    shifts, _plan = octave.plan(notes)
    assert shifts == [0] * len(notes)


def test_tracks_are_judged_separately():
    """人声要抬、器乐本来就一致 → 只动人声。"""
    notes = melody(A, down(A), A, tracks=[0, 0, 0])
    notes += melody(A, A, tracks=[1, 1])
    shifts, _plan = octave.plan(notes)
    assert shifts[:36] == [0] * 12 + [12] * 12 + [0] * 12
    assert shifts[36:] == [0] * len(shifts[36:])


# --------------------------------------------------------------- 查表 / 应用
def test_plan_lookup_tolerates_the_midi_time_rounding():
    """MIDI 的起始时刻是从 tick 换回来的，和 lab 差 ~1e-4 秒，查表要认得。"""
    notes = melody(A, down(A), A)
    _shifts, plan_ = octave.plan(notes)
    start = notes[12].start
    assert plan_.shift_for(start, 0) == 12
    assert plan_.shift_for(start + 1e-4, 0) == 12
    assert plan_.shift_for(start - 1e-4, 0) == 12
    assert plan_.shift_for(notes[0].start, 0) == 0        # 第一段没动
    assert plan_.shift_for(notes[24].start, 0) == 0       # 第三段没动


def test_lab_text_only_the_pitch_column_moves():
    """`normalize_lab_text` 只改第 3 列；列数 / 分隔符 / 轨道列都原样。"""
    notes = melody(A, down(A), A)
    shifts, plan_ = octave.plan(notes)
    rows = "".join(f"{n.start}\t{n.end}\t{n.pitch}\t{n.track}\n" for n in notes)
    text, moved = octave.normalize_lab_text(rows, plan_)
    assert moved == 12
    out = [line.split("\t") for line in text.splitlines()]
    assert len(out) == len(notes)
    for index, fields in enumerate(out):
        assert len(fields) == 4
        assert int(fields[2]) == notes[index].pitch + shifts[index]
        assert float(fields[0]) == notes[index].start
        assert int(fields[3]) == notes[index].track


def test_lab_text_without_a_track_column_uses_the_given_track():
    """`melody_vocal.lab` 只有 3 列，轨道号由调用方给。"""
    notes = melody(A, down(A), A)
    _shifts, plan_ = octave.plan(notes)
    rows = "".join(f"{n.start} {n.end} {n.pitch}\n" for n in notes)
    text, moved = octave.normalize_lab_text(rows, plan_, track=0)
    assert moved == 12
    pitches = [int(line.split()[2]) for line in text.splitlines()]
    assert pitches == [n.pitch + s for n, s in zip(notes, [0] * 12 + [12] * 12 + [0] * 12)]


def test_phrase_splitting_is_by_rest():
    notes = melody(A, A)
    phrases = octave.split_phrases(notes, octave.REST_GAP)
    assert [len(p) for p in phrases] == [12, 12]


def test_parse_lab_rows_reads_the_track_column():
    rows = octave.parse_lab_rows("1.0 1.5 60 0\n1.0 1.5 48 1\n坏了的一行\n")
    assert [(n.pitch, n.track) for n in rows] == [(60, 0), (48, 1)]


@pytest.mark.parametrize("name,expected", [("Vocal", 0), ("Ins", 1), ("", 0)])
def test_track_name_mapping(name, expected):
    assert octave.track_of_name(name) == expected


# --------------------------------------------------------------- 接进流水线
def test_apply_moves_labs_midi_and_playback_together(tmp_path):
    """`pitch.apply` 里 lab / MIDI / playback 走的是同一份计划，不能各改各的。

    MIDI 的起始时刻是从 tick 换回来的（和 lab 差 ~1e-4 秒），所以这条同时钉住
    "按时间最近邻查表"这件事。
    """
    import json

    import pretty_midi

    from sheetsage2_webui import pitch

    notes = melody(A, down(A), A)
    (tmp_path / "melody_vocal.lab").write_text(
        "".join(f"{n.start}\t{n.end}\t{n.pitch}\n" for n in notes), encoding="utf-8")
    (tmp_path / "melody_full.lab").write_text(
        "".join(f"{n.start}\t{n.end}\t{n.pitch}\t0\n" for n in notes),
        encoding="utf-8")

    midi = pretty_midi.PrettyMIDI(initial_tempo=120, resolution=960)
    instrument = pretty_midi.Instrument(program=0, name="Vocal")
    for note in notes:
        instrument.notes.append(pretty_midi.Note(velocity=100, pitch=note.pitch,
                                                 start=note.start, end=note.end))
    midi.instruments.append(instrument)
    midi.write(str(tmp_path / "melody_vocal.mid"))

    (tmp_path / "playback.json").write_text(json.dumps({
        "tracks": [{"name": "Vocal", "notes": [
            {"pitch": n.pitch, "start": n.start, "end": n.end} for n in notes]}],
    }), encoding="utf-8")

    _updated, stats = pitch.apply({"labs": {}}, tmp_path, semitones=0, octave_fix=True)
    assert stats.octave["moved_notes"] == 12

    want = [n.pitch + s for n, s in zip(notes, [0] * 12 + [12] * 12 + [0] * 12)]
    got_lab = [int(line.split("\t")[2])
               for line in (tmp_path / "melody_vocal.lab").read_text(
                   encoding="utf-8").splitlines()]
    got_mid = [n.pitch for inst in pretty_midi.PrettyMIDI(
        str(tmp_path / "melody_vocal.mid")).instruments for n in inst.notes]
    got_playback = [n["pitch"] for track in json.loads(
        (tmp_path / "playback.json").read_text(encoding="utf-8"))["tracks"]
        for n in track["notes"]]
    assert got_lab == want
    assert got_mid == want
    assert got_playback == want


def test_octave_correction_runs_even_when_the_global_shift_is_off(tmp_path):
    """只做八度校正（`semitones=0`）时照样要落地——否则"关掉整体平移"就没法单独看它。"""
    import json

    from sheetsage2_webui import pitch

    notes = melody(A, down(A), A)
    (tmp_path / "melody_vocal.lab").write_text(
        "".join(f"{n.start}\t{n.end}\t{n.pitch}\n" for n in notes), encoding="utf-8")
    _updated, stats = pitch.apply({"labs": {}}, tmp_path, semitones=0, octave_fix=True)
    assert stats.octave["moved_notes"] == 12
    assert stats.enabled is False
    assert stats.as_dict()["reason"] == "关闭了整体平移（八度一致性校正照常做）"
    assert json.loads(json.dumps(stats.as_dict()))["octave"]["moved_notes"] == 12
