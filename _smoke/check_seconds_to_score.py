"""秒 ↔ 记谱位置 的往返测试（钢琴卷帘保存路径的地基）。

为什么必须有：用户在钢琴卷帘上按**秒**拖动音符，保存时要写回**记谱位置**才能
序列化成 ABC。`:func:`app.abcp.seconds_to_score` 是 :func:`notes_to_seconds` 的逆，
两者必须严格互逆 —— 否则「编辑一次、整首歌悄悄偏移一点点」，是 §23「时值被放大
2.8 倍」那类**只在变速曲暴露**的慢性 bug 的温床。

覆盖：
  1. 真实样本（output/*/score.abc + 各自的 playback.json 小节表）逐音往返；
  2. 真实样本里带**变速小节表**的那个，单独确认也过（§16/§23 的教训）；
  3. 没有小节表时的等速回退路径也要互逆；
  4. 非零起点/零长度等边界。

运行（不需要服务）：
  .\\.venv\\Scripts\\python.exe _smoke\\check_seconds_to_score.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.abcp import AbcNote, notes_to_seconds, parse_abc, seconds_to_score  # noqa: E402
from app.rebuild import load_measures                                        # noqa: E402
from app.tempo import derive_tempo_map                                       # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAILS = []
TOL = 1e-6


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))
    if not ok:
        FAILS.append(name)


def roundtrip(abc_text, measures, label):
    """返回 (比较过的音符数, 最大绝对误差)。"""
    score = parse_abc(abc_text)
    warnings = notes_to_seconds(score.notes, bpm=score.bpm or 120.0, measures=measures)
    live = [n for n in score.notes if n.pitch is not None]
    if not live:
        return 0, 0.0, warnings
    spans = [(n.start, n.start + n.duration_seconds, n.pitch) for n in live]
    back = seconds_to_score(spans, bpm=score.bpm or 120.0, measures=measures)
    worst = 0.0
    for n, (onset, dur, pitch) in zip(live, back):
        worst = max(worst, abs(onset - n.onset), abs(dur - n.duration))
    return len(live), worst, warnings


# ---------------------------------------------------------------- 真实样本
print("=" * 74)
print("1. 真实样本逐音往返（output/*/score.abc）")
print("=" * 74)

samples = sorted(ROOT.glob("output/*/score.abc"))
if not samples:
    print("  ⚠ output/ 下没有样本 —— 真实样本这一半跳过（只跑合成用例）")

total_notes = 0
global_worst = 0.0
vary_tempo_seen = 0
for f in samples:
    measures = load_measures(f.parent)
    n, worst, warnings = roundtrip(f.read_text(encoding="utf-8", errors="replace"), measures, f.parent.name)
    if n == 0:
        continue
    total_notes += n
    global_worst = max(global_worst, worst)
    # 变速判定用项目自己的定义：速度表派生出的**段数 > 1** 才算真变速。
    # （直接比逐小节 BPM 的极差会把正常的量化抖动全判成变速，等于没测。）
    varied = len(derive_tempo_map(measures)) > 1 if measures else False
    if varied:
        vary_tempo_seen += 1
    print(f"  {f.parent.name[:38]:40} 音符={n:5} 小节表={len(measures):4} "
          f"{'变速' if varied else '等速'}  最大误差={worst:.3e}")

if total_notes:
    check(f"真实样本往返无损（{total_notes} 个音符，最大误差 {global_worst:.3e}）",
          global_worst < TOL, f"worst={global_worst}")
    check("样本里确实覆盖了变速曲（否则这条测试等于没测 §16/§23 那类 bug）",
          vary_tempo_seen > 0, f"只有 {vary_tempo_seen} 个变速样本")

# ---------------------------------------------------------------- 合成用例
print()
print("=" * 74)
print("2. 合成用例：变速小节表 / 等速回退 / 边界")
print("=" * 74)

# 变速小节表：4 个小节，前两个 120 BPM、后两个 60 BPM（整段速度翻倍地慢）
# score_start/end 用全音符单位；每小节 4 拍 = 1 个全音符
def make_table(bpms):
    out = []
    t = 0.0
    for i, bpm in enumerate(bpms):
        sec = 4 * 60.0 / bpm          # 一小节 4 拍
        out.append({"start": t, "end": t + sec, "score_start": float(i), "score_end": float(i + 1)})
        t += sec
    return out


vary = make_table([120.0, 120.0, 60.0, 60.0])
# 这条表的小节时长：2s, 2s, 4s, 4s（共 12s），记谱每小节 = 1 个全音符。
# 注意秒与记谱**不是线性关系** —— 这正是必须走逆映射、不能拿单一 BPM 硬除的原因。
cases = [
    ((0.0, 2.0), (0.0, 1.0), "第 1 小节整小节（120 BPM）"),
    ((2.0, 3.0), (1.0, 0.5), "第 2 小节前半"),
    ((4.0, 6.0), (2.0, 0.5), "第 3 小节前半（已降到 60 BPM，2 秒只值半个全音符）"),
    ((8.0, 12.0), (3.0, 1.0), "第 4 小节整小节（60 BPM）"),
    ((1.0, 5.0), (0.5, 1.75), "**跨越变速点**：120→60 BPM 边界两侧"),
]
spans = [(st, e, 60) for (st, e), _want, _label in cases]
back = seconds_to_score(spans, bpm=120.0, measures=vary)
worst = 0.0
for (st, e, _), (o, d, _), (_, (want_o, want_d), label) in zip(spans, back, cases):
    err = max(abs(o - want_o), abs(d - want_d))
    worst = max(worst, err)
    print(f"  秒({st:>5}, {e:>5}) → 记谱({o:.6f}, {d:.6f})  期望({want_o}, {want_d})  "
          f"{'OK ' if err < TOL else 'ERR'}  {label}")
check(f"变速小节表下秒→记谱正确（最大误差 {worst:.3e}）", worst < TOL, f"worst={worst}")

# 再用正向函数转回来，确认互逆
probe = [AbcNote(voice="Vocal", onset=o, duration=d, pitch=p) for o, d, p in back]
notes_to_seconds(probe, bpm=120.0, measures=vary)
fwd = [(n.start, n.start + n.duration_seconds) for n in probe]
worst2 = max(max(abs(a - st), abs(b - e)) for (a, b), (st, e, _) in zip(fwd, spans))
check(f"变速小节表下记谱→秒 也回到原值（最大误差 {worst2:.3e}）", worst2 < TOL, f"worst={worst2}")

# 等速回退（无小节表）
eq = [(0.5, 1.25, 60), (2.0, 2.5, 62)]
back_eq = seconds_to_score(eq, bpm=90.0, measures=None)
whole_seconds = 240.0 / 90.0
expect_eq = [(s / whole_seconds, (e - s) / whole_seconds, p) for s, e, p in eq]
check("无小节表时按等速换算（与 notes_to_seconds 的等速分支一致）",
      all(abs(a[0] - b[0]) < TOL and abs(a[1] - b[1]) < TOL for a, b in zip(back_eq, expect_eq)),
      f"{back_eq} vs {expect_eq}")

check("空输入返回空", seconds_to_score([], bpm=120.0) == [])

print()
if FAILS:
    print(f"FAIL（{len(FAILS)} 项）")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("PASS —— 秒 ↔ 记谱位置 严格互逆，变速与等速路径都覆盖")
