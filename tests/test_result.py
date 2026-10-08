r"""`result` 模块的回归测试。

这些测试不加载模型，只覆盖产物的解析规则——尤其是拍号归一化这种一改就错的
地方。跑法：

    .\.venv\Scripts\python.exe -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.result import (  # noqa: E402
    beat_metrics,
    beat_stats,
    inspect_output_dir,
    interval_stats,
    parse_lab_text,
    summarize_result,
)


def write_lab(path: Path, rows) -> Path:
    path.write_text(
        "".join("\t".join(str(x) for x in row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_parse_lab_text_handles_tabs_and_spaces():
    assert parse_lab_text("a\tb\tc\n d  e  f \n\n") == [["a", "b", "c"], ["d", "e", "f"]]


def test_beat_metrics_simple_duple_meter():
    # 4/4，每拍 0.5s -> 120 BPM
    rows = [[round(0.5 * i, 3), i % 4 + 1, 4, 4] for i in range(9)]
    stats = beat_metrics(rows)
    assert stats["meter"] == "4/4"
    assert stats["beat_unit_bpm"] == 120.0
    assert stats["bpm"] == 120.0
    assert stats["beats"] == 9


def test_beat_metrics_normalizes_compound_meter():
    """6/8 的行是八分音符，不能当成四分音符，否则速度翻倍。"""
    rows = [[round(0.3 * i, 3), i % 6 + 1, 6, 8] for i in range(13)]
    stats = beat_metrics(rows)
    assert stats["meter"] == "6/8"
    assert stats["beat_unit_bpm"] == 200.0     # 八分音符本身的速度
    assert stats["bpm"] == 100.0               # 归一化到四分音符
    assert stats["bpm"] * 2 == stats["beat_unit_bpm"]


def test_beat_metrics_without_meter_falls_back_to_none():
    stats = beat_metrics([["0.0"], ["0.5"], ["1.0"]])
    assert stats["meter"] is None
    assert stats["bpm"] is None
    assert stats["beats"] == 3


def test_beat_metrics_single_beat_is_not_an_error():
    stats = beat_metrics([["0.0", "1", "4", "4"]])
    assert stats["beats"] == 1
    assert stats["bpm"] is None


def test_beat_stats_reads_file(tmp_path):
    path = write_lab(tmp_path / "beat.lab", [[round(0.5 * i, 3), i % 4 + 1, 4, 4] for i in range(5)])
    assert beat_stats(path)["bpm"] == 120.0


def test_interval_stats_ignores_empty_and_backwards_spans(tmp_path):
    path = write_lab(tmp_path / "key.lab", [
        [0.0, 10.0, "C:major"],
        [10.0, 20.0, "C:major"],
        [30.0, 25.0, "bad"],       # end < start，丢弃
    ])
    stats = interval_stats(path)
    assert stats["rows"] == 2
    assert stats["covered_seconds"] == 20.0
    assert stats["labels"] == ["C:major"]


def test_inspect_output_dir_flags_missing_artifacts(tmp_path):
    report = inspect_output_dir(tmp_path)
    assert not report.ok
    assert "events.json" in report.missing
    assert any("缺少" in p for p in report.problems)


def test_inspect_output_dir_missing_directory():
    report = inspect_output_dir(Path("definitely/not/here"))
    assert not report.ok
    assert report.problems == ["产物目录不存在"]


# --------------------------------------------------- summarize_result 组装
def beat_lab_text(period: float, count: int, numerator: int = 4, denominator: int = 4,
                  offset: float = 0.01) -> str:
    lines = []
    for index in range(count):
        time = round((offset + index * period) / 0.01) * 0.01
        lines.append(f"{time:.3f}\t{index % numerator + 1}\t{numerator}\t{denominator}")
    return "\n".join(lines) + "\n"


def test_summarize_result_assembles_tempo_and_meter():
    """这条覆盖 summarize_result 把各来源拼起来的路径，不加载模型。"""
    result = {
        "audio": "x.flac", "duration_seconds": 100.0, "elapsed_seconds": 1.0, "dtype": "bf16",
        "melody_notes": 30, "vocal_notes": 20, "instrumental_notes": 10, "num_events": 40,
        "abc_measures": 12, "abc": "X:1\nM:4/4\nQ:1/4=104\nK:F\n",
        "peak_gpu_mib": 3400.0, "windows": [{}, {}], "warnings": [], "abc_error": None,
        "labs": {
            "beat": beat_lab_text(60 / 104, 180),
            "key": "0.000\t100.0\tF:major\n",
            "chord": "0.000\t5.0\tF:maj\n5.0\t100.0\tC:maj\n",
            "structure": "0.000\t100.0\tverse\n",
        },
    }
    summary = summarize_result(result, "cuda", "bf16").as_dict()

    assert summary["meter"] == "4/4"
    assert summary["abc_meter"] == "4/4"
    assert summary["bpm"] == pytest.approx(104.0, abs=0.05)
    assert summary["abc_bpm"] == 104.0
    assert summary["bpm_agreement"] is True
    assert summary["bpm_source"] == "beat_robust"
    assert summary["tempo_constant"] is True
    assert summary["pickup"] is None
    assert summary["keys"] == ["F:major"]
    assert summary["chords"] == ["F:maj", "C:maj"]
    assert summary["structure"] == ["verse"]


def test_summarize_result_tolerates_missing_abc():
    """模型可能不生成 score.abc，这条路径不能炸。"""
    result = {
        "audio": "x.flac", "duration_seconds": 5.0, "elapsed_seconds": 0.5, "dtype": "fp32",
        "melody_notes": 0, "vocal_notes": 0, "instrumental_notes": 0, "num_events": 0,
        "abc_measures": 0, "abc": None, "abc_error": "No key was decoded",
        "peak_gpu_mib": 0.0, "windows": [], "warnings": [],
        "labs": {"beat": "", "key": "", "chord": "", "structure": ""},
    }
    summary = summarize_result(result, "cpu", "fp32").as_dict()

    assert summary["abc_bpm"] is None
    assert summary["abc_meter"] is None
    assert summary["bpm"] is None
    assert summary["bpm_chosen"] is None
    assert summary["abc_error"] == "No key was decoded"
