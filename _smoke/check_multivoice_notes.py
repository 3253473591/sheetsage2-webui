"""验证 /api/tasks/{id}/notes 的多声部 / 和弦 / intro 行为（不跑推理、不需要 GPU）。

做法：在 ``output/`` 里**自动挑**一个已有的任务目录当样本
（真实扒谱一次就会生成；样本不存在则 SKIP），把 ``app.server.manager`` 换成
返回该目录的假任务，然后直接调用真实的端点函数。

跑法：项目自带 .venv 的 python
    .venv\\Scripts\\python.exe _smoke\\check_multivoice_notes.py
"""
from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import server  # noqa: E402
from app.abcp import notes_to_seconds, parse_abc, to_simple_notes  # noqa: E402
from app.rebuild import load_measures  # noqa: E402

MELODY = ("vocal", "melody", "lead")


def _pick_abc(task_dir: pathlib.Path) -> pathlib.Path | None:
    """与 server.get_notes 相同的优先级：export/<stem>.abc → score.melody.abc → score.abc。"""
    export = task_dir / "export"
    stem = next((p.name[: -len(".abc")] for p in export.glob("*.abc")), None) if export.is_dir() else None
    for cand in ([export / f"{stem}.abc"] if stem else []) + [
        task_dir / "score.melody.abc",
        task_dir / "score.abc",
    ]:
        if cand.is_file():
            return cand
    return None


def _discover() -> list[tuple[pathlib.Path, dict]]:
    out_root = ROOT / "output"
    found = []
    if not out_root.is_dir():
        return found
    for d in sorted(out_root.iterdir()):
        summary, abc = d / "summary.json", d / "score.abc"
        if not (d.is_dir() and summary.is_file() and abc.is_file()):
            continue
        try:
            meta = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _pick_abc(d) is not None:
            found.append((d, meta))
    return found


def _expected(task_dir: pathlib.Path, only_melody: bool) -> tuple[int, list[str], int]:
    """按服务端同一套规则独立算一遍：期望音符数、声部顺序、和弦数。"""
    abc_file = _pick_abc(task_dir)
    score = parse_abc(abc_file.read_text(encoding="utf-8"))
    notes_to_seconds(score.notes, bpm=score.bpm or 120.0, measures=load_measures(task_dir))
    voices = [v for v in score.voices if v.strip().lower() in MELODY] or score.voices
    selected = list(voices)
    if not only_melody:
        selected += [v for v in score.voices if v not in selected]
    total = sum(len(to_simple_notes(score.notes, voice=v)) for v in selected)

    chords = 0
    for name in ("chords.mid", "transcription.mid"):
        path = task_dir / name
        if not path.is_file():
            continue
        try:
            import pretty_midi

            parsed = pretty_midi.PrettyMIDI(str(path))
        except Exception:
            continue
        chords = sum(
            len(ins.notes) for ins in parsed.instruments
            if "chord" in (ins.name or "").lower()
        )
        if chords or name == "chords.mid":
            break
    return total, selected, chords


dirs = _discover()
if not dirs:
    print("SKIP: output/ 下没有可用的任务样本（先随便扒一首歌，或保留一个 output/<任务> 目录）。")
    sys.exit(0)

single_fixture = next(((d, m) for d, m in dirs if m.get("only_melody") is not False), dirs[0])
multi_fixture = next(((d, m) for d, m in dirs if m.get("only_melody") is False), dirs[0])
for label, fixture in (("单声部", single_fixture), ("多声部", multi_fixture)):
    print(f"样本({label}): {fixture[0].name}")

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
    if not ok:
        FAIL.append(name)


def notes_for(task_dir: pathlib.Path, only_melody: int) -> dict:
    stem = _pick_abc(task_dir).stem
    server.manager = SimpleNamespace(
        get=lambda _tid, _d=task_dir, _s=stem: SimpleNamespace(
            out_dir=str(_d),
            audio=SimpleNamespace(stem=_s),
            params={"only_melody": only_melody == 1},
            result={"only_melody": only_melody == 1},
        )
    )
    return json.loads(bytes(server.get_notes("fake", only_melody=only_melody).body).decode("utf-8"))


