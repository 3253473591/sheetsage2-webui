r"""`pipeline` 的接线测试：用桩引擎跑通，验证量化确实接进了产物。

不加载任何模型——`run()` 接受注入的 engine，桩只需要提供 `transcribe` /
`device` / `_effective_dtype`。这样能在 CI 里锁住"量化 -> project.json"这条线。

    .\.venv\Scripts\python.exe -m pytest tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.pipeline import run  # noqa: E402
from sheetsage2_webui.timeline import PPQ  # noqa: E402

GRID_TICKS = PPQ // 96       # 20 tick = 一拍的 1/96（32 分 × 三连音），与拍号无关


class StubEngine:
    """最小可用的引擎桩：给两小节 4/4，音符故意落在网格之间。"""

    device = "cpu"

    def __init__(self, *, melody: str, structure: str = "0.00\t4.00\tintro\n"):
        self._melody = melody
        self._structure = structure
        self.calls = 0

    def _effective_dtype(self):
        return "fp32"

    def transcribe(self, audio, out_dir, progress=None, **options):
        self.calls += 1
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        # 真引擎会把 lab 写到产物目录；这里照做，好验证 pipeline 不会去改它
        (out / "structure.lab").write_text(self._structure, encoding="utf-8")
        return {
            "duration_seconds": 4.0,
            "abc": 'X:1\nT:stub\nM:4/4\nL:1/4\nQ:1/4=120\nK:C\nC D E F|G A B c|',
            "labs": {
                "melody_full": self._melody,
                "beat": "0.00\t1\t4\t4\n0.50\t2\t4\t4\n1.00\t3\t4\t4\n"
                        "1.50\t4\t4\t4\n2.00\t1\t4\t4\n2.50\t2\t4\t4\n"
                        "3.00\t3\t4\t4\n3.50\t4\t4\t4\n",
                "key": "0.00\t4.00\tC:major\n",
                "chord": "0.00\t2.00\tC\n2.00\t4.00\tF\n",
                "structure": self._structure,
            },
            "playback": {"measures": [
                {"index": 0, "start": 0.0, "end": 2.0, "score_start": 0.0,
                 "score_end": 1.0, "leading_rest": 0.0, "trailing_rest": 0.0},
                {"index": 1, "start": 2.0, "end": 4.0, "score_start": 1.0,
                 "score_end": 2.0, "leading_rest": 0.0, "trailing_rest": 0.0},
            ]},
            "warnings": [],
        }


MELODY = "\n".join([
    "0.00 0.50 60 0",
    "0.50 0.95 62 0",
    "1.00 1.60 64 0",
    "2.00 2.60 65 0",
    "2.60 3.20 67 0",
    "3.20 3.90 69 0",
    "0.10 0.40 48 1",
    "0.60 1.10 48 1",
])


def test_run_quantizes_project_notes_by_default(tmp_path):
    engine = StubEngine(melody=MELODY)
    payload = run("stub.flac", tmp_path, engine=engine, write=True)

    assert engine.calls == 1
    project = payload["project"]
    quantize_block = project["quantize"]
    assert quantize_block["enabled"] is True
    assert quantize_block["grid_ticks"] == GRID_TICKS
    assert quantize_block["notes"] == 8
    assert quantize_block["failed_tracks"] == 0
    assert quantize_block["moved"] > 0                 # 故意给了不在网格上的音符

    for track in project["tracks"]:
        for note in track["notes"]:
            assert note["tick"] % GRID_TICKS == 0
            assert note["dur"] % GRID_TICKS == 0
            assert note["dur"] > 0

    decisions = payload["decisions"]
    assert decisions["quantized"] is True
    assert decisions["quantize_grid_ticks"] == GRID_TICKS
    assert decisions["quantize_moved"] == quantize_block["moved"]

    # 产物里也要能看到
    written = json.loads((tmp_path / "project.json").read_text(encoding="utf-8"))
    assert written["quantize"]["enabled"] is True
    assert written["quantize_shift_seconds"]["max"] >= 0.0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["quantize"]["grid_ticks"] == GRID_TICKS
    assert any("量化" in w for w in written["meta"]["warnings"])


def test_run_can_skip_quantization(tmp_path):
    payload = run("stub.flac", tmp_path, engine=StubEngine(melody=MELODY),
                  quantize=False, write=False)

    assert payload["project"]["quantize"] == {"enabled": False, "reason": "关闭了量化"}
    assert payload["decisions"]["quantized"] is False
    assert payload["decisions"]["quantize_grid_ticks"] is None
    assert not any("量化" in w for w in payload["project"]["meta"]["warnings"])
    # 不量化时音符还是原来的线性插值结果
    assert any(n["tick"] % GRID_TICKS for track in payload["project"]["tracks"]
               for n in track["notes"])


def test_quantized_notes_keep_their_count_and_pitches(tmp_path):
    raw = run("stub.flac", tmp_path, engine=StubEngine(melody=MELODY),
              quantize=False, write=False)
    quantized = run("stub.flac", tmp_path, engine=StubEngine(melody=MELODY),
                    quantize=True, write=False)

    for before, after in zip(raw["project"]["tracks"], quantized["project"]["tracks"]):
        assert len(before["notes"]) == len(after["notes"])
        # 量化是"按时间重新排序后搬 tick"，音高只是跟着音符走
        assert sorted(n["pitch"] for n in before["notes"]) == \
            sorted(n["pitch"] for n in after["notes"])
        assert [n["tick"] for n in after["notes"]] == \
            sorted(n["tick"] for n in after["notes"])


def test_pitch_is_shifted_down_an_octave_without_asking(tmp_path):
    """旋律的八度修正是**默认**行为（不做成参数）：比模型输出低 12，别的字段不动。"""
    payload = run("stub.flac", tmp_path, engine=StubEngine(melody=MELODY),
                  quantize=False, write=False)

    pitch_block = payload["project"]["pitch"]
    assert pitch_block["enabled"] is True
    assert pitch_block["semitones"] == -12
    assert pitch_block["abc"].startswith("skipped")     # 这个桩没有 notation/，重建跳过
    assert any("音高修正" in w for w in payload["project"]["meta"]["warnings"])

    model_pitches = sorted(int(row.split()[2]) for row in MELODY.splitlines())
    got = sorted(n["pitch"] for track in payload["project"]["tracks"]
                 for n in track["notes"])
    assert got == sorted(p - 12 for p in model_pitches)


def test_pitch_shift_can_be_turned_off_for_debugging(tmp_path):
    """只想看模型原始音高时（命令行 `--pitch-shift 0`）整条链路都要原样。

    八度一致性校正（`octave.py`）跟整体平移是两件事：这个桩里没有重复段，
    所以它一个音都没动，但统计块照旧要出现（`octave` 键）。
    """
    payload = run("stub.flac", tmp_path, engine=StubEngine(melody=MELODY),
                  quantize=False, pitch_shift=0, write=False)
    block = payload["project"]["pitch"]
    assert {k: v for k, v in block.items() if k != "octave"} == {
        "enabled": False, "semitones": 0, "labs": {}, "midis": {},
        "playback_notes": 0, "abc": "not_attempted", "warnings": [],
        "reason": "关闭了音高修正"}
    assert block["octave"]["moved_notes"] == 0
    model_pitches = sorted(int(row.split()[2]) for row in MELODY.splitlines())
    assert sorted(n["pitch"] for track in payload["project"]["tracks"]
                  for n in track["notes"]) == model_pitches
    assert not any("音高修正" in w for w in payload["project"]["meta"]["warnings"])


def test_octave_fix_can_be_turned_off(tmp_path):
    """`--no-octave-fix`：只做整体平移，统计块里说清楚它没跑。"""
    payload = run("stub.flac", tmp_path, engine=StubEngine(melody=MELODY),
                  quantize=False, octave_fix=False, write=False)
    block = payload["project"]["pitch"]
    assert block["semitones"] == -12
    assert block["octave"]["enabled"] is False
    assert block["octave"]["moved_notes"] == 0
    model_pitches = sorted(int(row.split()[2]) for row in MELODY.splitlines())
    got = sorted(n["pitch"] for track in payload["project"]["tracks"]
                 for n in track["notes"])
    assert got == sorted(p - 12 for p in model_pitches)


def test_octave_fix_reaches_the_project_notes(tmp_path):
    """八度校正发生在 tick 域工程**之前**，所以卷帘 / SVP 拿到的音高已经是对的。

    桩里放同一段旋律的三次出现（每次 8 个音），中间那次低一个八度：
    校正后 `project.json` 里应该正好是中间那 8 个音被抬回来。
    """
    phrase = [60, 62, 64, 65, 67, 69, 71, 72]
    rows, t = [], 0.0
    for index, pitches in enumerate((phrase, [p - 12 for p in phrase], phrase)):
        if index:
            t += 0.72                      # 静音 0.62s ≥ REST_GAP，一定断句
        for pitch in pitches:
            rows.append(f"{t:.3f} {t + 0.10:.3f} {pitch} 0")
            t += 0.10
    melody = "\n".join(rows) + "\n"
    assert t < 4.0                         # 桩的时间轴只有 4 秒

    fixed = run("stub.flac", tmp_path, engine=StubEngine(melody=melody), write=False)
    raw = run("stub.flac", tmp_path / "raw", engine=StubEngine(melody=melody),
              write=False, octave_fix=False)

    assert fixed["project"]["pitch"]["octave"]["moved_notes"] == 8
    got = [n["pitch"] for track in fixed["project"]["tracks"] for n in track["notes"]]
    base = [n["pitch"] for track in raw["project"]["tracks"] for n in track["notes"]]
    assert len(got) == len(base) == 24
    assert [g - b for g, b in zip(got, base)] == [0] * 8 + [12] * 8 + [0] * 8
    assert any("八度一致性校正" in w
               for w in fixed["project"]["meta"]["warnings"])


def test_simplicity_is_passed_through_and_changes_the_result(tmp_path):
    soft = run("stub.flac", tmp_path, engine=StubEngine(melody=MELODY),
               simplicity=0.0, write=False)
    hard = run("stub.flac", tmp_path, engine=StubEngine(melody=MELODY),
               simplicity=5.0, write=False)

    assert soft["project"]["quantize"]["simplicity"] == 0.0
    assert hard["project"]["quantize"]["simplicity"] == 5.0
    for track in (soft["project"]["tracks"] + hard["project"]["tracks"]):
        for note in track["notes"]:
            assert note["tick"] % GRID_TICKS == 0


def test_quantized_project_keeps_the_measure_table_untouched(tmp_path):
    """量化只动 tick：小节表（唯一时间真源）必须逐字节不变。"""
    raw = run("stub.flac", tmp_path, engine=StubEngine(melody=MELODY),
              quantize=False, write=False)
    quantized = run("stub.flac", tmp_path, engine=StubEngine(melody=MELODY),
                    quantize=True, write=False)

    assert quantized["project"]["timeline"]["measures"] == \
        raw["project"]["timeline"]["measures"]
    assert quantized["project"]["timeline"]["tempo_map_exact"] == \
        raw["project"]["timeline"]["tempo_map_exact"]


def test_project_sections_are_merged_and_translated(tmp_path):
    """模型的段落是按乐句给的：工程里的段落表要**合并相邻同名 + 翻成中文**。"""
    structure = ("0.00\t1.00\tintro\n1.00\t2.00\tintro\n2.00\t3.00\tverse\n"
                 "3.00\t4.00\tchorus\n")
    payload = run("stub.flac", tmp_path, engine=StubEngine(melody=MELODY,
                                                           structure=structure),
                  write=False)
    rows = payload["project"]["sections"]
    assert [row["label"] for row in rows] == ["前奏", "主歌", "副歌"]
    # 120 BPM、4/4：1 个四分 = PPQ tick = 0.5s，所以 1 秒 = 2 个四分
    second = 2 * PPQ
    # 前两行 intro 并成一段（0~2s），后两段各 1s
    assert [row["dur"] for row in rows] == [2 * second, second, second]
    assert rows[0]["tick"] == 0
    assert rows[1]["tick"] == rows[0]["dur"]
    assert sum(row["dur"] for row in rows) == 4 * second

    # `structure.lab` 是模型原始产物，**不动**（还是英文、还是 4 行）
    lab_text = (tmp_path / "structure.lab").read_text(encoding="utf-8")
    assert lab_text == structure
