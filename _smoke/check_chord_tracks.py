"""列出 output/ 各任务里 chords.mid / melody_*.mid / transcription.mid 的音符数与音域。

用来对照整合包的渲染语义：`rendering_sheetsage2.py::_select(tracks,"mix")` 放的是
`transcription.mid`（人声 + 器乐 + 和弦），我们页面试听在「取消仅主旋律乐谱」时
应当与它一致。没有样本时 SKIP。
    .venv\\Scripts\\python.exe _smoke\\check_chord_tracks.py
"""
import pathlib
import sys

import pretty_midi

ROOT = pathlib.Path(__file__).resolve().parent.parent / "output"
dirs = sorted(d for d in ROOT.iterdir() if (d / "transcription.mid").is_file()) if ROOT.is_dir() else []
if not dirs:
    print("SKIP: output/ 下没有 transcription.mid（先随便扒一首歌再跑）。")
    sys.exit(0)

for d in dirs:
    print("==", d.name)
    for f in ("chords.mid", "melody_vocal.mid", "melody_instrumental.mid", "transcription.mid"):
        p = d / f
        if not p.is_file():
            print("   missing", f)
            continue
        pm = pretty_midi.PrettyMIDI(str(p))
        desc = []
        for ins in pm.instruments:
            pitches = [n.pitch for n in ins.notes]
            desc.append(
                f"{ins.name!r}: {len(ins.notes)} notes"
                + (f", pitch {min(pitches)}-{max(pitches)}" if pitches else "")
            )
        print(f"   {f}: end={pm.get_end_time():.2f}s | " + ("; ".join(desc) or "no instruments"))
