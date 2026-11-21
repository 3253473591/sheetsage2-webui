"""`meta.json`：样本清单的生成、保留手填字段、以及过期刷新。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from sheetsage2_webui.meta import build_meta, write_meta

PROJECT = {
    "version": 1,
    "ppq": 1920,
    "audio": "t.flac",
    "duration_seconds": 12.0,
    "timeline": {"tempo_map": [{"tick": 0, "bpm": 100.0}], "measures": []},
    "meta": {"abc_bpm": 100.0, "abc_meter": "4/4"},
    "tracks": [{
        "id": "vocal",
        "notes": [
            {"tick": 0, "dur": 480, "pitch": 60, "lyric": "晚"},
            {"tick": 480, "dur": 480, "pitch": 62, "lyric": "风"},
        ],
    }],
}

LYRICS = {
    "text": "晚风",
    "language": "自动",
    "language_used": "Chinese",
    "aligner": "tifa",
    "source": "mix",
    "source_file": "t.flac",
    "chars": [
        {"text": "晚", "start": 0.0, "end": 0.5},
        {"text": "风", "start": 0.5, "end": 1.0, "parts": [0.7]},
    ],
    "lines": [{"text": "晚风", "start": 0.0, "end": 1.0}],
    "fill": {"assigned": 2, "absorbed": 0, "dropped": 0,
             "melisma": 1, "unmatched_notes": 1},
    "zero_length_chars": 0,
    "text_coverage": 1.0,
    "slices": 2,
    "warnings": ["歌词源：LRC（2 行），官方词逐行对齐，未做语音识别"],
}


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _svp(notes: list[str]) -> dict:
    return {"tracks": [{"mainGroup": {"notes": [{"lyrics": text} for text in notes]}}]}


def _sample(folder: Path, *, lyrics: bool = True,
            auto_notes: tuple[str, ...] = ("晚", "-", "风"),
            manual_notes: tuple[str, ...] = ("晚", "")) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    _write_json(folder / "project.json", PROJECT)
    if lyrics:
        _write_json(folder / "lyrics.json", LYRICS)
    _write_json(folder / "auto.svp", _svp(list(auto_notes)))
    _write_json(folder / "manual.svp", _svp(list(manual_notes)))
    return folder


def test_build_meta_reads_project_lyrics_and_svps(tmp_path):
    folder = _sample(tmp_path / "泽国同学 - 八千里路明月照 (氛围版)")
    meta = build_meta(folder)

    assert meta["title"] == "八千里路明月照"
    assert meta["artist"] == "泽国同学"
    assert meta["version"] == "氛围版"
    assert meta["language"] == "Chinese"
    assert meta["lyrics_source"] == "lrc"
    assert meta["lrc_lines_input"] == 2
    assert meta["stats"]["notes_auto"] == 3
    assert meta["stats"]["notes_manual"] == 2
    assert meta["stats"]["chars"] == 2
    assert meta["stats"]["parts_words"] == 1
    assert meta["stats"]["parts_total"] == 1
    assert meta["markers"]["auto"] == {"-": 1, "+": 0, "empty": 0, "dot": 0}
    assert meta["markers"]["manual"] == {"-": 0, "+": 0, "empty": 1, "dot": 0}
    assert meta["tempo"] == {"bpm": 100.0, "bpm_model": 100.0,
                             "meter": "4/4", "ppq": 1920}
    assert meta["files"] == {"auto_svp": "auto.svp", "manual_svp": "manual.svp",
                             "project": "project.json", "lyrics": "lyrics.json"}


def test_write_meta_creates_file_and_preserves_hand_edited_fields(tmp_path):
    folder = _sample(tmp_path / "歌 - 名")
    path = write_meta(folder)
    assert path == folder / "meta.json"

    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update(split="dev", genre="pop", tags=["zh", "dev"], notes="手填的说明")
    _write_json(path, meta)

    _write_json(folder / "manual.svp", _svp(["晚", "-", "风"]))
    os.utime(folder / "manual.svp", (time.time() + 5, time.time() + 5))
    write_meta(folder)

    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["stats"]["notes_manual"] == 3
    assert updated["split"] == "dev"
    assert updated["genre"] == "pop"
    assert updated["tags"] == ["zh", "dev"]
    assert updated["notes"] == "手填的说明"


def test_write_meta_skips_when_nothing_is_newer(tmp_path):
    folder = _sample(tmp_path / "歌 - 名")
    path = write_meta(folder)
    before = path.stat().st_mtime_ns
    time.sleep(0.01)
    write_meta(folder)
    assert path.stat().st_mtime_ns == before


def test_meta_without_lyrics_is_partial_but_valid(tmp_path):
    folder = _sample(tmp_path / "歌 - 名", lyrics=False)
    meta = build_meta(folder, include_lyrics=False)

    assert meta["language"] is None
    assert meta["lyrics_source"] is None
    assert meta["stats"]["chars"] == 0
    assert meta["stats"]["notes_auto"] == 3
    assert meta["stats"]["notes_manual"] == 2
    assert meta["files"]["lyrics"] is None
    assert meta["files"]["auto_svp"] == "auto.svp"


def test_write_meta_without_project_returns_none(tmp_path):
    folder = tmp_path / "空目录"
    folder.mkdir()
    assert write_meta(folder) is None
    assert not (folder / "meta.json").exists()
