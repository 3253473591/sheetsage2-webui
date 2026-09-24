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
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.abcp import AbcNote, AbcScore, notes_to_seconds, parse_abc, to_simple_note_objs, to_simple_notes
from app.harmony import DEGREE_CN
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
    "ROLL_FILENAME",
    "CHORD_TRACK_DISPLAY",
    "CHORD_TRACK_MIDI",
    "EXPORT_VOICE_TOKENS",
    "DEFAULT_EXPORT_VOICES",
    "voice_kind",
    "voice_display",
    "split_harmony_voice",
    "harmony_voice_name",
    "harmony_display",
    "MAX_HARMONY_DEGREE",
    "normalize_export_voices",
    "load_measures",
    "load_lyrics_words",
    "load_chord_notes",
    "load_roll",
    "save_roll",
    "merge_roll_tracks",
    "sanitize_roll_tracks",
    "score_from_roll",
    "select_roll_tracks",
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

# --------------------------------------------------------------------------
# 平行和声轨（卷帘「生成和声轨」产出）
# --------------------------------------------------------------------------
# 声部名约定：**源声部名 + 带符号的半音数**，例如 ``Vocal+3`` / ``Vocal-5``。
# 为什么用「后缀半音数」而不是另起一个 ``Harmony`` 声部名：
#   * ``voice_kind`` 能顺着前缀认出它**仍属人声** —— 否则 ``pick_voices``
#     会把它当未知声部丢掉，表现为「卷帘上看得见、导出的 SVP 里没有」；
#   * MIDI 轨名直接用声部名（Latin-1 限制），``Vocal+3`` 是纯 ASCII，不会变 ``Track``；
#   * SVP 轨名走 :func:`voice_display`，统一显示成「和声 +3」。
# 前端 ``web/pianoroll.js`` 里有一份**同规则**的实现，改这里必须同步改那边。
#: 和声声部名约定：``源声部名 + 方向 h 度数``，例如 ``Vocal+h3``（上行三度）、
#: ``Vocal-h6``（下行六度）。度数记法见 :mod:`app.harmony`。
#: 为什么是度数而不是半音：**音程本身只有放在调性里才有意义** —— C 上方的三度是 E，
#: D 上方的三度是 F，半音数并不相同。
_HARMONY_VOICE_RE = re.compile(r"^(?P<base>.+?)(?P<sign>[+-])h(?P<degree>[1-7])$")

#: 允许的和声度数（一~七度）
MAX_HARMONY_DEGREE = 7


def split_harmony_voice(name: str) -> tuple[str, int] | None:
    """``"Vocal+h3"`` → ``("Vocal", 3)``；``"Vocal-h6"`` → ``("Vocal", -6)``。

    不是和声声部名（含普通声部名 ``Vocal`` / ``Ins`` / ``V1``）返回 ``None``。
    """
    m = _HARMONY_VOICE_RE.match((name or "").strip())
    if not m:
        return None
    degree = int(m.group("degree"))
    return m.group("base"), (degree if m.group("sign") == "+" else -degree)


def harmony_voice_name(base: str, degree: int) -> str:
    """由源声部名与度数造出和声声部名（须与前端 ``harmonyVoiceName`` 同规则）。"""
    d = int(degree)
    if not 1 <= abs(d) <= MAX_HARMONY_DEGREE:
        raise ValueError(f"和声度数必须在 ±1..±{MAX_HARMONY_DEGREE} 度之间，收到 {degree!r}")
    return f"{(base or 'Vocal').strip()}{'+' if d > 0 else '-'}h{abs(d)}"


def harmony_display(degree: int) -> str:
    """和声轨的显示名（``3`` → ``和声 +三度``）。"""
    d = int(degree)
    return f"和声 {'+' if d > 0 else '-'}{DEGREE_CN[abs(d)]}"


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


#: 卷帘的存档文件名（任务目录根部，与 score.abc 同级）。
#: 它是**卷帘编辑后的唯一真相**：模型产出的 ABC 只用来做首次导入，
#: 再次打开卷帘与重新生成导出都读它，这样卷帘上的改动不会丢。
ROLL_FILENAME = "roll.json"


