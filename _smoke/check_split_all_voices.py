"""主人声 / 器乐旋律 到底会不会重叠？重叠了会不会也被拆成 主人声1/主人声2？

两问两答，都用证据：

A. **真实样本扫描** —— 遍历 `output/*/score.abc`（模型真实产物），按声部统计
   音符数 / 重叠对数 / 同一时刻最多几个音同时响。
   这回答「现状下主人声、器乐旋律有没有重叠」。

B. **合成验证** —— 直接构造带重叠的「主人声」「器乐旋律」轨喂给
   `split_polyphonic_tracks()`，打印它输出的轨名。
   这回答「如果重叠，会不会被拆、拆出来叫什么名字」。

运行（不需要服务）：
  .\\.venv\\Scripts\\python.exe _smoke\\check_split_all_voices.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.abcp import parse_abc                      # noqa: E402
from app.rebuild import split_polyphonic_tracks      # noqa: E402
from app.svpw import SvNote                          # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))
    if not ok:
        FAILS.append(name)


def stats(spans):
    """(重叠对数, 同一时刻最多几个音同时响)。"""
    pairs = 0
    for i in range(len(spans)):
        a0, a1 = spans[i]
        for j in range(i + 1, len(spans)):
            b0, b1 = spans[j]
            if a0 < b1 - 1e-9 and b0 < a1 - 1e-9:
                pairs += 1
    events = []
    for s, e in spans:
        events.append((s, 1))
        events.append((e, -1))
    events.sort(key=lambda x: (x[0], x[1]))   # 同刻先处理结束，避免"首尾相接"算重叠
    cur = peak = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    return pairs, peak


# ---------------------------------------------------------------- A. 真实样本
print("=" * 74)
print("A. 真实样本扫描：output/*/score.abc 里各声部到底有没有重叠")
print("=" * 74)

found_any = False
for abc_path in sorted(ROOT.glob("output/*/score.abc")):
    try:
        score = parse_abc(abc_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        print(f"  [跳过] {abc_path.parent.name}: 解析失败 {exc}")
        continue
    if not score.notes:
        continue
    found_any = True
    # 声部名保持 ABC 里的大小写；pitch is None 的是休止符，不算音符
    by_voice: dict[str, list[tuple[float, float]]] = {}
    for n in score.notes:
        if n.pitch is None:
            continue
        by_voice.setdefault(n.voice, []).append((n.onset, n.onset + n.duration))
    print(f"\n  {abc_path.parent.name}   声部={list(by_voice)}")
    for v, spans in by_voice.items():
        pairs, peak = stats(spans)
        verdict = "→ 会被拆成 " + "/".join(f"{v}{i+1}" for i in range(peak)) if peak > 1 else "→ 单轨，不拆"
        print(f"    {v:<12} 音符={len(spans):>5}  重叠对={pairs:>5}  峰值同时={peak}  {verdict}")

if not found_any:
    print("  （output/ 下没有可用的 score.abc 样本 —— 这一半跳过）")

# ---------------------------------------------------------------- B. 合成验证
print()
print("=" * 74)
print("B. 合成验证：给「主人声」「器乐旋律」故意塞重叠，看拆出来叫什么")
print("=" * 74)

# 主人声：3 个音，其中第 2 个与第 1 个重叠 → 峰值 2 → 应拆成 主人声1 / 主人声2
vocal = [SvNote(0.0, 1.0, 60), SvNote(0.5, 1.5, 64), SvNote(2.0, 3.0, 62)]
# 器乐旋律：4 个音，前 3 个互相重叠 → 峰值 3 → 应拆成 器乐旋律1/2/3
ins = [SvNote(0.0, 2.0, 67), SvNote(0.5, 2.5, 71), SvNote(1.0, 3.0, 74), SvNote(4.0, 5.0, 69)]
# 和弦：本来就复音，峰值 2
chords = [SvNote(0.0, 1.0, 48), SvNote(0.0, 1.0, 52)]

svp_in = [("主人声", vocal), ("器乐旋律", ins), ("和弦", chords)]
midi_in = [("Vocal", [(n.start, n.end, n.pitch, 100) for n in vocal]),
           ("Instrumental", [(n.start, n.end, n.pitch, 100) for n in ins]),
           ("Chords", [(n.start, n.end, n.pitch, 48) for n in chords])]

out_svp, out_midi, detail = split_polyphonic_tracks(svp_in, midi_in)
svp_names = [n for n, _ in out_svp]
print(f"  输入轨名：{ [n for n, _ in svp_in] }")
print(f"  输出轨名：{svp_names}")
print(f"  MIDI 轨名：{[n for n, _ in out_midi]}")
print(f"  拆分明细：{detail}")

check("主人声有重叠 → 拆成 主人声1/主人声2",
      "主人声1" in svp_names and "主人声2" in svp_names, str(svp_names))
check("器乐旋律有重叠 → 拆成 器乐旋律1/2/3",
      all(f"器乐旋律{i}" in svp_names for i in (1, 2, 3)), str(svp_names))
check("和弦有重叠 → 拆成 和弦1/和弦2",
      "和弦1" in svp_names and "和弦2" in svp_names, str(svp_names))
check("MIDI 侧同套分组且用 ASCII 名",
      "Chords1" in [n for n, _ in out_midi] and "Vocal1" in [n for n, _ in out_midi],
      str([n for n, _ in out_midi]))

# 关键不变量：拆完每条轨内部必须严格不重叠，且音符总数一个不少
total_in = sum(len(ns) for _, ns in svp_in)
total_out = sum(len(ns) for _, ns in out_svp)
check(f"音符一个都没丢（{total_in} → {total_out}）", total_in == total_out,
      f"{total_in} vs {total_out}")

all_mono = True
for name, ns in out_svp:
    spans = [(n.start, n.end) for n in ns]
    pairs, peak = stats(spans)
    if pairs or peak > 1:
        all_mono = False
        print(f"    ⚠ {name} 仍有重叠：pairs={pairs} peak={peak}")
check("拆完后每条轨都严格不重叠", all_mono)

# 单音的轨不该被加数字后缀
single = [("主人声", [SvNote(0.0, 1.0, 60), SvNote(1.0, 2.0, 62)])]
o2, _, _ = split_polyphonic_tracks(single, [("Vocal", [(0.0, 1.0, 60, 100), (1.0, 2.0, 62, 100)])])
check("本来就不重叠的轨保持原名、不加后缀", [n for n, _ in o2] == ["主人声"], str([n for n, _ in o2]))

print()
if FAILS:
    print(f"FAIL（{len(FAILS)} 项）")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("PASS —— 拆轨是通用的：任何轨（含主人声/器乐旋律）只要重叠就会被拆")
