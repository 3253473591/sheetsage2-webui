"""回归：调号解析（ABC 简写 K:Cm / K:Em / K:F#m）。

为什么单独测这一条
------------------
模型产出的调号是 **ABC 简写**：`K:Cm`、`K:Em`、`K:F#m`、`K:Am` —— **都没有冒号**。
早期实现只按 `root:mode` 切分（`key.partition(":")`），于是 `Cm` 被当成「C 大调」，
该降的 B/E/A 全变还原音：**旋律变成大调音阶，配着 C 小调的和弦，听起来就是跑调**
（用户实测反馈「导入后能变速了，但是跑调了」）。`Em/F#m/Am` 同样中招。

    .venv\\Scripts\\python.exe _smoke\\check_key_signature.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.abcp import _key_accidentals, parse_abc, split_key  # noqa: E402

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
    if not ok:
        FAIL.append(name)


# ---------------- 1. 调号 → 升降数量 ----------------
# (调号, 期望的升降合计)：升为正、降为负
CASES = [
    ("Cm", -3), ("C:min", -3), ("Cminor", -3), ("C:m", -3),   # ← 模型的实际写法
    ("C", 0), ("C:maj", 0), ("Cmaj", 0), ("Cmajor", 0),
    ("Em", 1), ("E:min", 1), ("F#m", 3), ("F#:min", 3), ("Am", 0),
    ("Bm", 2), ("G#m", 5), ("Dm", -1), ("Gm", -2), ("C#m", 4),
    ("Bb", -2), ("Eb", -3), ("Ab", -4), ("F#", 6), ("Db", -5),
]
for key, want in CASES:
    got = sum(_key_accidentals(key).values())
    check(f"K:{key} 升降合计 = {want}", got == want, f"实际 {got}")

check("split_key 兼容冒号写法", split_key("C:min") == ("C", "min"), str(split_key("C:min")))
check("split_key 兼容简写", split_key("Cm") == ("C", "m"), str(split_key("Cm")))
check("split_key 无调式时按大调", split_key("Bb") == ("Bb", "major"), str(split_key("Bb")))

# ---------------- 2. 端到端：同一段 ABC 在不同调号下的实际音高 ----------------
def pitches(abc: str) -> list[int]:
    return [n.pitch for n in sorted(parse_abc(abc).notes, key=lambda n: n.onset) if n.pitch is not None]


def tune(key: str) -> str:
    return f"X:1\nM:4/4\nL:1/4\nK:{key}\nE B A F|\n"


# C 小调：E→Eb(63)、B→Bb(70)、A→Ab(68)、F 不变(65)
check("K:Cm 的实际音高（E→Eb, B→Bb, A→Ab）",
      pitches(tune("Cm")) == [63, 70, 68, 65], str(pitches(tune("Cm"))))
# 还原写法 K:C 应当是自然音
check("K:C 的实际音高（全还原）",
      pitches(tune("C")) == [64, 71, 69, 65], str(pitches(tune("C"))))
# E 小调：F→F#
check("K:Em 的实际音高（F→F#）", pitches(tune("Em"))[3] == 66, str(pitches(tune("Em"))))
# F# 小调：E 保持还原、F 升（与 F# 大调的 E# 不同）
check("K:F#m 与 K:F# 不同（E vs E#）",
      pitches(tune("F#m"))[0] != pitches(tune("F#"))[0],
      f"{pitches(tune('F#m'))[0]} vs {pitches(tune('F#'))[0]}")

# ---------------- 3. 真实产物：C 小调的曲子不该出现还原 E ----------------
probe = ROOT / "output" / "_varytempo_probe" / "score.abc"
if probe.is_file():
    import collections

    score = parse_abc(probe.read_text(encoding="utf-8"))
    voc = [n.pitch for n in score.notes if (n.voice or "").lower() == "vocal" and n.pitch is not None]
    cnt = collections.Counter(p % 12 for p in voc)
    print(f"真实样本（模型调号 Cm）音高类：", dict(sorted(cnt.items(), key=lambda x: -x[1])[:8]))
    check("真实 Cm 曲子里降 E(D#) 数量 > 还原 E", cnt[3] > cnt[4], f"D#={cnt[3]} E={cnt[4]}")
    check("真实 Cm 曲子里降 B(A#) 存在", cnt[10] > 0, str(cnt.get(10)))
else:
    print("（跳过真实样本检查：缺 output/_varytempo_probe）")

print("\nFAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)
