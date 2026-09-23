"""MIDI/音符 → Synthesizer V Studio 工程文件（SVP, version 153）。

任务文档只说「SVP 本质是 JSON，version 固定为 153，notes 至少包含音高、起始、时长、歌词」，
但真实 153 工程的字段与时间单位并未给出。本模块的结构由 `D:\\调` 下 205 个真实
SVP 样本反解得到，关键事实如下：

* 工程音符位于 **``tracks[i].mainGroup.notes[]``**，不是 ``tracks[i].notes[]``；
  ``library[i].notes`` 是导入的参考素材，与工程音符无关。
* 时间单位是 **blick**，且 **1 个四分音符 = 705,600,000 blick**。
  纯秒数直接写进去会导致工程时间轴完全错乱。
* 顶层固定为 ``version / time / library / tracks / renderConfig``。
* 每个音符必须带齐 ``musicalType / onset / duration / lyrics / phonemes / accent /
  pitch / detune / instantMode / attributes / systemAttributes / pitchTakes / timbreTakes``。

参考样本：``D:\\调\\AAA约稿\\扒谱\\Battleplan Arclight.svp``（version 153，285 音符）。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "BLICK_PER_QUARTER",
    "SVP_VERSION",
    "SvNote",
    "sec_to_blick",
    "blick_to_sec",
    "build_svp",
    "write_svp",
    "notes_from_playback",
    "default_parameters",
]

SVP_VERSION = 153

#: Synthesizer V 内部时间单位：每个四分音符 705,600,000 blick。
from app.tempo import (
    BLICK_PER_QUARTER,
    blick_to_sec as _map_blick_to_sec,
    normalize_tempo_map,
    sec_to_blick as _map_sec_to_blick,
)

_PARAM_KEYS = (
    "pitchDelta",
    "vibratoEnv",
    "loudness",
    "tension",
    "breathiness",
    "voicing",
    "gender",
    "toneShift",
)

_DEFAULT_TRACK_COLORS = ("ff7db235", "ff4d8bff", "ffe0409b", "ff22c55e", "fff59e0b", "ffef4444")


class SvNote:
    """一个待写入 SVP 的音符（秒为单位）。"""

    __slots__ = ("start", "end", "pitch", "lyrics")

    def __init__(self, start: float, end: float, pitch: int, lyrics: str = "la") -> None:
        self.start = float(start)
        self.end = float(end)
        self.pitch = int(round(pitch))
        self.lyrics = (lyrics or "la").strip() or "la"

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"SvNote({self.start:.3f}->{self.end:.3f}, pitch={self.pitch}, {self.lyrics!r})"


def sec_to_blick(seconds: float, bpm: float) -> int:
    """秒 → blick。按给定 BPM 把秒换算成拍，再乘每拍 blick 数。"""
    if bpm <= 0:
        raise ValueError(f"bpm 必须为正数，收到 {bpm!r}")
    return int(round(float(seconds) * (float(bpm) / 60.0) * BLICK_PER_QUARTER))


def blick_to_sec(blick: int, bpm: float) -> float:
    """blick → 秒（与 :func:`sec_to_blick` 互逆，主要用于校验）。"""
    if bpm <= 0:
        raise ValueError(f"bpm 必须为正数，收到 {bpm!r}")
    return float(blick) / BLICK_PER_QUARTER * 60.0 / float(bpm)


def default_parameters() -> dict[str, Any]:
    """参数曲线容器（与真实 153 工程一致的空曲线）。"""
    return {key: {"mode": "cubic", "points": []} for key in _PARAM_KEYS}


def _take_block() -> dict[str, Any]:
    return {"activeTakeId": 0, "takes": [{"id": 0, "expr": 0.0, "liked": False}]}


def _note_json(note: SvNote, tempo_map: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """把音符换算成 SVP 的 blick。

    ⚠️ **时值必须两端各映射一次再相减，不能拿 duration 直接喂 _map_sec_to_blick。**

    `_map_sec_to_blick` 是按速度表做的**分段线性**「绝对时间 → blick」映射。
    单速度时它恰好等价于线性缩放，所以旧写法看不出问题；**一旦变速就全错**——
    实测那首变速曲（开头 255 BPM、之后 89.9 / 73.2 / 59.3 / 62.7）：
    后面每个音符的时值都按**开头那段 255 BPM** 换算，被拉长约 2.8 倍，
    于是音符尾巴盖住后面所有音 —— 主人声轨 767 对重叠、和弦轨 5684 对。
    虚拟歌姬软件不允许同轨重叠，用户实测反馈"音符重叠"就是这么来的。

    顺带：这也解释了为什么以前没被发现 —— 所有历史验收样本都是单速度曲；
    `check_tempo_map.py` 测的是 `sec_to_blick`/`blick_to_sec` 的往返，没测这条换算路径。
    """
    onset = _map_sec_to_blick(note.start, tempo_map)
    end = _map_sec_to_blick(note.end, tempo_map)
    duration = end - onset
    if duration <= 0:
        duration = 1
    return {
        "musicalType": "singing",
        "onset": onset,
        "duration": duration,
        "lyrics": note.lyrics,
        "phonemes": "",
        "accent": "",
        "pitch": note.pitch,
        "detune": 0,
        "instantMode": False,
        "attributes": {
            "dF0Left": 0.0,
            "dF0Right": 0.0,
            "dF0Vbr": 0.0,
            "evenSyllableDuration": True,
        },
        "systemAttributes": {"evenSyllableDuration": True},
        "pitchTakes": _take_block(),
        "timbreTakes": _take_block(),
    }


def build_svp(
    notes: Sequence[SvNote] | Iterable[SvNote],
    *,
    bpm: float,
    numerator: int = 4,
    denominator: int = 4,
    track_name: str = "主人声",
    project_name: str = "SheetSage2",
    lyrics_default: str = "la",
    start_time_seconds: float = 0.0,
    color: str = _DEFAULT_TRACK_COLORS[0],
    tempo_map: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造 SVP 153 工程字典。

    Args:
        notes: 音符序列（秒）。
        bpm: 速度；SVP 的 ``time.tempo`` 与 blick 换算都依赖它，必须真实。
            给了 ``tempo_map`` 时，它只作为速度表为空时的兜底。
        numerator / denominator: 拍号，如 ``4`` / ``4``。
        track_name: 轨道名（会显示在 SynthV 里）。
        project_name: 工程名，写入 ``renderConfig.filename``。
        lyrics_default: 缺歌词时的占位。
        start_time_seconds: 工程起始时间偏移。
        color: 轨道颜色（ARGH 十六进制，无 ``#``）。
        tempo_map: 速度表 ``[{"t": 秒, "bpm": ...}, ...]``，用于变速曲（见 app.tempo）。
            为 None / 单条时行为与只给 ``bpm`` 完全一致。
    """
    if bpm <= 0 and not tempo_map:
        raise ValueError(f"bpm 必须为正数，收到 {bpm!r}")
    # 统一走速度表：单条时等价于原来的单一 bpm
    tmap = normalize_tempo_map(tempo_map, bpm if bpm > 0 else 120.0)

    ordered = sorted(
        (n if isinstance(n, SvNote) else SvNote(*n) for n in notes),
        key=lambda n: (n.start, n.pitch),
    )
    # 补齐缺省歌词
    for n in ordered:
        if not n.lyrics:
            n.lyrics = lyrics_default

    group_id = str(uuid.uuid4())
    note_json = [_note_json(n, tmap) for n in ordered]

    main_group: dict[str, Any] = {
        "name": "main",
        "uuid": group_id,
        "parameters": default_parameters(),
        "vocalModes": {},
        "notes": note_json,
    }

    main_ref: dict[str, Any] = {
        "groupID": group_id,
        "blickAbsoluteBegin": 0,
        "blickAbsoluteEnd": -1,
        "blickOffset": 0,
        "pitchOffset": 0,
        "isInstrumental": False,
        "systemPitchDelta": {"mode": "cubic", "points": []},
        "database": {
            "name": "",
            "language": "",
            "phoneset": "",
            "languageOverride": "",
            "phonesetOverride": "",
            "backendType": "",
            "version": "-2",
        },
        "dictionary": "",
        "voice": {"vocalModeInherited": True, "vocalModePreset": "", "vocalModeParams": {}},
        "pitchTakes": _take_block(),
        "timbreTakes": _take_block(),
    }

    track = {
        "name": track_name,
        "dispColor": color,
        "dispOrder": 0,
        "renderEnabled": False,
        "mixer": {
            "gainDecibel": 0.0,
            "pan": 0.0,
            "mute": False,
            "solo": False,
            "display": True,
        },
        "mainGroup": main_group,
        "mainRef": main_ref,
        "groups": [],
    }

    return {
        "version": SVP_VERSION,
        "time": {
            "meter": [{"index": 0, "numerator": int(numerator), "denominator": int(denominator)}],
            # 速度表：可能多条（变速曲）。单速度时就是原来那一条，行为不变。
            "tempo": [
                {"position": _map_sec_to_blick(item["t"], tempo_map), "bpm": float(item["bpm"])}
                for item in tempo_map
            ],
            "startTimeSeconds": float(start_time_seconds),
        },
        "library": [],
        "tracks": [track],
        "renderConfig": {
            "destination": "",
            "filename": project_name,
            "numChannels": 1,
            "aspirationFormat": "noAspiration",
            "bitDepth": 16,
            "sampleRate": 44100,
            "exportMixDown": True,
            "exportPitch": False,
        },
    }


