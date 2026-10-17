"""一键流水线：丢一首音频进去，拿一份可以下载的产物出来。

这是给 WebUI 用的入口。设计前提是**用户不调任何参数**，所以所有判断都必须自动完成、
并且把理由写进报告：

| 决策 | 自动来源 |
|---|---|
| 拍号 | `tempo.estimate_meter()`，取最长的拍号段 |
| 弱起 | 开头那个短小节 → `pickup_seconds` |
| 主速度 | `tempo.reconcile()`：恒速曲用网格拟合，变速曲用模型 Q 值 |
| 是否恒速 | `estimate_tempo().is_constant` + `tempo_range` |
| 调性 | `key.lab` 分段（模型给的，本来就无需用户输入） |
| 秒→tick | `timeline.from_playback()`，与 BPM 无关 |

`bpm` / `pickup` 这类参数保留为**可选覆盖**，UI 不暴露。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .engine import EngineConfig, SheetSage2Engine
from .fsutil import write_text_atomic
from .pitch import DEFAULT_SEMITONES as PITCH_SEMITONES
from .pitch import apply as apply_pitch_shift, warnings_for as pitch_warnings
from .quantize import (
    QuantizeStats,
    note_shift_seconds,
    quantize_tracks,
)
from .result import (EXPECTED_MIDI, EXPECTED_TEXT, beat_times_from_labs, parse_lab_text,
                     summarize_result)
from .sections import canonical_label, merge_consecutive
from .tempo import (
    abc_meter,
    abc_tempo,
    estimate_meter,
    estimate_tempo,
    reconcile,
    sectional_tempo,
    segments_to_tempo_map,
    suggest_grid_recovery,
)
from .timeline import Timeline, from_beats, from_playback, notes_to_ticks, round_trip_error

ProgressFn = Callable[[Mapping[str, Any]], None]
PPQ = 1920


@dataclass
class Decisions:
    """自动裁决的结果与理由，直接进报告。"""

    meter: str | None = None
    pickup_seconds: float = 0.0
    bpm: float | None = None
    bpm_source: str | None = None
    bpm_confident: bool = True
    tempo_constant: bool = False
    tempo_range: dict[str, Any] | None = None
    timeline_source: str = "playback"
    keys: list[str] = field(default_factory=list)
    notes: int = 0
    round_trip_error: float = 0.0
    tempo_flattened: bool = False
    flatten_max_drift: float | None = None
    flatten_mean_drift: float | None = None
    tempo_mode: str = "section"                # section | measure | single
    tempo_segments: int = 0
    tempo_segments_detail: list[dict[str, Any]] = field(default_factory=list)
    halftime_hint: float | None = None         # 速度偏低时给出 ×2 的候选值
    stable_tempos: list[float] = field(default_factory=list)  # 去掉过渡段后的速度序列
    quantized: bool = False                    # 音符时值是否做过网格量化
    simplicity: float | None = None            # 量化力度滑杆（0~5）
    quantize_moved: int = 0                    # 被搬动的音符数
    quantize_moved_ratio: float = 0.0
    quantize_failed_tracks: int = 0
    quantize_grid_ticks: int | None = None     # 网格步长（项目 tick）
    quantize_max_shift_seconds: float | None = None
    pitch_semitones: int = 0                   # 旋律的音高后期处理（0 = 原样）

    def as_dict(self) -> dict[str, Any]:
        return dict(meter=self.meter, pickup_seconds=self.pickup_seconds, bpm=self.bpm,
                    bpm_source=self.bpm_source, bpm_confident=self.bpm_confident,
                    tempo_constant=self.tempo_constant, tempo_range=self.tempo_range,
                    timeline_source=self.timeline_source, keys=self.keys,
                    notes=self.notes, round_trip_error=self.round_trip_error,
                    tempo_mode=self.tempo_mode, tempo_segments=self.tempo_segments,
                    tempo_segments_detail=self.tempo_segments_detail,
                    stable_tempos=self.stable_tempos,
                    halftime_hint=self.halftime_hint,
                    tempo_flattened=self.tempo_flattened,
                    flatten_max_drift=self.flatten_max_drift,
                    flatten_mean_drift=self.flatten_mean_drift,
                    quantized=self.quantized, simplicity=self.simplicity,
                    quantize_moved=self.quantize_moved,
                    quantize_moved_ratio=self.quantize_moved_ratio,
                    quantize_failed_tracks=self.quantize_failed_tracks,
                    quantize_grid_ticks=self.quantize_grid_ticks,
                    quantize_max_shift_seconds=self.quantize_max_shift_seconds,
                    pitch_semitones=self.pitch_semitones)


# --------------------------------------------------------------------- 解析
def _rows(text: str | None) -> list[list[str]]:
    return parse_lab_text(text or "")


def _intervals(text: str | None, timeline: Timeline) -> list[dict[str, Any]]:
    """把 `chord.lab` / `key.lab` / `structure.lab` 的区间搬进 tick 域。"""
    result: list[dict[str, Any]] = []
    for row in _rows(text):
        if len(row) < 3:
            continue
        try:
            start, end = float(row[0]), float(row[1])
        except ValueError:
            continue
        if end <= start:
            continue
        tick = int(round(timeline.sec_to_tick(start)))
        end_tick = int(round(timeline.sec_to_tick(end)))
        result.append(dict(tick=tick, dur=max(1, end_tick - tick), label=row[2]))
    return result


def _merge_sections(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """段落：标签翻成中文 + 相邻同名合并（tick 域）。

    只动 `project.json` 的段落表，**不动** `structure.lab`（模型原始产物，自洽性检查逐行比），
    也**不动**速度拟合用的 `structure_seconds`——速度表的输入保持原样，
    否则会悄悄改掉已经验证过的速度结构（见 `docs/BPM算法规格.md`）。
    """
    spans = [(float(row["tick"]), float(row["tick"]) + float(row["dur"]),
              canonical_label(row.get("label"))) for row in rows]
    return [{"tick": int(start), "dur": max(1, int(end - start)), "label": label or ""}
            for start, end, label in merge_consecutive(spans)]


def _parse_key(label: str) -> dict[str, str]:
    """`F:major` → ``{"tonic": "F", "mode": "major"}``；`D:min` 归一成 minor。"""
    tonic, _, mode = label.partition(":")
    mode = mode or "major"
    if mode.startswith("min"):
        mode = "minor"
    elif mode.startswith("maj"):
        mode = "major"
    return {"tonic": tonic.strip() or "C", "mode": mode}


# --------------------------------------------------------------------- 主流程
def run(
    audio: str | Path,
    output_dir: str | Path,
    *,
    engine: SheetSage2Engine | None = None,
    progress: ProgressFn | None = None,
    bpm: float | None = None,
    pickup: str = "actual",
    tempo_mode: str = "section",
    quantize: bool = True,
    simplicity: float = 0.0,
    pitch_shift: int = PITCH_SEMITONES,
    octave_fix: bool = True,
    write: bool = True,
    **options: Any,
) -> dict[str, Any]:
    """转写 + 自动裁决 + 生成 tick 域工程文件与报告。

    返回 ``{"audio", "output_dir", "decisions", "project", "summary", "tempo",
    "files"}``。`project` 就是可以交给上层卷帘的 tick 域数据结构。

    `quantize` 默认开启：音符边界会吸到网格（`quantize.smart_quantize_musical`）。
    `simplicity` 是量化力度 0~5，默认 0（最小位移）。**注意 0 不等于"不动"**：
    节拍重要性阶梯仍然生效，只是 V2 口径下位移代价与阶梯收益同量级，所以只有本来
    就贴着强拍的音会被吸过去，已经在网格上的音原样通过。见 `docs/音符量化规格.md` §3。

    `pitch_shift` 是**旋律**的音高后期处理，默认 −12（降一个八度）：模型的旋律整体偏高
    一个八度，而和弦轨是准的，所以只动旋律、不动和弦，见 `pitch.py` 与
    `docs/WebUI问题清单.md` §1。传 0 就是原样。

    `octave_fix`（默认开）是**局部**的八度一致性校正：模型对同一段旋律的不同出现
    可能给不同的八度，这里按"重复段必须同高"逐段拉齐，见 `octave.py` 与
    `docs/八度校正规格.md`。传 False 就只做整体平移。
    """
    owned = engine is None
    engine = engine or SheetSage2Engine(EngineConfig())
    if owned:
        engine.load()

    audio_path = Path(audio)
    out = Path(output_dir)
    result = engine.transcribe(audio_path, out, progress=progress, **options)

    # ---------------------------------------------------------- 音高后期处理
    # 放在最前面：往后所有东西（tick 域工程、卷帘、SVP）都从 `result` 取音高，
    # 这里统一改完，就不会出现"工程里是对的、下载的 MIDI 里是错的"。
    result, pitch_stats = apply_pitch_shift(result, out, semitones=int(pitch_shift or 0),
                                            octave_fix=bool(octave_fix))

    labs = result.get("labs") or {}
    duration = float(result.get("duration_seconds") or 0.0)

    # ---------------------------------------------------------- 自动裁决
    beat_rows = _rows(labs.get("beat"))
    meter_estimate = estimate_meter(beat_rows)
    tempo_estimate = estimate_tempo(beat_rows)
    abc_text = result.get("abc")
    cross_check = reconcile(abc_tempo(abc_text), tempo_estimate)

    meter_tuple = (4, 4)
    if meter_estimate.primary:
        numerator, _, denominator = meter_estimate.primary.partition("/")
        meter_tuple = (int(numerator), int(denominator))

    playback = result.get("playback") or {}
    timeline = from_playback(playback, meter=meter_tuple, pickup=pickup)
    if not timeline.measures:
        # 模型没生成 ABC 就没有小节表，用强拍兜底
        timeline = from_beats(beat_rows, duration, meter=meter_tuple, pickup=pickup)

    # ---------------------------------------------------------- tick 域
    melody_rows = [[float(x) for x in line.replace("\t", " ").split()]
                   for line in (labs.get("melody_full") or "").splitlines() if line.strip()]
    notes = notes_to_ticks(melody_rows, timeline)
    vocal = [n for n in notes if n["track"] == 0]
    instrumental = [n for n in notes if n["track"] == 1]

    # ---------------------------------------------------------- 量化
    # tick 域的音符直接来自秒的线性插值，落在任意 tick 上。这里把它们吸到网格
    # （`quantize.smart_quantize_musical`），卷帘才画得出格子、DAW 才导得进去。
    # 量化只动 tick，不动小节表：小节表仍是唯一时间真源，BPM 依旧不参与出谱。
    # 口径是 Vocal2Midi **V2**：位移按"一拍"归一（不是按小节），见 docs/音符量化规格.md §3。
    quantize_stats: QuantizeStats | None = None
    quantize_shifts: dict[str, float] = {}
    if quantize:
        raw_tracks = [{"id": "vocal", "notes": vocal},
                      {"id": "instrumental", "notes": instrumental}]
        new_tracks, quantize_stats = quantize_tracks(
            raw_tracks,
            tick_length=timeline.tick_length,
            simplicity=simplicity,
            ticks_per_quarter=PPQ,
        )
        quantize_shifts = note_shift_seconds(
            raw_tracks[0]["notes"] + raw_tracks[1]["notes"],
            new_tracks[0] + new_tracks[1], timeline)
        for note in new_tracks[0] + new_tracks[1]:
            note["start_sec"] = round(timeline.tick_to_sec(note["tick"]), 4)
            note["end_sec"] = round(timeline.tick_to_sec(note["tick"] + note["dur"]), 4)
        vocal, instrumental = new_tracks

    keys = [dict(**_parse_key(item["label"]), tick=item["tick"], dur=item["dur"])
            for item in _intervals(labs.get("key"), timeline)]
    chords = _intervals(labs.get("chord"), timeline)
    # 段落：标签翻译成中文，**相邻同名合并**（模型是按乐句给标签的，不合并就是
    # "verse verse chorus chorus" 一串重复块）。`structure.lab` 本身不动。
    sections = _merge_sections(_intervals(labs.get("structure"), timeline))

    sample_times = [n["start_sec"] for n in notes] + [n["end_sec"] for n in notes]
    error = round_trip_error(timeline, sample_times) if sample_times else 0.0

    chosen_bpm = bpm or cross_check["chosen_bpm"]

    # 速度表：默认按段落拟合再合并。歌里的速度变化几乎总是发生在段落边界上，
    # `皮囊` 的 36 个结构段因此塌成 3 个速度段（124.311 / 109.994 / 124.069），
    # 既准又正好是人描述一首歌的方式。
    structure_seconds = [
        {"start": float(row[0]), "end": float(row[1]), "label": row[2]}
        for row in _rows(labs.get("structure")) if len(row) >= 3
    ]
    beat_times = beat_times_from_labs(labs)
    segments = []
    if tempo_mode == "section":
        segments = sectional_tempo(beat_times, structure_seconds,
                                   denominator=tempo_estimate.denominator or 4)

    if segments:
        tempo_map = segments_to_tempo_map(segments, timeline)
    elif tempo_mode == "measure":
        tempo_map = timeline.tempo_map()
    elif chosen_bpm:
        tempo_map = [{"tick": 0, "bpm": round(float(chosen_bpm), 4),
                      "numerator": meter_tuple[0], "denominator": meter_tuple[1],
                      "label": None, "beats": tempo_estimate.beats_total}]
    else:
        tempo_map = timeline.tempo_map()

    # 速度表相对真实拍点的偏差：恒速曲只有一个值，偏差就是模型节拍网格的抖动幅度
    map_drift = max((s.residual_max for s in segments), default=None)
    map_drift_mean = None
    if segments:
        total_beats = sum(s.beats for s in segments) or 1
        map_drift_mean = sum(s.residual_max * s.beats for s in segments) / total_beats

    # 速度偏低时给一个 ×2 的候选：模型偶尔会锁到半速脉冲上（`巴拉莱卡` 报 59，
    # 人工核对大约是 115–118）。这里只提示，不擅自翻倍——慢歌本身也可能是对的。
    halftime_hint = None
    if chosen_bpm and float(chosen_bpm) < 70.0:
        halftime_hint = round(float(chosen_bpm) * 2.0, 3)

    # 对"网格不可靠/过渡"的区段，额外算一份"能不能自动重建网格"的评估：
    # 重建后的速度、命中率，以及按重建网格重排时音符会移动多少。
    segment_details = []
    for segment in segments:
        detail = segment.as_dict()
        if not segment.reliable or segment.is_transition:
            recovery = suggest_grid_recovery(
                beat_times, segment.start, segment.end,
                denominator=tempo_estimate.denominator or 4)
            detail["recovery"] = recovery.as_dict()
        segment_details.append(detail)

    decisions = Decisions(
        meter=meter_estimate.primary,
        pickup_seconds=timeline.pickup_seconds(),
        bpm=chosen_bpm,
        bpm_source="override" if bpm else cross_check["source"],
        bpm_confident=False if bpm else cross_check["confident"],
        tempo_constant=tempo_estimate.is_constant,
        tempo_range=tempo_estimate.tempo_range,
        timeline_source=timeline.source,
        keys=[k["tonic"] + ":" + k["mode"] for k in keys],
        notes=len(notes),
        round_trip_error=error,
        tempo_mode=tempo_mode,
        tempo_segments=len(segments) if segments else len(tempo_map),
        tempo_segments_detail=segment_details,
        stable_tempos=[s.bpm_rounded if s.bpm_rounded is not None else round(s.bpm, 3)
                       for s in segments if s.reliable and not s.is_transition],
        halftime_hint=halftime_hint,
        tempo_flattened=len(tempo_map) == 1,
        flatten_max_drift=map_drift,
        flatten_mean_drift=map_drift_mean,
        quantized=quantize_stats is not None,
        simplicity=simplicity if quantize_stats is not None else None,
        quantize_moved=quantize_stats.moved if quantize_stats else 0,
        quantize_moved_ratio=round(quantize_stats.moved_ratio, 4) if quantize_stats else 0.0,
        quantize_failed_tracks=quantize_stats.failed_tracks if quantize_stats else 0,
        quantize_grid_ticks=quantize_stats.grid_ticks if quantize_stats else None,
        quantize_max_shift_seconds=quantize_shifts.get("max"),
        pitch_semitones=pitch_stats.semitones if pitch_stats.enabled else 0,
    )

    # 速度表和音符 tick 是解耦的：tick 来自小节表，速度表只决定 tick 怎么变回秒。
    # 因此**改 BPM 不需要动任何音符**——但会改播放时长。实测分段速度表（人看友好）
    # 会让时长差 −3.4 ~ +10.9s，逐小节速度表则精确到 ±0.01s（时间戳的量化下限）。
    # 两个都留下：`tempo_map` 给人看，`tempo_map_exact` 给导出用。
    exact_tempo_map = timeline.tempo_map()
    played = _played_seconds(tempo_map, timeline.tick_length)
    played_exact = _played_seconds(exact_tempo_map, timeline.tick_length)
    duration_error = round(played - duration, 3)

    project = {
        "version": 1,
        "ppq": PPQ,
        "audio": audio_path.name,
        "duration_seconds": round(duration, 4),
        "source": "sheetsage2",
        "timeline": {
            "measures": [m.as_dict() for m in timeline.measures],
            "tempo_map": tempo_map,
            "tempo_map_exact": exact_tempo_map,
            "pickup_seconds": timeline.pickup_seconds(),
            "tick_length": timeline.tick_length,
            "played_duration_seconds": round(played, 3),
            "duration_error_seconds": duration_error,
            "exact_tempo_map_duration_error_seconds": round(played_exact - duration, 3),
        },
        "keys": keys,
        "chords": chords,
        "sections": sections,
        "quantize": (quantize_stats.as_dict() if quantize_stats
                     else {"enabled": False, "reason": "关闭了量化"}),
        "quantize_shift_seconds": quantize_shifts,
        "pitch": pitch_stats.as_dict(),
        "tracks": [
            {"id": "vocal", "role": "vocal-lead", "name": "人声主旋律",
             "notes": [{k: v for k, v in n.items()
                        if k in ("tick", "dur", "pitch", "raw_tick", "raw_dur")}
                       for n in vocal]},
            {"id": "instrumental", "role": "instrument", "name": "器乐旋律",
             "notes": [{k: v for k, v in n.items()
                        if k in ("tick", "dur", "pitch", "raw_tick", "raw_dur")}
                       for n in instrumental]},
        ],
        "meta": {
            "abc_meter": abc_meter(abc_text),
            "abc_bpm": cross_check["abc_bpm"],
            "beat_bpm": cross_check["beat_bpm"],
            "bpm_median_interval": tempo_estimate.bpm_median_interval,
            "bpm_dominant": tempo_estimate.bpm_dominant,
            "meter_distribution": meter_estimate.distribution,
            "meter_consistent": meter_estimate.consistent,
            "warnings": list(result.get("warnings") or []) + timeline.warnings
                        + meter_estimate.warnings + tempo_estimate.warnings
                        + cross_check["notes"]
                        + ([f"速度偏低（{round(float(chosen_bpm), 3)} BPM）：可能是模型的半速锁定，"
                            f"×2 = {halftime_hint}；如确认可用 --bpm {halftime_hint} 覆盖"]
                           if halftime_hint else [])
                        + ([_instrumental_hint(segments, tempo_estimate.beats_total)]
                           if any(not s.reliable and s.beats >= 0.25 * max(1, tempo_estimate.beats_total)
                                  for s in segments) else [])
                        + ([f"速度表让总时长与原曲差 {duration_error:+.2f}s"
                            f"（{abs(duration_error) / max(1e-9, duration):.1%}）；"
                            f"导出请用 tempo_map_exact（逐小节，误差 "
                            f"{played_exact - duration:+.2f}s）"]
                           if abs(duration_error) > 0.5 else [])
                        + ([f"速度表：{decisions.tempo_segments} 段"
                            + "".join(f" / {s.bpm:.2f}@{s.start:.1f}s" for s in segments)
                            + f"；相对拍点偏差 平均 {map_drift_mean:.4f}s / 最大 {map_drift:.4f}s"]
                           if segments and map_drift is not None else [])
                        + _quantize_warnings(quantize_stats, quantize_shifts)
                        + pitch_warnings(pitch_stats),
        },
    }

    summary = summarize_result(result, engine.device, engine._effective_dtype()).as_dict()

    report = {
        "decisions": decisions.as_dict(),
        "tempo": {"estimate": tempo_estimate.as_dict(),
                  "cross_check": cross_check,
                  "mode": tempo_mode,
                  "segments": segment_details,
                  "map_drift_max_seconds": map_drift,
                  "map_drift_mean_seconds": map_drift_mean,
                  "played_duration_seconds": round(played, 3),
                  "duration_error_seconds": duration_error,
                  "exact_tempo_map_duration_error_seconds": round(played_exact - duration, 3),
                  "per_measure_bpm_median": _median_bpm(timeline)},
        "meter": meter_estimate.as_dict(),
        "timeline": timeline.as_dict(),
        "quantize": (dict(quantize_stats.as_dict(), shift_seconds=quantize_shifts)
                     if quantize_stats else {"enabled": False, "reason": "关闭了量化"}),
        "pitch": pitch_stats.as_dict(),
    }

    files: list[str] = []
    if write:
        # 原子写（G5）：先写同目录 `.part` 再 `os.replace`。进程在写 JSON 时被杀，
        # 磁盘上要么是旧文件、要么是新文件，不会有"半截 project.json"让 /lanes 500。
        write_text_atomic(out / "project.json", json.dumps(project, ensure_ascii=False,
                                                           indent=2))
        write_text_atomic(out / "report.json", json.dumps(report, ensure_ascii=False,
                                                          indent=2))

    # 产物自检（G4）：这里只做**便宜**的检查（必需文件在不在、是不是空文件）。
    # 完整的自洽性体检在 `result.inspect_output_dir`，WebUI 的 runner 会再跑一次并把
    # 结果放进阶段详情；以前这里硬编码 `artifacts_ok = True`，等于"完成"不设门禁。
    missing = [name for name in (*EXPECTED_TEXT, *EXPECTED_MIDI)
               if not (out / name).is_file() or (out / name).stat().st_size == 0]
    summary["artifacts_ok"] = not missing
    summary["artifacts_missing"] = missing

    if write:
        write_text_atomic(out / "summary.json", json.dumps(summary, ensure_ascii=False,
                                                          indent=2))
        files = sorted(str(p.relative_to(out)).replace("\\", "/")
                       for p in out.rglob("*") if p.is_file())

    return {"audio": str(audio_path), "output_dir": str(out), "decisions": decisions.as_dict(),
            "project": project, "report": report, "summary": summary, "files": files}


def _quantize_warnings(stats: QuantizeStats | None,
                       shifts: dict[str, float]) -> list[str]:
    """量化相关的告警。量化会把音符搬走，搬太多就是"网格和谱子对不上"的信号。"""
    if stats is None:
        return []
    messages = [f"音符量化：{stats.notes} 个音符吸到 "
                f"{stats.grid_ticks} tick（一拍的 1/96）网格，移动 {stats.moved} 个"
                f"（{stats.moved_ratio:.0%}）"
                + (f"，最大位移 {shifts['max']:.3f}s（中位 {shifts['median']:.3f}s）"
                   if shifts else "")]
    if stats.failed_tracks:
        messages.append(f"有 {stats.failed_tracks} 条轨道的量化失败（音符重叠或时值过短），"
                        f"该轨道保持未量化：" + "；".join(stats.messages))
    if shifts.get("max", 0.0) > 0.25:
        messages.append(f"量化把个别音符搬了 {shifts['max']:.3f}s（超过 1/4 秒）："
                        f"可能是节拍网格在该处不准，卷帘上留意")
    return messages


def _played_seconds(tempo_map: list[dict[str, Any]], tick_length: int, ppq: int = PPQ) -> float:
    """按速度表算一遍播放时长（决定导出结果和原曲对不对得上）。"""
    if not tempo_map or tick_length <= 0:
        return 0.0
    total = 0.0
    for index, entry in enumerate(tempo_map):
        start = entry["tick"]
        end = tempo_map[index + 1]["tick"] if index + 1 < len(tempo_map) else tick_length
        bpm = entry.get("bpm") or 0.0
        if bpm > 0 and end > start:
            total += (end - start) / ppq * 60.0 / bpm
    return total


def _instrumental_hint(segments, total_beats: int) -> str:
    """存在大片网格不可靠区时，提示可能是伴奏版。

    实测（同一首《壁上观》）：伴奏版 71.5–249.5s 的拍间隔在 81/88/107/86/120 BPM 之间乱跳，
    一致性 15%；**换成含人声的原唱，同一段变成 120.000、425 个拍点、一致性 100%**。
    缺了主唱这条线索，模型锁不住节拍网格。
    """
    unreliable = [s for s in segments
                  if not s.reliable and s.beats >= 0.25 * max(1, total_beats)]
    span = sum(s.end - s.start for s in unreliable)
    return (f"有 {span:.0f}s 的拍点网格不可靠（占 {unreliable[0].beats / max(1, total_beats):.0%} 拍点）；"
            f"若输入是伴奏版/纯音乐，换用含人声的完整混音通常能让节拍锁定明显变好")


def _median_bpm(timeline: Timeline) -> float | None:
    values = sorted(m.bpm for m in timeline.measures if m.bpm > 0)
    if not values:
        return None
    return round(values[len(values) // 2], 3)


def _main(argv: list[str] | None = None) -> int:
    """一键跑：``python -m sheetsage2_webui.pipeline song.flac --out output/song``"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="sheetsage2-pipeline",
                                     description="SheetSage2 一键流水线（零参数）")
    parser.add_argument("audio", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="产物目录")
    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    parser.add_argument("--bpm", type=float, default=None, help="可选：覆盖自动判定速度")
    parser.add_argument("--pickup", default="actual", choices=("actual", "padded"))
    parser.add_argument("--tempo-mode", default="section",
                        choices=("section", "measure", "single"),
                        help="速度表：按段落（默认）/ 逐小节 / 单一速度")
    parser.add_argument("--no-quantize", dest="quantize", action="store_false",
                        help="不做网格量化（默认做）")
    parser.add_argument("--simplicity", type=float, default=0.0,
                        help="量化力度 0~5：越大越敢往强拍上贴（默认 0，最小位移）")
    parser.add_argument("--pitch-shift", type=int, default=PITCH_SEMITONES,
                        help="旋律音高后期处理（半音）：默认 -12（模型旋律整体高一个八度）；"
                             "0 = 原样，-24 = 降两个八度。和弦轨不动")
    parser.add_argument("--no-octave-fix", dest="octave_fix", action="store_false",
                        help="不做「重复段八度一致性校正」（默认做）")
    args = parser.parse_args(argv)

    if not args.audio.is_file():
        print(f"找不到音频：{args.audio}", file=sys.stderr)
        return 2

    def progress(value):
        if value.get("stage") == "encoding":
            print(f"  [窗口 {value['window']}/{value['windows']}]", flush=True)

    engine = SheetSage2Engine(EngineConfig(device=args.device)).load()
    payload = run(args.audio, args.out, engine=engine, progress=progress, bpm=args.bpm,
                  pickup=args.pickup, tempo_mode=args.tempo_mode,
                  quantize=args.quantize, simplicity=args.simplicity,
                  pitch_shift=args.pitch_shift, octave_fix=args.octave_fix)
    decisions = payload["decisions"]
    print(f"\n拍号 {decisions['meter']}   弱起 {decisions['pickup_seconds']}s   "
          f"调性 {','.join(decisions['keys'])}")
    print(f"速度 {decisions['bpm']} BPM（来源 {decisions['bpm_source']}，"
          f"恒速 {decisions['tempo_constant']}）")
    for segment in decisions["tempo_segments_detail"]:
        flag = ""
        if not segment["reliable"]:
            flag = "  [网格不可靠]"
        elif segment.get("is_transition"):
            flag = "  [过渡]"
        elif segment.get("bpm_uncertain"):
            flag = "  [速度不确定]"
        sigma = segment.get("bpm_sigma")
        print(f"   {segment['start']:7.2f}-{segment['end']:7.2f}s  "
              f"{segment.get('bpm_rounded', segment['bpm']):>8} BPM"
              + (f" ±{sigma}" if sigma is not None else "")
              + f"  {segment['beats']:4d} 拍"
              + (f"   [{segment['label']}]" if segment.get("label") else "") + flag)
        recovery = segment.get("recovery")
        if recovery:
            auto = "可自动重建" if recovery["confident"] else "需人工确认"
            print(f"        重建评估：最佳网格 {recovery['bpm']} BPM，"
                  f"命中 {recovery['hit_ratio']:.0%}/{recovery['tight_ratio']:.0%}，"
                  f"音符位移中位 {recovery['median_shift']}s 最大 {recovery['max_shift']}s"
                  f" -> {auto}")
    print(f"音符 {decisions['notes']}   tick 总长 "
          f"{payload['project']['timeline']['tick_length']}   往返误差 "
          f"{decisions['round_trip_error']:.2e}s")
    quant = payload["project"]["quantize"]
    if quant.get("enabled"):
        shift = quant.get("shift_seconds") or {}
        print(f"量化：{quant['notes']} 个音符 -> {quant['grid_ticks']} tick（一拍的 1/96）网格，"
              f"移动 {quant['moved']}（{quant['moved_ratio']:.0%}）"
              + (f"，最大位移 {shift['max']:.3f}s" if shift else "")
              + (f"；{quant['failed_tracks']} 条轨道量化失败" if quant["failed_tracks"] else ""))
    else:
        print("量化：已关闭（--no-quantize）")
    pitch = payload["project"]["pitch"]
    if pitch.get("enabled"):
        print(f"音高：旋律整体 {pitch['semitones']:+d} 半音，"
              f"lab {sum(pitch['labs'].values())} 个音 / MIDI {len(pitch['midis'])} 个文件，"
              f"score.abc {pitch['abc']}")
    else:
        print("音高：原样（--pitch-shift 0）")
    header = payload['project']['timeline']
    print(f"播放时长：分段速度表误差 {header['duration_error_seconds']:+.2f}s；"
          f"逐小节速度表误差 {header['exact_tempo_map_duration_error_seconds']:+.2f}s"
          f"  -> 导出用 tempo_map_exact")
    print(f"产物 {len(payload['files'])} 个 -> {payload['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
