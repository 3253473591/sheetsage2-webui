"""ABC 反向序列化（钢琴卷帘落盘）的无损往返测试。

背景
----
「乐谱编辑」要从 ABC 文本框换成图形化钢琴卷帘：卷帘改音符，改动仍以 ABC 落盘，
导出管线（`app.rebuild` 解析 ABC）一行都不用改。所以 `app.abcs.score_to_abc()`
必须是 `app.abcp.parse_abc()` 的**严格逆运算**，本脚本就是这条契约的证据。

三部分
------
A. **真实数据往返（最重要）**：遍历 `output/*/score.abc`（模型真实产物）。
   每条声部单独取出 `(onset, duration, pitch)`（跳过休止）→ `score_to_abc()` →
   再 `parse_abc(merge_ties=False)` → 逐个音符比对声部/音高/起点/时值。
   复音声部会被跳过并**打印出来**（本模块只接受单音声部），不静默丢数据。

B. **边角情形**：升/降/双升降/还原号、极短时值、跨多小节长音、起点 0、
   开头留空、中央 C 上下八度（逗号/撇号）、单音声部、空声部列表。

C. **单音约束**：同声部内重叠必须 `ValueError`（交给调用方拆轨），而不是静默丢音。

运行（不需要服务）：
  .\\.venv\\Scripts\\python.exe _smoke\\check_abc_roundtrip.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.abcp import parse_abc                        # noqa: E402
from app.abcs import header_from_abc, score_to_abc    # noqa: E402
from app.rebuild import monophonic_groups             # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAILS = []
TOL = 1e-6

#: 全局统计（在真实数据那一段里累计，末尾打印真实数字）
STATS = {"files": 0, "voices": 0, "notes": 0, "max_err": 0.0, "overlap_skipped": 0}


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))
    if not ok:
        FAILS.append(name)
    return ok


# --------------------------------------------------------------------------
# 公共工具
# --------------------------------------------------------------------------
def notes_by_voice(abc_text, *, merge_ties=False):
    """解析 ABC，返回 ``{声部: [(onset, duration, pitch), ...]}``（跳过休止，按起点排序）。"""
    out = {}
    for note in parse_abc(abc_text, merge_ties=merge_ties).notes:
        if note.pitch is None:
            continue
        out.setdefault(note.voice, []).append((note.onset, note.duration, note.pitch))
    for value in out.values():
        value.sort()
    return out


def compare(want, got, label):
    """逐个音符比对，返回 ``(错误条数, 最大绝对误差, 说明)``。"""
    problems = []
    max_err = 0.0
    for voice, want_notes in want.items():
        got_notes = got.get(voice, [])
        if len(got_notes) != len(want_notes):
            problems.append(f"{voice} 音符数 {len(want_notes)} → {len(got_notes)}")
            continue
        for index, (a, b) in enumerate(zip(want_notes, got_notes)):
            onset_err = abs(a[0] - b[0])
            dur_err = abs(a[1] - b[1])
            max_err = max(max_err, onset_err, dur_err)
            if a[2] != b[2]:
                problems.append(f"{voice}[{index}] 音高 {a[2]} → {b[2]}")
            elif onset_err > TOL or dur_err > TOL:
                problems.append(
                    f"{voice}[{index}] onset/dur ({a[0]},{a[1]}) → ({b[0]},{b[1]})"
                )
            if len(problems) >= 4:
                break
    for voice in got:
        if voice not in want:
            problems.append(f"多出声部 {voice!r}")
    detail = "; ".join(problems[:4])
    return len(problems), max_err, f"{label}: {detail}" if detail else label


def roundtrip(abc_text, label):
    """原文 → 音符 → score_to_abc → 再解析，返回 ``(ok, detail, 最大误差, 音符数)``。

    复音声部跳过（本模块只收单音，见 :func:`app.abcs.score_to_abc`）：跳过时会显式
    体现在 detail 里，绝不假装通过。
    """
    try:
        want = notes_by_voice(abc_text)
    except Exception as exc:                       # noqa: BLE001 - 解析失败也要报出来
        return False, f"{label}: 原始 ABC 解析失败 {exc!r}", 0.0, 0

    voices, skipped = [], []
    for voice, notes in want.items():
        spans = [(s, s + d, p) for s, d, p in notes]
        if len(monophonic_groups(spans)) > 1:
            skipped.append(voice)
            continue
        voices.append((voice, notes))
    if not voices:
        return False, f"{label}: 没有单音声部（全是复音）", 0.0, 0

    try:
        out = score_to_abc(header_from_abc(abc_text), voices)
        got = notes_by_voice(out)
    except Exception as exc:                       # noqa: BLE001
        return False, f"{label}: score_to_abc/再解析失败 {exc!r}", 0.0, 0

    want_single = {v: n for v, n in voices}
    errors, max_err, detail = compare(want_single, got, label)
    if skipped:
        detail += f"（跳过复音声部 {','.join(skipped)}）"
    return errors == 0, detail, max_err, sum(len(n) for _, n in voices)


def expect(abc_text, label):
    """边角情形用：跑一次往返并直接记 PASS/FAIL，返回最大误差。"""
    ok, detail, max_err, notes = roundtrip(abc_text, label)
    check(f"{label}（{notes} 音，误差 {max_err:.3g}）", ok, detail)
    return max_err


def expect_voices(header, voices, label):
    """直接给 ``(header, voices)`` 的情形：走 score_to_abc → parse_abc 再逐音比对。"""
    want = {}
    for voice, notes in voices:
        if notes:
            want[voice] = sorted((float(o), float(d), int(p)) for o, d, p in notes)
    try:
        out = score_to_abc(header, voices)
    except Exception as exc:                       # noqa: BLE001 - 崩溃也要算失败而不是崩测试
        check(label, False, f"score_to_abc 抛异常 {exc!r}")
        return None
    got = {}
    for note in parse_abc(out, merge_ties=False).notes:
        if note.pitch is None:
            continue
        got.setdefault(note.voice, []).append((note.onset, note.duration, note.pitch))
    for value in got.values():
        value.sort()
    errors, max_err, detail = compare(want, got, label)
    total = sum(len(v) for v in want.values())
    check(f"{label}（{total} 音，误差 {max_err:.3g}）", errors == 0, detail)
    return out


def abc_with_unit(abc_text, numerator, denominator):
    """换掉输出里的 ``L:``，其它一律不动（用来验证时值记号是显式写出的）。"""
    out, replaced = [], False
    for line in abc_text.split("\n"):
        if line.startswith("L:"):
            out.append(f"L:{numerator}/{denominator}")
            replaced = True
        else:
            out.append(line)
    assert replaced, "生成的 ABC 里没有 L: 行"
    return "\n".join(out)


def unit_of(abc_text):
    """读出 ``L:`` 的时值（全音符）。"""
    for line in abc_text.split("\n"):
        if line.startswith("L:"):
            num, _, den = line[2:].partition("/")
            return float(num) / float(den)
    raise AssertionError("生成的 ABC 里没有 L: 行")


MINIMAL_HEADER = "X:1\nT:check\nM:4/4\nL:1/4\nQ:1/4=120\nK:C\n"


# ==========================================================================
# A. 真实数据往返
# ==========================================================================
print("=" * 76)
print("A. 真实数据往返：output/*/score.abc（模型真实产物）")
print("=" * 76)

samples = sorted(ROOT.glob("output/*/score.abc"))
if not samples:
    print("  [注意] output/*/score.abc 一个都没有 —— 这一段没有真实数据可用，"
          "只能靠下面的手写样本。")
else:
    for path in samples:
        text = path.read_text(encoding="utf-8", errors="replace")
        ok, detail, max_err, notes = roundtrip(text, path.parent.name)
        if not ok:
            check(f"真实往返 {path.parent.name}", False, detail)
            continue
        STATS["files"] += 1
        STATS["notes"] += notes
        STATS["max_err"] = max(STATS["max_err"], max_err)
        STATS["voices"] += len(notes_by_voice(text))
        print(f"  OK   {path.parent.name:<46} 音符={notes:>4}  最大误差={max_err:.3g}"
              + (("  " + detail.split("（", 1)[1]) if "（跳过" in detail else ""))

    check(f"真实样本全部无损往返（{STATS['files']} 个文件 / {STATS['notes']} 个音符）",
          STATS["files"] > 0 and not [f for f in FAILS if "真实往返" in f],
          f"通过 {STATS['files']} 个")
    print(f"  合计：文件 {STATS['files']} 个，音符 {STATS['notes']} 个，"
          f"最大绝对误差 {STATS['max_err']:.3g}")

# ==========================================================================
# B. 边角情形
# ==========================================================================
print()
print("=" * 76)
print("B. 边角情形")
print("=" * 76)

corner_cases = [
    ("升号 ^", MINIMAL_HEADER + "V:A\n^C4 ^F4 ^G4 |\n"),
    ("降号 _", MINIMAL_HEADER + "V:A\n_B4 _E4 _A4 |\n"),
    ("双升/双降 ^^ __", MINIMAL_HEADER + "V:A\n^^C4 __C4 ^^F4 __B4 |\n"),
    ("还原号 =（K:G 下调号已升 F）", "X:1\nT:check\nM:4/4\nL:1/4\nQ:1/4=120\nK:G\nV:A\n=F4 =C4 ^F4 |\n"),
    ("还原号 =（K:Eb 下调号已降 B/E/A）",
     "X:1\nT:check\nM:4/4\nL:1/4\nQ:1/4=120\nK:Eb\nV:A\n=B4 =E4 =A4 _B4 |\n"),
    ("极短时值 1/32 全音符（= 1/8 个四分音符）",
     "X:1\nT:check\nM:4/4\nL:1/32\nQ:1/4=120\nK:C\nV:A\nE/2E/2E/2E/2 z/2E/2E/2E/2 |\n"),
    ("极短时值 1/128 全音符 + 起点对齐",
     "X:1\nT:check\nM:4/4\nL:1/128\nQ:1/4=120\nK:C\nV:A\nC2 z2 D2 z122 |\n"),
    ("跨多小节长音（16 个全音符）",
     MINIMAL_HEADER + "V:A\nC64 |\n"),
    ("音符起点为 0", MINIMAL_HEADER + "V:A\nE4 E4 |\n"),
    ("开头留空（首音不在 0）", MINIMAL_HEADER + "V:A\nz8 E4 |\n"),
    ("中央 C 以下：逗号八度",
     MINIMAL_HEADER + "V:A\nC,4 E,4 G,4 C,,4 |\n"),
    ("中央 C 以上：撇号八度",
     MINIMAL_HEADER + "V:A\nc4 e4 g4 c'4 c''4 |\n"),
    ("单音声部", MINIMAL_HEADER + "V:A\nG4 |\n"),
    ("多声部各自留空", MINIMAL_HEADER + "V:A\nz4 C4 |\nV:B\nE4 z4 |\n"),
    ("休止贯穿整首（无音符声部，应不输出）",
     MINIMAL_HEADER + "V:A\nC4 |\nV:B\nz16 |\n"),
    ("变拍号 3/4", "X:1\nT:check\nM:3/4\nL:1/4\nQ:1/4=100\nK:F\nV:A\nF4 G4 A4 | _B4 c4 |\n"),
]
for label, text in corner_cases:
    expect(text, label)

# 空声部列表：只应产出头部，且再解析不出任何音符
empty_out = score_to_abc({"X": "1", "T": "空", "M": "4/4", "Q": "1/4=120", "K": "C"}, [])
check("空声部列表 → 只有头部、解析不出音符",
      "K:C" in empty_out and "V:" not in empty_out and not parse_abc(empty_out).notes,
      repr(empty_out))

# 单个声部但没有任何音符：同样不输出 V: 行（往返无从校验，见模块文档）
blank_out = score_to_abc({"K": "C"}, [("Vocal", [])])
check("声部存在但无音符 → 不输出该声部",
      "V:Vocal" not in blank_out and "V:" not in blank_out, repr(blank_out))

# --------------------------------------------------------------------------
# B2. 「基本单位」家族：时值/起点/空隙的组合不能把 L: 逼歪（上游报过的崩溃家族）
# --------------------------------------------------------------------------
print()
print("B2. 基本单位与时值（L: 必须整除所有写出来的长度）")
print("   上游报过的 repro：3 个全音符的长音曾把基本单位算成 3，写出 L:3/1 并崩溃")
print("-" * 76)

header_std = {"X": "1", "T": "t", "M": "4/4", "L": "1/8", "Q": "1/4=120", "K": "C"}
base_cases = [
    ("上游 repro：onset 0 + 3 个全音符", [(0.0, 3.0, 60)]),
    ("2 个全音符、不在小节线上（onset 0.25）", [(0.25, 2.0, 60)]),
    ("时值与起点共享大公约数（onset 1.5 / 时值 1.5）", [(1.5, 1.5, 60)]),
    ("单个极短音（1/128 全音符）", [(0.0, 1 / 128, 72)]),
    ("大段静默（0 → 5.0 的空隙）", [(0.0, 0.25, 60), (5.0, 0.25, 62)]),
    ("低音 MIDI 28（C,，逗号八度）", [(0.0, 0.5, 28)]),
    ("低音 MIDI 33", [(0.0, 0.5, 33)]),
    ("高音 MIDI 96（撇号八度）", [(0.0, 0.5, 96)]),
    ("高音 MIDI 103", [(0.0, 0.5, 103)]),
    ("各种临时记号同声部（还原/升/降/双升/双降）",
     [(0.0, 0.25, 60), (0.25, 0.25, 61), (0.5, 0.25, 61), (0.75, 0.25, 59),
      (1.0, 0.25, 62), (1.25, 0.25, 58), (1.5, 0.5, 60)]),
    ("变拍号下的小节线穿插", [(0.0, 0.25, 60), (2.0, 0.25, 64), (4.0, 1.0, 67)]),
]
for label, notes in base_cases:
    expect_voices(header_std, [("Vocal", notes)], label)

# 非 C 调号下的临时记号（`=` 必须覆盖调号，不能只抵消一个升降号）
for key in ("F#m", "Eb", "G", "Cm"):
    expect_voices(
        {"X": "1", "T": "t", "M": "4/4", "Q": "1/4=120", "K": key},
        [("Vocal", [(0.0, 0.25, 60), (0.25, 0.25, 61), (0.5, 0.25, 59), (0.75, 0.25, 62),
                    (1.0, 0.25, 58), (1.25, 0.5, 65)])],
        f"K:{key} 下显式记号覆盖调号",
    )

# 多声部：起点/时值各不相同
expect_voices(
    header_std,
    [
        ("Vocal", [(0.0, 0.25, 60), (1.0, 1.5, 64)]),
        ("Ins", [(0.125, 0.375, 55), (2.0, 2.0, 43), (4.5, 0.125, 79)]),
    ],
    "双声部（Vocal + Ins）起点/时值各异",
)

# 一个声部 1 个音 + 另一个声部 2 个音
expect_voices(
    header_std,
    [("Vocal", [(0.0, 1.0, 60)]), ("Ins", [(0.5, 0.5, 67), (1.0, 0.5, 69)])],
    "Vocal 1 音 + Ins 2 音",
)

# 时值记号必须是显式写出的：把输出的 L: 换成别的值，音符个数、音高、起点都不该变，
# 而时值必须**等比缩放**（缩放系数 = 新 L / 原 L）。哪个记号偷懒没写时值（靠 L: 默认值
# 顶），缩放就对不上。这同时验证了「每个音都带显式时值记号」这条要求。
l_explicit = score_to_abc(header_std, [("Vocal", [(0.0, 0.25, 60),
                                                  (0.25, 0.75, 64), (2.0, 1.5, 67)])])


def note_events(abc_text):
    """``[(onset, duration, pitch), ...]``，只取有声音符（休止的 pitch 是 None）。"""
    return [(n.onset, n.duration, n.pitch) for n in parse_abc(abc_text, merge_ties=False).notes
            if n.pitch is not None]


scale = 0.5 / unit_of(l_explicit)
as_written = note_events(l_explicit)
rescaled = note_events(abc_with_unit(l_explicit, 1, 2))
# 注意 onset 是**累计**的：L 一改，前面所有时值都缩放，onset 自然跟着缩放，
# 所以这里只比对「音高不变 + 时值等比缩放」。起点是否正确由上面的往返段负责。
l_ok = (
    len(as_written) == len(rescaled)
    and all(a[2] == b[2] and abs(a[1] * scale - b[1]) < 1e-9
            for a, b in zip(as_written, rescaled))
    and bool(as_written)
)
check("每个音的时值都是显式写出的（换掉 L: 后时值等比缩放、音高不变）", l_ok,
      f"原 L={unit_of(l_explicit)}：{as_written}；L:1/2 后 {rescaled}")

# 非 2 的幂分母的时值应当报错（不能卡死、也不能悄悄近似）
try:
    score_to_abc({"K": "C"}, [("A", [(0.0, 1 / 3, 60)])])
    check("1/3 全音符（非 2 的幂）→ 报错而不是卡死", False, "没有抛异常")
except ValueError:
    check("1/3 全音符（非 2 的幂）→ 报错而不是卡死", True)

# 时值精度超过上限时应报错，而不是悄悄近似
try:
    score_to_abc({"K": "C"}, [("A", [(0.0, 1.0 / 100000, 60)])])
    check("精度超上限 → ValueError", False, "没有抛异常")
except ValueError as exc:
    check("精度超上限 → ValueError", "精度" in str(exc), str(exc))

# 非正时值应报错
for bad, why in (((0.0, 0.0, 60), "时值 0"), ((-1.0, 0.25, 60), "负起点")):
    try:
        score_to_abc({"K": "C"}, [("A", [bad])])
        check(f"非法输入 {why} → ValueError", False, "没有抛异常")
    except ValueError as exc:
        check(f"非法输入 {why} → ValueError", True, str(exc))

# ==========================================================================
# C. 单音约束
# ==========================================================================
print()
print("=" * 76)
print("C. 单音约束（重叠必须报错，交给调用方拆轨）")
print("=" * 76)

overlapping = [("Vocal", [(0.0, 1.0, 60), (0.5, 1.0, 64), (2.0, 0.5, 62)])]
try:
    score_to_abc({"K": "C"}, overlapping)
    check("同声部重叠 → ValueError", False, "没有抛异常")
except ValueError as exc:
    check("同声部重叠 → ValueError", "重叠" in str(exc) and "Vocal" in str(exc), str(exc))

# 首尾相接不算重叠（真实数据里大量存在，不能误报）
try:
    score_to_abc({"K": "C"}, [("Vocal", [(0.0, 0.5, 60), (0.5, 0.5, 62), (1.0, 0.5, 64)])])
    check("首尾相接不算重叠", True)
except ValueError as exc:
    check("首尾相接不算重叠", False, str(exc))

# 同一时刻两个不同音高同样要报错
try:
    score_to_abc({"K": "C"}, [("Vocal", [(0.0, 1.0, 60), (0.0, 1.0, 64)])])
    check("同刻不同音高 → ValueError", False, "没有抛异常")
except ValueError as exc:
    check("同刻不同音高 → ValueError", True, str(exc))

# ==========================================================================
# 汇总
# ==========================================================================
print()
print("=" * 76)
if FAILS:
    print(f"FAIL（{len(FAILS)} 项）")
    for name in FAILS:
        print("  -", name)
    sys.exit(1)

if STATS["files"]:
    print(f"PASS —— 真实数据 {STATS['files']} 个文件 / {STATS['notes']} 个音符全部无损往返，"
          f"最大绝对误差 {STATS['max_err']:.3g}")
else:
    print("PASS —— 但没有真实样本（output/*/score.abc 为空），只验证了手写边角情形")
