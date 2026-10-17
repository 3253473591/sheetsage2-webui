"""SVP 导出：把 tick 域工程编成 Synthesizer V Studio 工程（`.svp`）。

**为什么用 LibreSVIP 而不是自己写 `.svp`**：见 `docs/SVP导出调研.md`。
这个模块只负责"我们的 `project.json` → libresvip 的通用模型 → `.svp`"这一段，
`.svp` 的 JSON 结构、版本差异、phont 表这些都不归我们管。

三组内容（下载时勾选，默认**只勾人声**）：

| key | 来源 | 轨数 |
|---|---|---|
| `vocal` | `project.json` 的 `tracks[vocal]`，带填词的 `lyric` | 1 |
| `instrumental` | `project.json` 的 `tracks[instrumental]`（没有歌词 → 用默认音节） | 1 |
| `chords` | `chords.mid` 的和弦音；**SVP 一轨只能有一个音**，按同时发声拆开 | N（实测 4） |

**时间轴的三个坑（都实测过，别改坏）**：

1. **tick 分辨率**：我们 `PPQ=1920`，LibreSVIP 基准 `TICKS_IN_BEAT=480` → 全部 **÷4**。
   量化网格是一拍的 1/96（20 tick）→ 5，整除，没有精度损失。
2. **`time_signature_list[0]` 是"前置小节（bar 0）"**：`synthv_generator` 里
   `first_bar_tick = time_signature_list[0].bar_length()`，导出时拍号整体 −1 小节、
   速度整体 −first_bar_tick。我们的第一小节常常是**弱起**，正好当这个 bar 0；
   它自己的那个 tempo 会被丢掉（可接受：一小节太短，估出来的 BPM 本来就不稳）。
   实测《光芒》86 小节 / 324 个音：**音符位置逐一相等**，变速除弱起那一个外全部保住。
3. **拍号只写变化点**：`time.meter` 是"从这里开始生效"，逐小节抄进去会让 SynthV 每个小节
   都标一次拍号。见 `_meter_list()`。
4. **速度同理**：`time.tempo` 也是"从这里开始生效"。恒速曲写**一条**（逐小节那张精确表是
   同一个速度加网格抖动），变速曲保留逐小节的变化点。见 `_tempo_list()`。

libresvip 是**可选依赖**（`pip install -e .[svp]`），所以这里**延迟 import**：
没装的时候 WebUI 其余功能照常，只在导出时报一句人话。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .timeline import Timeline, from_stored_measures

#: 我们能导出的三组内容（顺序就是界面上勾选框的顺序）。
SVP_PARTS: tuple[str, ...] = ("vocal", "instrumental", "chords")
#: 默认只勾人声。
DEFAULT_PARTS: tuple[str, ...] = ("vocal",)
#: 目标 SVP 版本（最兼容：1.9 及以下）。见 `docs/SVP导出调研.md` §1.5。
SVP_VERSION = "1.9"
#: 编码逻辑的版本：只要 `.svp` 的内容算法变了就 +1，用来让 `export/` 里的老缓存失效。
#: 2 = 拍号只写变化点（之前是逐小节抄，SynthV 会在每个小节上面都标一次拍号）。
#: 3 = 速度：恒速曲只写一条，变速曲折叠相同的相邻点（之前同样逐小节抄）。
#: 4 = 音符换算改成"起点/终点各自换算 + 同轨不叠音"（之前分开 round 会叠 1 tick）。
EXPORT_REVISION = 5
#: 折叠速度点时，"两个 BPM 算同一个速度"的相对容差（0.5%，比网格抖动略大）。
TEMPO_MERGE_TOLERANCE = 0.005
PART_LABELS: dict[str, str] = {
    "vocal": "人声轨",
    "instrumental": "乐器轨",
    "chords": "和弦轨",
}

#: 我们 PPQ 与 libresvip 基准的比（1920 / 480）。
PPQ_SCALE = 4

#: 没歌词的音符用什么音节占位（SVP 的每个音都要求一个音节/字）。
#: **只在没得延音时才用**——见 `_note_objects` 与 `CONTINUATION_MAX_GAP`。
#: 取值和 libresvip 的 `DEFAULT_*_LYRIC` 对齐。
FILLER_LYRIC: dict[str, str] = {
    "Chinese": "啦", "Cantonese": "啦",
    "Japanese": "ラ",
}
FILLER_DEFAULT = "la"

#: 没分到字的音，与前面那个音之间还允许算**同一个字的延音**的最大间隙（SV 域 tick）。
#:
#: 依据是 `samples/对齐/` 那对 SVP：人工修过的工程里 55 个延音标记
#: **全部与前一个音无缝相接**，而且**全文一个占位音节都没有**——
#: 人工的做法是"没字就当上一个字的延音，从不发明一个『啦』"。
#: 我们没分到字的音 57 个里有 54 个是无缝的，说明它们本来就是同一个字的延续，
#: 只是逐字时间戳没给到那里（SheetSage2 多扒 / 少扒音符时尤其明显）。
#:
#: 取**半个四分音符**（SV 域一拍 480 tick）：剩下那几个卡在 16 分 / 8 分休止后面
#: （实测 240 tick），比这更长的休止（半拍以上）多是一句的开头，那里没有字可续，
#: 只能退回占位音节。注意这只改**唱什么字**，音符起点 / 时值一个都不动，
#: 所以那个休止不会被吃掉。
CONTINUATION_MAX_GAP = 240

#: 我们的语言名 -> libresvip 的 `LanguageOption`（声库语言的默认值）。
LANGUAGE_TO_SV: dict[str, str] = {
    "Chinese": "mandarin", "Cantonese": "cantonese", "Japanese": "japanese",
    "English": "english", "Korean": "korean", "Spanish": "spanish",
    "German": "german", "French": "french", "Portuguese": "portuguese",
}


class SvpUnavailable(RuntimeError):
    """没装 libresvip（可选依赖）。API 把它翻成 501 + 安装提示。"""


def _require_libresvip() -> Any:
    try:
        from libresvip.model.base import (  # noqa: PLC0415
            Note, Project, SingingTrack, SongTempo, TimeSignature,
        )
        from libresvip.plugins.svp.options import (  # noqa: PLC0415
            LanguageOption, SVProjectVersionCompatibility, VibratoOption,
        )
        from libresvip.plugins.svp.synthv_studio_converter import (  # noqa: PLC0415
            SynthVStudioConverter,
        )
    except ImportError as exc:                       # pragma: no cover - 依赖是否装
        raise SvpUnavailable(
            "导出 SVP 需要 libresvip：`pip install -e .[svp]`（或 `pip install libresvip`）"
        ) from exc
    return {
        "Note": Note, "Project": Project, "SingingTrack": SingingTrack,
        "SongTempo": SongTempo, "TimeSignature": TimeSignature,
        "LanguageOption": LanguageOption,
        "SVProjectVersionCompatibility": SVProjectVersionCompatibility,
        "VibratoOption": VibratoOption,
        "SynthVStudioConverter": SynthVStudioConverter,
    }


# --------------------------------------------------------------------- 读工程
def load_project(out_dir: Path) -> dict[str, Any]:
    path = Path(out_dir) / "project.json"
    if not path.is_file():
        raise FileNotFoundError("还没有 project.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _timeline(project: Mapping[str, Any]) -> Timeline:
    measures = (project.get("timeline") or {}).get("measures") or []
    if not measures:
        raise ValueError("工程里没有小节表，无法换算时间")
    return from_stored_measures(measures)


def _track(project: Mapping[str, Any], track_id: str) -> list[dict[str, Any]]:
    for track in project.get("tracks") or []:
        if track.get("id") == track_id:
            return list(track.get("notes") or [])
    return []


def to_sv_tick(our_tick: float) -> int:
    """我们的 tick（PPQ 1920）→ libresvip 的 tick（480）。"""
    return round(float(our_tick) / PPQ_SCALE)


# --------------------------------------------------------------------- 和弦拆轨
def chord_voices(notes: Sequence[Mapping[str, Any]]) -> list[list[dict[str, Any]]]:
    """把和弦音按**同时发声**拆成一个音一轨。

    SVP 的一轨是单声部的，而 `chords.mid` 里每个和弦是 4 个同时发声的音
    （实测《光芒》125 个和弦 × 4 = 500 个音）。拆法：按起始时间分组，
    组内按音高从低到高，第 i 个音进第 i 轨——**低音永远在第 1 轨**，读起来最顺。

    同一条轨里如果前一个音还没结束下一个就来了（和弦持续到下一个和弦），
    就把前一个音截到后一个的起点，避免 SVP 里出现同轨叠音。
    """
    groups: dict[float, list[dict[str, Any]]] = {}
    for note in notes:
        groups.setdefault(round(float(note.get("start") or 0.0), 4), []).append(dict(note))
    voices: list[list[dict[str, Any]]] = []
    for start in sorted(groups):
        pitches = sorted(groups[start], key=lambda item: float(item.get("pitch") or 0))
        while len(voices) < len(pitches):
            voices.append([])
        for index, note in enumerate(pitches):
            voices[index].append(note)
    for voice in voices:
        voice.sort(key=lambda item: float(item.get("start") or 0.0))
        for earlier, later in zip(voice, voice[1:]):
            if float(earlier.get("end") or 0.0) > float(later.get("start") or 0.0):
                earlier["end"] = later["start"]
    return [voice for voice in voices if voice]


def chord_notes(out_dir: Path) -> list[dict[str, Any]]:
    """`chords.mid` → `[{start, end, pitch}]`（秒）。没有就返回空。"""
    path = Path(out_dir) / "chords.mid"
    if not path.is_file():
        return []
    import pretty_midi

    midi = pretty_midi.PrettyMIDI(str(path))
    out: list[dict[str, Any]] = []
    for instrument in midi.instruments:
        for note in instrument.notes:
            if note.end > note.start:
                out.append({"start": float(note.start), "end": float(note.end),
                            "pitch": int(note.pitch)})
    return out


# --------------------------------------------------------------------- 可勾选项
def available_parts(out_dir: Path, project: Mapping[str, Any]) -> list[dict[str, Any]]:
    """下载弹窗要显示的三行：有没有内容、多少音、会占几轨、默认勾不勾。"""
    out_dir = Path(out_dir)
    rows: list[dict[str, Any]] = []
    vocal = _track(project, "vocal")
    instrumental = _track(project, "instrumental")
    voices = chord_voices(chord_notes(out_dir))
    counts = {
        "vocal": (len(vocal), 1, sum(1 for note in vocal if note.get("lyric"))),
        "instrumental": (len(instrumental), 1, 0),
        "chords": (sum(len(voice) for voice in voices), len(voices), 0),
    }
    for key in SVP_PARTS:
        notes, tracks, with_lyric = counts[key]
        row: dict[str, Any] = {
            "key": key,
            "label": PART_LABELS[key],
            "notes": notes,
            "tracks": tracks,
            "default": key in DEFAULT_PARTS,
            "available": notes > 0,
        }
        if key == "vocal" and notes:
            row["detail"] = f"{with_lyric} 个音有歌词" if with_lyric else "还没识别歌词"
        elif key == "chords" and tracks > 1:
            row["detail"] = f"一轨只能有一个音，会拆成 {tracks} 轨"
        elif key == "instrumental" and notes:
            row["detail"] = "没有歌词，用默认音节占位"
        rows.append(row)
    return rows


# --------------------------------------------------------------------- 语言
def resolve_language(options: Mapping[str, Any], out_dir: Path) -> str:
    """工程选的语言；选的是"自动"就退回 `lyrics.json` 里模型判出来的那个。"""
    wanted = str(options.get("language") or "").strip()
    if wanted:
        return wanted
    path = Path(out_dir) / "lyrics.json"
    if path.is_file():
        try:
            used = str(json.loads(path.read_text(encoding="utf-8")).get("language_used") or "")
        except (OSError, ValueError):
            used = ""
        if used:
            return used
    return ""


def filler_lyric(language: str) -> str:
    return FILLER_LYRIC.get(language, FILLER_DEFAULT)


def sv_language(language: str) -> str:
    return LANGUAGE_TO_SV.get(language, "mandarin")


# --------------------------------------------------------------------- 编码
def _meter_list(timeline_block: Mapping[str, Any], api: Mapping[str, Any]) -> list[Any]:
    """拍号：第 0 条是**前置小节**（bar 0），真正的小节从 bar_index=1 起。

    我们的第一小节（弱起）就是那个 bar 0；它的长度由 `bar_length()` 决定
    `first_bar_tick`，正好等于弱起长度。

    **只写变化点，不是每个小节一条。** SVP 的 `time.meter` 是"从这里开始用这个拍号"，
    一直生效到下一条为止，所以整首 4/4 只需要一条。我们的 `timeline.measures` 是逐小节的
    （那才是秒 ↔ tick 的真源），照抄进去就会让 SynthV 在**每个小节上面都标一次 4/4**
    （实测《光芒》45s 版：21 条 → 文件里 20 条一模一样的 4/4），既糊住标尺又挡住改拍号。
    """
    measures = timeline_block.get("measures") or []
    TimeSignature = api["TimeSignature"]
    first = measures[0]
    meters = [TimeSignature(bar_index=0,
                            numerator=int(first.get("numerator") or 1),
                            denominator=int(first.get("denominator") or 8))]
    last = (meters[0].numerator, meters[0].denominator)
    for index, row in enumerate(measures[1:], start=1):
        numerator = int(row.get("numerator") or 4)
        denominator = int(row.get("denominator") or 4)
        if (numerator, denominator) == last:            # 和上一条一样 -> 不是变化点
            continue
        meters.append(TimeSignature(bar_index=index,
                                    numerator=numerator, denominator=denominator))
        last = (numerator, denominator)
    return meters


def _tempo_list(timeline_block: Mapping[str, Any], api: Mapping[str, Any]) -> list[Any]:
    """速度：位置用我们的 tick ÷4。

    **恒速曲只写一条**（`timeline.tempo_map` 只有一段时）。逐小节那张 `tempo_map_exact`
    在恒速曲上其实是**同一个速度加一点网格抖动**（《光芒》86 小节里只有 103.896 / 104.348
    两个值来回跳，外加弱起与末小节的碎片值 111.1 / 244.1），照抄进去 SV 会在**每个小节**
    上都标一次速度（实测 45s 版：文件里 20 条）。所以恒速曲写一条，值就是界面上报的那个 BPM。
    代价：整首播完与原曲差 0.4%~1.8%（实测三首恒速曲 1.09 / 1.28 / −3.34 秒）——
    逐小节的精确表仍在 `project.json` 里，需要严格对齐时用它。

    变速曲保留逐小节的精确表（那里的每个点都是真的变化），只把数值相同的相邻点折叠掉。
    """
    SongTempo = api["SongTempo"]
    measures = timeline_block.get("measures") or []
    tempo_map = timeline_block.get("tempo_map") or []

    if len(tempo_map) == 1:                          # 恒速：一条到底
        first = tempo_map[0]
        bpm = float(first.get("bpm") or 0.0)
        if bpm > 0:
            return [SongTempo(position=to_sv_tick(first.get("tick") or 0),
                              bpm=round(bpm, 4))]

    out: list[Any] = []
    seen: set[int] = set()
    last: float | None = None
    for row in measures:
        bpm = row.get("bpm")
        if not bpm:
            continue
        position = to_sv_tick(row.get("tick_start") or 0)
        if position in seen:
            continue
        seen.add(position)
        value = float(bpm)
        # 和上一条相差在 TEMPO_MERGE_TOLERANCE 以内就当没变（跟拍号折叠一个道理）
        if last is not None and abs(value - last) <= TEMPO_MERGE_TOLERANCE * last:
            continue
        out.append(SongTempo(position=position, bpm=round(value, 4)))
        last = value
    return out


def to_sv_window(raw: Mapping[str, Any]) -> tuple[int, int]:
    """一个音在我们 tick 域里的 `(起点, 终点)` → SV 域（**起点和终点各自换算**）。

    ⚠️ **不要**用 `to_sv_tick(tick)` + `to_sv_tick(dur)` 分开算：我们 PPQ=1920、SV 是 480，
    未量化的 tick 不是 4 的倍数，两次独立 `round()` 会让"本来正好头尾相接"的两个音在
    SV 域里叠 1 tick —— libresvip 的 `PitchSimulator` 会直接抛
    `NotesOverlappedError: Notes overlapped near bar N`（实测《旷古回响》bar 76：
    我们的 `576478+478 / 576956`，SV 域 `144120+120 = 144240` vs `144239`）。
    """
    start = to_sv_tick(raw.get("tick") or 0)
    end = to_sv_tick((raw.get("tick") or 0) + (raw.get("dur") or 0))
    return start, end


def _note_rows(notes: Iterable[Mapping[str, Any]]) -> tuple[list[tuple[int, int, Mapping]], dict]:
    """把音符排好、**保证 SV 域里一轨不叠音**，返回 `(行, 统计)`。

    规则（和和弦那条路一个道理，SVP 一轨是单声部的）：

    - 起点被上一个音的终点顶到后面 → **往后推**；
    - 终点越过下一个音的起点 → **截到下一个音的起点**；
    - 推完/截完之后没有长度了 → **丢掉**（SVP 摆不下两个同 tick 的音）。
    """
    rows: list[tuple[int, int, Mapping[str, Any]]] = []
    for raw in notes:
        start, end = to_sv_window(raw)
        if end <= start:
            continue
        rows.append((start, end, raw))
    rows.sort(key=lambda row: (row[0], row[1], int(row[2].get("pitch") or 0)))

    stats = {"dropped": 0, "shifted": 0, "trimmed": 0}
    out: list[tuple[int, int, Mapping[str, Any]]] = []
    cursor: int | None = None
    for index, (start, end, raw) in enumerate(rows):
        next_start = rows[index + 1][0] if index + 1 < len(rows) else None
        if cursor is not None and start < cursor:
            start = cursor
            stats["shifted"] += 1
        if next_start is not None and end > next_start:
            end = next_start
            stats["trimmed"] += 1
        if end <= start:                    # 被前后挤没了：宁可少一个音，也不能叠
            stats["dropped"] += 1
            continue
        out.append((start, end, raw))
        cursor = end
    return out, stats


def _note_objects(notes: Iterable[Mapping[str, Any]], *, language: str,
                  api: Mapping[str, Any],
                  stats: dict | None = None) -> list[Any]:
    """音符 → libresvip 的 `Note`（**顺手保证同轨不叠音**，见 `_note_rows`）。

    没分到字的音**不再一律填占位音节**，按"能不能续上一个字"分两种：

    - 与前面那个音之间没有像样的休止（间隙 ≤ `CONTINUATION_MAX_GAP`）→ 写延音 `-`。
      人工修过的工程里从来不发明音节，而占位『啦』会让声库唱出一串"啦啦啦"——
      那是导出制造的错误，不是识别出来的。**SheetSage2 多扒 / 少扒音符时主要靠这条兜。**
    - 中间隔着一个够长的休止（多半是下一句的开头，前面没有字可续）→ 只能退回占位音节。

    **占位音节自己也算锚点**：所以一句里连着几个没字的音，只有"前面真的没有字"
    的那一个会拿到占位音节，后面连续没字的都续成 `-`（而不是"啦啦啦"一串）。

    器乐轨一个字都没有：那种轨上每个音都"前面没有字"，所以照旧每个音一个占位音节，
    不会变成"整句只唱一个字"。
    """
    Note = api["Note"]
    filler = filler_lyric(language)
    rows, adjustments = _note_rows(notes)
    if stats is not None:
        for key, value in adjustments.items():
            stats[key] = stats.get(key, 0) + value
    # 这条轨本来有没有歌词：没有（器乐轨 / 纯和弦）就别拿占位音节当锚点
    track_has_chars = any(
        str(raw.get("lyric") or "").strip() not in ("", "-", "+")
        for _start, _end, raw in rows)
    out: list[Any] = []
    head_alive = False      # 前面有字可续，而且从它到现在没断过
    prev_end: int | None = None
    continued = filled = 0
    for start, end, raw in rows:
        lyric = str(raw.get("lyric") or "").strip()
        # 转音/延续标记：SVP 里延续就是 "-"，英文的 "+" 也统一成 "-"
        if lyric == "+":
            lyric = "-"
        if head_alive and (prev_end is None or start - prev_end > CONTINUATION_MAX_GAP):
            head_alive = False          # 链条断了：休止之后是新的开始
        if not lyric:
            if head_alive:
                lyric = "-"             # 没字可挂：当上一个字的延音
                continued += 1
            else:
                lyric = filler          # 前面没有字：只能占位
                filled += 1
                head_alive = track_has_chars    # 占位音节自己也能当锚点
        elif lyric != "-":
            head_alive = True
        out.append(Note(start_pos=start, length=end - start,
                        key_number=int(raw.get("pitch") or 60),
                        lyric=lyric))
        prev_end = end
    if stats is not None:
        stats["continued"] = continued
        stats["filler"] = filled
    return out


def _chord_note_objects(voice: Sequence[Mapping[str, Any]], timeline: Timeline, *,
                        language: str, api: Mapping[str, Any]) -> list[Any]:
    Note = api["Note"]
    filler = filler_lyric(language)
    out: list[Any] = []
    for raw in voice:
        start = to_sv_tick(timeline.sec_to_tick(float(raw.get("start") or 0.0)))
        end = to_sv_tick(timeline.sec_to_tick(float(raw.get("end") or 0.0)))
        if end <= start:
            continue
        out.append(Note(start_pos=start, length=end - start,
                        key_number=int(raw.get("pitch") or 60), lyric=filler))
    return out


def build_svp(
    out_dir: str | Path,
    *,
    parts: Sequence[str] = DEFAULT_PARTS,
    options: Mapping[str, Any] | None = None,
    version: str = "1.9",
) -> tuple[bytes, dict[str, Any]]:
    """编一个 `.svp`，返回 `(文件字节, 统计)`。

    `parts` 是 `SVP_PARTS` 的子集；`options` 是工程的 `job.options`
    （用里面的 `language`）。
    """
    api = _require_libresvip()
    out_dir = Path(out_dir)
    project = load_project(out_dir)
    timeline = _timeline(project)
    timeline_block = project.get("timeline") or {}
    options = dict(options or {})
    language = resolve_language(options, out_dir)

    wanted = [key for key in SVP_PARTS if key in set(parts)]
    tracks: list[Any] = []
    stats: dict[str, Any] = {"parts": wanted, "language": language or "自动",
                             "tracks": [], "notes": 0, "adjust": {}}
    adjust: dict[str, int] = stats["adjust"]

    if "vocal" in wanted:
        notes = _note_objects(_track(project, "vocal"), language=language, api=api,
                              stats=adjust)
        if notes:
            tracks.append(api["SingingTrack"](title="人声", ai_singer_name="",
                                              note_list=notes))
            stats["tracks"].append({"key": "vocal", "title": "人声", "notes": len(notes)})
            stats["notes"] += len(notes)

    if "instrumental" in wanted:
        notes = _note_objects(_track(project, "instrumental"), language=language, api=api,
                              stats=adjust)
        if notes:
            tracks.append(api["SingingTrack"](title="乐器", ai_singer_name="",
                                              note_list=notes))
            stats["tracks"].append({"key": "instrumental", "title": "乐器",
                                    "notes": len(notes)})
            stats["notes"] += len(notes)

    if "chords" in wanted:
        voices = chord_voices(chord_notes(out_dir))
        for index, voice in enumerate(voices, start=1):
            notes = _chord_note_objects(voice, timeline, language=language, api=api)
            if not notes:
                continue
            # 第 1 轨是低音（`chord_voices` 按音高从低到高分配）
            title = f"和弦 {index}" + ("（低音）" if index == 1 else "")
            tracks.append(api["SingingTrack"](title=title, ai_singer_name="",
                                              note_list=notes))
            stats["tracks"].append({"key": "chords", "title": title, "notes": len(notes)})
            stats["notes"] += len(notes)

    if not tracks:
        raise ValueError("选中的内容里没有音符，导不出 SVP")

    sv_project = api["Project"](
        version="",
        song_tempo_list=_tempo_list(timeline_block, api),
        time_signature_list=_meter_list(timeline_block, api),
        track_list=tracks,
    )
    language_option = api["LanguageOption"]
    compatibility = api["SVProjectVersionCompatibility"]
    vibrato = api["VibratoOption"]
    version_compatibility = {
        "1.9": compatibility.BELOW_1_9_0,
        "1.11": compatibility.BETWEEN_1_10_0_AND_1_11_2,
        "2.0": compatibility.ABOVE_2_0_0,
    }.get(str(version), compatibility.BELOW_1_9_0)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="sheetsage-svp-") as work:
        target = Path(work) / "export.svp"
        api["SynthVStudioConverter"].dump(target, sv_project, {
            "version_compatibility": version_compatibility,
            "vibrato": vibrato.NONE,
            "down_sample": 20,
            "language_override": language_option(sv_language(language)),
        })
        payload = target.read_bytes()

    stats["bytes"] = len(payload)
    stats["version"] = str(version)
    if any(adjust.values()):
        stats["warnings"] = [
            "为了让 SVP 里同一条轨不叠音，导的时候挪了/截了/丢了几个音："
            + "、".join(f"{label} {adjust[key]} 个"
                        for key, label in (("shifted", "往后推"), ("trimmed", "截短"),
                                           ("dropped", "丢弃"))
                        if adjust.get(key))
        ]
    return payload, stats


def cache_key(parts: Sequence[str], version: str, options: Mapping[str, Any]) -> str:
    """同一个选择复用同一份 `.svp`（文件落在工程根目录的 `export/` 下）。

    键里带 `EXPORT_REVISION`：改了编码逻辑（比如"拍号只写变化点"）就让老缓存作废，
    不然已经导出过的工程会一直发旧文件。
    """
    raw = json.dumps({"parts": list(parts), "version": version, "rev": EXPORT_REVISION,
                      "language": options.get("language") or ""},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def export_path(job_root: Path, key: str) -> Path:
    target = Path(job_root) / "export"
    target.mkdir(parents=True, exist_ok=True)
    return target / f"{key}.svp"
