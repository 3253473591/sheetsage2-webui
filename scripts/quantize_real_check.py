"""在 output/ 里的真实工程上验证量化：网格吸附率、位移、失败情况。

网格步长 = **一拍的 1/96**（`quantize.GRID_DIVISOR`，PPQ=1920 时 20 tick），
与拍号无关；96 = 32 × 3，所以直线族与三连音族都落得下。
量化**只看** `raw_tick` / `raw_dur`（量化前的位置）——老工程没有这两个字段时退回
`tick` / `dur`，但那样量的是"已经量化过的结果"，参考意义不大。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
from sheetsage2_webui.quantize import (
    GRID_DIVISOR,
    note_shift_seconds,
    quantize_tracks,
)
from sheetsage2_webui.timeline import from_stored_measures

PPQ = 1920
GRID_TICKS = PPQ // GRID_DIVISOR      # 20 tick = 一拍的 1/96


def on_grid_ratio(notes, step):
    if not notes:
        return 1.0
    good = 0
    for n in notes:
        if n["tick"] % step == 0 and (n["tick"] + n["dur"]) % step == 0:
            good += 1
    return good / len(notes)


rows = []
for project_dir in sorted(Path("output").iterdir()):
    if not project_dir.is_dir():
        continue
    path = project_dir / "project.json"
    if not path.is_file():
        continue
    project = json.loads(path.read_text(encoding="utf-8"))
    measures = project["timeline"]["measures"]
    if not measures:
        print(f"{project_dir.name:44s} 没有小节表，跳过")
        continue
    timeline = from_stored_measures(measures, ppq=project.get("ppq", PPQ))

    line = f"{project_dir.name[:42]:44s} step={GRID_TICKS:4d} "
    for track in project["tracks"]:
        notes = list(track["notes"])
        # 量化前的位置：优先 raw_*，老工程退回 tick/dur
        raw = [{"tick": int(n.get("raw_tick", n["tick"])),
                "dur": int(n.get("raw_dur", n["dur"])),
                "pitch": n["pitch"]} for n in notes]
        before = on_grid_ratio(raw, GRID_TICKS)
        new_tracks, stats = quantize_tracks(
            [{"id": track["id"], "notes": raw}],
            tick_length=project["timeline"]["tick_length"],
            simplicity=0.0, ticks_per_quarter=PPQ)
        after = on_grid_ratio(new_tracks[0], GRID_TICKS)
        # 逐音对应：quantize_tracks 内部按 (tick, pitch) 排序，这里按同序重排原始表
        order = sorted(range(len(raw)), key=lambda i: (raw[i]["tick"], raw[i]["pitch"]))
        pairs_before = [raw[i] for i in order]
        shift = note_shift_seconds(pairs_before, new_tracks[0], timeline)
        big = sum(1 for a, b in zip(pairs_before, new_tracks[0])
                  if abs(b["tick"] - a["tick"]) >= GRID_TICKS)
        line += (f"| {track['id'][:4]} n={len(raw):4d} "
                 f"grid {before:5.1%}->{after:5.1%} moved={stats.moved:4d} "
                 f"big={big:4d} fail={stats.failed_tracks} ")
        if shift:
            line += (f"mean={shift['mean']:.3f} p90={shift['p90']:.3f} "
                     f"max={shift['max']:.3f}s ")
        rows.append(dict(song=project_dir.name, track=track["id"], n=len(raw),
                         before=before, after=after, moved=stats.moved, big=big,
                         fail=stats.failed_tracks, **(shift or {})))
    print(line)

if rows:
    total = sum(r["n"] for r in rows)
    print()
    print(f"合计 {len(rows)} 轨 / {total} 音；失败 {sum(r['fail'] for r in rows)} 轨；"
          f"位移 ≥ 一个网格步（{GRID_TICKS} tick）的音符 "
          f"{sum(r['big'] for r in rows)}/{total}")
