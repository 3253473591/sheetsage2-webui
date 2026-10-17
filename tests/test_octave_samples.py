r"""用 `samples/对齐/` 里那对 SVP 钉住八度一致性校正的行为。

那对文件是同一首《我本将心向明月》的两个版本：

- `王朝1982,朱旭BooBoo - 我本将心向明月.svp` —— 本项目跑出来的机扒工程；
- `…（人工）.svp` —— 用户在 Synthesizer V Studio 里修过的版本。

**这对样本暴露的不是"时间对齐"问题**：381 个机扒音里有 372 个 onset 与人工版
逐 tick 相同、时值分布也几乎一样。差的是**八度**——同一段副歌在歌里出现三次，
机扒给了 66 / 54 / 66，人工版把中间那次抬回 66；381 个音里 76 个差整整 12 个半音。

所以这里断言的是一条能自己验证的不变量：**重复段必须同高**。

    .\.venv\Scripts\python.exe -m pytest tests/test_octave_samples.py -q
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

from sheetsage2_webui import octave  # noqa: E402

SAMPLES = PROJECT_ROOT / "samples"
ALIGN = SAMPLES / "对齐"
QUANTIZE = SAMPLES / "量化"

MACHINE = "王朝1982,朱旭BooBoo - 我本将心向明月.svp"
MANUAL = "王朝1982,朱旭BooBoo - 我本将心向明月（人工）.svp"

#: SVP 的 blick 与工程 tick 的关系：一小节 705_600_000 blick = 1920 tick。
PPQ = 1920
SVP_BAR = 705_600_000
PPQ_SCALE = Fraction(PPQ, SVP_BAR)
#: 两个 SVP 里记的速度（用来把 tick 换成秒——`REST_GAP` 是秒）。
BPM = 90.001


def _load_svp(directory: Path, name: str) -> list[tuple[int, float, float, int]]:
    """SV 工程 → `(tick, 起始秒, 结束秒, 音高)`，按时间排好。文件尾部有一个 NUL。"""
    path = directory / name
    if not path.is_file():
        pytest.skip(f"样本不在：{path}")
    doc = json.loads(path.read_bytes().rstrip(b"\x00").decode("utf-8"))
    rows = []
    for track in doc["tracks"]:
        for note in track["mainGroup"]["notes"]:
            tick = int(Fraction(note["onset"]) * PPQ_SCALE)
            end_tick = int(Fraction(note["onset"] + note["duration"]) * PPQ_SCALE)
            rows.append((tick, tick / PPQ * 60.0 / BPM,
                         end_tick / PPQ * 60.0 / BPM, note["pitch"]))
    rows.sort()
    return rows


def _notes(rows):
    return [octave.Note(start=start, end=end, pitch=pitch, track=0)
            for _tick, start, end, pitch in rows]


@pytest.fixture(scope="module")
def machine():
    return _load_svp(ALIGN, MACHINE)


@pytest.fixture(scope="module")
def manual():
    return _load_svp(ALIGN, MANUAL)


@pytest.fixture(scope="module")
def fixed(machine):
    shifts, plan_ = octave.plan(_notes(machine))
    return shifts, plan_


def _paired(machine, manual, shifts):
    """按 onset（tick）配对，返回 `(机扒音高, 我们校正后, 人工音高, 我们平移了几)`。"""
    human = {tick: pitch for tick, _s, _e, pitch in manual}
    out = []
    for (tick, _s, _e, pitch), shift in zip(machine, shifts):
        if tick in human:
            out.append((pitch, pitch + shift, human[tick], shift))
    return out


# ------------------------------------------------------------------ 前提
def test_the_pair_shares_its_timing(machine, manual):
    """前提：这不是"对不上时间"——372 个 onset 逐 tick 相同，音数也只差 2。"""
    assert len(machine) == 381
    assert len(manual) == 379
    shared = {t for t, _s, _e, _p in machine} & {t for t, _s, _e, _p in manual}
    assert len(shared) == 372


def test_the_human_lifted_whole_octaves(machine, manual):
    """前提：人工版改动的**大头是整八度**——76 个音正好 +12，
    另外 4 个位置的差值不是整八度（{7, 8, 10, 14}），全都出在人工合并/新增音的地方，
    不是八度问题（那 4 个我们本来就不该跟）。"""
    human = {tick: pitch for tick, _s, _e, pitch in manual}
    deltas = [human[t] - p for t, _s, _e, p in machine if t in human]
    assert len(deltas) == 372
    assert sum(1 for d in deltas if d == 12) == 76
    assert sorted(d for d in deltas if d not in (0, 12)) == [7, 8, 10, 14]


# ------------------------------------------------------------------ 结果
def test_the_fix_reproduces_the_human_edit(machine, manual, fixed):
    """核心断言：与人工版逐音相同的比例从 292/372（79%）升到 361/372（97%），
    人工改过的 80 个音里我们跟对 69 个。"""
    shifts, _plan = fixed
    pairs = _paired(machine, manual, shifts)
    assert len(pairs) == 372

    before = sum(1 for p, _q, h, _s in pairs if p == h)
    after = sum(1 for p, q, h, _s in pairs if q == h)
    changed = [(p, q, h) for p, q, h, _s in pairs if h != p]
    hit = sum(1 for p, q, h in changed if q == h)

    assert (before, after) == (292, 361)
    assert len(changed) == 80
    assert hit == 69


def test_only_the_repeated_sections_are_touched(machine, fixed):
    """只有那两段重复段动，别的音一个都不动（宁可不改，也不乱改）。"""
    shifts, plan_ = fixed
    moved = [(tick, shift) for (tick, _s, _e, _p), shift in zip(machine, shifts)
             if shift]
    assert len(moved) == 73
    assert {shift for _tick, shift in moved} == {12}
    # 机扒第 103 拍（副歌尾句，section 0）到第 197 拍（副歌，section 1）
    assert min(tick for tick, _s in moved) == 103 * PPQ
    assert max(tick for tick, _s in moved) == 198 * PPQ - 1 * 960
    assert plan_.stats.moved_notes == 73
    assert plan_.stats.groups == 2
    assert plan_.stats.groups < plan_.stats.links  # 平票的那几处没动手


def test_the_manual_version_is_left_alone(machine, manual, fixed):
    """把人工版再喂回算法：它已经是自洽的，不该再动（跑第二遍不漂）。"""
    shifts, plan_ = octave.plan(_notes(manual))
    assert plan_.stats.moved_notes == 0
    assert shifts == [0] * len(manual)


# ------------------------------------------------------------------ 反例
@pytest.mark.parametrize("name", [
    "忘川风华录 - 旷古回响.svp",
    "忘川风华录 - 旷古回响（人工）.svp",
])
def test_another_song_is_not_touched(name):
    """《旷古回响》那两个 SV 工程没有"差整八度的重复段" → 一个音都不动。"""
    rows = _load_svp(QUANTIZE, name)
    assert rows, "样本里没有音符"
    shifts, plan_ = octave.plan(_notes(rows))
    assert shifts == [0] * len(rows)
    assert plan_.stats.moved_notes == 0
