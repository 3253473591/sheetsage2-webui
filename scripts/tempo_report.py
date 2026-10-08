"""速度报告与干预：对已有转写产物做 BPM 的估计、吸附与恒速化评估。

用法：
    python scripts/tempo_report.py                        # 报告 output/ 下所有产物
    python scripts/tempo_report.py --bpm 104              # 指定目标速度，评估漂移
    python scripts/tempo_report.py --snap integer         # 顺便给出吸附值
    python scripts/tempo_report.py --apply --bpm 104      # 把恒速网格写进产物
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.result import read_rows  # noqa: E402
from sheetsage2_webui.tempo import (  # noqa: E402
    abc_tempo,
    compare_to_constant,
    constant_beat_times,
    estimate_tempo,
    reconcile,
    retime,
    snap_bpm,
)


def analyse(beat_lab: Path, target_bpm: float | None, snap: str) -> dict[str, Any]:
    rows = read_rows(beat_lab)
    estimate = estimate_tempo(rows)
    times = sorted(float(r[0]) for r in rows if r)

    # 模型自带的 BPM：只有生成出 score.abc 时才有
    abc_path = beat_lab.parent / "score.abc"
    abc_bpm = abc_tempo(abc_path.read_text(encoding="utf-8", errors="replace")) \
        if abc_path.is_file() else None

    data: dict[str, Any] = {
        "file": str(beat_lab),
        "estimate": estimate.as_dict(),
        "cross_check": reconcile(abc_bpm, estimate),
        "snapped_bpm": snap_bpm(estimate.bpm, snap),
    }
    if estimate.bpm and estimate.denominator and len(times) >= 2:
        data["snapped_to_integer"] = snap_bpm(estimate.bpm, "integer")
        data["snapped_to_half"] = snap_bpm(estimate.bpm, "half")
        data["vs_estimate"] = compare_to_constant(
            times, estimate.anchor or times[0], estimate.bpm, estimate.denominator)

    if target_bpm and estimate.bpm and estimate.denominator:
        data["vs_target"] = compare_to_constant(
            times, estimate.anchor or times[0], target_bpm, estimate.denominator)
        # 把模型网格整体缩放到目标速度，看看会动多少
        ratio = estimate.bpm / target_bpm
        _, shift = retime(times, ratio, anchor=estimate.anchor or times[0])
        data["retime_shift"] = shift
    return data


def apply_constant_grid(beat_lab: Path, bpm: float, destination: Path) -> dict[str, Any]:
    """把拍网格按恒定速度重写一份（不动原产物，写到 destination）。"""
    rows = read_rows(beat_lab)
    estimate = estimate_tempo(rows)
    if not estimate.denominator:
        raise SystemExit("拍号缺失，无法生成恒速拍网格")
    times = [float(r[0]) for r in rows if r]
    anchor = estimate.anchor if estimate.anchor is not None else min(times)
    grid = constant_beat_times(anchor, bpm, estimate.denominator, len(times))

    numerator, denominator = estimate.meter.split("/")
    lines = []
    for index, (original, new) in enumerate(zip(sorted(times), grid)):
        position = index % int(numerator) + 1
        lines.append(f"{new:.6f}\t{position}\t{numerator}\t{denominator}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return compare_to_constant(sorted(times), anchor, bpm, estimate.denominator)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SheetSage2 速度报告与干预")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "output")
    parser.add_argument("--bpm", type=float, default=None, help="目标速度（四分音符）")
    parser.add_argument("--snap", default="none", choices=("none", "integer", "half"))
    parser.add_argument("--apply", action="store_true", help="按 --bpm 写出恒速拍网格")
    parser.add_argument("--json", type=Path, default=None, help="把报告写成 JSON")
    args = parser.parse_args(argv)

    if args.apply and not args.bpm:
        parser.error("--apply 需要同时给出 --bpm")

    labs = sorted(args.output_root.glob("*/beat.lab"))
    if not labs:
        print(f"{args.output_root} 下没有找到 beat.lab", file=sys.stderr)
        return 2

    reports = []
    for lab in labs:
        report = analyse(lab, args.bpm, args.snap)
        reports.append(report)
        est = report["estimate"]
        naive = est["bpm_median_interval"]
        robust = est["bpm"]

        print(f"\n=== {lab.parent.name} ===")
        print(f"  拍号 {est['meter']}，拍点 {est['beats_total']} 个，"
              f"稳定段 {est['regular_start']}–{est['regular_end']}s "
              f"（覆盖 {est['regular_coverage']:.1%}）")
        print(f"  鲁棒 BPM（最小二乘） : {robust}"
              + (f"     [朴素中位间隔法：{naive}，差 {naive - robust:+.3f}]"
                 if naive and robust else ""))
        cross = report["cross_check"]
        if cross["abc_bpm"] is None:
            print("  模型 Q 值            : 无（未生成 score.abc，只能用拟合值）")
        else:
            mark = "一致" if cross["agreement"] else "**不一致**"
            print(f"  模型 Q 值            : {cross['abc_bpm']}  vs 拟合 {cross['beat_bpm']}"
                  f"  ->  {mark}（差 {cross['delta']:+}）")
            print(f"  实际采用             : {cross['chosen_bpm']}（来源 {cross['source']}）"
                  + ("" if cross["confident"] else "  [该曲非恒速，此值仅供参考]"))
        if est["tempo_range"]:
            rng = est["tempo_range"]
            print(f"  局部速度             : {rng['min']}–{rng['max']} BPM"
                  f"（中位 {rng['median']}），主速度 {est['bpm_dominant']}")
        print(f"  拍单位 BPM           : {est['beat_unit_bpm']}")
        print(f"  残差 max {est['residual_max']}s  std {est['residual_std']}s  "
              f"可视为恒速：{'是' if est['is_constant'] else '否'}")
        print(f"  吸附：整数 {report['snapped_to_integer']}，半整数 {report['snapped_to_half']}")
        if "vs_estimate" in report:
            cmp = report["vs_estimate"]
            print(f"  对齐到自身速度网格：最大漂移 {cmp['drift_max']}s，末拍 {cmp['drift_end']}s")
        if "vs_target" in report:
            cmp = report["vs_target"]
            print(f"  对齐到目标 {args.bpm} BPM：最大漂移 {cmp['drift_max']}s，"
                  f"末拍 {cmp['drift_end']}s，平均 {cmp['drift_mean']}s")
            print(f"  整体缩放到位移：最大 {report['retime_shift']['shift_max']}s，"
                  f"平均 {report['retime_shift']['shift_mean']}s")
        for warning in est["warnings"]:
            print(f"  ! {warning}")

        if args.apply:
            destination = lab.parent / "beat.constant.lab"
            info = apply_constant_grid(lab, args.bpm, destination)
            print(f"  已写出恒速拍网格 -> {destination}（最大漂移 {info['drift_max']}s）")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
