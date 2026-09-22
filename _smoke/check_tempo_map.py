"""回归：速度表（变速曲）。

断言
----
1. 从变速曲的小节表派生出 110 → 75 → 110 的速度表（含两处过渡小节）；
2. 秒↔blick 分段换算往返误差为 0；
3. **单速度歌仍然是 1 条**（回归：旧行为不能被改坏）；
4. 走完整导出后：SVP 的 `time.tempo` 条数与位置正确、MIDI 里有等量的 set_tempo。

样本来自 output/ 下已有任务（**复制到 tmp/ 再跑，不动真实产物**）。
    .venv\\Scripts\\python.exe _smoke\\check_tempo_map.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.rebuild import rebuild_exports  # noqa: E402
from app.tempo import blick_to_sec, derive_tempo_map, sec_to_blick  # noqa: E402

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
    if not ok:
        FAIL.append(name)


def measures_of(run: pathlib.Path) -> list[dict]:
    pb = json.loads((run / "playback.json").read_text(encoding="utf-8"))
    if isinstance(pb, dict) and isinstance(pb.get("measures"), list):
        return pb["measures"]
    if isinstance(pb, dict):
        for v in pb.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "start" in v[0]:
                return v
    return []


VAR = ROOT / "output" / "_varytempo_probe"      # 变速曲 110→75→110
SINGLE = ROOT / "output" / "_octfix_probe"      # 单速度
if not (VAR / "playback.json").is_file():
    print("SKIP: 缺少 output/_varytempo_probe（先跑一遍那首变速歌）")
    sys.exit(0)

# ---------------- 1. 派生 ----------------
tm = derive_tempo_map(measures_of(VAR))
print("变速曲速度表:", [f"{i['t']:.2f}s/{i['bpm']:.2f}" for i in tm])
check("变速曲派生出多条速度", len(tm) >= 3, str(len(tm)))
check("首条从 0 秒开始", abs(tm[0]["t"]) < 1e-9, str(tm[0]))
check("开头约 110 BPM", 105 <= tm[0]["bpm"] <= 115, f"{tm[0]['bpm']}")
slow = max(tm, key=lambda i: i["measures"])
check("最长的一段是 75 BPM", abs(slow["bpm"] - 75.0) < 1.0, f"{slow['bpm']}")
check("75 段从 ~98.6s 开始", abs(slow["t"] - 98.6) < 1.5, f"{slow['t']}")
check("结尾回到约 110 BPM", 105 <= tm[-1]["bpm"] <= 115, f"{tm[-1]['bpm']}")
check("速度表按时间升序", all(a["t"] < b["t"] for a, b in zip(tm, tm[1:])))

# ---------------- 2. 往返 ----------------
end = measures_of(VAR)[-1]["end"]
xs = [i * 3.0 for i in range(int(end // 3) + 1)] + [end]
err = max(abs(blick_to_sec(sec_to_blick(t, tm), tm) - t) for t in xs)
check("秒↔blick 往返误差 ≈ 0", err < 1e-6, f"{err*1e6:.3f} µs")

# ---------------- 3. 单速度回归 ----------------
if (SINGLE / "playback.json").is_file():
    tm1 = derive_tempo_map(measures_of(SINGLE))
    print("单速度曲速度表:", [f"{i['t']:.2f}s/{i['bpm']:.2f}" for i in tm1])
    check("单速度曲只派生 1 条", len(tm1) == 1, str(len(tm1)))
else:
    print("（跳过单速度回归：缺 output/_octfix_probe）")

# ---------------- 4. 端到端导出 ----------------
work = ROOT / "tmp" / "_tempo_check"
shutil.rmtree(work, ignore_errors=True)
shutil.copytree(VAR, work, ignore=shutil.ignore_patterns("export"))
abc = (work / "score.abc").read_text(encoding="utf-8")
info = rebuild_exports(work, abc, bpm=110.0, only_melody=True, project_name="tempo")
check("导出成功", bool(info.get("ok")), str(info.get("error")))
check("导出摘要里带速度表", len(info.get("tempo_map") or []) == len(tm),
      str(info.get("tempo_changes")))
check("摘要报告速度变化条数", info.get("tempo_changes") == len(tm), str(info.get("tempo_changes")))

svp_path = work / "export" / "tempo.svp"
obj, _ = json.JSONDecoder().raw_decode(svp_path.read_text(encoding="utf-8"))
tempo = obj["time"]["tempo"]
print("SVP time.tempo:", tempo)
check("SVP 速度条数与速度表一致", len(tempo) == len(tm), f"{len(tempo)} vs {len(tm)}")
check("SVP 速度值一致",
      all(abs(a["bpm"] - b["bpm"]) < 1e-6 for a, b in zip(tempo, tm)),
      str(tempo))
check("SVP position 与秒对得上",
      all(abs(blick_to_sec(a["position"], tm) - b["t"]) < 0.02 for a, b in zip(tempo, tm)),
      str([round(blick_to_sec(a["position"], tm), 2) for a in tempo]))
check("SVP position 升序", all(a["position"] < b["position"] for a, b in zip(tempo, tempo[1:])))
notes = obj["tracks"][0]["mainGroup"]["notes"]
check("SVP 有音符", len(notes) > 100, str(len(notes)))
check("SVP 音符 onset 升序", all(a["onset"] <= b["onset"] for a, b in zip(notes, notes[1::])))

mid_path = work / "export" / "tempo.mid"
import mido  # noqa: E402

mid = mido.MidiFile(str(mid_path))
bpms: list[float] = []
head: list[tuple[int, str]] = []
abs_tick = 0
for msg in mid.tracks[0]:
    abs_tick += msg.time
    if msg.type == "set_tempo":
        bpms.append(round(mido.tempo2bpm(msg.tempo), 3))
    if msg.type in ("set_tempo", "time_signature") and len(head) < 3:
        head.append((abs_tick, msg.type))
print("MIDI set_tempo:", bpms)
check("MIDI 写了多速度", len(bpms) == len(tm), f"{len(bpms)} vs {len(tm)}")
check("MIDI 速度值一致", all(abs(a - b["bpm"]) < 0.1 for a, b in zip(bpms, tm)), str(bpms))
# mido 的 time 是增量：曾经把 time_signature 推到最后一个 tempo 的 tick 上（195839）
check("time_signature 在 tick 0", any(t == 0 and k == "time_signature" for t, k in head),
      str(head))
check("首个事件是 tick 0 的 set_tempo", head and head[0] == (0, "set_tempo"), str(head))
note_count = sum(1 for t in mid.tracks for m in t if m.type == "note_on")
check("MIDI 有音符事件", note_count > 100, str(note_count))

shutil.rmtree(work, ignore_errors=True)
print("\nFAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)
