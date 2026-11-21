#!/usr/bin/env python3
"""挂字评测 CLI：对样本目录跑 alignment-only，不加载模型、不需要音频。

    python scripts/bench_score.py --root "samples/对齐/中"
    python scripts/bench_score.py --root "samples/对齐/中" --offset 0
    python scripts/bench_score.py --root "samples/对齐/中" --json reports/bench_zh.json
    python scripts/bench_score.py --root "samples/对齐/中" --include-stress

口径见 `src/sheetsage2_webui/bench.py`：人工重复字会折叠成延音；每首默认先搜一个
全局 offset（manual vs lyrics.json），再报 content / exact / ±1 / marker。

`meta.json` 里 `split == "stress"` 的样本（对唱/合唱这类音符层就选错旋律的）**不计入
主指标**，单独一行汇总。另外，人工谱与 `lyrics.json` 文本相似度 < **70%** 的样本
（整句都不是同一份歌词）也单列——那种样本的 content/marker 量的是文本差异；
70%~90% 仍进主指标，但会打告警。`--include-stress` 才把 stress 并进主指标
（文本 <70% 的样本始终单列）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sheetsage2_webui.bench import (  # noqa: E402
    DEFAULT_MAX_OFFSET,
    DEFAULT_OFFSET_STEP,
    partition,
    pending,
    scan,
    summarize,
)


def _print_row(row: dict) -> None:
    name = row["name"]
    if len(name) > 30:
        name = name[:29] + "…"
    marker = (f"{row['marker_hit']}/{row['markers']}"
              if row["markers"] else "-")
    print(f"{name:32s} {row['offset']:+7.2f} {row['heads']:6d} "
          f"{row['content']:4d}/{row['heads']:<3d} "
          f"{row['exact']:4d}/{row['heads']:<3d} "
          f"{row['pm1']:4d}/{row['heads']:<3d} {marker:>8s} "
          f"{row['missing']:5d} "
          f"{row['absorbed']:4d}/{row['dropped']:<4d}")
    if row.get("adlib_excluded"):
        print(f"  · ad-lib 剔除 {row['adlib_excluded']} 个（oh/wow/yeah 这类，文本源里没有）")
    for warning in row.get("warnings") or []:
        print(f"  ! {warning}")


def _print_summary(label: str, rows: list[dict]) -> dict:
    stats = summarize(rows)
    print(f"\n{label} {stats['songs']} 首  head {stats['content']}/{stats['heads']} "
          f"({stats['content_ratio']:.1%})  "
          f"exact {stats['exact']}/{stats['heads']} ({stats['exact_ratio']:.1%})  "
          f"±1 {stats['pm1']}/{stats['heads']} ({stats['pm1_ratio']:.1%})  "
          f"marker {stats['marker_hit']}/{stats['markers']} "
          f"({stats['marker_ratio']:.1%})  "
          f"marker_any {stats['marker_any_ratio']:.1%}")
    if stats["songs"]:
        print(f"      歌级均值（每首等权）  exact {stats['exact_song_mean']:.1%} "
              f"± {stats['exact_song_sd']:.1%}（sd），最差 {stats['exact_song_min']:.1%}")
        print(f"      hidden {stats['missing']}/{stats['heads']} "
              f"({stats['hidden_ratio']:.1%})   "
              f"|Δ|>1s {stats['boundary_gt1s']}/{stats['heads']} "
              f"({stats['boundary_ratio']:.1%})")
    return stats


def _print_diagnosis(rows: list[dict]) -> None:
    """Δ 分布 + 上下文切片：回答「差的那一个音集中在哪种结构上」。"""
    total: dict[str, int] = {}
    for row in rows:
        for key, value in (row.get("delta_hist") or {}).items():
            total[key] = total.get(key, 0) + value
    if not total:
        return
    order = ["<=-3", "-2", "-1", "0", "1", "2", ">=3"]
    head = sum(total.get(key, 0) for key in order)
    print("\n--- 诊断：生成头相对人工音的下标偏差 Δ ---")
    print("  " + "  ".join(f"{key}:{total.get(key, 0)}" for key in order))
    off = head - total.get("0", 0)
    print(f"  命中 {total.get('0', 0)}/{head}，偏差 {off}（其中 "
          f"-1:{total.get('-1', 0)}  +1:{total.get('1', 0)}）")

    buckets: dict[str, list[int]] = {}
    for row in rows:
        for name, (exact, count) in (row.get("slices") or {}).items():
            item = buckets.setdefault(name, [0, 0])
            item[0] += exact
            item[1] += count
    print("\n--- 诊断：上下文切片（exact 率，总体 "
          f"{total.get('0', 0) / max(1, head):.1%}）---")
    for name, (exact, count) in sorted(buckets.items(),
                                       key=lambda item: -item[1][1]):
        base = total.get("0", 0) / max(1, head)
        mark = "↑" if exact / max(1, count) > base + 0.03 else (
            "↓" if exact / max(1, count) < base - 0.03 else " ")
        print(f"  {name:14s} {exact:5d}/{count:<5d} = {exact / max(1, count):6.1%} {mark}")


def _print_product(rows: list[dict]) -> None:
    """端到端口径：自动音符 + 自动字时间，映射回人工谱再比。"""
    have = [row for row in rows if row.get("product")]
    if not have:
        return
    heads = sum(row["product"]["heads"] for row in have)
    content = sum(row["product"]["content"] for row in have)
    exact = sum(row["product"]["exact"] for row in have)
    pm1 = sum(row["product"]["pm1"] for row in have)
    rates = [row["product"]["note_match_rate"] for row in have]
    print(f"\n--- PRODUCT {len(have)} 首（自动音符 + 自动字时间，端到端）---")
    print(f"  content {content}/{heads} ({content / max(1, heads):.1%})  "
          f"exact {exact}/{heads} ({exact / max(1, heads):.1%})  "
          f"±1 {pm1}/{heads} ({pm1 / max(1, heads):.1%})  "
          f"音符映射率 {sum(rates) / len(rates):.1%}")
    for row in sorted(have, key=lambda item: item["product"]["exact"]
                      / max(1, item["product"]["heads"]))[:5]:
        product = row["product"]
        print(f"    {row['name'][:26]:28s} exact "
              f"{product['exact']}/{product['heads']} "
              f"({product['exact'] / max(1, product['heads']):.1%})  "
              f"映射 {product['note_match_rate']:.0%}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="挂字 alignment-only 评测")
    parser.add_argument("--root", type=Path, default=ROOT / "samples" / "对齐" / "中",
                        help="样本根目录（默认 samples/对齐/中）")
    parser.add_argument("--offset", type=float, default=None,
                        help="固定全局 offset（秒）；不传则每首自动搜")
    parser.add_argument("--max-offset", type=float, default=DEFAULT_MAX_OFFSET)
    parser.add_argument("--step", type=float, default=DEFAULT_OFFSET_STEP)
    parser.add_argument("--include-stress", action="store_true",
                        help="把 split=stress 的样本并进主指标（默认单列）")
    parser.add_argument("--diagnose", action="store_true",
                        help="额外输出逐 token 的 Δ 分布与上下文切片")
    parser.add_argument("--product", action="store_true",
                        help="额外输出端到端口径（自动音符 + 自动字时间）")
    parser.add_argument("--json", type=Path, default=None,
                        help="把逐首结果写成 JSON")
    args = parser.parse_args(argv)

    rows = scan(args.root, offset=args.offset, max_abs=args.max_offset,
                step=args.step, diagnose=args.diagnose, product=args.product)
    waits = pending(args.root)
    if not rows:
        print(f"{args.root} 下没有可评的样本（需要 project.json/lyrics.json/manual.svp）",
              file=sys.stderr)
        if waits:
            print(f"待补 manual.svp：{len(waits)} 个 — " + "、".join(waits),
                  file=sys.stderr)
        return 1

    main_rows, stress_rows, text_rows = partition(
        rows, include_stress=args.include_stress)

    print(f"{'歌名':32s} {'offset':>7s} {'heads':>6s} {'content':>8s} "
          f"{'exact':>7s} {'±1':>7s} {'marker':>8s} {'miss':>5s} "
          f"{'abs/drop':>9s}")
    for row in main_rows:
        _print_row(row)

    if not main_rows:
        print("（主指标为空：全部样本都在 stress 或文本不一致里）")

    stats = _print_summary("TOTAL", main_rows) if main_rows else summarize([])

    if args.diagnose and main_rows:
        _print_diagnosis(main_rows)

    if args.product and main_rows:
        _print_product(main_rows)

    if text_rows:
        print("\n--- 文本不一致（单列，不进主指标：人工词与 lyrics.json 不是同一份）---")
        for row in text_rows:
            _print_row(row)
        _print_summary("TEXT-MISMATCH", text_rows)

    if stress_rows and not args.include_stress:
        print("\n--- stress（单列，不计入主指标：音符层就选错旋律，挂字层做不到）---")
        for row in stress_rows:
            _print_row(row)
        _print_summary("STRESS", stress_rows)

    shares = [(row, float(row.get("triplet_share") or 0.0)) for row in main_rows]
    heavy = [(row, share) for row, share in shares if share >= 0.2]
    light = [(row, share) for row, share in shares if share < 0.2]
    if heavy:
        print("\n--- 三连音桶（人工谱 ≥20% 间隔在三连音位，模型词汇表表示不了）---")
        for row, share in heavy:
            print(f"  {row['name'][:30]:32s} 三连音间隔 {share:.1%}  exact {row['exact']/row['heads']:.1%}")
        print(f"  三连音重 {sum(r['exact'] for r,_ in heavy)}/{sum(r['heads'] for r,_ in heavy)} = "
              f"{sum(r['exact'] for r,_ in heavy)/max(1,sum(r['heads'] for r,_ in heavy)):.1%}"
              f"   非三连音 {sum(r['exact'] for r,_ in light)}/{sum(r['heads'] for r,_ in light)} = "
              f"{sum(r['exact'] for r,_ in light)/max(1,sum(r['heads'] for r,_ in light)):.1%}")

    if waits:
        print(f"\n待补 manual.svp：{len(waits)} 个 — " + "、".join(waits))

    if args.json is not None:
        payload = {"main": stats,
                   "stress": summarize(stress_rows) if stress_rows else None,
                   "text_mismatch": (summarize(text_rows) if text_rows else None),
                   "rows": rows,
                   "pending": waits}
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
