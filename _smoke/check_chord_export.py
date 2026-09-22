"""验证导出产物的和弦轨：取消「仅主旋律乐谱」时，MIDI 与 SVP 都要多一条 Chords 轨。

做法：在 ``output/`` 里自动挑一个任务目录，**复制到 tmp/ 下**再跑 `rebuild_exports()`
（绝不动真实产物），然后检查：
  * only_melody=False → MIDI 3 轨（Vocal/Ins/Chords），SVP 3 轨，和弦数与 chords.mid 一致
  * only_melody=True  → MIDI 2 轨、SVP 2 轨，不含 Chords
无样本时 SKIP、返回 0。
    .venv\\Scripts\\python.exe _smoke\\check_chord_export.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

import pretty_midi

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.rebuild import load_chord_notes, rebuild_exports  # noqa: E402

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
    if not ok:
        FAIL.append(name)


def pick_fixture() -> pathlib.Path | None:
    out_root = ROOT / "output"
    if not out_root.is_dir():
        return None
    for d in sorted(out_root.iterdir()):
        if not d.is_dir():
            continue
        abc = next(
            (p for p in [d / "score.melody.abc", d / "score.abc"] if p.is_file()), None
        )
        if abc and (d / "chords.mid").is_file() and (d / "playback.json").is_file():
            return d
    return None


def tracks_of(mid: pathlib.Path) -> dict[str, int]:
    pm = pretty_midi.PrettyMIDI(str(mid))
    return {i.name: len(i.notes) for i in pm.instruments}


def svp_tracks(svp: pathlib.Path) -> list[str]:
    obj, _ = json.JSONDecoder().raw_decode(svp.read_text(encoding="utf-8"))
    return [t.get("name") for t in obj.get("tracks", [])]


fixture = pick_fixture()
if fixture is None:
    print("SKIP: output/ 下没有带 chords.mid 的任务样本（先随便扒一首歌再跑）。")
    sys.exit(0)

print("样本:", fixture.name)
work = ROOT / "tmp" / "_check_chord_export"
shutil.rmtree(work, ignore_errors=True)
shutil.copytree(fixture, work, ignore=shutil.ignore_patterns("export"))

expected_chords = len(load_chord_notes(work))
abc_text = next(
    p.read_text(encoding="utf-8")
    for p in [work / "score.melody.abc", work / "score.abc"]
    if p.is_file()
)
summary = json.loads((work / "summary.json").read_text(encoding="utf-8")) if (work / "summary.json").is_file() else {}
bpm = summary.get("bpm") or 120.0

# ---------------- 取消勾选 → 要带和弦 ----------------
info = rebuild_exports(work, abc_text, bpm=bpm, only_melody=False, project_name="probe")
print("  only_melody=False ->", info.get("voices"), "| chord_notes:", info.get("chord_notes"),
      "| exports:", [(e["kind"], e.get("tracks") or e.get("lines")) for e in info.get("exports", [])])
mid = work / "export" / "probe.mid"
svp = work / "export" / "probe.svp"
tracks = tracks_of(mid)
names = svp_tracks(svp)
print("   MIDI 轨:", tracks)
print("   SVP  轨:", names)
check("MIDI 含 Chords 轨", "Chords" in tracks, str(list(tracks)))
check("MIDI 和弦数与 chords.mid 一致", tracks.get("Chords") == expected_chords,
      f"{tracks.get('Chords')} != {expected_chords}")
check("SVP 含和弦轨", "和弦" in names, str(names))
check("SVP 是 3 轨（主人声/器乐旋律/和弦）", len(names) == 3, str(names))
check("返回摘要报告了和弦数", info.get("chord_notes") == expected_chords, str(info.get("chord_notes")))

# SVP 结构保障：新增的第 3 轨走的是与第 2 轨完全相同的 build_svp 路径，
# 但仍逐音符校验必填字段，避免和弦轨把交付物写坏。
REQUIRED = {
    "musicalType", "onset", "duration", "lyrics", "phonemes", "accent", "pitch",
    "detune", "instantMode", "attributes", "systemAttributes", "pitchTakes", "timbreTakes",
}
obj, _ = json.JSONDecoder().raw_decode(svp.read_text(encoding="utf-8"))
missing: set[str] = set()
onsets_ok = True
for t in obj.get("tracks", []):
    notes = (t.get("mainGroup") or {}).get("notes") or []
    for n in notes:
        missing |= REQUIRED - set(n)
    starts = [(n.get("onset") or {}).get("value") if isinstance(n.get("onset"), dict) else n.get("onset")
              for n in notes]
    starts = [s for s in starts if s is not None]
    if starts != sorted(starts):
        onsets_ok = False
check("SVP version = 153", obj.get("version") == 153, str(obj.get("version")))
check("SVP 每轨音符必填字段齐全", not missing, str(sorted(missing)))
check("SVP 各轨 onset 单调不减", onsets_ok)
check("SVP 三轨都有音符",
      all(((t.get("mainGroup") or {}).get("notes") or []) for t in obj.get("tracks", [])),
      str([len(((t.get("mainGroup") or {}).get("notes") or [])) for t in obj.get("tracks", [])]))

# 和弦力度沿用模型（48），旋律仍是 100
if "Chords" in tracks:
    pm = pretty_midi.PrettyMIDI(str(mid))
    chord_inst = next(i for i in pm.instruments if i.name == "Chords")
    vocal_inst = next((i for i in pm.instruments if i.name != "Chords"), None)
    check("和弦轨力度沿用模型 48", {n.velocity for n in chord_inst.notes} == {48},
          str(sorted({n.velocity for n in chord_inst.notes})[:5]))
    if vocal_inst:
        check("旋律轨力度仍是 100", {n.velocity for n in vocal_inst.notes} == {100})

# ---------------- 勾选 → 不应带和弦 ----------------
info1 = rebuild_exports(work, abc_text, bpm=bpm, only_melody=True, project_name="probe1")
tracks1 = tracks_of(work / "export" / "probe1.mid")
names1 = svp_tracks(work / "export" / "probe1.svp")
print("  only_melody=True  -> MIDI 轨:", tracks1, "| SVP 轨:", names1)
check("勾选后 MIDI 无 Chords", "Chords" not in tracks1, str(list(tracks1)))
check("勾选后 SVP 无和弦轨", "和弦" not in names1, str(names1))
# 勾选「仅主旋律乐谱」= 只留主旋律声部，所以 SVP / MIDI 都只剩 1 轨（主人声）
check("勾选后只导出主旋律 1 轨", len(names1) == 1 and len(tracks1) == 1,
      f"SVP {names1} / MIDI {list(tracks1)}")

shutil.rmtree(work, ignore_errors=True)
print("\nFAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)
