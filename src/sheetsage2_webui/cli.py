"""命令行入口：把音频交给 SheetSage2，写出产物并打印摘要。

    python -m sheetsage2_webui.cli "凤凰传奇 - 光芒.flac" --out-root output
    python -m sheetsage2_webui.cli song.flac --bpm 104 --bpm-snap integer
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .engine import EngineConfig, SheetSage2Engine, TranscriptionError
from .service import transcribe_to_directory


def _slug(path: Path) -> str:
    """把音频文件名转成可做目录名的安全串。"""
    keep = [c if (c.isalnum() or c in "._-") else "_" for c in path.stem]
    return "".join(keep).strip("_") or "audio"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sheetsage2", description="SheetSage2 转写")
    parser.add_argument("audio", nargs="+", type=Path, help="音频文件（wav/flac/mp3…）")
    parser.add_argument("--out-root", type=Path, default=Path("output"), help="产物根目录")
    parser.add_argument("--model", default=EngineConfig.model_id, help="模型 id 或本地目录")
    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    parser.add_argument("--dtype", default="bf16", choices=("bf16", "fp32"))
    parser.add_argument("--preset", default="default", choices=("default", "paper"))
    parser.add_argument("--max-seconds", type=float, default=None, help="只转写前 N 秒（调试用）")
    parser.add_argument("--overlap", type=float, default=None, help="窗口重叠秒数")
    parser.add_argument("--lookahead", type=float, default=None, help="窗口右向上下文秒数")
    parser.add_argument("--bpm", type=float, default=None,
                        help="手动指定速度（四分音符）；给出后记录模型网格相对它的漂移")
    parser.add_argument("--bpm-snap", default="none", choices=("none", "integer", "half"),
                        help="把估计速度吸附到整数 / 半整数")
    parser.add_argument("--local-files-only", action="store_true", help="只用本地缓存")
    parser.add_argument("--no-cpu-fallback", action="store_true", help="显存不足时不退回 CPU")
    parser.add_argument("--quiet", action="store_true", help="不打印逐窗口进度")
    return parser


def make_progress(quiet: bool):
    state = {"last": None, "decoded": 0}

    def progress(value):
        stage = value.get("stage")
        if stage == "encoding":
            print(f"  [窗口 {value['window']}/{value['windows']}] 开始，起点 {value['start']:.1f}s",
                  flush=True)
        elif stage == "decoding" and not quiet:
            tokens = value.get("tokens", 0)
            if tokens - state["decoded"] >= 64 or tokens < state["decoded"]:
                state["decoded"] = tokens
                print(f"    解码 {tokens} tokens", flush=True)
        elif stage == "window_complete":
            print(f"  [窗口 {value['window']}/{value['windows']}] 完成", flush=True)

    return progress


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    missing = [p for p in args.audio if not p.is_file()]
    if missing:
        for path in missing:
            print(f"找不到音频：{path}", file=sys.stderr)
        return 2

    engine = SheetSage2Engine(EngineConfig(
        model_id=args.model,
        device=args.device,
        dtype=args.dtype,
        preset=args.preset,
        local_files_only=args.local_files_only,
        cpu_fallback=not args.no_cpu_fallback,
    ))

    print(f"加载模型 {args.model} ...", flush=True)
    started = time.monotonic()
    try:
        engine.load()
    except TranscriptionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"模型就绪：device={engine.device}，耗时 {time.monotonic() - started:.1f}s", flush=True)

    summaries = []
    failures = 0
    for audio in args.audio:
        out_dir = args.out_root / _slug(audio)
        print(f"\n=== {audio.name} -> {out_dir} ===", flush=True)
        options: dict[str, object] = {}
        if args.max_seconds:
            options["max_seconds"] = args.max_seconds
        if args.overlap is not None:
            options["overlap_seconds"] = args.overlap
        if args.lookahead is not None:
            options["lookahead_seconds"] = args.lookahead

        try:
            payload = transcribe_to_directory(
                audio, out_dir, engine=engine, progress=make_progress(args.quiet),
                bpm=args.bpm, bpm_snap=args.bpm_snap, **options)
        except TranscriptionError as exc:
            failures += 1
            print(f"失败：{exc}", file=sys.stderr)
            continue

        summary = payload["summary"]
        tempo = payload["tempo"]
        estimate = tempo["estimate"]
        report = payload["artifacts"]
        summaries.append(summary)

        print(f"  时长 {summary['duration_seconds']:.1f}s，"
              f"耗时 {summary['elapsed_seconds']:.1f}s，"
              f"GPU 峰值 {summary['peak_gpu_mib']:.0f}MiB")
        print(f"  音符 melody={summary['notes']['melody']} "
              f"(vocal={summary['notes']['vocal']}, ins={summary['notes']['instrumental']})，"
              f"events={summary['events']}")
        print(f"  调性 {summary['keys']}，拍号 {summary['meter']}，"
              f"BPM={summary['bpm']}（朴素中位法 {summary['bpm_median_interval']}，"
              f"拍单位 {summary['beat_unit_bpm']}）")
        abc_bpm = summary.get("abc_bpm")
        if abc_bpm is None:
            print("  模型 Q 值：无（未生成 score.abc）")
        else:
            mark = "一致" if summary["bpm_agreement"] else "不一致"
            print(f"  模型 Q 值 {abc_bpm} vs 节拍网格拟合 {summary['bpm']}：{mark}"
                  f"（差 {summary['bpm_delta']:+}），采用 {summary['bpm_chosen']}"
                  f"（来源 {summary['bpm_source']}）")
        print(f"  恒速：{'是' if summary['tempo_constant'] else '否'}，"
              f"稳定段覆盖 {summary['tempo_coverage']:.1%}，"
              f"残差 max {summary['tempo_residual_max']}s")
        if not summary["tempo_constant"]:
            rng = summary.get("tempo_range") or {}
            print(f"  ! 该曲不是恒速曲：局部速度 {rng.get('min')}–{rng.get('max')} BPM"
                  f"（中位 {rng.get('median')}），主速度 {summary.get('bpm_dominant')}；"
                  f"任何单一 BPM 都只是摘要")
        abc_meter = summary.get("abc_meter")
        if abc_meter is not None:
            mark = "一致" if abc_meter == summary["meter"] else "不一致"
            print(f"  拍号交叉校验：beat.lab {summary['meter']} vs "
                  f"score.abc M: {abc_meter} -> {mark}")
        pickup = summary.get("pickup")
        if pickup:
            print(f"  弱起小节：{pickup['meter']}，{pickup['beats']} 拍，"
                  f"{pickup['start']}–{pickup['end']}s（{pickup['seconds']}s）")
        if not summary.get("meter_consistent", True):
            print("  ! 中途有转拍号")
        if args.bpm_snap != "none":
            print(f"  吸附到{args.bpm_snap}：{tempo['snapped_bpm']}")
        if "vs_target" in tempo:
            cmp = tempo["vs_target"]
            print(f"  指定 {args.bpm} BPM：模型网格最大漂移 {cmp['drift_max']}s，"
                  f"末拍 {cmp['drift_end']}s，平均 {cmp['drift_mean']}s")
        print(f"  段落 {summary['structure']}")
        print(f"  ABC 小节 {summary['abc_measures']}"
              + (f"（ABC 不可用：{summary['abc_error']}）" if summary["abc_error"] else ""))
        print(f"  产物检查：{'通过' if report['ok'] else '有问题 ' + str(report['problems'])}")

    if summaries:
        index_path = args.out_root / "index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n汇总写入 {index_path}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
