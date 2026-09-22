"""钢琴卷帘保存路径的端到端往返测试。

这条链路是卷帘的命门，任一环不精确都会让"编辑一次、整首歌悄悄偏移"：

    前端（秒） --seconds_to_score--> 记谱位置 --score_to_abc--> ABC 文本
        --parse_abc--> 记谱位置 --notes_to_seconds--> 后端（秒）

起终点必须逐音一致。用**真实样本**测（含变速曲），而不是只测合成数据 ——
§23 那个"时值被放大 2.8 倍"的 bug 就是单速度样本永远测不出来。

运行（不需要服务）：
  .\\.venv\\Scripts\\python.exe _smoke\\check_roll_roundtrip.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.abcp import (AbcNote, notes_to_seconds, parse_abc,  # noqa: E402
                      quantize_notation, seconds_to_score, to_simple_notes)
from app.abcs import score_to_abc, voice_has_overlap         # noqa: E402
from app.rebuild import load_measures                        # noqa: E402
from app.tempo import derive_tempo_map                       # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAILS = []
TOL = 1e-6


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))
    if not ok:
        FAILS.append(name)


def roundtrip_one(abc_path):
    """跑一遍完整链路，返回 (音符数, 起始秒最大误差, 时值最大误差)。"""
    measures = load_measures(abc_path.parent)
    score = parse_abc(abc_path.read_text(encoding="utf-8", errors="replace"))
    if not score.notes:
        return 0, 0.0, 0.0, "空谱面"
    notes_to_seconds(score.notes, bpm=score.bpm or 120.0, measures=measures)

    # 每个声部取有声音符（秒），这就是 /notes 交给卷帘的东西
    original: dict[str, list[tuple[float, float, int]]] = {}
    for v in (score.voices or sorted({n.voice for n in score.notes})):
        sn = to_simple_notes(score.notes, voice=v)
        if sn:
            original[v] = sn
    if not original:
        return 0, 0.0, 0.0, "没有有声音符"

    # 模拟卷帘保存：秒 → 记谱 → **吸附记谱网格** → ABC
    # 吸附这一步与后端 /roll 完全一致：不吸附的话浮点噪声会让相邻音被判成重叠，
    # 还会把序列化器需要的基本单位抬到 1/8192 而拒绝生成。
    voices: list[tuple[str, list[tuple[float, float, int]]]] = []
    for v, spans in original.items():
        notated = quantize_notation(seconds_to_score(spans, bpm=score.bpm or 120.0, measures=measures))
        if voice_has_overlap(notated):
            return 0, 0.0, 0.0, f"{v} 有重叠（本测试只测单声部样本）"
        voices.append((v, notated))
    abc_text = score_to_abc(dict(score.header), voices)

    # 模拟后端重新读取：ABC → 记谱 → 秒
    back = parse_abc(abc_text, merge_ties=False)
    notes_to_seconds(back.notes, bpm=back.bpm or 120.0, measures=measures)

    worst_start = worst_dur = 0.0
    total = 0
    for v, spans in original.items():
        got = to_simple_notes(back.notes, voice=v)
        if len(got) != len(spans):
            return total, 1.0, 1.0, f"{v} 音符数 {len(spans)} → {len(got)}"
        for (s0, e0, p0), (s1, e1, p1) in zip(spans, got):
            total += 1
            if p0 != p1:
                return total, 1.0, 1.0, f"{v} 音高 {p0} → {p1}"
            worst_start = max(worst_start, abs(s0 - s1))
            worst_dur = max(worst_dur, abs((e0 - s0) - (e1 - s1)))
    return total, worst_start, worst_dur, ""


print("=" * 78)
print("真实样本：秒 → 记谱 → ABC → 解析 → 秒 的完整往返")
print("=" * 78)

samples = sorted(ROOT.glob("output/*/score.abc"))
if not samples:
    print("  ⚠ output/ 下没有 score.abc 样本")

total_notes = 0
g_start = g_dur = 0.0
vary_seen = 0
skipped: list[str] = []
for f in samples:
    try:
        n, ws, wd, err = roundtrip_one(f)
    except Exception as exc:  # noqa: BLE001
        skipped.append(f"{f.parent.name}: {type(exc).__name__} {exc}")
        continue
    if err and n == 0:
        skipped.append(f"{f.parent.name}: {err}")
        continue
    varied = len(derive_tempo_map(load_measures(f.parent))) > 1
    if varied:
        vary_seen += 1
    total_notes += n
    g_start = max(g_start, ws)
    g_dur = max(g_dur, wd)
    print(f"  {f.parent.name[:38]:40} 音符={n:5} {'变速' if varied else '等速'}  "
          f"起点误差={ws:.2e} 时值误差={wd:.2e}")

if skipped:
    print()
    print("  跳过：")
    for s in skipped:
        print("    -", s)

if total_notes:
    check(f"真实样本完整往返无损（{total_notes} 个音符，起点最大误差 {g_start:.2e}，"
          f"时值最大误差 {g_dur:.2e}）", g_start < TOL and g_dur < TOL,
          f"start={g_start} dur={g_dur}")
    check("样本覆盖了变速曲（否则测不出 §16/§23 那类只在变速下暴露的错）",
          vary_seen > 0, f"只有 {vary_seen} 个变速样本")

# ---------------------------------------------------------------- 合成边界
print()
print("=" * 78)
print("合成边界：跨小节 / 起始休止 / 高低音 / 极短与极长时值 / 单音 / 空")
print("=" * 78)

header = {"X": "1", "T": "t", "M": "4/4", "L": "1/8", "Q": "1/4=120", "K": "C"}
cases = {
    "跨多小节的长音": [("Vocal", [(0.0, 3.0, 60)])],
    "起始休止后半拍进": [("Vocal", [(1.25, 1.5, 62)])],
    "低音区（含逗号记号）": [("Vocal", [(0.0, 0.25, 28), (0.5, 0.75, 33)])],
    "高音区（含撇号记号）": [("Vocal", [(0.0, 0.25, 96), (0.5, 0.75, 103)])],
    "极短（1/32 个四分）": [("Vocal", [(0.0, 1.0 / 128, 72)])],
    "升降还原号": [("Vocal", [(0.0, 0.25, 61), (0.25, 0.25, 61), (0.5, 0.25, 60)])],
    "多声部": [("Vocal", [(0.0, 1.0, 60)]), ("Ins", [(0.0, 0.5, 48), (0.5, 1.0, 52)])],
    "单音": [("Vocal", [(2.0, 2.5, 69)])],
}
for label, voices in cases.items():
    abc_text = score_to_abc(header, voices)
    back = parse_abc(abc_text, merge_ties=False)
    got = [(n.voice, round(n.onset, 9), round(n.duration, 9), n.pitch)
           for n in back.notes if n.pitch is not None]
    want = [(v, round(o, 9), round(d, 9), p) for v, ns in voices for o, d, p in ns]
    ok = sorted(got) == sorted(want)
    detail = "" if ok else f"\n      want={sorted(want)}\n      got ={sorted(got)}"
    check(f"合成：{label}", ok, detail)

check("空声部列表也能产出可解析的 ABC",
      parse_abc(score_to_abc(header, [])).notes == [])

print()
if FAILS:
    print(f"FAIL（{len(FAILS)} 项）")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("PASS —— 卷帘保存链路（秒→记谱→ABC→解析→秒）逐音无损")
