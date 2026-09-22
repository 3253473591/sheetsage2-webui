r"""回归测试：SVP 必须「同轨不重叠」，且变速曲的时值换算必须正确。

钉死 2026-09-15 抓到的那个 bug：`svpw._note_json` 把**时长**当成**绝对时间**喂给
分段线性的 `_map_sec_to_blick`，单速度时等价、**一变速度就全错** —— 那首变速曲
（开头 255 BPM、之后 89.9/73.2/59.3/62.7）后面所有音符的时值被按开头那段换算，
拉长约 2.8 倍，于是音符尾巴盖住后面所有音（主人声 767 对重叠、和弦 5684 对）。
虚拟歌姬软件不允许同轨重叠。

运行：<整合包或项目>\\untime\\python.exe  _smoke\check_svp_monophonic.py
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.svpw import BLICK_PER_QUARTER, SvNote, build_svp  # noqa: E402
from app.tempo import normalize_tempo_map, sec_to_blick  # noqa: E402
from app.rebuild import monophonic_reduce  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))
    if not ok:
        FAILS.append(name)


def overlaps(notes):
    items = sorted((int(n["onset"]), int(n["onset"]) + int(n["duration"])) for n in notes)
    pairs = 0
    for a in range(len(items)):
        s1, e1 = items[a]
        for b in range(a + 1, len(items)):
            s2, e2 = items[b]
            if s2 >= e1:
                break
            pairs += 1
    return pairs


#: 复刻那首变速曲的速度表（开头极快、之后掉下来 —— 正是放大 2.8 倍的原因）
TMAP = [{"t": 0.0, "bpm": 255.32}, {"t": 6.0, "bpm": 89.86},
        {"t": 226.0, "bpm": 73.17}, {"t": 228.0, "bpm": 59.26}]

print("=" * 74)
print("1. 变速曲里的时值：0.5 秒的音，换算成 blick 必须还是 0.5 秒")
print("=" * 74)
norm = normalize_tempo_map(TMAP, 89.86)
# 落在第二段（89.86 BPM）里的一个 0.5 秒音符
start, dur = 100.0, 0.5
n = SvNote(start, start + dur, 60)
svp = build_svp([n], bpm=89.86, tempo_map=TMAP)
got = svp["tracks"][0]["mainGroup"]["notes"][0]
want_on = sec_to_blick(start, norm)
want_dur = sec_to_blick(start + dur, norm) - want_on
check("onset 正确", abs(int(got["onset"]) - int(want_on)) <= 1,
      f'{got["onset"]} vs {want_on}')
check("duration 正确（= 两端各映射一次相减）",
      abs(int(got["duration"]) - int(want_dur)) <= 1,
      f'{got["duration"]} vs {want_dur}')
# 老写法（直接映射 duration）会得到什么，用来量化差多少
wrong = sec_to_blick(dur, norm)
print(f"     正确 duration = {int(want_dur)} blick；"
      f"老写法 = {int(wrong)} blick（放大了 {wrong / max(1, want_dur):.2f} 倍）")

print()
print("=" * 74)
print("2. 单速度曲不能受影响（老写法在单速度下本来就对）")
print("=" * 74)
single = normalize_tempo_map([], 120.0)
s2 = build_svp([SvNote(10.0, 10.5, 60)], bpm=120.0, tempo_map=[{"t": 0, "bpm": 120.0}])
g2 = s2["tracks"][0]["mainGroup"]["notes"][0]
want2 = sec_to_blick(10.5, single) - sec_to_blick(10.0, single)
check("单速度时値不变", abs(int(g2["duration"]) - int(want2)) <= 1,
      f'{g2["duration"]} vs {want2}')

print()
print("=" * 74)
print("3. 一段连续旋律：变速下也必须零重叠")
print("=" * 74)
melody = []
t = 95.0
for i in range(200):                     # 200 个连续八分音符（0.25 秒）
    melody.append(SvNote(t, t + 0.25, 60 + (i % 7)))
    t += 0.25
svp3 = build_svp(melody, bpm=89.86, tempo_map=TMAP)
notes3 = svp3["tracks"][0]["mainGroup"]["notes"]
ov = overlaps(notes3)
check(f"连续旋律零重叠（实际 {ov} 对）", ov == 0, f"{ov} 对")

print()
print("=" * 74)
print("4. 单音化：模型自带的复音（和弦）要被压成单音")
print("=" * 74)
chord = [SvNote(1.0, 3.0, 60), SvNote(1.0, 3.0, 64), SvNote(1.0, 3.0, 67),   # C 大三和弦
         SvNote(3.0, 5.0, 62), SvNote(3.0, 5.0, 65), SvNote(3.0, 5.0, 69)]   # Dm
red = monophonic_reduce(chord)
check("和弦被压成每拍一个音（保留最低音）", len(red) == 2, f"剩下 {len(red)} 个")
check("保留的是最低音", [r.pitch for r in red] == [60, 62], str([r.pitch for r in red]))
check("压缩后零重叠", overlaps([{"onset": int(r.start * 1e9), "duration": int(r.duration * 1e9)}
                              for r in red]) == 0)

print()
if FAILS:
    print(f"FAIL（{len(FAILS)} 项）")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("PASS —— 变速曲时値正确、SVP 同轨零重叠")
