"""给上层（将来的 WebUI / 服务端）用的单一入口。

上层的需求是"给一个音频路径，拿到一份可 JSON 化的结果"，所以这里把
引擎、摘要、产物体检三件事收成一个函数，调用方不需要知道模型细节。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .engine import EngineConfig, SheetSage2Engine
from .result import beat_times_from_labs, inspect_output_dir, summarize_result
from .tempo import abc_tempo, compare_to_constant, estimate_tempo, reconcile, retime, snap_bpm

ProgressFn = Callable[[Mapping[str, Any]], None]


def tempo_report(result: Mapping[str, Any], bpm: float | None = None,
                 snap: str = "none") -> dict[str, Any]:
    """一次转写的速度报告，并给出干预所需的信息。

    - ``estimate``：鲁棒估计（最小二乘 + 稳定段），以及朴素中位间隔法作对照
    - ``cross_check``：模型 `score.abc` 里 `Q:1/4=` 的值 vs 我们的拟合值
    - ``snapped_bpm``：按 `snap` 吸附后的值
    - ``target``：指定了 `bpm` 时，模型拍网格相对该恒定速度网格的漂移，以及
      把整条网格缩放到目标速度时每个拍点会移动多少
    """
    labs = result.get("labs") or {}
    rows = [line.split("\t") if "\t" in line else line.split()
            for line in (labs.get("beat") or "").splitlines() if line.strip()]
    estimate = estimate_tempo(rows)
    times = beat_times_from_labs(labs)

    report: dict[str, Any] = {
        "estimate": estimate.as_dict(),
        "snapped_bpm": snap_bpm(estimate.bpm, snap),
        "cross_check": reconcile(abc_tempo(result.get("abc")), estimate),
    }
    if estimate.bpm and estimate.denominator and len(times) >= 2:
        anchor = estimate.anchor if estimate.anchor is not None else times[0]
        report["vs_estimate"] = compare_to_constant(
            times, anchor, estimate.bpm, estimate.denominator)
        if bpm:
            report["target"] = bpm
            report["vs_target"] = compare_to_constant(times, anchor, bpm, estimate.denominator)
            _, shift = retime(times, estimate.bpm / bpm, anchor=anchor)
            report["retime_shift"] = shift
    return report


def transcribe_to_directory(
    audio: str | Path,
    output_dir: str | Path,
    *,
    engine: SheetSage2Engine | None = None,
    progress: ProgressFn | None = None,
    bpm: float | None = None,
    bpm_snap: str = "none",
    write_summary: bool = True,
    **options: Any,
) -> dict[str, Any]:
    """转写一个音频，把产物写到 `output_dir`，返回可 JSON 化的结果描述。

    参数
    ----
    audio          音频路径（wav/flac/mp3…，靠 ffmpeg 解码）
    output_dir     产物目录，会被创建
    engine         复用已加载的引擎；不传则按默认配置现建一个
    progress       进度回调，收到 {'stage': ..., 'window': i, 'windows': n, ...}
    bpm            手动指定速度（四分音符）。给出后会记录模型网格相对它的漂移
    bpm_snap       把估计速度吸附到 none / integer / half
    options        透传给模型 `transcribe`（preset / max_seconds / overlap_seconds ...）

    返回
    ----
    ``{"audio", "output_dir", "summary", "tempo", "artifacts", "files", "warnings", "abc"}``
    其中 ``artifacts.ok`` 表示产物完整且自洽；``files`` 是相对的产物路径列表。
    """
    owned = engine is None
    engine = engine or SheetSage2Engine(EngineConfig())
    if owned:
        engine.load()

    audio_path = Path(audio)
    out = Path(output_dir)
    result = engine.transcribe(audio_path, out, progress=progress, **options)

    summary = summarize_result(result, engine.device, engine._effective_dtype()).as_dict()
    report = inspect_output_dir(out, summary["duration_seconds"])
    summary["artifacts_ok"] = report.ok

    tempo = tempo_report(result, bpm=bpm, snap=bpm_snap)
    summary["bpm_effective"] = bpm or tempo["snapped_bpm"] or summary.get("bpm")
    summary["bpm_override"] = bpm

    if write_summary:
        (out / "summary.json").write_text(
            json.dumps({**summary, "tempo": tempo}, ensure_ascii=False, indent=2), encoding="utf-8")

    files = sorted(
        str(path.relative_to(out)).replace("\\", "/")
        for path in out.rglob("*") if path.is_file()
    )

    return {
        "audio": str(audio_path),
        "output_dir": str(out),
        "summary": summary,
        "tempo": tempo,
        "artifacts": report.as_dict(),
        "files": files,
        "warnings": summary.get("warnings") or [],
        "abc": result.get("abc"),
    }
