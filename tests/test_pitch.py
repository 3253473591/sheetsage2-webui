r"""`pitch` 模块（旋律音高后期处理）的回归测试。

要盯住三件事：

1. **旋律真的降了**（lab 的 pitch 列 / MIDI 音符 / playback.json）；
2. **和弦一动不动**（同一份产物里和弦本来就在正确音域，动它就是把对的改错）；
3. 其余字段（时间、力度、轨名、小节表）逐字节不变。

    .\.venv\Scripts\python.exe -m pytest tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pretty_midi
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui import pitch  # noqa: E402


# --------------------------------------------------------------------- lab
def test_shift_lab_text_only_touches_the_pitch_column():
    text = "30.055\t30.197499999999998\t74\n30.1975\t30.34\t76\n\n"
    shifted, moved = pitch.shift_lab_text(text, -12)
    assert moved == 2
    assert shifted == "30.055\t30.197499999999998\t62\n30.1975\t30.34\t64\n\n"


def test_shift_lab_text_keeps_the_track_column_of_melody_full():
    """`melody_full.lab` 是 `start end pitch track`：只动第 3 列，track 列别丢。"""
    shifted, moved = pitch.shift_lab_text("2.365\t2.65\t81\t1\n", -12)
    assert (shifted, moved) == ("2.365\t2.65\t69\t1\n", 1)


def test_shift_lab_text_passes_junk_through():
    text = "not a lab line\n"
    assert pitch.shift_lab_text(text, -12) == (text, 0)


# --------------------------------------------------------------------- MIDI
def write_midi(path: Path, tracks: dict[str, list[int]]) -> None:
    midi = pretty_midi.PrettyMIDI()
    for name, pitches in tracks.items():
        instrument = pretty_midi.Instrument(program=0, name=name)
        for index, value in enumerate(pitches):
            instrument.notes.append(pretty_midi.Note(
                velocity=100, pitch=value, start=float(index), end=float(index) + 0.9))
        midi.instruments.append(instrument)
    midi.write(str(path))


def read_midi(path: Path) -> dict[str, list[int]]:
    midi = pretty_midi.PrettyMIDI(str(path))
    return {instrument.name: [note.pitch for note in instrument.notes]
            for instrument in midi.instruments}


def test_shift_midi_file_shifts_every_track(tmp_path: Path):
    path = tmp_path / "melody_vocal.mid"
    write_midi(path, {"Vocal": [65, 76, 86]})
    moved, kept = pitch.shift_midi_file(path, -12)
    assert (moved, kept) == (3, [])
    assert read_midi(path) == {"Vocal": [53, 64, 74]}


def test_shift_midi_file_skips_chord_tracks_in_a_mixed_file(tmp_path: Path):
    path = tmp_path / "transcription.mid"
    write_midi(path, {"Vocal": [76], "Ins": [81], "Chords": [36, 52, 65]})
    moved, kept = pitch.shift_midi_file(path, -12, skip_chord_tracks=True)
    assert moved == 2 and kept == ["Chords"]
    assert read_midi(path) == {"Vocal": [64], "Ins": [69], "Chords": [36, 52, 65]}


def test_shift_midi_file_never_writes_an_illegal_pitch(tmp_path: Path):
    path = tmp_path / "low.mid"
    write_midi(path, {"Vocal": [3, 60]})
    pitch.shift_midi_file(path, -12)
    assert read_midi(path) == {"Vocal": [0, 48]}       # 3 - 12 夹到 0，不会写出负数


# --------------------------------------------------------------------- playback
def test_shift_playback_moves_melody_only():
    payload = {
        "duration": 10.0,
        "measures": [{"start": 0.0, "end": 2.0}],
        "tracks": [
            {"name": "Vocal", "notes": [{"pitch": 74, "start": 1.0, "end": 1.2}]},
            {"name": "Ins", "notes": [{"pitch": 81, "start": 1.0, "end": 1.2}]},
            {"name": "Chords", "notes": [{"pitch": 38, "start": 0.0, "end": 2.0}]},
        ],
    }
    shifted, moved = pitch.shift_playback(payload, -12)
    assert moved == 2
    assert [t["notes"][0]["pitch"] for t in shifted["tracks"]] == [62, 69, 38]
    assert shifted["measures"] == payload["measures"]
    # 不能动调用方传进来的对象（转写结果后面还要用）
    assert payload["tracks"][0]["notes"][0]["pitch"] == 74


# --------------------------------------------------------------------- apply
@pytest.fixture()
def model_out(tmp_path: Path) -> Path:
    """一份最小的"模型产物目录"：旋律 lab/MIDI + 混合 MIDI + 和弦 + playback。"""
    tmp_path.joinpath("melody_full.lab").write_text(
        "0\t1\t81\t1\n1\t2\t76\t0\n", encoding="utf-8")
    tmp_path.joinpath("melody_vocal.lab").write_text("1\t2\t76\n", encoding="utf-8")
    tmp_path.joinpath("melody_instrumental.lab").write_text("0\t1\t81\n", encoding="utf-8")
    write_midi(tmp_path / "melody.mid", {"Vocal": [76], "Ins": [81]})
    write_midi(tmp_path / "melody_vocal.mid", {"Vocal": [76]})
    write_midi(tmp_path / "melody_instrumental.mid", {"Ins": [81]})
    write_midi(tmp_path / "transcription.mid",
               {"Vocal": [76], "Ins": [81], "Chords": [38, 45, 50]})
    write_midi(tmp_path / "chords.mid", {"Chords": [38, 45, 50]})
    tmp_path.joinpath("playback.json").write_text(json.dumps({
        "duration": 2.0, "measures": [{"start": 0.0, "end": 1.0}],
        "tracks": [{"name": "Vocal", "notes": [{"pitch": 76, "start": 1.0, "end": 2.0}]},
                   {"name": "Chords", "notes": [{"pitch": 38, "start": 0.0, "end": 1.0}]}],
    }, indent=2), encoding="utf-8")
    return tmp_path


def make_result(out: Path) -> dict:
    labs = {name: (out / f"{name}.lab").read_text(encoding="utf-8")
            for name in pitch.MELODY_LABS}
    return {"labs": labs, "abc": "X:1\n", "warnings": []}


def test_apply_shifts_the_melody_and_leaves_the_chords(model_out: Path):
    result = make_result(model_out)
    updated, stats = pitch.apply(result, model_out, semitones=-12)

    assert stats.enabled and stats.semitones == -12
    assert stats.labs == {"melody_full.lab": 2, "melody_vocal.lab": 1,
                          "melody_instrumental.lab": 1}
    assert stats.playback_notes == 1
    assert any("和弦轨" in w for w in stats.warnings)

    # 内存里的 labs 也降了（后面建 tick 域工程用的是它）
    assert updated["labs"]["melody_vocal"] == "1\t2\t64\n"
    # 磁盘上的 lab / MIDI 一起降
    assert (model_out / "melody_vocal.lab").read_text(encoding="utf-8") == "1\t2\t64\n"
    assert read_midi(model_out / "melody_vocal.mid") == {"Vocal": [64]}
    assert read_midi(model_out / "transcription.mid") == {
        "Vocal": [64], "Ins": [69], "Chords": [38, 45, 50]}
    # 和弦产物一个字节都不该变
    assert read_midi(model_out / "chords.mid") == {"Chords": [38, 45, 50]}
    playback = json.loads((model_out / "playback.json").read_text(encoding="utf-8"))
    assert [t["notes"][0]["pitch"] for t in playback["tracks"]] == [64, 38]
    # 这个假目录没有 notation/，ABC 重建应当跳过并告警，而不是抛异常
    assert stats.abc.startswith("skipped")
    assert any("score.abc" in w for w in stats.warnings)


def test_apply_with_zero_semitones_touches_nothing(model_out: Path):
    before = (model_out / "melody_vocal.lab").read_text(encoding="utf-8")
    updated, stats = pitch.apply(make_result(model_out), model_out, semitones=0)
    assert not stats.enabled and stats.semitones == 0
    assert stats.labs == {} and stats.midis == {}
    assert (model_out / "melody_vocal.lab").read_text(encoding="utf-8") == before
    assert read_midi(model_out / "melody_vocal.mid") == {"Vocal": [76]}
    assert updated["abc"] == "X:1\n"


def test_apply_accepts_two_octaves(model_out: Path):
    _updated, stats = pitch.apply(make_result(model_out), model_out, semitones=-24)
    assert read_midi(model_out / "melody_vocal.mid") == {"Vocal": [52]}
    assert json.loads((model_out / "playback.json").read_text(encoding="utf-8"))[
        "tracks"][0]["notes"][0]["pitch"] == 52
    assert stats.as_dict()["semitones"] == -24


# --------------------------------------------------------------------- 告警
def test_warnings_for_says_what_moved_and_what_did_not():
    stats = pitch.PitchShiftStats(enabled=True, semitones=-12,
                                  labs={"melody_vocal.lab": 324},
                                  playback_notes=324)
    messages = pitch.warnings_for(stats)
    assert any("-12 半音" in m and "1 个八度" in m for m in messages)
    assert any("和弦轨不动" in m for m in messages)
    assert pitch.warnings_for(pitch.PitchShiftStats()) == []


# --------------------------------------------------------------------- ABC
def test_rebuild_abc_reproduces_the_real_score_abc():
    """拿真工程的 `notation/` 重建，必须**逐字节**等于模型自己写的 `score.abc`。

    模型写 `score.abc` 走的就是这条路；只要这里相等，"降八度后重建"就只改了音高。
    """
    out = PROJECT_ROOT / "output" / "凤凰传奇_-_光芒"
    if not (out / "notation" / "song_melody.mid").is_file():
        pytest.skip("没有 output/凤凰传奇_-_光芒/notation（产物目录是忽略的）")
    text, error = pitch.rebuild_abc(out)
    assert error is None and text is not None
    assert text == (out / "score.abc").read_text(encoding="utf-8")
