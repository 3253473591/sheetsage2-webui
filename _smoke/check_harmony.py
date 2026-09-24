"""平行和声：度数语义、按调性平移、歌词继承，以及**它必须真的进得了 SVP**。

本用例专治两类会静默出错的问题
------------------------------
1. **导出时被丢掉**：和声轨能不能导出取决于 ``rebuild_exports`` 里的
   :func:`pick_voices`，它**只认** ``voice_kind(name)``。声部名没登记进
   ``voice_kind`` 就会在导出那一步消失 —— 卷帘上有、SVP 里没有，且不报错。
2. **音程算错一位**：把"三度"当成 2 步还是 3 步、一度是不是 0 步，
   错了的表现是"听着像和声但总差一个音"，很难一眼看出来。所以下面把
   每个度数在 C 大调上的结果**写死**成期望值。

用法::

    .venv\\Scripts\\python.exe _smoke\\check_harmony.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.abcp import scale_pitch_classes  # noqa: E402
from app.harmony import (  # noqa: E402
    build_harmony_notes,
    chromatic_semitones,
    degree_steps,
    diatonic_shift,
)
from app.rebuild import (  # noqa: E402
    harmony_display,
    harmony_voice_name,
    normalize_export_voices,
    rebuild_exports,
    score_from_roll,
    select_roll_tracks,
    split_harmony_voice,
    voice_display,
    voice_kind,
)

WORK = ROOT / "tmp" / "_harmony_check"
FAILURES: list[str] = []

HEADER = {"meter": "4/4", "meter_tuple": (4, 4), "q": 120.0, "key": "C"}
BPM = 120.0

_PITCH_NAME = {0: "C", 1: "C#", 2: "D", 3: "D#", 4: "E", 5: "F",
               6: "F#", 7: "G", 8: "G#", 9: "A", 10: "A#", 11: "B"}


def pname(pitch: int) -> str:
    return f"{_PITCH_NAME[pitch % 12]}{pitch // 12 - 1}"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def note(start: float, end: float, pitch: int, lyric: str) -> dict:
    return {"start": start, "end": end, "pitch": pitch, "lyric": lyric}


# --------------------------------------------------------------------------
# 1. 声部名约定与归类（前后端必须一致的那套）
# --------------------------------------------------------------------------

def test_naming() -> None:
    print("\n--- 1. 声部名约定与归类 ---")
    check("split('Vocal+h3') → ('Vocal', 3)", split_harmony_voice("Vocal+h3") == ("Vocal", 3),
          str(split_harmony_voice("Vocal+h3")))
    check("split('Vocal-h6') → ('Vocal', -6)", split_harmony_voice("Vocal-h6") == ("Vocal", -6),
          str(split_harmony_voice("Vocal-h6")))
    for plain in ("Vocal", "Ins", "chords", "V1", "Chords2", "Vocal+3"):
        check(f"非和声名不误判：{plain}", split_harmony_voice(plain) is None)
    check("构造 harmony_voice_name('Vocal', 3)", harmony_voice_name("Vocal", 3) == "Vocal+h3")
    check("构造 harmony_voice_name('Vocal', -7)", harmony_voice_name("Vocal", -7) == "Vocal-h7")
    check("显示名 +三度", harmony_display(3) == "和声 +三度", harmony_display(3))
    check("显示名 -六度", harmony_display(-6) == "和声 -六度", harmony_display(-6))
    # 导出能否成立的关键两条
    check("voice_kind 仍归人声", voice_kind("Vocal+h3") == "vocal", str(voice_kind("Vocal+h3")))
    check("voice_display → 和声 +三度", voice_display("Vocal+h3") == "和声 +三度")
    check("普通声部行为不变",
          (voice_kind("Vocal"), voice_kind("Ins"), voice_kind("Harmony")) == ("vocal", "ins", None))
    for bad in (0, 8, -8):
        try:
            harmony_voice_name("Vocal", bad)
            check(f"拒绝非法度数 {bad}", False, "居然接受了")
        except ValueError:
            check(f"拒绝非法度数 {bad}", True)


# --------------------------------------------------------------------------
# 2. 度数语义与按调性平移（期望值写死）
# --------------------------------------------------------------------------

def test_engine() -> None:
    print("\n--- 2. 度数语义：一度=0 步、三度=2 步 ---")
    check("degree_steps 1..7", [degree_steps(d) for d in range(1, 8)] == [0, 1, 2, 3, 4, 5, 6],
          str([degree_steps(d) for d in range(1, 8)]))
    check("固定半音模板（大调）", [chromatic_semitones(d) for d in range(1, 8)] == [0, 2, 4, 5, 7, 9, 11])

    print("\n--- 3. C 大调音阶上各度数的结果（注意半音数会随音级变）---")
    c_major = scale_pitch_classes("C")
    check("C 大调音阶 = [0,2,4,5,7,9,11]", c_major == [0, 2, 4, 5, 7, 9, 11], str(c_major))
    scale_notes = [60, 62, 64, 65, 67, 69, 71, 72]   # C4 起的整条音阶
    cases = {
        1: [60, 62, 64, 65, 67, 69, 71, 72],               # 同音
        2: [62, 64, 65, 67, 69, 71, 72, 74],               # 二度
        3: [64, 65, 67, 69, 71, 72, 74, 76],               # 三度（C→E 是大三度、D→F 是小三度）
        4: [65, 67, 69, 71, 72, 74, 76, 77],
        5: [67, 69, 71, 72, 74, 76, 77, 79],
        6: [69, 71, 72, 74, 76, 77, 79, 81],
        7: [71, 72, 74, 76, 77, 79, 81, 83],
    }
    for degree, want in cases.items():
        got = [diatonic_shift(p, c_major, degree) for p in scale_notes]
        check(f"+{degree}度", got == want,
              " ".join(pname(p) for p in got) + ("" if got == want else "  期望 " + " ".join(pname(p) for p in want)))

    print("\n--- 4. 主音不是 C（曾在这里掉过八度）与下行、跨八度 ---")
    check("A 小调 +三度：A4→C5", [diatonic_shift(p, scale_pitch_classes("Am"), 3) for p in [69, 71, 72, 74]]
          == [72, 74, 76, 77], str([pname(p) for p in [diatonic_shift(p, scale_pitch_classes("Am"), 3) for p in [69, 71, 72, 74]]]))
    check("Eb 大调 +三度：F→Ab", diatonic_shift(65, scale_pitch_classes("Eb"), 3) == 68,
          pname(diatonic_shift(65, scale_pitch_classes("Eb"), 3)))
    check("C 小调 +三度：C→Eb", diatonic_shift(60, scale_pitch_classes("Cm"), 3) == 63)
    check("B3 +三度跨八度 → D4", diatonic_shift(59, c_major, 3) == 62, pname(diatonic_shift(59, c_major, 3)))
    check("C4 -三度下行跨八度 → A3", diatonic_shift(60, c_major, -3) == 57, pname(diatonic_shift(60, c_major, -3)))
    check("调外音 C#4 +三度 → F4", diatonic_shift(61, c_major, 3) == 65, pname(diatonic_shift(61, c_major, 3)))
    for bad in (0, 8, -9):
        try:
            degree_steps(bad)
            check(f"拒绝非法度数 {bad}", False, "居然接受了")
        except ValueError:
            check(f"拒绝非法度数 {bad}", True)


# --------------------------------------------------------------------------
# 5. 出音符：歌词与时长必须照搬
# --------------------------------------------------------------------------

def test_build() -> None:
    print("\n--- 5. 出音符：平行和声照搬歌词与时值 ---")
    src = [note(0.0, 1.0, 60, "春"), note(1.0, 2.0, 62, "眠")]
    out, info = build_harmony_notes(src, scale=scale_pitch_classes("C"), degree=3)
    check("音高按调性平移（C→E, D→F）", [n["pitch"] for n in out] == [64, 65], str([n["pitch"] for n in out]))
    check("歌词逐音继承（不是占位词）", [n["lyric"] for n in out] == ["春", "眠"])
    check("起止时间照搬", [(n["start"], n["end"]) for n in out] == [(0.0, 1.0), (1.0, 2.0)])
    check("统计说明走了调内", info.get("mode") == "diatonic", str(info))
    # 没有调性时退回固定半音
    out2, info2 = build_harmony_notes(src, scale=[], degree=3, semitones=chromatic_semitones(3))
    check("无调性时退回固定半音（+4）", [n["pitch"] for n in out2] == [64, 66], str([n["pitch"] for n in out2]))
    check("统计说明走了半音", info2.get("mode") == "chromatic", str(info2))


# --------------------------------------------------------------------------
# 6. 端到端：和声轨必须出现在 SVP 里，且歌词逐音继承
# --------------------------------------------------------------------------

def svp_notes(path: Path) -> dict[str, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(t.get("name")): list((t.get("mainGroup") or {}).get("notes") or [])
            for t in data.get("tracks") or []}


def export(tracks: list[dict], voices: list[str], tag: str) -> tuple[dict, dict[str, list[dict]]]:
    out = WORK / tag
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    sel = normalize_export_voices(voices)
    score = score_from_roll(select_roll_tracks(tracks, sel), bpm=BPM, header=HEADER)
    result = rebuild_exports(out, "", score=score, bpm=BPM, export_voices=sel, project_name="harmony_check")
    svp = out / "export" / "harmony_check.svp"
    return result, (svp_notes(svp) if svp.is_file() else {})


def melody(voice: str = "Vocal", display: str = "主人声", harmony: int | None = None) -> dict:
    pitches = [60, 62, 64]
    lyrics = ["春", "眠", "不"]
    if harmony is not None:
        pitches = [diatonic_shift(p, scale_pitch_classes("C"), harmony) for p in pitches]
    return {"voice": voice, "kind": "vocal", "display": display, "is_vocal": True, "editable": True,
            "notes": [note(float(i), float(i + 1), p, ly) for i, (p, ly) in enumerate(zip(pitches, lyrics))]}


def test_export() -> None:
    print("\n--- 6. 主人声 + 和声+三度 一起导出 ---")
    tracks = [melody(), melody(voice="Vocal+h3", display="和声 +三度", harmony=3)]
    result, svp = export(tracks, ["vocal"], "with_harmony")

    check("导出成功", result.get("ok") is True, str(result.get("error") or ""))
    check("SVP 里两条轨（主人声 + 和声 +三度）",
          sorted(svp) == sorted(["主人声", "和声 +三度"]), str(sorted(svp)))
    check("导出结果里的轨名一致",
          result.get("exports", [{}])[0].get("tracks") == ["主人声", "和声 +三度"],
          str(result.get("exports", [{}])[0].get("tracks")))
    if "和声 +三度" not in svp:
        return
    main, harm = svp["主人声"], svp["和声 +三度"]
    check("音高逐音 +三度（C→E, D→F, E→G）", [n["pitch"] for n in harm] == [64, 65, 67],
          str([n["pitch"] for n in harm]))
    check("歌词逐音继承", [n["lyrics"] for n in harm] == ["春", "眠", "不"], str([n["lyrics"] for n in harm]))
    check("时值一致", [(n["onset"], n["duration"]) for n in harm] == [(n["onset"], n["duration"]) for n in main])
    check("MIDI 也写出来了", (WORK / "with_harmony" / "export" / "harmony_check.mid").is_file())

    print("\n--- 7. 反证：没被 voice_kind 认出的声部会在导出时消失 ---")
    bad = [melody(), melody(voice="Harmony", display="和声 +三度", harmony=3)]
    result2, svp2 = export(bad, ["vocal"], "unregistered")
    check("任务照样成功（不报错，所以更难发现）", result2.get("ok") is True)
    check("该轨确实被丢掉了（只剩主人声）", sorted(svp2) == ["主人声"], str(sorted(svp2)))
    check("卷帘侧本来能选中它（select_roll_tracks 认 kind）",
          len(select_roll_tracks(bad, ["vocal"])) == 2)

    print("\n--- 8. 多层叠置（柱式感）---")
    stacked = [melody(),
               melody(voice="Vocal+h3", display="和声 +三度", harmony=3),
               melody(voice="Vocal-h6", display="和声 -六度", harmony=-6)]
    _r, svp3 = export(stacked, ["vocal"], "stacked")
    check("两条和声可并存", sorted(svp3) == sorted(["主人声", "和声 +三度", "和声 -六度"]), str(sorted(svp3)))


def main() -> int:
    print("=" * 66)
    print("  平行和声回归（无需显卡 / ffmpeg / 权重）")
    print("=" * 66)
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        test_naming()
        test_engine()
        test_build()
        test_export()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)

    print()
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项失败")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
