"""把 SheetSage2 的产物目录读成可判定的结构化信息。

验证脚本不应该去猜产物内容，所以这里集中处理 lab / MIDI / JSON 的解析，
并给出一组自洽性检查（时间范围、音高范围、节拍速度、区间覆盖率……）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tempo import abc_meter, abc_tempo, estimate_meter, estimate_tempo, reconcile

#: 一次完整转写应当产出的文件。缺一个都记进 problems。
EXPECTED_TEXT = (
    "events.json",
    "events.tsv",
    "playback.json",
    "melody_full.lab",
    "melody_vocal.lab",
    "melody_instrumental.lab",
    "chord.lab",
    "key.lab",
    "structure.lab",
    "beat.lab",
    "downbeat.lab",
    "rhythm_events.lab",
    "tokens.txt",
    "tokens.json",
    "result.json",
)
EXPECTED_MIDI = (
    "transcription.mid",
    "melody.mid",
    "melody_vocal.mid",
    "melody_instrumental.mid",
    "chords.mid",
    "notation/song_melody.mid",
)
#: 模型自身允许缺失的产物：beat 不足 2 个时不会生成 ABC。
OPTIONAL = ("score.abc",)


# --------------------------------------------------------------------- 解析
def read_rows(path: Path) -> list[list[str]]:
    """读 tab/空格分隔的 lab / tsv，返回字段列表。"""
    return parse_lab_text(path.read_text(encoding="utf-8", errors="replace"))


def parse_lab_text(text: str) -> list[list[str]]:
    """把 lab 文本切成字段列表。"""
    rows: list[list[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(line.split("\t") if "\t" in line else line.split())
    return rows


def beat_metrics(rows: list[list[str]], method: str = "robust") -> dict[str, Any]:
    """从 beat.lab 的行推出拍号与速度。

    速度实现在 `tempo.estimate_tempo()`：先在整条网格上做最小二乘（避免 0.01s 时间戳
    量化把 104 BPM 读成 103.45），并只在最长的稳定段上拟合（避免歌曲结尾散拍污染）。
    拍号实现在 `tempo.estimate_meter()`：取最长的拍号段作为主拍号，并识别开头的
    **弱起小节**（光芒开头是 1/8 弱起，朋友的酒是 3/8 弱起）。

    这里只是把两部分结果摊平成字典，并补一个 `beats` 别名。
    """
    tempo_estimate = estimate_tempo(rows, method=method)
    meter_estimate = estimate_meter(rows)

    data = tempo_estimate.as_dict()
    data["beats"] = tempo_estimate.beats_total
    data.update({
        "meter": meter_estimate.primary,
        "denominator": meter_estimate.denominator,
        "meter_distribution": meter_estimate.distribution,
        "meter_consistent": meter_estimate.consistent,
        "meter_changes": meter_estimate.changes,
        "pickup": meter_estimate.pickup,
    })
    return data


def beat_stats(path: Path, method: str = "robust") -> dict[str, Any]:
    """beat.lab 的节拍统计。"""
    return beat_metrics(read_rows(path), method=method)


def beat_times_from_labs(labs: dict[str, str]) -> list[float]:
    """从模型返回的 labs 里取出拍点时间（升序）。"""
    times: list[float] = []
    for row in parse_lab_text(labs.get("beat") or ""):
        try:
            times.append(float(row[0]))
        except (ValueError, IndexError):
            continue
    return sorted(times)


def interval_stats(path: Path, value_index: int = 2) -> dict[str, Any]:
    """区间型 lab（chord/key/structure）的统计。"""
    rows = [r for r in read_rows(path) if len(r) > value_index]
    spans = []
    for row in rows:
        try:
            start, end = float(row[0]), float(row[1])
        except ValueError:
            continue
        if end > start:
            spans.append((start, end, row[value_index]))

    if not spans:
        return {"rows": 0, "covered_seconds": 0.0, "first_start": None,
                "last_end": None, "labels": []}

    labels: list[str] = []
    for _, _, label in spans:
        if label not in labels:
            labels.append(label)
    return {
        "rows": len(spans),
        "covered_seconds": round(sum(e - s for s, e, _ in spans), 3),
        "first_start": round(min(s for s, _, _ in spans), 3),
        "last_end": round(max(e for _, e, _ in spans), 3),
        "labels": labels,
    }


def midi_stats(path: Path) -> dict[str, Any]:
    """MIDI 的音符统计；解析失败时返回 error 而不是抛异常。"""
    try:
        import pretty_midi

        midi = pretty_midi.PrettyMIDI(str(path))
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    notes = [n for inst in midi.instruments for n in inst.notes]
    pitches = [n.pitch for n in notes]
    return {
        "tracks": [inst.name for inst in midi.instruments],
        "notes": len(notes),
        "pitch_min": min(pitches) if pitches else None,
        "pitch_max": max(pitches) if pitches else None,
        "first_note": round(min(n.start for n in notes), 3) if notes else None,
        "last_note_end": round(max(n.end for n in notes), 3) if notes else None,
        "resolution": midi.resolution,
        "end_time": round(midi.get_end_time(), 3),
    }


def events_stats(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data.get("events", [])
    times = [float(e["time"]) for e in events if "time" in e]
    fields: dict[str, int] = {}
    for event in events:
        for key in event.get("values", {}):
            fields[key] = fields.get(key, 0) + 1
    return {
        "schema_version": data.get("schema_version"),
        "has_eos": data.get("has_eos"),
        "prompts": data.get("prompts"),
        "events": len(events),
        "time_first": round(min(times), 3) if times else None,
        "time_last": round(max(times), 3) if times else None,
        "monotonic": all(b >= a for a, b in zip(times, times[1:])),
        "fields": fields,
    }


# --------------------------------------------------------------------- 报告
@dataclass
class ArtifactReport:
    """产物目录的体检结果。"""

    directory: str
    ok: bool = True
    missing: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(directory=self.directory, ok=self.ok, missing=self.missing,
                    empty=self.empty, problems=self.problems, metrics=self.metrics)


def inspect_output_dir(directory: str | Path, duration_seconds: float | None = None) -> ArtifactReport:
    """读取产物目录，做完整性与自洽性检查。"""
    root = Path(directory)
    report = ArtifactReport(directory=str(root))
    metrics: dict[str, Any] = {}

    if not root.is_dir():
        report.ok = False
        report.problems.append("产物目录不存在")
        return report

    expected = list(EXPECTED_TEXT) + list(EXPECTED_MIDI)
    for name in expected:
        path = root / name
        if not path.is_file():
            report.missing.append(name)
        elif path.stat().st_size == 0:
            report.empty.append(name)

    if report.missing:
        report.ok = False
        report.problems.append(f"缺少 {len(report.missing)} 个产物文件")

    abc = root / "score.abc"
    metrics["abc_present"] = abc.is_file() and abc.stat().st_size > 0

    # --- events.json
    events_path = root / "events.json"
    if events_path.is_file() and events_path.stat().st_size:
        try:
            metrics["events"] = events_stats(events_path)
        except Exception as exc:
            report.ok = False
            report.problems.append(f"events.json 解析失败：{exc}")

    # --- labs
    for name in ("chord.lab", "key.lab", "structure.lab"):
        path = root / name
        if path.is_file() and path.stat().st_size:
            metrics[name] = interval_stats(path)

    beat_path = root / "beat.lab"
    if beat_path.is_file() and beat_path.stat().st_size:
        metrics["beat.lab"] = beat_stats(beat_path)

    # --- playback.json
    playback_path = root / "playback.json"
    if playback_path.is_file() and playback_path.stat().st_size:
        try:
            playback = json.loads(playback_path.read_text(encoding="utf-8"))
            metrics["playback"] = {
                "duration": playback.get("duration"),
                "measures": len(playback.get("measures") or []),
                "tracks": [t.get("name") for t in playback.get("tracks") or []],
                "notes": sum(len(t.get("notes") or []) for t in playback.get("tracks") or []),
                "warnings": playback.get("warnings") or [],
            }
        except Exception as exc:
            report.ok = False
            report.problems.append(f"playback.json 解析失败：{exc}")

    # --- MIDI
    midi_metrics: dict[str, Any] = {}
    for name in EXPECTED_MIDI:
        path = root / name
        if path.is_file() and path.stat().st_size:
            midi_metrics[name] = midi_stats(path)
    metrics["midi"] = midi_metrics

    # ------------------------------------------------------------ 自洽性
    duration = duration_seconds
    if duration is None:
        result_path = root / "result.json"
        if result_path.is_file():
            try:
                duration = float(json.loads(result_path.read_text(encoding="utf-8"))["duration_seconds"])
            except Exception:
                duration = None

    if duration:
        metrics["duration_seconds"] = round(float(duration), 3)
        tolerance = max(1.0, duration * 0.01)

        ev = metrics.get("events")
        if ev and ev.get("time_last") is not None:
            if ev["time_last"] > duration + tolerance:
                report.ok = False
                report.problems.append(
                    f"events 时间戳 {ev['time_last']}s 超出音频时长 {duration:.1f}s")
            if not ev.get("monotonic"):
                report.ok = False
                report.problems.append("events 的时间戳不是单调不减")
            if not ev.get("events"):
                report.ok = False
                report.problems.append("events 为空")

        beats = metrics.get("beat.lab") or {}
        bpm = beats.get("bpm")
        if bpm is not None and not 30.0 <= bpm <= 300.0:
            report.ok = False
            report.problems.append(
                f"节拍推出的 BPM={bpm}（拍号 {beats.get('meter')}）不在合理区间 30–300")

        for name in ("chord.lab", "key.lab", "structure.lab"):
            info = metrics.get(name)
            if info is None:
                continue
            if info["rows"] == 0:
                report.ok = False
                report.problems.append(f"{name} 没有任何区间")
            elif info["last_end"] and info["last_end"] > duration + tolerance:
                report.ok = False
                report.problems.append(
                    f"{name} 末端 {info['last_end']}s 超出音频时长 {duration:.1f}s")

        melody = midi_metrics.get("melody.mid") or {}
        if melody and melody.get("notes"):
            if melody["last_note_end"] > duration + tolerance:
                report.ok = False
                report.problems.append(
                    f"melody.mid 末音 {melody['last_note_end']}s 超出音频时长 {duration:.1f}s")
            if melody["pitch_min"] is None or not 0 <= melody["pitch_min"] <= 127:
                report.ok = False
                report.problems.append("melody.mid 存在非法音高")
        elif melody:
            report.ok = False
            report.problems.append(f"melody.mid 解析异常：{melody.get('error') or '没有音符'}")

    report.metrics = metrics
    return report


@dataclass
class TranscriptionSummary:
    """一次转写的高层摘要，供日志和报告直接引用。"""

    audio: str
    duration_seconds: float
    elapsed_seconds: float
    device: str
    dtype: str
    notes: dict[str, int]
    events: int
    abc_measures: int
    abc_present: bool
    keys: list[str]
    bpm: float | None
    structure: list[str]
    chords: list[str]
    meter: str | None = None
    beat_unit_bpm: float | None = None
    bpm_median_interval: float | None = None   # 朴素中位间隔法，仅作对照
    abc_bpm: float | None = None               # 模型 score.abc 里 Q:1/4= 的值
    bpm_chosen: float | None = None            # 校验后实际采用的值
    bpm_source: str | None = None              # beat_robust | abc_q
    bpm_agreement: bool = True                 # 两个来源是否一致
    bpm_delta: float | None = None
    bpm_dominant: float | None = None          # 众数拍间隔给出的"主速度"
    bpm_confident: bool = True                 # 恒速曲才敢说这个 BPM 可信
    tempo_range: dict[str, Any] | None = None  # 局部速度的 min/max/median
    abc_meter: str | None = None               # 模型 score.abc 里 M: 的值
    meter_consistent: bool = True              # 除弱起外是否全程同一拍号
    pickup: dict[str, Any] | None = None       # 弱起小节（开头那个短小节）
    tempo_constant: bool = False               # 是否可当作恒速曲
    tempo_coverage: float = 0.0                # 稳定段覆盖率
    tempo_residual_max: float | None = None
    peak_gpu_mib: float = 0.0
    windows: int = 0
    warnings: list[str] = field(default_factory=list)
    abc_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(
            audio=self.audio, duration_seconds=round(self.duration_seconds, 3),
            elapsed_seconds=round(self.elapsed_seconds, 2), device=self.device, dtype=self.dtype,
            notes=self.notes, events=self.events, abc_measures=self.abc_measures,
            abc_present=self.abc_present, keys=self.keys, bpm=self.bpm,
            structure=self.structure, chords=self.chords,
            meter=self.meter, beat_unit_bpm=self.beat_unit_bpm,
            bpm_median_interval=self.bpm_median_interval,
            abc_bpm=self.abc_bpm, bpm_chosen=self.bpm_chosen,
            bpm_source=self.bpm_source, bpm_agreement=self.bpm_agreement,
            bpm_delta=self.bpm_delta,
            bpm_dominant=self.bpm_dominant, bpm_confident=self.bpm_confident,
            tempo_range=self.tempo_range,
            abc_meter=self.abc_meter, meter_consistent=self.meter_consistent,
            pickup=self.pickup,
            tempo_constant=self.tempo_constant, tempo_coverage=self.tempo_coverage,
            tempo_residual_max=self.tempo_residual_max,
            peak_gpu_mib=round(self.peak_gpu_mib, 1), windows=self.windows,
            warnings=self.warnings, abc_error=self.abc_error,
        )


def summarize_result(result: dict[str, Any], device: str, dtype: str) -> TranscriptionSummary:
    """从模型返回的 result 字典提炼摘要。"""
    labs = result.get("labs") or {}

    def labels(name: str, index: int = 2) -> list[str]:
        out: list[str] = []
        for line in (labs.get(name) or "").splitlines():
            parts = line.split("\t") if "\t" in line else line.split()
            if len(parts) > index and parts[index] not in out:
                out.append(parts[index])
        return out

    beat_rows = parse_lab_text(labs.get("beat") or "")
    estimate = estimate_tempo(beat_rows)
    beats = beat_metrics(beat_rows)
    cross_check = reconcile(abc_tempo(result.get("abc")), estimate)

    return TranscriptionSummary(
        audio=str(result.get("audio")),
        duration_seconds=float(result.get("duration_seconds") or 0.0),
        elapsed_seconds=float(result.get("elapsed_seconds") or 0.0),
        device=device,
        dtype=str(result.get("dtype")),
        notes={
            "melody": int(result.get("melody_notes") or 0),
            "vocal": int(result.get("vocal_notes") or 0),
            "instrumental": int(result.get("instrumental_notes") or 0),
        },
        events=int(result.get("num_events") or 0),
        abc_measures=int(result.get("abc_measures") or 0),
        abc_present=bool(result.get("abc")),
        keys=labels("key"),
        bpm=beats["bpm"],
        structure=labels("structure"),
        chords=labels("chord"),
        meter=beats["meter"],
        beat_unit_bpm=beats["beat_unit_bpm"],
        bpm_median_interval=beats["bpm_median_interval"],
        abc_bpm=cross_check["abc_bpm"],
        bpm_chosen=cross_check["chosen_bpm"],
        bpm_source=cross_check["source"],
        bpm_agreement=cross_check["agreement"],
        bpm_delta=cross_check["delta"],
        bpm_dominant=beats["bpm_dominant"],
        bpm_confident=cross_check["confident"],
        tempo_range=beats["tempo_range"],
        abc_meter=abc_meter(result.get("abc")),
        meter_consistent=beats["meter_consistent"],
        pickup=beats["pickup"],
        tempo_constant=beats["is_constant"],
        tempo_coverage=beats["regular_coverage"],
        tempo_residual_max=beats["residual_max"],
        peak_gpu_mib=float(result.get("peak_gpu_mib") or 0.0),
        windows=len(result.get("windows") or []),
        warnings=list(result.get("warnings") or []),
        abc_error=result.get("abc_error"),
    )
