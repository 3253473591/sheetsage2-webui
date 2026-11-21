"""样本清单 `meta.json`：给一个工程目录生成/刷新可读的元数据。

`project.json` / `lyrics.json` 是给程序读的；`meta.json` 是给人（和评测脚本）读的：
标题、语言、歌词源、BPM、音符数、挂字统计、`parts` 覆盖、`+`/`-` 计数。
它**不是**模型产物，而是工程收尾时顺手落的一份索引；手填的
`split` / `genre` / `difficulty` / `tags` / `notes` 会在刷新时保留。

产物约定：
- 平铺（`project.json` / `lyrics.json` / `auto.svp` / `manual.svp`）或
  `auto/`、`manual/` 子目录都支持；
- 只有 `project.json` 时也能生成（歌词阶段还没跑），字段为 null/0；
- 只刷新过期文件：源文件（project/lyrics/SVP/音频）都比 meta.json 旧时跳过。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from .fsutil import write_text_atomic

SCHEMA_VERSION = 1

AUDIO_SUFFIXES = {".flac", ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus"}
LRC_LINES_RE = re.compile(r"LRC[（(](\d+)\s*行[)）]")
DOT_MARKERS = ("·", "・", "•")
LANGUAGE_TAGS = {"Chinese": "zh", "English": "en", "Japanese": "ja",
                 "Cantonese": "yue", "Korean": "ko"}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_svp(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes().rstrip(b"\x00").decode("utf-8"))


def _find(folder: Path, name: str, *subdirs: str) -> Path | None:
    for sub in ("", *subdirs):
        candidate = folder / sub / name if sub else folder / name
        if candidate.is_file():
            return candidate
    return None


def _svp_stats(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    data = _read_svp(path)
    notes: list[dict[str, Any]] = []
    for track in data.get("tracks") or []:
        notes.extend((track.get("mainGroup") or {}).get("notes") or [])
    counts = Counter(str(note.get("lyrics") or "") for note in notes)
    return {
        "notes": len(notes),
        "markers": {
            "-": counts["-"],
            "+": counts["+"],
            "empty": counts[""],
            "dot": sum(counts[marker] for marker in DOT_MARKERS),
        },
    }


def _project_stats(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    meta = data.get("meta") or {}
    tracks = data.get("tracks") or []
    vocal = next((track for track in tracks if track.get("id") == "vocal"), None)
    vocal_notes = (vocal or {}).get("notes") or []
    tempo_map = (data.get("timeline") or {}).get("tempo_map") or []
    first_tempo = tempo_map[0] if tempo_map else {}
    return {
        "duration_seconds": data.get("duration_seconds"),
        "ppq": data.get("ppq"),
        "bpm": first_tempo.get("bpm") or meta.get("beat_bpm") or meta.get("abc_bpm"),
        "bpm_model": meta.get("abc_bpm"),
        "meter": meta.get("abc_meter") or meta.get("meter"),
        "vocal_notes": len(vocal_notes),
    }


def _lyrics_stats(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    data = _read_json(path)
    chars = data.get("chars") or []
    fill = data.get("fill") or {}
    warnings = [str(item) for item in (data.get("warnings") or [])]
    lrc_lines = None
    for warning in warnings:
        match = LRC_LINES_RE.search(warning)
        if match:
            lrc_lines = int(match.group(1))
            break
    lower = "\n".join(warnings).lower()
    if "lrc" in lower:
        source = "lrc"
    elif "txt" in lower:
        source = "txt"
    else:
        source = "asr"
    return {
        "language": data.get("language") or "",
        "language_used": data.get("language_used") or "unknown",
        "aligner": data.get("aligner") or "unknown",
        "source": data.get("source") or "mix",
        "source_file": data.get("source_file"),
        "chars": len(chars),
        "lines": len(data.get("lines") or []),
        "lrc_lines_input": lrc_lines,
        "lyrics_source": source,
        "zero_length_chars": data.get("zero_length_chars"),
        "text_coverage": data.get("text_coverage"),
        "parts_words": sum(1 for char in chars if char.get("parts")),
        "parts_total": sum(len(char.get("parts") or ()) for char in chars),
        "fill": {
            "assigned": fill.get("assigned"),
            "absorbed": fill.get("absorbed"),
            "dropped": fill.get("dropped"),
            "melisma": fill.get("melisma"),
            "unmatched_notes": fill.get("unmatched_notes"),
        },
    }


def _split_name(folder_name: str) -> tuple[str, str | None, str | None]:
    """`artist - title (version)` -> (title, artist, version)。认不出就整串当标题。"""
    stem = folder_name.strip()
    version = None
    match = re.search(r"\s*[（(]([^（）()]+)[)）]\s*$", stem)
    if match:
        version = match.group(1).strip()
        stem = stem[: match.start()].strip()
    artist, separator, title = stem.partition(" - ")
    if not separator:
        return stem, None, version
    return title.strip(), artist.strip(), version


def _find_audio(folder: Path) -> Path | None:
    for sub in ("", "audio", ".webui/input"):
        base = folder / sub if sub else folder
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES:
                return path
    return None


def _relative(path: Path | None, folder: Path) -> str | None:
    return None if path is None else path.relative_to(folder).as_posix()


def _default_notes(lyrics: dict[str, Any] | None, has_manual: bool,
                   has_audio: bool) -> str:
    if lyrics is None:
        return "仅扒谱；歌词识别后更新。"
    bits = [str(lyrics["language_used"])]
    if lyrics["lyrics_source"] == "lrc":
        lines = lyrics["lrc_lines_input"]
        bits.append(f"LRC {lines} 行" if lines else "LRC")
    else:
        bits.append(f"{lyrics['lyrics_source']} 歌词源")
    bits.append("manual 已精修" if has_manual else "无 manual.svp")
    if not has_audio:
        bits.append("音频未随样本提供")
    return "；".join(bits) + "。"


def _empty_stats(notes_auto: int | None, notes_manual: int | None) -> dict[str, Any]:
    return {
        "notes_auto": notes_auto,
        "notes_manual": notes_manual,
        "chars": 0,
        "lyric_lines": 0,
        "assigned": 0,
        "absorbed": 0,
        "dropped": 0,
        "melisma": 0,
        "unmatched_notes": 0,
        "zero_length_chars": None,
        "text_coverage": None,
        "parts_words": 0,
        "parts_total": 0,
        "stats_note": "仅扒谱阶段；歌词识别后更新。",
    }


def build_meta(folder: Path, *, include_lyrics: bool = True,
               existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """扫描一个工程目录，返回 meta 字典（不落盘）。"""
    folder = Path(folder)
    project_path = _find(folder, "project.json", "auto")
    if project_path is None:
        raise FileNotFoundError(f"{folder} 缺 project.json")

    lyrics_path = _find(folder, "lyrics.json", "auto") if include_lyrics else None
    auto_path = _find(folder, "auto.svp", "auto")
    manual_path = _find(folder, "manual.svp", "manual")
    project = _project_stats(project_path)
    lyrics = _lyrics_stats(lyrics_path)
    auto = _svp_stats(auto_path)
    manual = _svp_stats(manual_path)
    audio = _find_audio(folder)
    title, artist, version = _split_name(folder.name)

    notes_auto = (auto or {}).get("notes") or project["vocal_notes"] or None
    notes_manual = (manual or {}).get("notes")
    if lyrics is None:
        stats = _empty_stats(notes_auto, notes_manual)
        language = None
        language_selected = None
        lyrics_source = None
        lrc_lines = None
        aligner = None
        asr_note = "尚未跑歌词识别"
        tags = []
    else:
        stats = {
            "notes_auto": notes_auto,
            "notes_manual": notes_manual,
            "chars": lyrics["chars"],
            "lyric_lines": lyrics["lines"],
            "assigned": lyrics["fill"]["assigned"],
            "absorbed": lyrics["fill"]["absorbed"],
            "dropped": lyrics["fill"]["dropped"],
            "melisma": lyrics["fill"]["melisma"],
            "unmatched_notes": lyrics["fill"]["unmatched_notes"],
            "zero_length_chars": lyrics["zero_length_chars"],
            "text_coverage": lyrics["text_coverage"],
            "parts_words": lyrics["parts_words"],
            "parts_total": lyrics["parts_total"],
            "stats_note": "chars 中文为字、英文为词；assigned 含 absorbed；"
                          "assigned + dropped = chars",
        }
        language = lyrics["language_used"]
        language_selected = lyrics["language"]
        lyrics_source = lyrics["lyrics_source"]
        lrc_lines = lyrics["lrc_lines_input"]
        aligner = lyrics["aligner"]
        asr_note = ("LRC 模式未跑语音识别" if lyrics_source == "lrc"
                    else "样本未记录 ASR 模型")
        tags = [LANGUAGE_TAGS.get(language, language.lower()), lyrics_source,
                *(["manual"] if manual else [])]

    source_file = (lyrics or {}).get("source_file") or (audio.name if audio else None)
    manual_date = None
    for path in (manual_path, project_path):
        if path is not None:
            manual_date = date.fromtimestamp(path.stat().st_mtime).isoformat()
            break

    meta: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "song_id": folder.name,
        "title": title,
        "artist": artist,
        "version": version,
        "language": language,
        "language_selected": language_selected,
        "lyrics_source": lyrics_source,
        "lrc_lines_input": lrc_lines,
        "aligner": aligner,
        "asr_model": None,
        "asr_model_note": asr_note,
        "audio": {
            "source": (lyrics or {}).get("source") or "mix",
            "source_file": source_file,
            "file": _relative(audio, folder),
            "duration_seconds": project["duration_seconds"],
            "included": audio is not None,
        },
        "tempo": {
            "bpm": project["bpm"],
            "bpm_model": project["bpm_model"],
            "meter": project["meter"],
            "ppq": project["ppq"],
        },
        "stats": stats,
        "markers": {
            "auto": (auto or {}).get("markers"),
            "manual": (manual or {}).get("markers"),
        },
        "files": {
            "auto_svp": _relative(auto_path, folder),
            "manual_svp": _relative(manual_path, folder),
            "project": _relative(project_path, folder),
            "lyrics": _relative(lyrics_path, folder),
        },
        "split": "unassigned",
        "genre": None,
        "difficulty": None,
        "tags": tags,
        "notes": _default_notes(lyrics, manual is not None, audio is not None),
        "created_at": manual_date or date.today().isoformat(),
    }

    # 重跑时别把人工填的字段冲掉；统计字段始终以文件为准。
    if existing:
        for key in ("split", "genre", "difficulty", "tags", "notes",
                    "created_at", "asr_model"):
            if existing.get(key) not in (None, "", []):
                meta[key] = existing[key]
        for key, value in existing.items():
            meta.setdefault(key, value)
    return meta


def _source_paths(folder: Path, include_lyrics: bool) -> list[Path]:
    paths = [
        _find(folder, "project.json", "auto"),
        _find(folder, "auto.svp", "auto"),
        _find(folder, "manual.svp", "manual"),
        _find_audio(folder),
    ]
    if include_lyrics:
        paths.append(_find(folder, "lyrics.json", "auto"))
    return [path for path in paths if path is not None]


def _is_stale(folder: Path, meta_path: Path, include_lyrics: bool) -> bool:
    meta_mtime = meta_path.stat().st_mtime_ns
    return any(path.stat().st_mtime_ns > meta_mtime
               for path in _source_paths(folder, include_lyrics))


def write_meta(folder: Path, *, include_lyrics: bool = True,
               force: bool = False) -> Path | None:
    """生成/刷新 `<folder>/meta.json`；没有 project.json 时返回 None。

    默认只在 meta 缺失、损坏或源文件更新过时写盘；`force=True` 强制刷新。
    手填的 split/genre/difficulty/tags/notes 会保留。
    """
    folder = Path(folder)
    if _find(folder, "project.json", "auto") is None:
        return None
    meta_path = folder / "meta.json"
    existing = None
    valid_existing = False
    if meta_path.is_file():
        try:
            existing = _read_json(meta_path)
            valid_existing = True
        except (OSError, ValueError):
            existing = None
            valid_existing = False
    if valid_existing and not force and not _is_stale(folder, meta_path,
                                                      include_lyrics):
        return meta_path
    meta = build_meta(folder, include_lyrics=include_lyrics, existing=existing)
    write_text_atomic(meta_path, json.dumps(meta, ensure_ascii=False,
                                            indent=2) + "\n")
    return meta_path
