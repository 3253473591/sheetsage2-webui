"""男女合唱导出：主唱（女）+ 和声（男）两条旋律轨。

背景
----
用户自己的 MSST 工作流能分出两条 stem：
* `人声干声 & 无和声伴奏`  → **主唱**（`*_Vocals_noreverb_dry.wav`）
* `提取和声干声 & 带和声伴奏` → **和声**（`*_Instrumental_vocals.wav`）

主唱那条丢给 SheetSage2 能得到完整、可导出的谱面；
**和声那条 SheetSage2 的 ABC 会生成失败**（模型把速度判成一半、拍号 2/4，
`abc_error = Interval … shorter than the ABC subbeat grid`），但 `melody_vocal.lab`
里的「秒 + 音高」是好的 —— 所以**不需要它的 ABC**，直接把音符当成第二条旋律轨挂上去。

用法::

    .venv\\Scripts\\python.exe _smoke\\duet_export.py \
        --lead-run output\\_octfix_probe \
        --harmony-run output\\_harmony_probe \
        --out tmp\\duet_export \
        --midi tmp\\duet_export_2track.mid --listen-wav tmp\\duet_export_listen.wav
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "_smoke"))

from app.rebuild import rebuild_exports  # noqa: E402
from duet_voice_split import render_midi, render_wav  # noqa: E402


def read_lab(path: pathlib.Path) -> list[tuple[float, float, int]]:
    notes = []
    if not path.is_file():
        return notes
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            try:
                notes.append((float(parts[0]), float(parts[1]), int(float(parts[2]))))
            except ValueError:
                continue
    return notes


def octave_shift_for(run: pathlib.Path) -> tuple[int, str]:
    """该任务的八度校正量：已 applied 就是 0（音频里已经改过），否则取检测值。"""
    summary = run / "summary.json"
    if not summary.is_file():
        return 0, "无 summary"
    info = (json.loads(summary.read_text(encoding="utf-8")).get("octave_fix") or {})
    if info.get("applied"):
        return 0, "已应用"
    shift = int(info.get("shift") or 0)
    return shift, info.get("reason", "")


def main() -> int:
    argv = sys.argv[1:]
    lead_run = harmony_run = out_dir = None
    midi_path = listen_path = None
    if "--lead-run" in argv:
        i = argv.index("--lead-run"); lead_run = pathlib.Path(argv[i + 1]); del argv[i:i + 2]
    if "--harmony-run" in argv:
        i = argv.index("--harmony-run"); harmony_run = pathlib.Path(argv[i + 1]); del argv[i:i + 2]
    if "--out" in argv:
        i = argv.index("--out"); out_dir = pathlib.Path(argv[i + 1]); del argv[i:i + 2]
    if "--midi" in argv:
        i = argv.index("--midi"); midi_path = pathlib.Path(argv[i + 1]); del argv[i:i + 2]
    if "--listen-wav" in argv:
        i = argv.index("--listen-wav"); listen_path = pathlib.Path(argv[i + 1]); del argv[i:i + 2]
    if lead_run is None or harmony_run is None or out_dir is None:
        print(__doc__)
        return 2
    for d in (lead_run, harmony_run):
        if not d.is_dir():
            print("目录不存在:", d)
            return 2

    lead = read_lab(lead_run / "melody_vocal.lab")
    harm = read_lab(harmony_run / "melody_vocal.lab")
    shift, why = octave_shift_for(harmony_run)
    print(f"主唱音符 {len(lead)}；和声音符 {len(harm)}")
    print(f"和声八度校正 {shift:+d}（{why}）")
    harm = [(a, b, max(0, min(127, p + shift))) for a, b, p in harm]
    harm = [(a, b, p) for a, b, p in harm if b - a >= 0.05]

    abc_file = lead_run / "score.abc"
    if not abc_file.is_file():
        print("主唱任务里没有 score.abc:", abc_file)
        return 2
    abc = abc_file.read_text(encoding="utf-8")

    # 复制主唱任务目录，避免动到真实产物
    shutil.rmtree(out_dir, ignore_errors=True)
    shutil.copytree(lead_run, out_dir, ignore=shutil.ignore_patterns("export"))

    info = rebuild_exports(
        out_dir, abc, bpm=None, only_melody=False, lyrics="la",
        project_name="duet",
        extra_tracks=[("男声和声", harm, "Harmony")],
    )
    print("导出结果:", info.get("ok"), [e["kind"] for e in info.get("exports", [])])
    for e in info.get("exports", []):
        print(f"   {e['kind']}: tracks={e.get('tracks')} notes={e.get('notes')}  {e.get('path')}")

    if midi_path:
        render_midi(midi_path, [("Lead", lead), ("Harmony", harm)])
        print("两轨 MIDI:", midi_path)
    if listen_path:
        render_wav(listen_path, sorted(lead + harm))
        print("试听 wav（主唱+和声叠加）:", listen_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
