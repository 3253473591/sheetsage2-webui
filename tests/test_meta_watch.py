"""`meta.json` 的自动刷新：手动加/改 SVP 后，不用再跑命令。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from sheetsage2_webui.meta_watch import MetaWatcher

PROJECT = {
    "version": 1,
    "ppq": 1920,
    "audio": "t.flac",
    "duration_seconds": 8.0,
    "timeline": {"tempo_map": [{"tick": 0, "bpm": 100.0}], "measures": []},
    "meta": {"abc_bpm": 100.0, "abc_meter": "4/4"},
    "tracks": [{
        "id": "vocal",
        "notes": [{"tick": 0, "dur": 480, "pitch": 60},
                  {"tick": 480, "dur": 480, "pitch": 62}],
    }],
}

LYRICS = {
    "text": "ab",
    "language": "自动",
    "language_used": "Chinese",
    "aligner": "tifa",
    "source": "mix",
    "chars": [{"text": "a", "start": 0.0, "end": 0.5},
              {"text": "b", "start": 0.5, "end": 1.0}],
    "lines": [{"text": "ab", "start": 0.0, "end": 1.0}],
    "fill": {"assigned": 2, "absorbed": 0, "dropped": 0,
             "melisma": 0, "unmatched_notes": 0},
    "zero_length_chars": 0,
    "text_coverage": 1.0,
    "warnings": [],
}


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_watcher_refreshes_meta_when_manual_svp_appears(tmp_path):
    song = tmp_path / "samples" / "歌 - 名"
    song.mkdir(parents=True)
    _write_json(song / "project.json", PROJECT)
    _write_json(song / "lyrics.json", LYRICS)

    watcher = MetaWatcher([tmp_path / "samples"], interval=0.05)
    watcher.start()
    try:
        assert _wait_for(lambda: (song / "meta.json").is_file())
        _write_json(song / "manual.svp", {
            "tracks": [{"mainGroup": {"notes": [{"lyrics": "a"},
                                                  {"lyrics": "-"},
                                                  {"lyrics": "b"}]}}],
        })
        assert _wait_for(lambda: (
            (song / "meta.json").is_file()
            and json.loads((song / "meta.json").read_text(encoding="utf-8"))
            ["stats"]["notes_manual"] == 3
        ))
    finally:
        watcher.stop()
