"""从 ABC 重建导出产物（SVP / MIDI）。

用途
----
用户在「乐谱编辑」里改完 ABC 后，导出的 SVP / MIDI 必须反映改动
（已定裁决：编辑后导出也用编辑结果）。

流程::

    ABC 文本 ──parse_abc──▶ 记谱音符 ──notes_to_seconds──▶ 秒
                                              │
                          ┌───────────────────┴───────────────────┐
                          ▼                                       ▼
                   build_svp → *.svp                      pretty_midi → *.mid

时间对齐依赖 ``playback.json`` 的小节表（见 :mod:`app.abcp`），因此换算后的
绝对时间与原曲一致，而不是按固定 BPM 累加。

产物统一写到 ``<任务目录>/export/``，与 SheetSage2 的原始产物分开，避免覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.abcp import AbcScore, notes_to_seconds, parse_abc, to_simple_notes
from app.lyrics import LyricWord, assign_lyrics
from app.svpw import SvNote, build_svp, write_svp
from app.tempo import (
    BLICK_PER_QUARTER,
    derive_tempo_map,
    sec_to_blick as _map_sec_to_blick,
)

__all__ = [
    "EXPORT_DIRNAME",
    "LYRICS_FILENAME",
    "CHORD_TRACK_DISPLAY",
    "CHORD_TRACK_MIDI",
    "EXPORT_VOICE_TOKENS",
    "DEFAULT_EXPORT_VOICES",
    "voice_kind",
    "normalize_export_voices",
    "load_measures",
    "load_lyrics_words",
    "load_chord_notes",
    "rebuild_exports",
    "pick_voices",
    "monophonic_groups",
    "split_polyphonic_tracks",
]

EXPORT_DIRNAME = "export"
#: 歌词识别产物文件名（放在任务目录根部）
LYRICS_FILENAME = "lyrics.json"

#: 主旋律声部候选名（小写）
_MELODY_NAMES = {"vocal", "melody", "lead", "voice"}
#: 声部显示名映射
_DISPLAY = {
    "vocal": "主人声",
    "lead": "主人声",
    "voice": "主人声",
    "melody": "主旋律",
    "ins": "器乐旋律",
    "instrumental": "器乐旋律",
    "chords": "和弦",
    "chord": "和弦",
}

#: 和弦轨的显示名 / MIDI 轨名。MIDI 的轨名走 Latin-1，只能 ASCII；
#: 与模型的 ``transcription.mid`` 保持一致，都叫 ``Chords``。
CHORD_TRACK_DISPLAY = "和弦"
CHORD_TRACK_MIDI = "Chords"


def load_measures(out_dir: str | Path) -> list[dict[str, Any]]:
    """读取 ``playback.json`` 的小节表；缺失时返回空列表。"""
    p = Path(out_dir) / "playback.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("measures") or []


def load_lyrics_words(out_dir: str | Path) -> list[LyricWord] | None:
    """读取歌词识别产物（``lyrics.json``）。

    同时兼容两种形状：

    * 本项目自己的 ``{"words": [{"text","start","end"}, ...]}``；
    * 整合包 ``asr.json`` 的 ``{"items": [[字, 起, 止], ...]}`` 契约。

    返回 ``None`` 表示尚无歌词。
    """
    p = Path(out_dir) / LYRICS_FILENAME
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    words: list[LyricWord] = []
    for item in data.get("words") or []:
        try:
            words.append(
                LyricWord(
                    str(item["text"]),
                    float(item["start"]),
                    float(item["end"]),
                    line=int(item.get("line", -1)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    if not words:
        for item in data.get("items") or []:
            try:
                words.append(LyricWord(str(item[0]), float(item[1]), float(item[2])))
            except (IndexError, TypeError, ValueError):
                continue
    if not words:
        return None
    words.sort(key=lambda w: (w.start, w.end))
    return words


#: 可选的导出内容，与前端 ③ 的三个勾选一一对应：
#: 人声主旋律 / 器乐旋律 / 和弦。**勾什么导什么，完全由用户决定。**
EXPORT_VOICE_TOKENS = ("vocal", "ins", "chords")

#: 默认只勾"人声主旋律"（用户 2026-09-15 定的默认）
DEFAULT_EXPORT_VOICES = ("vocal",)

#: 声部名 → 这三个 token 之一
_VOICE_KIND = {
    "vocal": "vocal", "melody": "vocal", "lead": "vocal", "voice": "vocal",
    "ins": "ins", "instrumental": "ins", "accompaniment": "ins",
}


def voice_kind(name: str) -> str | None:
    """把一个 ABC 声部名归到 ``vocal`` / ``ins``，认不出来返回 ``None``。"""
    return _VOICE_KIND.get((name or "").strip().lower())


def normalize_export_voices(
    export_voices: Sequence[str] | None,
    only_melody: bool | None = None,
) -> list[str]:
    """把「导出哪些内容」归一成 ``["vocal", "ins", "chords"]`` 的子集。

    * 给了 ``export_voices`` 就用它（过滤掉不认识的 token，保持声明顺序）；
    * 没给但给了老的 ``only_melody`` → 向后兼容映射：
      ``True`` → 只要人声主旋律；``False`` → 三个都要；
    * 两个都没给 → :data:`DEFAULT_EXPORT_VOICES`。
    """
    if export_voices is not None:
        if isinstance(export_voices, str):
            export_voices = [p for p in export_voices.replace(",", " ").split() if p]
        picked = [t for t in EXPORT_VOICE_TOKENS if t in {str(x).strip().lower() for x in export_voices}]
        return picked or list(DEFAULT_EXPORT_VOICES)
    if only_melody is None:
        return list(DEFAULT_EXPORT_VOICES)
    return ["vocal"] if only_melody else list(EXPORT_VOICE_TOKENS)


def pick_voices(score: AbcScore, export_voices: Sequence[str]) -> list[str]:
    """按「要导出哪些内容」从 ABC 里挑出声部（保持 ABC 里的声明顺序）。"""
    wanted = {str(t).strip().lower() for t in export_voices}
    available = list(score.voices)
    if not available:
        available = sorted({n.voice for n in score.notes})
    picked = [v for v in available if voice_kind(v) in wanted]
    return picked


def _display_name(voice: str) -> str:
    return _DISPLAY.get(voice.strip().lower(), voice)


def _midi_safe(name: str) -> str:
    """MIDI 的 meta 事件（轨道名）是 Latin-1 编码，中文会直接抛 UnicodeEncodeError。

    这里把非 Latin-1 字符替换掉，保证写出不会因名称而失败。
    """
    try:
        name.encode("latin-1")
        return name
    except UnicodeEncodeError:
        cleaned = "".join(ch if ord(ch) < 256 else "_" for ch in name).strip("_")
        return cleaned or "Track"


def monophonic_groups(spans: Sequence[Sequence[float]]) -> list[list[int]]:
    """把可能互相重叠的音符分成若干组，**每组内部严格不重叠**，且组数最少。

    为什么需要：SheetSage2 的每条轨都是复音的 —— 实测模型自带的
    ``melody_vocal.mid`` 的 Vocal 轨同时发声峰值就到 2、``chords.mid`` 到 8
    （和弦本来就是多音同时）。而 Synthesizer V 这类"一人一声部"的虚拟歌姬
    **不允许同轨音符重叠**，用户实测反馈导入后音符叠在一起。

    **用户定的处理方式（2026-09-15）：不要截短丢掉，而是默认拆成
    ``和弦1`` / ``和弦2`` …** —— 一条信息都不少，每条又都满足"同轨不重叠"。

    算法是标准的贪心区间划分：按 ``(起点, 音高)`` 排序，每个音放进
    "上一音结束 <= 本音起点"的**第一个**组，没有就开新组。
    对区间图着色这是最优解：**组数 = 同一时刻最多几个音同时响**。
    起点相同时按音高升序处理，于是第 1 组总是拿到最低音（稳定的"低音线"）。

    Returns:
        每组的**原始下标**列表（组内按起点有序）。
    """
    order = sorted(range(len(spans)), key=lambda i: (float(spans[i][0]), float(spans[i][2])))
    group_ends: list[float] = []
    groups: list[list[int]] = []
    for i in order:
        start = float(spans[i][0])
        end = float(spans[i][1])
        for gi, last_end in enumerate(group_ends):
            if last_end <= start + 1e-9:
                groups[gi].append(i)
                group_ends[gi] = end
                break
        else:
            groups.append([i])
            group_ends.append(end)
    return groups


def split_polyphonic_tracks(
    svp_tracks: Sequence[tuple[str, Sequence[SvNote]]],
    midi_tracks: Sequence[tuple[str, Sequence[Sequence[float]]]],
) -> tuple[
    list[tuple[str, list[SvNote]]],
    list[tuple[str, list[Sequence[float]]]],
    list[str],
]:
    """把复音的轨拆成若干条严格单音的轨（SVP 与 MIDI 用**同一套分组**，一一对应）。

    * 本来就单音的轨：**保持原名**，不拆、不加数字后缀；
    * 复音的轨：拆成 ``名字1`` / ``名字2`` …（如 ``和弦1``、``和弦2``），
      MIDI 侧同理用 ASCII 名 ``Chords1``、``Chords2``。

    Returns:
        ``(svp_tracks, midi_tracks, 拆分明细)``；明细形如 ``["和弦 → 4 条"]``，写进诊断。
    """
    out_svp: list[tuple[str, list[SvNote]]] = []
    out_midi: list[tuple[str, list[Sequence[float]]]] = []
    detail: list[str] = []
    for (display, ns), (midi_name, mns) in zip(svp_tracks, midi_tracks):
        svp_list = list(ns)
        midi_list = list(mns)
        if not svp_list:
            continue
        groups = monophonic_groups([(n.start, n.end, n.pitch) for n in svp_list])
        if len(groups) <= 1:
            out_svp.append((display, svp_list))
            out_midi.append((midi_name, midi_list))
            continue
        detail.append(f"{display} → {len(groups)} 条")
        for gi, idx in enumerate(groups, start=1):
            out_svp.append((f"{display}{gi}", [svp_list[i] for i in idx]))
            out_midi.append((f"{midi_name}{gi}", [midi_list[i] for i in idx]))
    return out_svp, out_midi, detail


def load_chord_notes(out_dir: str | Path) -> list[dict[str, Any]]:
    """读取 SheetSage2 产出的和弦轨（``chords.mid``），返回秒为单位的音符。

    和弦在模型侧**不是 ABC 声部**：``chord.lab`` 只记录区间与和弦名，ABC 里是行内
    ``"Cm"`` 标注；真正的可播放和弦音符由模型的 ``build_playback()`` 展开成
    ``chords.mid``。所以要导出一条和弦轨，只能从这里取。

    找不到 ``chords.mid`` 时回退到 ``transcription.mid``，但**只取名字含 chord 的轨**，
    避免把旋律重复叠一遍。

    Returns:
        ``[{"start": 秒, "end": 秒, "pitch": MIDI, "velocity": 力度}, ...]``，按时间排序。
    """
    root = Path(out_dir)
    path = root / "chords.mid"
    if not path.is_file():
        path = root / "transcription.mid"
    if not path.is_file():
        return []
    try:
        import pretty_midi

        parsed = pretty_midi.PrettyMIDI(str(path))
    except Exception:  # noqa: BLE001 - 和弦是可选轨，读不出来就当没有
        return []

    out: list[dict[str, Any]] = []
    for instrument in parsed.instruments:
        if "chord" not in (instrument.name or "").strip().lower():
            continue
        for note in instrument.notes:
            if note.end <= note.start:
                continue
            out.append(
                {
                    "start": float(note.start),
                    "end": float(note.end),
                    "pitch": int(note.pitch),
                    "velocity": int(note.velocity),
                }
            )
    out.sort(key=lambda n: (n["start"], n["pitch"]))
    return out


def _write_midi_mapped(
    tracks: Sequence[tuple[str, Sequence[Sequence[float]]]],
    path: Path,
    tempo_map: Sequence[dict[str, Any]],
    *,
    numerator: int = 4,
    denominator: int = 4,
) -> Path | None:
    """变速曲的 MIDI：用 mido 写一条**导电轨**放速度表（set_tempo 事件）。

    pretty_midi 只支持单一 initial_tempo，写不出速度变化 —— 而 MIDI 里没有速度变化，
    DAW 里的小节线和网格就会全错（音符绝对时间是对的，但结构不对）。
    换算与 SVP 共用同一套分段线性映射，两者不会打架。

    事件顺序对齐「模型自带 transcription.mid / pretty_midi 输出」的惯例：
    ``set_tempo`` → ``time_signature`` → ``track_name``。
    早期版本把 track_name 放最前且漏了 time_signature，导入 SynthV 时
    会被当成没有速度信息（退回默认 120）。
    """
    try:
        import mido
    except ImportError:
        return None

    tpq = 480

    def tick(seconds: float) -> int:
        return int(round(_map_sec_to_blick(seconds, tempo_map) * tpq / BLICK_PER_QUARTER))

    mid = mido.MidiFile(ticks_per_beat=tpq)
    conductor = mido.MidiTrack()
    mid.tracks.append(conductor)

    # mido 的 `time` 是**增量**，不是绝对位置：必须先把「绝对 tick + 同 tick 内的次序」
    # 排好，再统一换算成增量发射。否则后 append 的 meta（比如 time_signature）会被
    # 推到最后一个 set_tempo 的位置上去（实测落到了 tick 195839 而不是 0）。
    meta: list[tuple[int, int, Any]] = []
    for i, item in enumerate(tempo_map):
        meta.append((tick(item["t"]), 0,
                     mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(item["bpm"]), time=0)))
    meta.append((0, 1, mido.MetaMessage(
        "time_signature", numerator=int(numerator), denominator=int(denominator), time=0)))
    meta.append((0, 2, mido.MetaMessage("track_name", name="Tempo", time=0)))
    meta.sort(key=lambda e: (e[0], e[1]))
    prev = 0
    for at, _order, msg in meta:
        msg.time = max(0, at - prev)
        prev = at
        conductor.append(msg)

    for name, notes in tracks:
        tr = mido.MidiTrack()
        mid.tracks.append(tr)
        tr.append(mido.MetaMessage("track_name", name=_midi_safe(name), time=0))
        tr.append(mido.Message("program_change", program=0, time=0))
        events: list[tuple[int, int, int, int]] = []   # (tick, off=0/on=1, pitch, velocity)
        for note in notes:
            start, end, pitch = float(note[0]), float(note[1]), int(note[2])
            velocity = int(note[3]) if len(note) > 3 else 100
            if end <= start:
                continue
            events.append((tick(start), 1, pitch, max(1, min(127, velocity))))
            events.append((tick(end), 0, pitch, 0))
        # 同一 tick 上先关后开，避免同音高重叠时把前一个音"吞掉"
        events.sort(key=lambda e: (e[0], e[1]))
        prev = 0
        for at, kind, pitch, velocity in events:
            delta = max(0, at - prev)
            prev = at
            tr.append(
                mido.Message("note_on" if kind else "note_off",
                             note=max(0, min(127, pitch)), velocity=velocity, time=delta)
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    mid.save(str(path))
    return path


def write_midi(
    tracks: Sequence[tuple[str, Sequence[Sequence[float]]]],
    path: str | Path,
    *,
    bpm: float,
    tempo_map: Sequence[dict[str, Any]] | None = None,
    numerator: int = 4,
    denominator: int = 4,
) -> Path | None:
    """把 ``[(名称, [(start秒, end秒, midi[, 力度]), ...]), ...]`` 写成标准 MIDI。

    力度可选：给 4 元组就按它写（和弦轨用模型自己的 48，旋律轨用默认 100），
    只给 3 元组则用 100。

    速度表超过一条时走 :func:`_write_midi_mapped`（mido + set_tempo + time_signature），
    否则沿用 pretty_midi（老路径，行为不变）。
    """
    target = Path(path)
    if tempo_map is not None and len(tempo_map) > 1:
        written = _write_midi_mapped(
            tracks, target, tempo_map, numerator=numerator, denominator=denominator)
        if written is not None:
            return written
        # mido 不可用就退回单速度版本（音符绝对时间仍然正确）

    try:
        import pretty_midi
    except ImportError:
        return None

    pm = pretty_midi.PrettyMIDI(initial_tempo=bpm)
    for name, notes in tracks:
        inst = pretty_midi.Instrument(program=0, name=_midi_safe(name))
        for note in notes:
            start, end, pitch = float(note[0]), float(note[1]), int(note[2])
            velocity = int(note[3]) if len(note) > 3 else 100
            if end <= start:
                continue
            inst.notes.append(
                pretty_midi.Note(
                    velocity=max(1, min(127, velocity)),
                    pitch=max(0, min(127, int(pitch))),
                    start=max(0.0, float(start)),
                    end=float(end),
                )
            )
        if inst.notes:
            pm.instruments.append(inst)
    if not pm.instruments:
        return None
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    pm.write(str(target))
    return target


def rebuild_exports(
    out_dir: str | Path,
    abc_text: str,
    *,
    bpm: float | None = None,
    export_voices: Sequence[str] | None = None,
    only_melody: bool | None = None,
    lyrics: str = "la",
    project_name: str | None = None,
    measures: Sequence[dict[str, Any]] | None = None,
    write_abc: bool = True,
    lyrics_words: Sequence[LyricWord] | None = None,
    use_stored_lyrics: bool = True,
    continuation: str = "auto",
    extra_tracks: Sequence[tuple[str, Sequence[Sequence[float]]]] | None = None,
) -> dict[str, Any]:
    """由 ABC 文本重建 ``export/`` 下的 SVP 与 MIDI。

    歌词处理
    --------
    歌词以**带时间戳的词**（``lyrics.json``）存储，在导出时才对**当前音符**做分配。
    这样用户在「乐谱编辑」里改动 ABC、音符集合变化后，歌词不会错位——分配永远
    基于本次真实的音符。

    Args:
        lyrics: 无法匹配时（或没有歌词时）使用的统一占位歌词。
        lyrics_words: 直接给出词表；``None`` 时按 ``use_stored_lyrics`` 决定是否
            从 ``<out_dir>/lyrics.json`` 读取。

    Returns:
        结果摘要，含产物路径、音符数、歌词分配统计与诊断信息。
    """
    out = Path(out_dir)
    export_dir = out / EXPORT_DIRNAME
    export_dir.mkdir(parents=True, exist_ok=True)
    project = project_name or out.name

    # 「导出哪些内容」完全由用户决定（③ 的三个人声主旋律/器乐旋律/和弦勾选）
    sel = normalize_export_voices(export_voices, only_melody)
    # 和弦轨来自 chords.mid，**不是 ABC 声部**，所以「只勾和弦」时 ABC 会是空的，
    # 但导出仍然要能成立 —— 提前取好，别让下面「ABC 没有音符」的早返回拦掉。
    chord_notes = load_chord_notes(out) if "chords" in sel else []

    score = parse_abc(abc_text or "")
    if not score.notes and not chord_notes:
        return {"ok": False, "error": "ABC 中没有解析到任何音符", "exports": []}

    table = list(measures) if measures is not None else load_measures(out)
    tempo = float(bpm or score.bpm or 120.0)
    # 速度表：变速曲（如 110→75→110）必须靠它才能让 SVP/MIDI 的小节网格正确。
    # 原料就是那份小节表；单速度时会退化成一条，行为与旧的单一 bpm 一致。
    tempo_map = derive_tempo_map(table) if table else []
    if not tempo_map:
        tempo_map = [{"t": 0.0, "bpm": tempo}]
    warnings = notes_to_seconds(score.notes, bpm=tempo, measures=table)

    voices = pick_voices(score, sel)
    exports: list[dict[str, Any]] = []
    svp_tracks: list[tuple[str, list[SvNote]]] = []
    midi_tracks: list[tuple[str, list[tuple[float, float, int]]]] = []

    # 所选声部可能整段是休止（例如只裁了纯器乐前奏的片段，人声还没进）。
    # 这时**明确回退**到真正有音符的声部，并把回退写进 warnings，
    # 绝不静默换数据——否则会把器乐当成「主人声」交付。
    def _voiced(v: str) -> list[tuple[float, float, int]]:
        return to_simple_notes(score.notes, voice=v)

    if voices and not any(_voiced(v) for v in voices):
        others = [v for v in sorted({n.voice for n in score.notes}) if v not in voices and _voiced(v)]
        if others:
            warnings.append(
                f"所选声部 {voices} 在该片段内没有音符（可能尚未进入主旋律），"
                f"已回退为 {others} 导出，请在乐谱编辑中确认声部是否符合预期。"
            )
            voices = others
        else:
            voices = []

    # ---- 歌词：优先用识别结果，按当前音符做分配；否则统一占位 ----
    words = lyrics_words
    if words is None and use_stored_lyrics:
        words = load_lyrics_words(out)
    lyric_stats: dict[str, Any] = {}
    lyrics_source = "recognized" if words else "fallback"

    for voice in voices:
        simple = to_simple_notes(score.notes, voice=voice)
        if not simple:
            continue
        display = _display_name(voice)
        spans = [(s, e) for s, e, _ in simple]
        if words:
            note_lyrics, lyric_stats = assign_lyrics(
                words, spans, fallback=lyrics, continuation=continuation
            )
        else:
            note_lyrics = [lyrics] * len(simple)
        svp_tracks.append(
            (
                display,
                [SvNote(s, e, p, ly) for (s, e, p), ly in zip(simple, note_lyrics)],
            )
        )
        # MIDI 轨道名走 Latin-1，用 ABC 里的 ASCII 声部名（Vocal/Ins），不要中文
        midi_tracks.append((voice, simple))

    if not svp_tracks and not chord_notes:
        return {"ok": False, "error": "所选内容没有可用音符", "exports": [], "warnings": warnings}

    # ---- 和弦轨（勾了「和弦」才加；上面已经取好）----
    # 为什么必须单独取：和弦在 SheetSage2 里**不是 ABC 声部**，而是 chord.lab 的区间 +
    # ABC 行内的 "Cm" 标注；真正的和弦音符由模型的 build_playback() 展开成 chords.mid
    # （exports_sheetsage2.py:178-180）。只按 ABC 声部导出就会整条丢掉，
    # 表现为「整合包有和弦、我们没有」。
    if chord_notes:
        svp_tracks.append(
            (
                CHORD_TRACK_DISPLAY,
                [SvNote(n["start"], n["end"], n["pitch"], lyrics) for n in chord_notes],
            )
        )
        # MIDI 轨道名用 ASCII（Latin-1 限制），与模型 transcription.mid 里的 'Chords' 同名
        midi_tracks.append(
            (
                CHORD_TRACK_MIDI,
                [(n["start"], n["end"], n["pitch"], n["velocity"]) for n in chord_notes],
            )
        )

    # ---- 附加旋律轨（用于男女合唱：把另一条声部的音符直接挂上去）----
    # 对象不需要 ABC：例如「和声 stem」跑出来的 SheetSage2 可能因为节拍判错而 ABC 生成失败，
    # 但 melody_vocal.lab 里的「秒 + 音高」是好的，直接挂成第二条旋律轨即可。
    for display, notes, *rest in extra_tracks or ():
        notes = [n for n in notes if n[1] > n[0]]
        if not notes:
            continue
        svp_tracks.append((display, [SvNote(s, e, p, lyrics) for s, e, p, *_ in notes]))
        # MIDI 轨名走 Latin-1：中文会被剥光变成 "Track"，所以允许单独给 ASCII 名
        midi_name = rest[0] if rest else (display if display.isascii() else _midi_safe(display))
        midi_tracks.append(
            (midi_name, [(n[0], n[1], n[2], n[3] if len(n) > 3 else 100) for n in notes])
        )

    # ---- 拆分复音轨：虚拟歌姬要求同轨不重叠，拆成 和弦1 / 和弦2 … ----
    # 覆盖所有来源：ABC 声部、和弦轨、extra_tracks。本来就单音的轨保持原名不拆。
    svp_tracks, midi_tracks, split_detail = split_polyphonic_tracks(svp_tracks, midi_tracks)
    if split_detail:
        warnings.append(
            "复音拆轨（虚拟歌姬要求同轨不重叠）：" + "；".join(split_detail)
            + f"；共 {len(svp_tracks)} 条 SVP 轨 / {sum(len(n) for _, n in svp_tracks)} 个音符"
        )
    if not svp_tracks:
        return {"ok": False, "error": "没有任何可用音符", "exports": [], "warnings": warnings}

    num, den = score.meter
    first_display, first_notes = svp_tracks[0]
    project_svp = build_svp(
        first_notes,
        bpm=tempo,
        numerator=num,
        denominator=den,
        track_name=first_display,
        project_name=project,
        lyrics_default=lyrics,
        tempo_map=tempo_map,
    )
    for order, (display, ns) in enumerate(svp_tracks[1:], start=1):
        extra = build_svp(
            ns,
            bpm=tempo,
            numerator=num,
            denominator=den,
            track_name=display,
            project_name=project,
            lyrics_default=lyrics,
            color=["ff7db235", "ff4d8bff", "ffe0409b", "ff22c55e"][order % 4],
            tempo_map=tempo_map,
        )
        track = extra["tracks"][0]
        track["dispOrder"] = order
        project_svp["tracks"].append(track)

    svp_path = export_dir / f"{project}.svp"
    write_svp(svp_path, project_svp)
    exports.append(
        {
            "kind": "svp",
            "path": str(svp_path),
            "rel": f"{EXPORT_DIRNAME}/{svp_path.name}",
            "version": project_svp["version"],
            "notes": sum(len(n) for _, n in svp_tracks),
            "tracks": [d for d, _ in svp_tracks],
        }
    )

    # ---- MIDI ----
    midi_path = export_dir / f"{project}.mid"
    written = write_midi(
        midi_tracks, midi_path, bpm=tempo, tempo_map=tempo_map,
        numerator=num, denominator=den,
    )
    if written:
        exports.append(
            {
                "kind": "midi",
                "path": str(written),
                "rel": f"{EXPORT_DIRNAME}/{midi_path.name}",
                "notes": sum(len(n) for _, n in midi_tracks),
                "tracks": [d for d, _ in midi_tracks],
            }
        )
    else:
        warnings.append("MIDI 写出不可用（pretty_midi 缺失或没有音符）")

    # ---- 回写编辑后的 ABC，便于留档与复现 ----
    if write_abc:
        abc_path = export_dir / f"{project}.abc"
        abc_path.write_text(abc_text, encoding="utf-8")
        exports.append(
            {
                "kind": "abc",
                "path": str(abc_path),
                "rel": f"{EXPORT_DIRNAME}/{abc_path.name}",
                "lines": len([l for l in abc_text.splitlines() if l.strip()]),
            }
        )

    return {
        "ok": True,
        "bpm": tempo,
        "meter": score.header.get("meter"),
        "key": score.header.get("key"),
        "voices": voices,
        "note_events": len(score.notes),
        "aligned_by_measure_table": bool(table),
        "exports": exports,
        "warnings": warnings,
        "lyrics_source": lyrics_source,
        "lyric_word_count": len(words) if words else 0,
        "lyric_stats": lyric_stats,
        # 和弦轨（只在实际加进去时非 0）——让「诊断信息」能看出这条轨到底有没有
        # 速度表（单速度时长度 1）——让「诊断」能看出这是不是变速曲
        "tempo_map": tempo_map,
        "tempo_changes": len(tempo_map),
        "chord_notes": len(chord_notes),
        "chord_track": CHORD_TRACK_DISPLAY if chord_notes else None,
    }
