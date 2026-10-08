"""探针：Qwen3-ForcedAligner 直接吃"参考歌词"能不能消掉零长度时间戳。

对照：现有 lyrics.json 走的是 ASR 文本 -> 对齐器（324 字里 53 个零长度）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

import sheetsage2_webui  # noqa: F401  先准备 ffmpeg / OMP

from sheetsage2_webui.asr import Section, sections_from_lab  # noqa: E402
from sheetsage2_webui.bootstrap import find_ffmpeg  # noqa: E402

MODELS = PROJECT / "models"
SLUG = "凤凰传奇_-_光芒"
AUDIO = PROJECT / "samples" / "凤凰传奇 - 光芒.flac"
project_dir = PROJECT / "output" / SLUG


def zero_ratio(items):
    if not items:
        return 0.0
    return sum(1 for i in items if i["end"] <= i["start"] + 1e-9) / len(items)


def main() -> int:
    import subprocess
    import torch
    from qwen_asr.inference.qwen3_forced_aligner import Qwen3ForcedAligner

    reference = json.loads((project_dir / "lyrics.json").read_text(encoding="utf-8"))
    sections = sections_from_lab((project_dir / "structure.lab").read_text(encoding="utf-8"),
                                 max_seconds=90.0)
    print(f"切片 {len(sections)} 个，总时长 {sum(s.seconds for s in sections):.1f}s")
    chars = reference["chars"]
    print(f"参考歌词 {len(chars)} 字，零长度 {zero_ratio(chars):.0%}")

    print("\n加载对齐器 ...")
    started = time.monotonic()
    aligner = Qwen3ForcedAligner.from_pretrained(
        str(MODELS / "Qwen3-ForcedAligner-0.6B"),
        dtype=torch.bfloat16, device_map="cuda:0")
    print(f"加载 {time.monotonic() - started:.1f}s")

    ffmpeg = find_ffmpeg()
    work = PROJECT / "tmp" / "probe-fa"
    work.mkdir(parents=True, exist_ok=True)

    # 取前 3 个切片做对照
    for index, section in enumerate(sections[:3]):
        # 参考歌词里落在该切片内的字
        subset = [c for c in chars if section.start <= c["start"] < section.end]
        if not subset:
            continue
        text = "".join(c["text"] for c in subset)
        clip = work / f"slice-{index:03d}.wav"
        subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", f"{section.start:.3f}",
                        "-to", f"{section.end:.3f}", "-i", str(AUDIO),
                        "-ac", "1", "-ar", "16000", str(clip)], check=True)

        started = time.monotonic()
        result = aligner.align(audio=str(clip), text=text, language="Chinese")
        items = [dict(text=i.text, start=round(i.start_time, 3), end=round(i.end_time, 3))
                 for i in result[0]]
        elapsed = time.monotonic() - started

        ref_subset = [dict(text=c["text"], start=c["start"], end=c["end"])
                      for c in subset]
        print(f"\n切片 {index}  {section.start:.1f}-{section.end:.1f}s  "
              f"参考 {len(ref_subset)} 字 / 对齐器吐出 {len(items)} 条，耗时 {elapsed:.1f}s")
        print(f"   参考文本零长度占比 {zero_ratio(ref_subset):.0%}  ->  "
              f"对齐器零长度占比 {zero_ratio(items):.0%}")
        print(f"   前 6 条：{[(i['text'], i['start'], i['end']) for i in items[:6]]}")
        # 逐字比较起点差
        diffs = []
        for a, b in zip(ref_subset, items):
            if a["text"] == b["text"]:
                diffs.append(abs(a["start"] - b["start"]))
        if diffs:
            diffs.sort()
            print(f"   与现有时间戳的起点差（同字）：中位 {diffs[len(diffs)//2]:.3f}s  "
                  f"最大 {diffs[-1]:.3f}s  n={len(diffs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
