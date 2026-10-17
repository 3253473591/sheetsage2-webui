"""扒谱音高的后期处理：**先校八度、再整体降一个八度**。

模型（SheetSage2）的音高有两个毛病，一个全局、一个局部：

| 毛病 | 表现 | 这一层怎么办 |
|---|---|---|
| **整体偏高一个八度** | `melody_vocal.mid` 中位 69~81，最高到 95（B6）——人声到不了 | 整条旋律 `−12`（`semitones`，默认值，面板上不暴露） |
| **个别段落低一个八度** | 同一段副歌三次出现给 66 / 54 / 66（`samples/对齐`）| `octave.py`：重复段必须同高，逐段拉齐 |

第二个是拿到 `samples/对齐` 那对 SVP 之后才发现的：`−12` 是**整体**平移，
动不了"只有某一段低一个八度"。证据与算法见 `octave.py` 的模块头与
`docs/八度校正规格.md`。

**为什么只动旋律、不动和弦**（实测，见 `docs/WebUI问题清单.md` §1）：

| 产物 | 音域 | 判断 |
|---|---|---|
| `chords.mid` | 低音 36~47（C2~B2），整体 36~69 | **正常**：键盘/贝斯的常规音域 |
| `melody_vocal.mid` | 中位 69~81，最高到 95（B6）/ 90（F#6） | **不正常**：人声到不了 |

同一份产物里和弦是对的、旋律高得离谱（而且男女声的中位不分层），所以偏移只可能出在
"旋律"这一路上——和弦一根手指都不动。

**覆盖的产物**（都是"谱"的一部分，用户会拿去听/看/导）：

- `melody_full.lab` / `melody_vocal.lab` / `melody_instrumental.lab` —— pitch 列；
- `melody.mid` / `melody_vocal.mid` / `melody_instrumental.mid` —— 全部音符；
- `notation/song_melody.mid` —— ABC 的旋律来源，改完**重建 `score.abc`**；
- `transcription.mid` —— 只动 Vocal / Ins 轨，Chords 轨保持；
- `playback.json` —— 只动 Vocal / Ins 轨（WebUI 不用它，但它在下载包里）；
- `score.abc` —— 用模型自带的 `generate_abc_from_exports()` 从 `notation/` 重建。
  实测**能从原始 `notation/` 逐字节复现模型写的 `score.abc`**（2525B 完全相等），
  所以"降八度后重建"是忠实的，不会顺手改别的。

**不动的**：`chords.mid` / `notation/song_chords.txt`、`events.*` / `tokens.*` / `result.json`
（原始模型输出，要留着能复查）、`beat.lab` / `structure.lab` / `key.lab`（不含音高）。

旋律内部也只动音高：tick / 秒 / 力度 / 轨名 / 拍号 / 速度一个都不碰。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .bootstrap import PROJECT_ROOT
from .octave import OctavePlan, OctaveStats
from .octave import plan_from_labs as _octave_plan_from_labs
from .octave import track_of_name as _track_of_name
from .octave import normalize_lab_text as _octave_lab_text

#: 一个八度。
OCTAVE = 12
#: 默认降一个八度（模型输出的旋律偏高一个八度）。
DEFAULT_SEMITONES = -OCTAVE

#: 模型返回的 labs 键 / 磁盘文件名（两边同名，只有后缀不同）。
MELODY_LABS: tuple[str, ...] = ("melody_full", "melody_vocal", "melody_instrumental")
#: 只含旋律的 MIDI：里面所有轨都是旋律。
MELODY_MIDIS: tuple[str, ...] = (
    "melody.mid", "melody_vocal.mid", "melody_instrumental.mid",
    "notation/song_melody.mid",
)
#: 混合 MIDI：含 Chords 轨，按轨名过滤。
MIXED_MIDIS: tuple[str, ...] = ("transcription.mid",)
#: 轨名里带这个（不区分大小写）就不动它。
CHORD_TRACK_HINT = "chord"

_ABC_MODULE = "notation_sheetsage2"


@dataclass
class PitchShiftStats:
    """一次音高后期处理的汇总（进 `project.json` / `report.json`）。"""

    enabled: bool = False
    semitones: int = 0
    labs: dict[str, int] = field(default_factory=dict)      # 文件名 -> 改了几个音
    midis: dict[str, int] = field(default_factory=dict)
    playback_notes: int = 0
    abc: str = "not_attempted"
    warnings: list[str] = field(default_factory=list)
    #: 八度一致性校正的统计（`octave.OctaveStats.as_dict()`）。
    #: 它和 `semitones` 是两件事：`semitones` 是**整体**平移，
    #: 这里是**重复段之间**的局部八度校正。
    octave: dict[str, Any] = field(default_factory=dict)

    @property
    def notes(self) -> int:
        return sum(self.labs.values()) or sum(self.midis.values())

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "enabled": self.enabled,
            "semitones": self.semitones,
            "labs": dict(self.labs),
            "midis": dict(self.midis),
            "playback_notes": self.playback_notes,
            "abc": self.abc,
            "warnings": list(self.warnings),
        }
        if self.octave:
            data["octave"] = dict(self.octave)
        if not self.enabled:
            data["reason"] = ("关闭了整体平移（八度一致性校正照常做）"
                              if self.octave.get("moved_notes")
                              else "关闭了音高修正")
        return data


# --------------------------------------------------------------------- lab
def shift_lab_text(text: str, semitones: int) -> tuple[str, int]:
    """把 lab 文本第 3 列（pitch）整体平移，返回 `(新文本, 改了几个音)`。

    列数不固定：`melody_full.lab` 是 `start end pitch track`，`melody_vocal.lab` 是
    `start end pitch`。分隔符（tab / 空格）原样保留，只换第 3 列的数。
    """
    out: list[str] = []
    moved = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        use_tab = "\t" in line
        fields = stripped.split("\t") if use_tab else stripped.split()
        if len(fields) < 3:
            out.append(line)
            continue
        try:
            pitch = int(round(float(fields[2])))
        except ValueError:
            out.append(line)
            continue
        fields[2] = str(pitch + semitones)
        moved += 1
        out.append("\t".join(fields) if use_tab else " ".join(fields))
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), moved


def _shift_lab_file(path: Path, semitones: int) -> int:
    text, moved = shift_lab_text(path.read_text(encoding="utf-8", errors="replace"),
                                 semitones)
    path.write_text(text, encoding="utf-8")
    return moved


# --------------------------------------------------------------------- MIDI
def _is_chord_track(name: str) -> bool:
    return CHORD_TRACK_HINT in str(name or "").lower()


def shift_midi_file(path: Path, semitones: int, *,
                    skip_chord_tracks: bool = False,
                    plan: OctavePlan | None = None) -> tuple[int, list[str]]:
    """平移一个 MIDI 里的音高，返回 `(改了几个音, 没动的和弦轨名)`。

    `skip_chord_tracks=True` 时（混合 MIDI）轨名带 "chord" 的整轨跳过。
    `plan`（八度一致性校正）在场时，先按轨名 + 起始秒查这一份局部平移，
    再加上整体 `semitones`——两条路都在这里落地，MIDI 不会和 lab 走散。
    """
    import pretty_midi

    midi = pretty_midi.PrettyMIDI(str(path))
    moved = 0
    kept: list[str] = []
    for instrument in midi.instruments:
        if skip_chord_tracks and _is_chord_track(instrument.name):
            kept.append(instrument.name or "?")
            continue
        track = _track_of_name(instrument.name)
        for note in instrument.notes:
            local = plan.shift_for(float(note.start), track) if plan else 0
            note.pitch = max(0, min(127, int(note.pitch) + semitones + local))
            moved += 1
    midi.write(str(path))
    return moved, kept


# --------------------------------------------------------------------- playback
def shift_playback(payload: Mapping[str, Any], semitones: int, *,
                   plan: OctavePlan | None = None) -> tuple[dict[str, Any], int]:
    """`playback.json`：只动非和弦轨的音高，小节表 / 速度 / 时长原样。"""
    data = json.loads(json.dumps(payload))           # 深拷贝，不动调用方的对象
    moved = 0
    for track in data.get("tracks") or []:
        if _is_chord_track(track.get("name")):
            continue
        index = _track_of_name(track.get("name"))
        for note in track.get("notes") or []:
            if "pitch" not in note:
                continue
            local = 0
            if plan and "start" in note:
                local = plan.shift_for(float(note["start"]), index)
            note["pitch"] = max(0, min(127, int(note["pitch"]) + semitones + local))
            moved += 1
    return data, moved


# --------------------------------------------------------------------- ABC
def _abc_builder():
    """拿到模型自带的 ABC 生成器（`models/SheetSage2/notation_sheetsage2.py`）。"""
    models = PROJECT_ROOT / "models"
    if not models.is_dir():
        return None
    for candidate in sorted(models.iterdir()):
        if (candidate / f"{_ABC_MODULE}.py").is_file() and candidate.name.isidentifier():
            if str(models) not in sys.path:
                sys.path.insert(0, str(models))
            module = __import__(f"{candidate.name}.{_ABC_MODULE}", fromlist=["*"])
            return module
    return None


def rebuild_abc(out_dir: Path) -> tuple[str | None, str | None]:
    """从 `notation/` 重建 `score.abc`，返回 `(文本 或 None, 错误说明)`。

    模型写 `score.abc` 走的就是这条路（`exports_sheetsage2` → `generate_abc_from_data`），
    实测能从同一份 `notation/` 逐字节复现，所以重建是忠实的。
    """
    melody = out_dir / "notation" / "song_melody.mid"
    if not melody.is_file():
        return None, "没有 notation/song_melody.mid"
    module = _abc_builder()
    if module is None:
        return None, "找不到模型的 notation_sheetsage2（models/ 不在？）"
    try:
        text, _score, _paths = module.generate_abc_from_exports(
            melody, output_path=out_dir / "score.abc")
    except Exception as exc:                          # 重建失败不该毁掉整条流水线
        return None, f"{type(exc).__name__}: {exc}"
    return text, None


# --------------------------------------------------------------------- 入口
def apply(result: Mapping[str, Any], out_dir: str | Path, *,
          semitones: int = DEFAULT_SEMITONES,
          octave_fix: bool = True) -> tuple[dict[str, Any], PitchShiftStats]:
    """把一次转写结果里的旋律音高改对，**同时改内存里的 result 和磁盘上的产物**。

    两件事，一起做完：

    1. **八度一致性校正**（`octave.py`，`octave_fix=True` 时开）：模型对同一段
       旋律的不同出现可能给不同的八度（`samples/对齐` 那对 SVP 实证 20% 的音），
       这一步按"重复段必须同高"把它们拉齐；
    2. **整体平移** `semitones`（默认 −12）：模型的旋律整体偏高一个八度。

    顺序是"先局部、后整体"——两者都是整八度，可交换，但先局部能让日志说清
   "哪几个音是重复段校正动的"。

    返回新的 `result`（`labs` / `playback` / `abc` 已同步）与统计。
    """
    out = Path(out_dir)
    stats = PitchShiftStats(enabled=bool(semitones), semitones=int(semitones))
    updated = dict(result)

    # labs：内存 + 磁盘兜底（模型可能没把文本放进 result）
    labs = {name: text for name, text in (updated.get("labs") or {}).items()}
    for name in MELODY_LABS:
        if name not in labs:
            path = out / f"{name}.lab"
            if path.is_file():
                labs[name] = path.read_text(encoding="utf-8", errors="replace")

    plan = OctavePlan()
    octave_stats = OctaveStats(enabled=False)
    if octave_fix:
        plan = _octave_plan_from_labs(labs)
        octave_stats = plan.stats
        stats.warnings.extend(octave_stats.warnings)
    stats.octave = octave_stats.as_dict()

    if not semitones and plan.empty:
        return updated, stats

    # labs：局部（八度）→ 整体（semitones）
    for name in MELODY_LABS:
        text = labs.get(name)
        if text is None:
            continue
        track = {"melody_vocal": 0, "melody_instrumental": 1}.get(name)
        shifted = text
        local = 0
        if not plan.empty:
            shifted, local = _octave_lab_text(shifted, plan, track=track)
        shifted, moved = shift_lab_text(shifted, semitones)
        labs[name] = shifted
        stats.labs[f"{name}.lab"] = moved + local
        path = out / f"{name}.lab"
        if path.is_file():                            # 模型没写过就别凭空造产物
            path.write_text(shifted, encoding="utf-8")
    updated["labs"] = labs

    # MIDI：只含旋律的直接平移；混合 MIDI 跳过和弦轨
    for name in MELODY_MIDIS:
        path = out / name
        if path.is_file():
            moved, _ = shift_midi_file(path, semitones, plan=plan)
            stats.midis[name] = moved
    for name in MIXED_MIDIS:
        path = out / name
        if path.is_file():
            moved, kept = shift_midi_file(path, semitones, skip_chord_tracks=True,
                                          plan=plan)
            stats.midis[name] = moved
            if kept:
                stats.warnings.append(f"{name}：和弦轨 {'/'.join(kept)} 保持原音高")

    # playback.json：只动非和弦轨
    playback_path = out / "playback.json"
    playback = updated.get("playback")
    if not isinstance(playback, Mapping) and playback_path.is_file():
        try:
            raw = json.loads(playback_path.read_text(encoding="utf-8"))
        except ValueError:
            raw = None
        playback = raw if isinstance(raw, Mapping) else None
    if isinstance(playback, Mapping):
        shifted_playback, moved = shift_playback(playback, semitones, plan=plan)
        updated["playback"] = shifted_playback
        stats.playback_notes = moved
        if playback_path.is_file():
            playback_path.write_text(
                json.dumps(shifted_playback, indent=2), encoding="utf-8")

    # score.abc：旋律 MIDI 已经平移过，这里重建
    abc_text, error = rebuild_abc(out)
    if abc_text is not None:
        (out / "score.abc").write_text(abc_text, encoding="utf-8")
        updated["abc"] = abc_text
        stats.abc = "rebuilt"
    else:
        stats.abc = f"skipped: {error}"
        stats.warnings.append(f"score.abc 没有重建（{error}），它还是原音高")
    return updated, stats


def warnings_for(stats: PitchShiftStats) -> list[str]:
    """给报告用的白话告警（跟量化告警一个风格）。"""
    messages: list[str] = []
    if stats.enabled:
        total = stats.notes or stats.playback_notes
        octaves = (f"（{abs(stats.semitones) // OCTAVE} 个八度）"
                   if stats.semitones % OCTAVE == 0 else "")
        messages.append(
            f"音高修正：旋律（人声 + 器乐）整体 {stats.semitones:+d} 半音{octaves}，"
            f"影响 {total} 个音；和弦轨不动（实测和弦本来就在正确音域）")
    elif stats.octave.get("moved_notes"):
        messages.append("音高修正：整体平移关掉了，但八度一致性校正照做")
    messages.extend(stats.warnings)
    return messages