def load_roll(out_dir: str | Path) -> dict[str, Any] | None:
    """读取卷帘存档；不存在或坏了返回 ``None``（调用方回退到 ABC）。"""
    p = Path(out_dir) / ROLL_FILENAME
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def merge_roll_tracks(
    existing: Sequence[Mapping[str, Any]] | None,
    incoming: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """按**声部名**合并卷帘轨道：来什么覆盖什么，没来的原样留着。

    为什么不整体替换：``export_voices`` 会让卷帘只看到被勾选的声部。整体替换的话，
    用户「取消勾选 → 保存 → 再勾上」就会把那条轨的音符全丢掉。
    """
    by_voice: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for tr in list(existing or ()) + list(incoming or ()):
        if not isinstance(tr, Mapping):
            continue
        v = str(tr.get("voice") or "").strip()
        if not v:
            continue
        if v not in by_voice:
            order.append(v)
        by_voice[v] = dict(tr)
    return [by_voice[v] for v in order]


def sanitize_roll_tracks(
    tracks: Sequence[Mapping[str, Any]] | None,
    *,
    max_notes: int = 20000,
) -> list[dict[str, Any]]:
    """校验并清洗前端传来的卷帘轨道。

    前端已经约束过一次（音量、网格、不重叠），但**接口不能信前端**：一个坏音符
    （零长、负起点、越界音高）会一路走到 SVP/MIDI 写出，报出来的错会完全对不上源头。

    和弦轨（``kind == "chords"``）直接跳过：它来自 ``chords.mid``，不是 ABC 声部，
    落盘会造出一条假的旋律轨。

    Raises:
        ValueError: 有硬性非法数据时（消息可直接回给用户）。
    """
    out: list[dict[str, Any]] = []
    total = 0
    for tr in tracks or ():
        if not isinstance(tr, Mapping):
            continue
        voice = str(tr.get("voice") or "").strip()
        kind = str(tr.get("kind") or "").strip().lower()
        if not voice or kind == "chords":
            continue
        if tr.get("editable") is False:
            continue
        notes: list[dict[str, Any]] = []
        for n in tr.get("notes") or ():
            if not isinstance(n, Mapping):
                continue
            try:
                start = float(n["start"])
                end = float(n["end"])
                pitch = int(n["pitch"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("卷帘音符缺少 start/end/pitch 或不是数字")
            if not (start >= -1e-6):
                raise ValueError(f"音符起点为负：{start}")
            if not (end > start):
                raise ValueError(f"音符时长为 0 或负数：{start} → {end}")
            if not (0 <= pitch <= 127):
                raise ValueError(f"音高越界：{pitch}")
            lyric = n.get("lyric")
            notes.append({
                "start": round(start, 6),
                "end": round(end, 6),
                "pitch": pitch,
                "lyric": ("" if lyric is None else str(lyric)),
            })
            total += 1
            if total > max_notes:
                raise ValueError(f"音符数超过上限 {max_notes}")
        out.append({
            "voice": voice,
            "kind": kind or "ins",
            "display": str(tr.get("display") or voice),
            "is_vocal": bool(tr.get("is_vocal")),
            "editable": True,
            "notes": notes,
        })
    return out


def save_roll(
    out_dir: str | Path,
    tracks: Sequence[Mapping[str, Any]],
    *,
    source_abc: str | None = None,
    bpm: float | None = None,
    header: Mapping[str, Any] | None = None,
) -> Path:
    """把**给定的这一份完整轨道表**写成 ``roll.json``。

    注意：这里**不做合并**（照写）。合并是调用方的事，因为只有它同时拿得到
    "当前全量轨"（``roll.json`` 或 ABC 导入）与"本次上传的可见轨"——
    见 :func:`merge_roll_tracks`。早期版本在这里从空存档起步，结果**首次保存**
    就把没被勾选（因而不在卷帘里、也就不在上传数据里）的声部整条丢掉了。
    """
    out = Path(out_dir)
    prev = load_roll(out) or {}
    data = {
        "version": 1,
        "source_abc": source_abc or prev.get("source_abc"),
        "bpm": float(bpm) if bpm else prev.get("bpm"),
        "header": _jsonable_header(header) if header else (prev.get("header") or {}),
        "tracks": [dict(t) for t in (tracks or ()) if isinstance(t, Mapping)],
    }
    path = out / ROLL_FILENAME
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _jsonable_header(header: Mapping[str, Any] | None) -> dict[str, Any]:
    """``AbcScore.header`` 里的 ``meter_tuple`` 是元组，JSON 里会变数组，这里统一成列表。"""
    out: dict[str, Any] = {}
    for k, v in (header or {}).items():
        out[k] = list(v) if isinstance(v, tuple) else v
    return out


def score_from_roll(
    tracks: Sequence[Mapping[str, Any]],
    *,
    bpm: float,
    header: Mapping[str, Any] | None = None,
) -> AbcScore:
    """把**卷帘的音符表**（单位秒）造成一个已定位好的 :class:`AbcScore`。

    为什么造 ``AbcScore`` 而不是"把音符序列化回 ABC 文本再解析一遍"：
    ABC 的时值是分数记号，绝对位置靠小节与累计时值推出来。要把任意秒数写回去，
    得处理连音线、休止填充、小节对齐，任何一处近似都会让导出的时间整体偏移。
    这里直接把秒塞进 ``start``，走的是**同一条导出管线**（和弦轨、复音拆轨、
    八度、速度表全都不变），只是不走有损的文本往返。

    ``lyrics_per_note=True``：歌词是逐音录的，导出时不能被 LRC 分配逻辑覆盖。
    """
    score = AbcScore(header=dict(header or {}))
    score.header.setdefault("q", float(bpm))
    score.header.setdefault("meter", "4/4")
    score.header.setdefault("meter_tuple", (4, 4))

    for tr in tracks or ():
        if not isinstance(tr, Mapping):
            continue
        voice = str(tr.get("voice") or "").strip()
        if not voice:
            continue
        if voice not in score.voices:
            score.voices.append(voice)
        for n in tr.get("notes") or ():
            try:
                start = float(n["start"])
                end = float(n["end"])
                pitch = int(n["pitch"])
            except (KeyError, TypeError, ValueError):
                continue
            if end <= start:
                continue
            note = AbcNote(
                voice=voice,
                onset=0.0,      # 卷帘路径不用记谱位置，位置只有秒
                duration=0.0,
                pitch=pitch,
                lyric=(str(n["lyric"]) if n.get("lyric") else None),
            )
            note.start = start
            # ``_dur_sec`` 是本项目既有约定（notes_to_seconds 也写这个私有属性），
            # AbcNote.duration_seconds 读它。
            note._dur_sec = end - start
            score.notes.append(note)

    score.lyrics_per_note = True
    return score


def select_roll_tracks(
    tracks: Sequence[Mapping[str, Any]],
    export_voices: Sequence[str],
) -> list[dict[str, Any]]:
    """按「导出哪些内容」挑出卷帘轨道（和弦轨不属于这里，见 ``load_chord_notes``）。"""
    wanted = {str(t).strip().lower() for t in export_voices}
    out: list[dict[str, Any]] = []
    for tr in tracks or ():
        if not isinstance(tr, Mapping):
            continue
        kind = str(tr.get("kind") or "").strip().lower()
        if kind == "chords":
            continue
        if voice_kind(str(tr.get("voice") or "")) in wanted or kind in wanted:
            out.append(dict(tr))
    return out


#: 可选的导出内容，与前端 ③ 的三个勾选一一对应：#: 人声主旋律 / 器乐旋律 / 和弦。**勾什么导什么，完全由用户决定。**
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
    key = (name or "").strip().lower()
    kind = _VOICE_KIND.get(key)
    if kind:
        return kind
    # 和声声部（``Vocal+3``）按**前缀**归属：它仍然是人声，只是叠了一条平行线。
    # 这里不认的话，rebuild_exports 里的 pick_voices 会把它丢掉 ——
    # 表现为「卷帘上明明有和声轨，导出的 SVP 里却没有」。
    split = split_harmony_voice(key)
    return _VOICE_KIND.get(split[0]) if split else None


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
    split = split_harmony_voice(voice)
    if split:
        return harmony_display(split[1])
    return _DISPLAY.get(voice.strip().lower(), voice)


def voice_display(voice: str) -> str:
    """声部名的中文显示名（``Vocal`` → ``主人声``）。

    公开给卷帘用：轨标签必须与导出时写进 SVP 的轨名一致，否则用户在卷帘上看到的
    「轨」和导出后在 Synthesizer V 里看到的「轨」会对不上号。
    """
    return _display_name(voice)


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
    abc_text: str = "",
    *,
    bpm: float | None = None,
    export_voices: Sequence[str] | None = None,
    only_melody: bool | None = None,
    lyrics: str = "la",
    project_name: str | None = None,
    measures: Sequence[dict[str, Any]] | None = None,
    lyrics_words: Sequence[LyricWord] | None = None,
    use_stored_lyrics: bool = True,
    continuation: str = "auto",
    extra_tracks: Sequence[tuple[str, Sequence[Sequence[float]]]] | None = None,
    score: AbcScore | None = None,
) -> dict[str, Any]:
    """由 ABC 文本（或一份已定位好的乐谱）重建 ``export/`` 下的 SVP 与 MIDI。

    Args:
        score: **已经带秒级时间**的乐谱（卷帘路径，见 :func:`score_from_roll`）。
            给了它就不再解析 ``abc_text``、也不再跑 ``notes_to_seconds`` ——
            那些音符的 ``start`` 已经是秒，再换算一次会把时间算错。
            此时歌词按 ``AbcNote.lyric`` **逐音**取用（``lyrics_per_note``），
            不再走 LRC/占位词分配。

    歌词处理（ABC 路径）
    --------------------
    歌词以**带时间戳的词**（``lyrics.json``）存储，在导出时才对**当前音符**做分配。
    这样音符集合变化后歌词不会错位——分配永远基于本次真实的音符。
    """
    out = Path(out_dir)
    export_dir = out / EXPORT_DIRNAME
    export_dir.mkdir(parents=True, exist_ok=True)
    project = project_name or out.name

    # 「导出哪些内容」完全由用户决定（③ 的人声主旋律/器乐旋律/和弦勾选）
    sel = normalize_export_voices(export_voices, only_melody)
    # 和弦轨来自 chords.mid，**不是 ABC 声部**，所以「只勾和弦」时乐谱会是空的，
    # 但导出仍然要能成立 —— 提前取好，别让下面「没有音符」的早返回拦掉。
    chord_notes = load_chord_notes(out) if "chords" in sel else []
    table = list(measures) if measures is not None else load_measures(out)

    pre_timed = score is not None
    if score is None:
        score = parse_abc(abc_text or "")
    if not score.notes and not chord_notes:
        return {"ok": False, "error": "乐谱中没有解析到任何音符", "exports": []}

    tempo = float(bpm or score.bpm or 120.0)
    # 速度表：变速曲（如 110→75→110）必须靠它才能让 SVP/MIDI 的小节网格正确。
    # 原料就是那份小节表；单速度时会退化成一条，行为与旧的单一 bpm 一致。
    tempo_map = derive_tempo_map(table) if table else []
    if not tempo_map:
        tempo_map = [{"t": 0.0, "bpm": tempo}]
    warnings: list[str] = []
    if not pre_timed:
        warnings = list(notes_to_seconds(score.notes, bpm=tempo, measures=table))

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

    # ---- 歌词：卷帘路径逐音自带；ABC 路径按识别结果分配，否则统一占位 ----
    words = lyrics_words
    if words is None and use_stored_lyrics:
        words = load_lyrics_words(out)
    lyric_stats: dict[str, Any] = {}
    lyrics_source = "per_note" if pre_timed else ("recognized" if words else "fallback")

    for voice in voices:
        objs = to_simple_note_objs(score.notes, voice=voice)
        if not objs:
            continue
        simple = [(n.start, n.start + n.duration_seconds, n.pitch) for n in objs]
        display = _display_name(voice)
        if pre_timed or getattr(score, "lyrics_per_note", False):
            # 卷帘路径：歌词是逐音录入的，**直接用**。
            # 不能落到 assign_lyrics：那是按时间窗对齐的，会把用户逐音填好的词冲掉。
            note_lyrics = [(o.lyric if o.lyric else lyrics) for o in objs]
            lyric_stats = {
                "source": "per_note",
                "notes": len(objs),
                "filled": sum(1 for o in objs if o.lyric),
            }
        elif words:
            note_lyrics, lyric_stats = assign_lyrics(
                words, [(s, e) for s, e, _ in simple], fallback=lyrics, continuation=continuation
            )
        else:
            note_lyrics = [lyrics] * len(objs)
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

    # 不再产出 ABC 文件：ABC 只是模型的输出格式与内部导入格式，用户侧的可编辑真相是
    # roll.json（见 save_roll），③ 也不再提供 ABC 下载。留着 export/<歌名>.abc 只会
    # 让用户以为它是个可交付产物。
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