def write_svp(path: str | Path, project: Mapping[str, Any]) -> Path:
    """把工程字典写成 UTF-8 JSON 文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(project, ensure_ascii=False, indent=1), encoding="utf-8")
    return target


def notes_from_playback(
    playback: Mapping[str, Any] | str | Path,
    *,
    track_name: str = "Vocal",
    lyrics: str | Mapping[int, str] | None = None,
    merge_same_pitch: bool = True,
    min_duration: float = 0.05,
) -> list[SvNote]:
    """从 SheetSage2 的 ``playback.json`` 提取音符。

    ``playback.json`` 形如 ``{"tracks":[{"name":"Vocal","notes":[{"pitch","start","end",...}]}]}``，
    秒为单位，正是 SVP 需要的输入。

    Args:
        playback: 已解析的字典，或 ``playback.json`` 路径。
        track_name: 取哪条轨（``仅主旋律乐谱`` 时用 ``Vocal``）。
        lyrics: 统一歌词字符串，或 ``{pitch: 歌词}`` 映射。
        merge_same_pitch: 合并**首尾相接且音高相同**的相邻音符。
        min_duration: 短于该值（秒）的音符会被丢弃，用于去噪。
    """
    if isinstance(playback, (str, Path)):
        playback = json.loads(Path(playback).read_text(encoding="utf-8"))

    tracks = playback.get("tracks") or []
    chosen = None
    for t in tracks:
        if str(t.get("name", "")).strip().lower() == track_name.strip().lower():
            chosen = t
            break
    if chosen is None and tracks:
        chosen = tracks[0]
    if chosen is None:
        return []

    def lyric_for(pitch: int) -> str:
        if lyrics is None:
            return "la"
        if isinstance(lyrics, Mapping):
            return lyrics.get(pitch, "la")
        return str(lyrics)

    raw: list[SvNote] = []
    for item in chosen.get("notes") or []:
        try:
            start = float(item["start"])
            end = float(item["end"])
            pitch = int(item["pitch"])
        except (KeyError, TypeError, ValueError):
            continue
        if end - start < min_duration:
            continue
        raw.append(SvNote(start, end, pitch, lyric_for(pitch)))

    raw.sort(key=lambda n: (n.start, n.pitch))
    if not merge_same_pitch:
        return raw

    merged: list[SvNote] = []
    for note in raw:
        if merged:
            prev = merged[-1]
            if prev.pitch == note.pitch and abs(prev.end - note.start) < 1e-6:
                prev.end = note.end
                continue
        merged.append(SvNote(note.start, note.end, note.pitch, note.lyrics))
    return merged
