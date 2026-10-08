r"""用 `samples/` 里两个真实样本钉住量化的行为（V1→V2 的口径修复）。

这两条是用户实际报上来的"量化有点问题"：

1. `忘川风华录 - 旷古回响.svp` / `（人工）.svp`：SV 工程，onset/duration 全是
   `BAR/8` 的整数倍（八分网格）。V1 口径下 656 个音里 350 个被搬走、最大挪 **1 拍**
   （人工版 363 个音里 162 个被搬走）；V2 口径下它们**原样通过**。
2. `皮囊.mid`：192 PPB，onset 落在 1/3、2/3 拍（八分三连音）。三十二分网格表示不了
   三连音，但量化至少**不能把整条旋律塌成整数拍**：V1 把 357/420 个音搬走、最大 1 拍，
   量化后只剩 2 个不同的拍位；V2 把位移压到**半个网格步（320 tick）以内**，
   拍位层次保住。

    .\.venv\Scripts\python.exe -m pytest tests/test_quantize_samples.py -q
"""

from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.quantize import (  # noqa: E402
    MAX_SHIFT_COLS,
    smart_quantize_musical,
)

SAMPLES = PROJECT_ROOT / "samples"
PPQ = 1920
GRID_TICKS = PPQ // 96                     # 网格步长 = 一拍/96 = 20 tick（32 分 × 三连音）
SVP_BAR = 705_600_000                      # SV 工程的一小节 = 1920 tick（4/4）
QUARTER = 705_600_000                      # 一拍 = 上面的 1/4，所以 SVP 的一小节正好 4 拍

# 工程里第一条音符在 tick 7680（= 第 4 小节），对应 SVP 的 onset 2_822_400_000
SVP_TICK_OFFSET = 7680


def _load_svp(name: str) -> list[tuple[int, int]]:
    """SV 工程 → 工程 tick 的 (start, end)。文件尾部有一个 NUL，要 strip。"""
    path = SAMPLES / name
    if not path.is_file():
        pytest.skip(f"样本不在：{path}")
    doc = json.loads(path.read_bytes().rstrip(b"\x00").decode("utf-8"))
    scale = Fraction(PPQ, SVP_BAR)
    out = []
    for note in doc["tracks"][0]["mainGroup"]["notes"]:
        start = int(Fraction(note["onset"]) * scale)
        end = int(Fraction(note["onset"] + note["duration"]) * scale)
        out.append((start + SVP_TICK_OFFSET, end + SVP_TICK_OFFSET))
    return out


def _load_midi(name: str) -> tuple[list[tuple[int, int]], int]:
    """MIDI → 工程 tick 的 (start, end)，以及 PPB。"""
    path = SAMPLES / name
    if not path.is_file():
        pytest.skip(f"样本不在：{path}")
    import mido

    mid = mido.MidiFile(str(path))
    track = max(mid.tracks, key=len)               # 音符都在最长的那条轨上
    now, pending, got = 0, {}, []
    for msg in track:
        now += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            pending.setdefault(msg.note, []).append(now)
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            if pending.get(msg.note):
                got.append((pending[msg.note].pop(0), now))
    got.sort()
    scale = Fraction(PPQ, mid.ticks_per_beat)
    return [(int(s * scale), int(e * scale)) for s, e in got], mid.ticks_per_beat


# --------------------------------------------------- 旷古回响：已经在网格上就别动
@pytest.mark.parametrize("name", [
    "忘川风华录 - 旷古回响.svp",
    "忘川风华录 - 旷古回响（人工）.svp",
])
def test_kuguang_notes_are_left_untouched(name):
    """两个 SV 工程的 onset 全在网格上 → 量化必须逐 tick 原样返回。

    V1 口径下这两个文件分别有 350/656、162/363 个音被搬走（最大 1 拍），
    这就是用户看到的"量化有点问题"。
    """
    notes = _load_svp(name)
    assert notes, "样本里没有音符"

    # 前提：这些 onset 本来就是八分/十六分位置（在 32 分网格上）
    off_grid = [n for n in notes if n[0] % GRID_TICKS]
    assert not off_grid, f"样本前提不成立：{len(off_grid)} 个 onset 不在网格上"

    result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok, result.message
    assert list(result.notes) == notes, "本来就在网格上的旋律被搬走了"
    assert result.moved == 0


def test_kuguang_manual_has_finer_rhythm_than_the_machine_version():
    """两个文件都是**十六分**粒度的网格，但机扒版几乎只落在八分/四分上。

    也就是说人工版多出来的层次（八分以下的 16 分位置）才是"谱面信息"。V1 口径下
    量化把这些层次抹平成八分——手工修过的节奏会被量化毁掉。
    """
    sixteenth = PPQ // 4
    machine = [s for s, _ in _load_svp("忘川风华录 - 旷古回响.svp")]
    manual = [s for s, _ in _load_svp("忘川风华录 - 旷古回响（人工）.svp")]

    # 前提：两个文件都在十六分网格上（32 分网格自然覆盖）
    assert all(s % sixteenth == 0 for s in machine + manual)

    eighth = PPQ // 2
    machine_off = sum(1 for s in machine if s % eighth)
    manual_off = sum(1 for s in manual if s % eighth)
    assert machine_off <= 2, f"机扒版应当基本只在八分位置，实际有 {machine_off} 个例外"
    assert manual_off >= 30, f"人工版应当有明显更多的八分以下层次，实际只有 {manual_off} 个"


