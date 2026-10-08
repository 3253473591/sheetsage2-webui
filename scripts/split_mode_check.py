r"""填词 ③ 切分模式的对照：值不值得改音符？

背景：现在的填词走 **onset** 模式（字往音上挂，音符一个都不动），对应 Vocal2Midi 的英文路径；
他们的中文路径是 **切分**——按字的时间边界把音符切开，一个字拿到自己的一段。切分能让
"一音多字"（模型没扒出那么细的音）显示出来，代价是**音符表被改写**（新增的音是模型没扒出来的
音位，是我们按时间戳补的），而零长度时间戳的字没有边界可切。

这个脚本不碰 GPU、不跑模型：它拿 `output/<工程>/lyrics.json` 里已经识别好的逐字时间戳，
对 `project.json` 的人声轨分别跑两种模式，把代价摊开来看。

    .\.venv\Scripts\python.exe scripts\split_mode_check.py
    .\.venv\Scripts\python.exe scripts\split_mode_check.py --only 光芒 相思
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from sheetsage2_webui.lyrics import assign_lyrics, assign_lyrics_split, note_spans_seconds  # noqa: E402
from sheetsage2_webui.timeline import from_stored_measures  # noqa: E402

OUTPUT = PROJECT / "output"
REPORT = PROJECT / "reports" / "切分模式对照.md"


@dataclass
class Char:
    """`lyrics.json` 里的字 -> `assign_lyrics` 要的形状。"""

    text: str
    start: float
    end: float


def load_case(directory: Path) -> dict | None:
    project_path = directory / "project.json"
    lyrics_path = directory / "lyrics.json"
    if not project_path.is_file() or not lyrics_path.is_file():
        return None
    project = json.loads(project_path.read_text(encoding="utf-8"))
    lyrics = json.loads(lyrics_path.read_text(encoding="utf-8"))
    chars = [Char(str(item["text"]), float(item["start"]), float(item["end"]))
             for item in lyrics.get("chars") or [] if str(item.get("text") or "").strip()]
    if not chars:
        return None
    vocal = next((t for t in project.get("tracks") or [] if t.get("id") == "vocal"), None)
    if not vocal or not vocal.get("notes"):
        return None
    timeline = from_stored_measures(project["timeline"]["measures"])
    spans = note_spans_seconds(timeline, vocal["notes"])
    return {"name": directory.name, "chars": chars, "spans": spans,
            "duration": project.get("duration_seconds"),
            "zero": sum(1 for c in chars if c.end <= c.start + 1e-9)}


def analyse(case: dict) -> dict:
    base = assign_lyrics(case["spans"], case["chars"])
    split = assign_lyrics_split(case["spans"], case["chars"])
    # 现模式里"显示不出来"的字：被挤在同一个音上的那些
    invisible_now = len(base.absorbed_chars)
    return {
        "name": case["name"],
        "notes": len(case["spans"]),
        "chars": len(case["chars"]),
        "zero": case["zero"],
        "now_invisible": invisible_now,
        "now_dropped": base.dropped,
        "now_melisma": base.melisma,
        "split_visible": split.visible,
        "split_absorbed": split.absorbed,
        "split_dropped": split.dropped,
        "split_cut": split.cut_notes,
        "split_added": split.added_notes,
        "split_ratio": split.ratio,
        "split_notes_after": len(split.notes),
        "split_untouched": split.untouched_notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="填词切分模式对照（离线，不跑模型）")
    parser.add_argument("--only", nargs="*", default=None, help="只跑名字里含这些串的工程")
    parser.add_argument("--no-report", action="store_true", help="不写 reports/切分模式对照.md")
    args = parser.parse_args(argv)

    cases = []
    for directory in sorted(OUTPUT.iterdir()):
        if not directory.is_dir():
            continue
        if args.only and not any(key in directory.name for key in args.only):
            continue
        case = load_case(directory)
        if case:
            cases.append(case)
    if not cases:
        print("output/ 下没有同时具备 project.json 和 lyrics.json 的工程", file=sys.stderr)
        return 2

    rows = [analyse(case) for case in cases]
    header = ("工程", "音", "字", "零长度", "现:挤占", "切分:可见", "切分:挤占",
              "切不开", "切开音", "新增音", "改动")
    widths = (26, 5, 5, 7, 8, 10, 10, 7, 8, 8, 7)
    lines = ["  ".join(title.ljust(width) for title, width in zip(header, widths))]
    for row in rows:
        lines.append("  ".join(str(value).ljust(width) for value, width in zip(
            (row["name"][:26], row["notes"], row["chars"], row["zero"],
             row["now_invisible"], row["split_visible"], row["split_absorbed"],
             row["split_dropped"], row["split_cut"], row["split_added"],
             f"{row['split_ratio']:.1%}"), widths)))
    report = "\n".join(lines)
    print(report)

    print("\n怎么看这些数：")
    print("  · 现:挤占 = onset 模式下和别的字挤在同一个音上、歌词串里显示不出来的字数")
    print("  · 切分:可见 = 切分模式下真的拿到自己那一段的字数（挤占应该趋近于 0）")
    print("  · 切不开 = 窗口落在已被切走的区间里、切分也救不了的字")
    print("  · 新增音/改动 = 代价：谱面里多出来的音符数，以及它占原音符的比例")

    if not args.no_report:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(
            "# 填词 ③ 切分模式对照\n\n"
            "由 `scripts/split_mode_check.py` 机械生成：拿已经识别好的逐字时间戳，"
            "对同一条人声轨分别跑 onset（现在线上用的）和切分两种模式。\n\n"
            "```\n" + report + "\n```\n\n"
            "口径说明：\n"
            "- **现:挤占**：onset 模式下和别的字挤在同一个音上、歌词串里显示不出来的字数。\n"
            "- **切分:可见**：切分模式下真的拿到自己那一段的字数。\n"
            "- **切不开**：窗口落在已被切走的区间里，切分也救不了。\n"
            "- **零长度**：对齐器给不出时间的字，没有边界可切——切分模式对它无能为力。\n"
            "- **新增音 / 改动**：代价。新增的是模型没扒出来的音位，由时间戳补出来；"
            "改动比例 = 新增音 / 原音符数。\n\n"
            "结论用 `docs/交接-下一次对话.md` §5 的口径记，别只贴数字。\n",
            encoding="utf-8")
        print(f"\n写入 {REPORT.relative_to(PROJECT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
