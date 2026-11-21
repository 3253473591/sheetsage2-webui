"""金标文本模式：用 `ref.mid` 自带的歌词过 TIFA，量**纯挂字层**。

为什么需要它：英文样本的 `lyrics.json` 是 ASR/人工词，和 `ref.mid` 里的词不是同一份
（缩写、漏句、断句都不同），直接比会把"词源差异"算成"挂字错误"。
这里把文本换成 `ref.mid` 的歌词、并**按 MIDI 的音符时间切句**，只让 TIFA 在每句的
音频窗口里出字时间，于是量到的就是挂字算法本身。

用法：
    python scripts/bench_gold_text.py --root "samples/对齐/英"
    python scripts/bench_gold_text.py --only "willow"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import sheetsage2_webui  # noqa: E402,F401  (import side effects)

# 有的环境（含本仓库的开发沙箱）会给 `tempfile.mkdtemp` 建出**连子进程都写不进去**的
# 目录，TIFA/ffmpeg 会报 PermissionError。这里只在这个评测脚本里换成普通目录。
import tempfile as _tempfile  # noqa: E402

_SEQ = [0]


def _plain_mkdtemp(suffix=None, prefix=None, dir=None):
    _SEQ[0] += 1
    base = Path(dir) if dir else ROOT / "tmp" / "pytmp"
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"plain-{prefix or 'tmp'}-{_SEQ[0]}"
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


_tempfile.mkdtemp = _plain_mkdtemp

from sheetsage2_webui.asr import AsrEngine, AsrRun, Section  # noqa: E402
from sheetsage2_webui.bench import (Char, estimate_offset, read_midi_notes,  # noqa: E402
                                    score_alignment, summarize, text_similarity)
from sheetsage2_webui.webui.runner_models import default_asr_config  # noqa: E402

GAP = 1.2                 # 同句内两音间隔超过这个就断句（秒）
SKIP_TOKENS = {"+", "-"}
PAD = 0.15                # 每句音频窗口前后留一点


def audio_path(folder: Path) -> Path | None:
    pointer = folder / "audio.txt"
    if pointer.is_file():
        raw = pointer.read_text(encoding="utf-8-sig").strip()
        candidate = (folder / raw).resolve()
        if candidate.is_file():
            return candidate
    return None


def phrases(notes) -> list[tuple[float, float, str]]:
    """把逐音歌词按停顿聚成句，返回 `(起点, 终点, 文本)`。"""
    out: list[tuple[float, float, str]] = []
    buffer: list[str] = []
    start = end = 0.0
    previous = None
    for note in notes:
        token = note.lyric.strip()
        if token in SKIP_TOKENS or not token:
            continue
        if buffer and previous is not None and note.start - previous > GAP:
            out.append((start, end, " ".join(buffer)))
            buffer = []
        if not buffer:
            start = note.start
        buffer.append(token)
        end = note.end if note.end > note.start else note.start
        previous = note.start
    if buffer:
        out.append((start, end, " ".join(buffer)))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="samples/对齐/英")
    parser.add_argument("--language", default="en")
    parser.add_argument("--only", default="", help="只跑名字包含此子串的样本")
    parser.add_argument("--save", default="", help="把逐字时间存成 JSON（调试用）")
    args = parser.parse_args()

    root = Path(args.root)
    config = default_asr_config()
    config.device = "cuda:0"
    engine = AsrEngine(config)
    engine.load()
    work_root = ROOT / "tmp" / "pytmp" / "gold"
    work_root.mkdir(parents=True, exist_ok=True)

    rows = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        if args.only and args.only.lower() not in folder.name.lower():
            continue
        midi = folder / "ref.mid"
        if not midi.is_file():
            midi = folder / "ref.midi"
        audio = audio_path(folder)
        if not midi.is_file() or audio is None:
            continue
        notes = read_midi_notes(midi)
        if not notes:
            continue
        work = work_root / f"gold-{folder.name[:40]}"
        work.mkdir(parents=True, exist_ok=True)
        run = AsrRun()
        chars: list[Char] = []
        try:
            for index, (start, end, text) in enumerate(phrases(notes)):
                window = Section(max(0.0, start - PAD), end + PAD)
                clip = engine._extract(audio, window, work / f"p{index:03d}.wav", tifa=True)
                for item in engine._align_with_tifa([(clip, text, window.start)],
                                                    run, args.language, args.language):
                    chars.append(Char(item.text, item.start, item.end,
                                      tuple(getattr(item, "parts", ()) or ())))
        except Exception as exc:                     # noqa: BLE001 - 一首失败不影响别的
            print(f"{folder.name[:30]:32s} 失败 {type(exc).__name__}: {exc}", flush=True)
            continue
        if not chars:
            print(f"{folder.name[:30]:32s} 没出字", flush=True)
            continue
        offset = estimate_offset(notes, chars)
        result = score_alignment(notes, chars, offset=offset)
        result["name"] = folder.name
        result["offset"] = round(offset, 3)
        result["similarity"] = round(text_similarity(notes, chars), 4)
        rows.append(result)
        print(f"{folder.name[:30]:32s} text {result['similarity']:5.1%} "
              f"head {result['content']:4d}/{result['heads']:<4d} "
              f"exact {result['exact']/result['heads']:5.1%} "
              f"±1 {result['pm1']/result['heads']:5.1%}", flush=True)

    if rows:
        stats = summarize(rows)
        print(f"\n合计 {len(rows)} 首  content {stats['content']}/{stats['heads']} = "
              f"{stats['content_ratio']:.1%}  exact {stats['exact_ratio']:.1%}  "
              f"±1 {stats['pm1_ratio']:.1%}  marker {stats['marker_ratio']:.1%}")
        if args.save:
            Path(args.save).write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