# --------------------------------------------------- 皮囊：三连音
def test_pisang_triplets_are_represented_by_the_grid():
    """`皮囊.mid` 是 192 PPB：onset 落在 1/6、1/3、1/2、2/3、5/6 拍（三连音）。

    **网格能表示三连音**：一拍 = 1920 project tick 时，1/3 拍 = 640、1/6 拍 = 320，
    而网格步长是 20 tick（一拍 / 96 = 32 × 3）——640 = 32 × 20、320 = 16 × 20，都对得上。
    这条测试钉住的就是这件事：**三连音不该因为"落不到格子"而被搬走**。

    只有 2 的幂的网格做不到这一点（一拍/32 = 60、一拍/64 = 30，都不是 640 / 320 的因子）。
    """
    notes, ppb = _load_midi("皮囊.mid")
    assert notes, "样本里没有音符"
    assert ppb == 192, f"样本前提变了：ticks_per_beat={ppb}"

    triplet = [s for s, _ in notes
               if (Fraction(s, PPQ) % 1) not in (0, Fraction(1, 2))]
    assert len(triplet) > 50, f"样本前提不成立：只有 {len(triplet)} 个非整数拍位置"

    # 前提：所有 onset 本来就在网格上（20 tick）
    off = [s for s, _ in notes if s % GRID_TICKS]
    assert not off, f"前提不成立：{len(off)} 个 onset 不在 {GRID_TICKS} tick 网格上"

    result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok, result.message

    # 1) 所有边界落在网格上，时值非零
    for start, end in result.notes:
        assert start % GRID_TICKS == 0 and end % GRID_TICKS == 0
        assert end > start

    # 2) 位移封顶在一个三连音步（1/3 拍 = 640 tick）：量化不许"搬家"
    #    （V1 会把音挪整整一拍 1920 tick）
    worst = max(abs(new[0] - old[0]) for old, new in zip(notes, result.notes))
    assert worst <= PPQ // 3, f"最大位移 {worst} tick 超过一个三连音步（{PPQ // 3}）"

    # 3) 节奏没被塌成单一拍位：量化后仍有多档起音位置
    positions = {Fraction(s, PPQ) % 1 for s, _ in result.notes}
    assert len(positions) >= 3, f"量化把节奏塌平了，只剩 {sorted(map(str, positions))}"


def test_pisang_triplets_are_left_alone():
    """`皮囊.mid` 的三连音**原样通过**（420 个音一个都不动）。

    这条曾经是反例：网格落得下三连音、但阶梯不认它（1/3 拍落在兜底 −0.8λ），
    于是 198 个三连音位里只有 1 个留下。加上三连音两档 + 单音位移上限之后修好。
    """
    notes, _ = _load_midi("皮囊.mid")
    result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok, result.message
    assert list(result.notes) == list(notes), "三连音被搬走了"
    assert result.moved == 0


# --------------------------------------------------- 巴拉莱卡：三连音为主 + 四分音符
def test_balalaika_triplets_and_quarters_are_left_alone():
    """`巴拉莱卡（The Rod）.mid`：**三连音为主、夹四分音符**，量化后几乎不动。

    这是把三连音档去掉时最惨的一个样本：569 个音里 **479 个被搬走**，
    126 个 1/3 拍与 167 个 2/3 拍**全部**被吸到 3/8 / 3/4 上，三连音节奏被抹平。
    加上三连音两档 + 单音位移上限之后：**567/569 原样通过**，最大位移 40 tick
    （= 单音上限）。
    """
    notes, ppb = _load_midi("巴拉莱卡（The Rod）.mid")
    assert notes, "样本里没有音符"
    assert ppb == 192, f"样本前提变了：ticks_per_beat={ppb}"

    # 前提：三连音是主体（1/3 与 2/3 拍上有一半以上的音），并且全都落在网格上
    tri = [s for s, _ in notes
           if (Fraction(s, PPQ) % 1) in (Fraction(1, 3), Fraction(2, 3))]
    assert len(tri) > len(notes) // 2, f"前提不成立：只有 {len(tri)} 个三连音位"
    assert not [s for s, _ in notes if s % GRID_TICKS]

    result = smart_quantize_musical(notes, simplicity=0.0, ticks_per_quarter=PPQ)
    assert result.ok, result.message

    # 1) 绝大多数音原样不动
    assert result.moved <= len(notes) * 0.02, f"被搬走 {result.moved}/{len(notes)}"

    # 2) 位移封顶在单音上限
    worst = max(abs(new[0] - old[0]) for old, new in zip(notes, result.notes))
    assert worst <= MAX_SHIFT_COLS * GRID_TICKS, f"最大位移 {worst} 超过单音上限"

    # 3) 三连音的**数目**不能被抹掉（允许 ±2% 的吸附误差）
    kept = sum(1 for s, _ in result.notes
               if (Fraction(s, PPQ) % 1) in (Fraction(1, 3), Fraction(2, 3)))
    assert abs(kept - len(tri)) <= max(2, len(tri) // 50), \
        f"三连音位从 {len(tri)} 掉到 {kept}"

    # 4) 四分音符位置也保住
    quarters = sum(1 for s, _ in notes if s % PPQ == 0)
    assert sum(1 for s, _ in result.notes if s % PPQ == 0) >= quarters - 2
