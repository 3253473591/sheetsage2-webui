"""用已扒好的工程的区间划分跑一遍歌词识别。

    python scripts/asr_demo.py "凤凰传奇_-_光芒"
    python scripts/asr_demo.py "凤凰传奇_-_光芒" --model 0.6B --no-aligner
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from sheetsage2_webui.asr import AsrConfig, AsrEngine, sections_from_lab  # noqa: E402

MODELS = PROJECT / "models"
SAMPLES = PROJECT / "samples"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 SheetSage2 区间跑歌词识别")
    parser.add_argument("slug", help="output/ 下的工程目录名")
    parser.add_argument("--model", default="Qwen3-ASR-0.6B")
    parser.add_argument("--aligner", default="Qwen3-ForcedAligner-0.6B")
    parser.add_argument("--max-slice", type=float, default=90.0)
    parser.add_argument("--language", default="Chinese")
    parser.add_argument("--no-aligner", action="store_true", help="只要文本，不要时间戳")
    parser.add_argument("--fill", action="store_true",
                        help="把逐字时间戳填进 project.json 的人声轨并落盘")
    parser.add_argument("--track", default="vocal", help="要填词的轨道 id")
    parser.add_argument("--tolerance", type=float, default=0.20, help="填词容差（秒）")
    parser.add_argument("--only-empty", action="store_true", help="只填目前为空的音符")
    parser.add_argument("--audio", type=Path, default=None, help="默认按工程里的音频名到 samples/ 找")
    args = parser.parse_args(argv)

    project_dir = PROJECT / "output" / args.slug
    structure = project_dir / "structure.lab"
    if not structure.is_file():
        print(f"找不到 {structure}", file=sys.stderr)
        return 2

    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    audio = args.audio or (SAMPLES / project["audio"])
    if not audio.is_file():
        print(f"找不到音频 {audio}", file=sys.stderr)
        return 2

    sections = sections_from_lab(structure.read_text(encoding="utf-8"),
                                 max_seconds=args.max_slice)
    print(f"工程 {args.slug}   音频 {audio.name} {project['duration_seconds']:.1f}s")
    print(f"structure.lab 合并后得到 {len(sections)} 个切片：")
    for section in sections:
        label = f"  [{section.label}]" if section.label else ""
        print(f"   {section.start:7.1f}-{section.end:7.1f}s  {section.seconds:5.1f}s{label}")

    config = AsrConfig(
        asr_model=MODELS / args.model,
        aligner_model=None if args.no_aligner else MODELS / args.aligner,
        language=args.language,
        max_slice_seconds=args.max_slice,
    )
    engine = AsrEngine(config)
    started = time.monotonic()
    engine.load()
    print(f"\n模型加载 {time.monotonic() - started:.1f}s")

    def progress(value):
        print(f"   [{value['index']}/{value['total']}] "
              f"{value['start']:.1f}-{value['end']:.1f}s ...", flush=True)

    run = engine.transcribe_sections(audio, sections, progress=progress)
    engine.release()

    print(f"\n耗时 {run.elapsed_seconds:.1f}s   切片 {run.slices}/{len(sections)}   "
          f"GPU 峰值 {run.peak_gpu_mib:.0f}MiB   语言 {run.language}")
    print(f"零长度时间戳 {run.zero_length_chars}/{len(run.chars)}"
          f"（{run.zero_length_ratio:.0%}）——对齐器对这些字给不出时间，填词只能按邻近音符猜")
    print(f"相邻字最大空白 {run.max_gap_seconds:.1f}s（第 {run.max_gap_at + 1} 个字之前）"
          f"   时间戳覆盖 {run.text_coverage:.0%} 的识别文本")
    print(f"\n歌词（{len(run.text)} 字）：\n{run.text}")
    if run.chars:
        print(f"\n逐字时间戳 {len(run.chars)} 条，前 8 条：")
        for item in run.chars[:8]:
            print(f"   {item.text}  {item.start:7.2f} - {item.end:7.2f}s")
    for warning in run.warnings:
        print(f"! {warning}")

    if args.fill:
        if not run.chars:
            print("\n没有时间戳，无法填词", file=sys.stderr)
            return 1
        from sheetsage2_webui.lyrics import fill_project

        result = fill_project(project, run.chars, track_id=args.track,
                              tolerance=args.tolerance, only_empty=args.only_empty)
        if not result["found"]:
            print(f"\n填词失败：{result['reason']}", file=sys.stderr)
            return 1
        (project_dir / "project.json").write_text(
            json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        (project_dir / "lyrics.json").write_text(
            json.dumps({"text": run.text, "fill": result["fill"],
                        "chars": [c.as_dict() for c in run.chars]},
                       ensure_ascii=False, indent=2), encoding="utf-8")

        stats = result["fill"]
        print(f"\n已填进 {args.track} 轨（{result['notes']} 个音）：")
        print(f"   挂字 {stats['assigned']}   转音标记 {stats['melisma']}   "
              f"没分到字 {stats['unmatched_notes']}   "
              f"间隙丢弃 {stats['dropped']}   同音挤占 {stats['absorbed']}")
        if stats["dropped_chars"]:
            print(f"   落在音符间隙、没挂上：{''.join(stats['dropped_chars'])}")
        if stats["absorbed_chars"]:
            print(f"   和别的字挤在同一个音上（显示不出来）：{''.join(stats['absorbed_chars'])}")
        print(f"   匹配方式：{stats['reasons']}")
        raw = sum(1 for n in next(t for t in project["tracks"]
                                  if t["id"] == args.track)["notes"] if "raw_tick" in n)
        print(f"   匹配用的是{'量化前的位置（raw_tick）' if raw else '当前 tick'}")
        notes = project["tracks"][0]["notes"] if False else next(
            t for t in project["tracks"] if t["id"] == args.track)["notes"]
        filled = [(n["tick"], n["lyric"]) for n in notes if n.get("lyric")]
        print(f"   前 20 个：{' '.join(text for _, text in filled[:20])}")
        print(f"   写入 {project_dir / 'project.json'} 和 lyrics.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