# ---------------- 单声部（仅主旋律乐谱 = 勾选）----------------
d1 = single_fixture[0]
exp_notes, exp_voices, exp_chords = _expected(d1, True)
one = notes_for(d1, 1)
print(f"\n[单声部] {d1.name}: voices={one['voices']} notes={len(one['notes'])} "
      f"highlight={len(one['highlight_notes'])} chords={len(one['chord_notes'])}")
check("勾选后只返回主旋律声部", one["voices"] == exp_voices, f"{one['voices']} != {exp_voices}")
check("勾选后音符数与乐谱一致", len(one["notes"]) == exp_notes, f"{len(one['notes'])} != {exp_notes}")
check("勾选后不带和弦轨", one["chord_notes"] == [])
if len(exp_voices) <= 1:
    check("单声部保留播放高亮", len(one["highlight_notes"]) > 0)

# ---------------- 多声部（仅主旋律乐谱 = 取消勾选）----------------
d0 = multi_fixture[0]
exp_notes, exp_voices, exp_chords = _expected(d0, False)
many = notes_for(d0, 0)
print(f"\n[多声部] {d0.name}: voices={many['voices']} notes={len(many['notes'])} "
      f"highlight={len(many['highlight_notes'])} chords={len(many['chord_notes'])}")
check("取消勾选后返回全部声部", many["voices"] == exp_voices, f"{many['voices']} != {exp_voices}")
check("取消勾选后音符数与乐谱一致", len(many["notes"]) == exp_notes, f"{len(many['notes'])} != {exp_notes}")
check("取消勾选后带上和弦轨", len(many["chord_notes"]) == exp_chords,
      f"{len(many['chord_notes'])} != {exp_chords}")
if exp_chords:
    check("和弦音高落在钢琴音域 21–109",
          all(21 <= c["pitch"] <= 109 for c in many["chord_notes"]))
    check("和弦时值有效", all(c["end"] > c["start"] for c in many["chord_notes"]))
    check("duration 覆盖到和弦结尾",
          many["duration"] >= max(c["end"] for c in many["chord_notes"]) - 1e-6)
if len(exp_voices) > 1:
    check("多声部关闭按序号的高亮", many["highlight_notes"] == [])
    check("多声部、勾选后高亮仍可用（单声部）",
          len(notes_for(d0, 1)["highlight_notes"]) > 0)
# 注意：必须在**同一个任务目录**上比。output/ 下可能有多个不同歌的任务，
# 跨歌比音符数没有意义（早期版本这里就踩过）。
same_single = notes_for(d0, 1)
check("同一首歌：多声部音符数 ≥ 单声部",
      len(many["notes"]) >= len(same_single["notes"]),
      f"{len(many['notes'])} < {len(same_single['notes'])}")

# ---------------- intro 是否发声 ----------------
try:
    sections = json.loads((d0 / "summary.json").read_text(encoding="utf-8"))["sections"]
    intro_end = next(s["end"] for s in sections if s["label"] == "intro")
    m_in = [n for n in many["notes"] if n["start"] < intro_end]
    s_in = [n for n in one["notes"] if n["start"] < intro_end]
    print(f"\nintro 0–{intro_end:.2f}s: 多声部 {len(m_in)} 音（和弦 {len([c for c in many['chord_notes'] if c['start'] < intro_end])}）"
          f" / 仅主旋律 {len(s_in)} 音")
    check("加入器乐声部后 intro 音符不减", len(m_in) >= len(s_in),
          f"{len(m_in)} < {len(s_in)}")
    if len(m_in) > len(s_in):
        print("      → 这就是整合包里 intro 会响、只喂主旋律时不响的原因（声音来自 V: Ins）")
except (OSError, ValueError, StopIteration):
    print("\n（无段落信息，跳过 intro 检查）")

# ---------------- 和弦起点与 chord.lab 对齐 ----------------
lab_file = d0 / "chord.lab"
if lab_file.is_file() and many["chord_notes"]:
    rows = [ln.split("\t") for ln in lab_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    first = next((float(t) for t, _e, name in rows if name.strip().upper() != "N"), None)
    if first is not None:
        print(f"chord.lab 首个非 N 和弦 {first:.2f}s；音符轨首个 {many['chord_notes'][0]['start']:.2f}s")
        check("和弦轨起点与 chord.lab 对齐",
              abs(many["chord_notes"][0]["start"] - first) < 0.01)

print("\nFAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)
