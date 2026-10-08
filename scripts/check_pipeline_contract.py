r"""把两个结论固定成可复现的检查：

    .\.venv\Scripts\python.exe scripts\check_pipeline_contract.py

1. **量化必须留下 `raw_tick` / `raw_dur`**，而且填词必须优先用它们。
   没有它们时，量化会让填词质量明显变差（实测《光芒》29→60、《相思》18→42）。
2. **填词的"看不到的字"比例应当与曲式无关**：恒速曲和变速曲都在 ~9% 量级。
   变速曲的意义在于——秒↔tick 是分段线性映射，量化位移和填词对齐都在这张表上做，
   如果结论只在恒速曲成立，说明哪里悄悄按单一 BPM 反推了。

任何一条不成立就返回非 0，顺便把数字打出来供人工核对。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from sheetsage2_webui.asr import AlignedChar  # noqa: E402
from sheetsage2_webui.lyrics import assign_lyrics, note_spans_seconds  # noqa: E402
from sheetsage2_webui.timeline import from_stored_measures  # noqa: E402

PPQ = 1920
#: 填词"看不到的字"比例的基准。它**取决于段落里字比音多多少**：
#: 字/音 ≈ 1.0（光芒、相思）时约 9%；字/音 1.36（巴拉莱卡，快嘴）时约 39%。
#: 所以判据是 `看不到比例 ≤ 基准 + 斜率 × max(0, 字/音 − 1)`。
BASE_HIDDEN_RATIO = 0.12
HIDDEN_SLOPE = 0.80


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def hidden_ratio(fill) -> float:
    total = fill.assigned + fill.dropped
    return (len(fill.absorbed_chars) + fill.dropped) / total if total else 0.0


def char_gap(chars) -> tuple[float, int]:
    """相邻字之间的最大空白（秒）及位置；`index == 0` 表示空白在第一个字之前（前奏）。

    前奏/尾奏的空白是无害的；只有夹在唱词中间的大空白才说明对齐器漏了音频。
    """
    worst, worst_at = 0.0, 0
    for index, (previous, current) in enumerate(zip(chars, chars[1:]), start=1):
        gap = current.start - previous.end
        if gap > worst:
            worst, worst_at = gap, index
    return worst, worst_at


def check_song(project_dir: Path) -> tuple[bool, dict]:
    project = load(project_dir / "project.json")
    lyrics = load(project_dir / "lyrics.json")
    chars = [AlignedChar(text=c["text"], start=c["start"], end=c["end"])
             for c in lyrics["chars"]]
    timeline = from_stored_measures(project["timeline"]["measures"])
    track = next(t for t in project["tracks"] if t["id"] == "vocal")
    notes = track["notes"]

    bpms = [m["bpm"] for m in project["timeline"]["measures"] if m["bpm"] > 0]
    gap, gap_at = char_gap(chars)
    row: dict = {
        "song": project_dir.name,
        "tempo_segments": len(project["timeline"]["tempo_map"]),
        "bpm_span": (round(min(bpms), 1), round(max(bpms), 1)) if bpms else None,
        "notes": len(notes),
        "chars": len(chars),
        "align_gap": round(gap, 2),
        "align_gap_at": gap_at,
        "problems": [],
    }
    # 字比音多多少：决定了"看不到"的基准
    row["chars_per_note"] = round(len(chars) / len(notes), 2) if notes else 0.0
    row["hidden_limit"] = BASE_HIDDEN_RATIO + HIDDEN_SLOPE * max(0.0, row["chars_per_note"] - 1.0)

    # ---- 1. 量化留下 raw_*，且填词用 raw_* ----
    has_raw = sum(1 for n in notes if "raw_tick" in n and "raw_dur" in n)
    row["raw_fields"] = f"{has_raw}/{len(notes)}"
    if has_raw != len(notes):
        row["problems"].append(f"只有 {has_raw}/{len(notes)} 个音符带 raw_tick/raw_dur")

    fixed = assign_lyrics(note_spans_seconds(timeline, notes), chars)
    row["hidden_with_raw"] = len(fixed.absorbed_chars) + fixed.dropped

    stripped = [{"tick": n["tick"], "dur": n["dur"], "pitch": n["pitch"]} for n in notes]
    broken = assign_lyrics(note_spans_seconds(timeline, stripped), chars)
    row["hidden_without_raw"] = len(broken.absorbed_chars) + broken.dropped

    row["hidden_ratio"] = round(hidden_ratio(fixed), 4)
    if row["hidden_ratio"] > row["hidden_limit"]:
        # 两种可能：段落里确实字比音多（快嘴），或者这份对齐本身有问题。
        # 把最大空白一起报出来，让人能分辨——对齐坏掉时那句"看不到"就说明不了任何事。
        row["problems"].append(
            f"看不到的字 {row['hidden_ratio']:.1%} 超过基准 "
            f"{row['hidden_limit']:.1%}（字/音 {row['chars_per_note']}，"
            f"最大空白 {row['align_gap']}s@{row['align_gap_at']}）：先确认这份对齐是否可信")
    # 核心不变量：带着 raw_* 时，量化不该让填词变差
    if has_raw == len(notes) and row["hidden_with_raw"] > row["hidden_without_raw"]:
        row["problems"].append(
            f"带 raw_* 反而更差（{row['hidden_with_raw']} > "
            f"{row['hidden_without_raw']}），raw_* 可能没被用上")

    # ---- 2. 量化位移（在分段线性映射上算，和 BPM 无关） ----
    raw = note_spans_seconds(timeline, notes)
    shifts = sorted(abs(a[0] - b[0]) for a, b in zip(raw, note_spans_seconds(timeline, stripped)))
    row["shift_median"] = round(shifts[len(shifts) // 2], 4)
    row["shift_max"] = round(shifts[-1], 4)

    # ---- 3. 往返自洽：raw_tick 换回秒应当等于原音符的秒 ----
    #     还没跑过新流水线的工程没有 raw_*，这时跳过（第 1 项已经报了）。
    worst = 0.0
    for note in notes:
        if "raw_tick" not in note:
            continue
        seconds = timeline.tick_to_sec(note["raw_tick"])
        back = timeline.sec_to_tick(seconds)
        worst = max(worst, abs(back - note["raw_tick"]))
    row["round_trip_ticks"] = round(worst, 6)
    if has_raw and worst > 1e-3:
        row["problems"].append(f"raw_tick 往返误差 {worst} tick 过大")

    return not row["problems"], row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定量化/填词的流水线约定")
    parser.add_argument("slugs", nargs="*", help="output/ 下的工程名；默认扫全部有歌词的")
    args = parser.parse_args(argv)

    root = PROJECT / "output"
    dirs = ([root / slug for slug in args.slugs] if args.slugs
            else [d for d in sorted(root.iterdir())
                  if d.is_dir() and (d / "lyrics.json").is_file()])
    if not dirs:
        print("没有带 lyrics.json 的工程", file=sys.stderr)
        return 1

    ok_all = True
    print(f"{'工程':32s} {'速度段':>4s} {'BPM 范围':>14s} {'字/音':>5s} {'字':>5s} "
          f"{'raw':>8s} {'看不到':>5s} {'占比':>6s} {'基准':>6s} {'无raw':>6s} "
          f"{'对齐空':>6s} {'位移max':>8s}")
    for directory in dirs:
        ok, row = check_song(directory)
        ok_all &= ok
        span = f"{row['bpm_span'][0]}~{row['bpm_span'][1]}" if row["bpm_span"] else "-"
        print(f"{row['song'][:30]:32s} {row['tempo_segments']:4d} {span:>14s} "
              f"{row['chars_per_note']:5.2f} {row['chars']:5d} {row['raw_fields']:>8s} "
              f"{row['hidden_with_raw']:5d} {row['hidden_ratio']:6.1%} "
              f"{row['hidden_limit']:6.1%} {row['hidden_without_raw']:6d} "
              f"{row['align_gap']:6.1f} {row['shift_max']:8.3f}")
        for problem in row.get("problems", []):
            print(f"    !! {problem}")

    print(f"\n结论：{'全部通过' if ok_all else '有不通过项'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
