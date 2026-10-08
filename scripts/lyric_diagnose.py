r"""填词质量诊断：可以直接跑单首，也可以扫 output/ 下所有有歌词的工程。

    .\.venv\Scripts\python.exe scripts\lyric_diagnose.py
    .\.venv\Scripts\python.exe scripts\lyric_diagnose.py 毛阿敏_-_相思 --show

报告的字只能有四种下场，**绝不允许悄悄消失**：

| 下场 | 含义 | 算不算"看见" |
|---|---|---|
| 挂字（window / fallback） | 字写进了某个音的歌词位 | ✅ |
| 转音延续（continuation） | 字是前一个字的延续，写进 `-`/`+` | ✅ |
| 同音挤占（absorbed） | 那段旋律音比字少，音位放不下它，**登记可查** | ❌ |
| 间隙丢弃（dropped） | 字的起点落在音符之间的空档里，真的无音可挂 | ❌ |

"显示不出来" = 同音挤占 + 间隙丢弃。这正是修复前被静默吞掉的那部分。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from sheetsage2_webui.asr import AlignedChar  # noqa: E402
from sheetsage2_webui.lyrics import assign_lyrics, note_spans_seconds  # noqa: E402
from sheetsage2_webui.quantize import bar_quarters_from_timeline  # noqa: E402
from sheetsage2_webui.timeline import from_stored_measures  # noqa: E402

PPQ = 1920


def load(project_dir: Path):
    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    lyrics = json.loads((project_dir / "lyrics.json").read_text(encoding="utf-8"))
    chars = [AlignedChar(text=c["text"], start=c["start"], end=c["end"])
             for c in lyrics["chars"]]
    timeline = from_stored_measures(project["timeline"]["measures"])
    track = next((t for t in project["tracks"] if t["id"] == "vocal"), None)
    if track is None:
        return None
    return project, lyrics, chars, timeline, track


def measure(project_dir: Path, *, show: bool = False) -> dict | None:
    loaded = load(project_dir)
    if loaded is None:
        return None
    project, lyrics, chars, timeline, track = loaded
    spans = note_spans_seconds(timeline, track["notes"])
    fill = assign_lyrics(spans, chars)

    ref = Counter(c.text for c in chars)
    shown = Counter(t for t in fill.lyrics if t and t not in ("-", "+"))
    hidden = Counter()
    for text in fill.absorbed_chars + fill.dropped_chars:
        hidden[text] += 1

    row = {
        "song": project_dir.name,
        "notes": len(spans),
        "chars": len(chars),
        "zero_length": sum(1 for c in chars if c.end <= c.start + 1e-9),
        "assigned": fill.assigned,
        "melisma": fill.melisma,
        "unmatched_notes": fill.unmatched_notes,
        "absorbed": len(fill.absorbed_chars),
        "dropped": fill.dropped,
        "reasons": fill.reasons,
        "missing_from_display": "".join((ref - shown).elements()),
        "hidden": "".join(fill.absorbed_chars) + "|" + "".join(fill.dropped_chars),
        "warnings": len(fill.warnings),
    }

    if show:
        print(f"\n=== {project_dir.name} ===")
        print(f"音频 {project.get('duration_seconds')}s   拍号小节 {len(project['timeline']['measures'])}   "
              f"bar_quarters={bar_quarters_from_timeline(timeline):.2f}")
        print(f"音符 {row['notes']}   字 {row['chars']}   零长度字 {row['zero_length']}"
              f"（{row['zero_length'] / max(1, row['chars']):.0%}）")
        print(f"挂字 {row['assigned']}   转音 {row['melisma']}   没分到字的音 {row['unmatched_notes']}")
        print(f"同音挤占 {row['absorbed']}   间隙丢弃 {row['dropped']}   "
              f"-> 显示不出来 {row['absorbed'] + row['dropped']}")
        print(f"匹配方式 {row['reasons']}")
        if fill.absorbed_chars:
            print(f"  挤占的字：{''.join(fill.absorbed_chars)}")
        if fill.dropped_chars:
            print(f"  间隙丢弃：{''.join(fill.dropped_chars)}")
        print(f"  显示串缺少（挤占+丢弃）：{row['missing_from_display'] or '（无）'}")
        filled = [(round(spans[i][0], 2), t) for i, t in enumerate(fill.lyrics) if t]
        print(f"  抽样：{'  '.join(f'{a}:{b}' for a, b in filled[:24])}")
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="填词质量诊断")
    parser.add_argument("slugs", nargs="*", help="output/ 下的工程名；不给就扫全部有歌词的")
    parser.add_argument("--show", action="store_true", help="打印每首歌的细节")
    args = parser.parse_args(argv)

    root = PROJECT / "output"
    if args.slugs:
        dirs = [root / slug for slug in args.slugs]
    else:
        dirs = [d for d in sorted(root.iterdir())
                if d.is_dir() and (d / "lyrics.json").is_file()]

    rows = [row for row in (measure(d, show=args.show) for d in dirs) if row]
    if not rows:
        print("没有找到带 lyrics.json 的工程", file=sys.stderr)
        print("先跑：python scripts/asr_demo.py <工程名> --model Qwen3-ASR-1.7B --fill",
              file=sys.stderr)
        return 1

    print(f"\n{'工程':40s} {'音符':>5s} {'字':>5s} {'零长':>5s} "
          f"{'挂字':>5s} {'转音':>5s} {'挤占':>5s} {'间隙':>5s} {'看不到':>6s}")
    for row in rows:
        print(f"{row['song'][:38]:40s} {row['notes']:5d} {row['chars']:5d} "
              f"{row['zero_length']:5d} {row['assigned']:5d} {row['melisma']:5d} "
              f"{row['absorbed']:5d} {row['dropped']:5d} "
              f"{row['absorbed'] + row['dropped']:6d}")
    total_chars = sum(r["chars"] for r in rows)
    total_hidden = sum(r["absorbed"] + r["dropped"] for r in rows)
    print(f"\n合计：{len(rows)} 首歌 / {total_chars} 字，显示不出来 {total_hidden} "
          f"（{total_hidden / max(1, total_chars):.1%}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
