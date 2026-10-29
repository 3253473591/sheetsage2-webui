r"""SVP 导出的**没字音符**处理：延音还是占位音节。

规则（依据 `samples/对齐/` 那对 SVP，人工修过的工程全文 0 个占位音节、
55 个延音标记全部与前一个音无缝相接）：

- 没分到字的音，只要与前面"有字的音"**无缝相连** → 写延音 `-`（同一个字的延续）；
- 中间隔着休止（链条断了，多半是下一句的开头）→ 退回占位音节（`啦` / `ラ` / `la`）。

这不依赖 libresvip——`_note_objects` 的 `api` 参数直接喂一个假的 `Note`。

    .\.venv\Scripts\python.exe -m pytest tests/test_svp_lyrics.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui import svp  # noqa: E402

SAMPLES = PROJECT_ROOT / "samples"
ALIGN = SAMPLES / "对齐"
MACHINE = "王朝1982,朱旭BooBoo - 我本将心向明月.svp"
MANUAL = "王朝1982,朱旭BooBoo - 我本将心向明月（人工）.svp"

#: SVP 的 blick 与工程 tick 的换算（一小节 705_600_000 blick = 1920 tick）。
PPQ = 1920
SVP_BAR = 705_600_000
PPQ_SCALE = PPQ / SVP_BAR


class FakeNote:
    """libresvip `Note` 的最小替身，只记下导出时给的字。"""

    def __init__(self, start_pos: int, length: int, key_number: int, lyric: str):
        self.start_pos = start_pos
        self.length = length
        self.key_number = key_number
        self.lyric = lyric


API = {"Note": FakeNote}


def note(tick: int, dur: int, pitch: int = 60, lyric: str = "") -> dict:
    return {"tick": tick, "dur": dur, "pitch": pitch, "lyric": lyric}


def export(notes: list[dict], *, language: str = "Chinese") -> list[str]:
    return [n.lyric for n in svp._note_objects(notes, language=language, api=API)]


# --------------------------------------------------------------------- 规则
def test_a_note_without_a_char_continues_the_previous_word():
    """无缝相连的没字音 → 延音 `-`，而不是发明一个『啦』。"""
    notes = [note(0, 480, 60, "你"), note(480, 480, 62), note(960, 480, 64)]
    assert export(notes) == ["你", "-", "-"]


def test_a_rest_breaks_the_chain():
    """隔一个够长的休止 → 链条断了，退回占位音节（那是下一句的开头）。"""
    notes = [note(0, 480, 60, "你"), note(3840, 480, 62), note(4320, 480, 64)]
    assert export(notes) == ["你", "啦", "-"]


def test_a_short_rest_still_counts_as_the_same_word():
    """半个四分音符以内的休止（换气）不算断句——那个字还是接着唱。"""
    assert export([note(0, 480, 60, "你"), note(960, 480, 62)]) == ["你", "-"]


def test_the_first_note_without_a_char_uses_the_filler():
    """轨首就没有字（前面根本没有字可延续）→ 占位音节。"""
    assert export([note(0, 480, 60), note(480, 480, 62, "你")]) == ["啦", "你"]


def test_an_instrumental_track_never_becomes_a_melisma():
    """器乐轨一个字都没有：全是占位音节，不会变成"整句只唱一个字"。"""
    notes = [note(0, 480, 55), note(480, 480, 57), note(960, 480, 59)]
    assert export(notes) == ["啦", "啦", "啦"]


def test_a_chain_of_empty_notes_holds_one_syllable():
    """一句里连着几个没字的音：只有第一个占位，后面都续成 `-`（不是"啦啦啦"）。"""
    notes = [note(0, 480, 60, "你"), note(480, 480, 62), note(960, 480, 64),
             note(1440, 480, 65)]
    assert export(notes) == ["你", "-", "-", "-"]


def test_a_filler_can_be_continued_after_a_rest():
    """休止之后连着两个没字的音：第一个占位，第二个续它——不再是两个『啦』。"""
    notes = [note(0, 480, 60, "你"), note(3840, 480, 62), note(4320, 480, 64)]
    assert export(notes) == ["你", "啦", "-"]


def test_continuation_markers_are_kept_as_is():
    """`-` 与 `+` 含义不同（延长当前音节 / 进下一个音节），SVP 里两个都保留。

    依据：人工修的《Fall Out Boy - The Phoenix》里 `lighting - +`、`running + -`
    是混着写的，SynthV 认这两种标记。
    """
    notes = [note(0, 480, 60, "la"), note(480, 480, 62, "+"), note(960, 480, 64, "-")]
    assert export(notes, language="English") == ["la", "+", "-"]


def test_the_stats_count_both_kinds():
    stats: dict = {}
    notes = [note(0, 480, 60, "你"), note(480, 480, 62), note(3840, 480, 64)]
    svp._note_objects(notes, language="Chinese", api=API, stats=stats)
    assert stats["continued"] == 1
    assert stats["filler"] == 1


# --------------------------------------------------------------------- 真样本
def _load_svp(name: str) -> list[dict]:
    path = ALIGN / name
    if not path.is_file():
        pytest.skip(f"样本不在：{path}")
    doc = json.loads(path.read_bytes().rstrip(b"\x00").decode("utf-8"))
    rows = []
    for track in doc["tracks"]:
        for n in track["mainGroup"]["notes"]:
            rows.append({"tick": round(int(n["onset"]) * PPQ_SCALE),
                         "dur": round(int(n["duration"]) * PPQ_SCALE),
                         "pitch": n["pitch"],
                         # 机扒工程里的『啦』就是"没分到字"的占位：还原成空串
                         "lyric": "" if n["lyrics"] == "啦" else n["lyrics"]})
    rows.sort(key=lambda x: (x["tick"], x["pitch"]))
    return rows


def test_the_align_sample_had_57_placeholders():
    """前提：那首歌机扒出 381 个音，57 个没分到字（现在的导出会全填『啦』）。"""
    rows = _load_svp(MACHINE)
    assert len(rows) == 381
    assert sum(1 for r in rows if not r["lyric"]) == 57


def test_placeholders_become_continuations_instead_of_la():
    """修完之后：占位音节从 **57 个掉到 1 个**，延音 91 个——
    而人工版是 55 个延音、0 个占位音节。剩下那 1 个是 32 拍长休止后的句首，
    前面根本没有字可续（ASR 又没给字），占位是唯一选择。"""
    rows = _load_svp(MACHINE)
    got = export(rows)
    assert sum(1 for lyric in got if lyric == "啦") == 1
    assert sum(1 for lyric in got if lyric == "-") == 91
    leftover = [index for index, lyric in enumerate(got) if lyric == "啦"]
    assert len(leftover) == 1
    # 沿链条复算一遍：凡是前面有字可续的，都不该是占位音节
    head_alive = False
    prev_end = None
    for index, (row, lyric) in enumerate(zip(rows, got)):
        start = svp.to_sv_tick(row["tick"])
        gap_ok = prev_end is not None and start - prev_end <= svp.CONTINUATION_MAX_GAP
        if head_alive and not gap_ok:
            head_alive = False                       # 休止：链条断了
        if lyric and lyric != "-":
            head_alive = True
        elif not lyric:
            assert not head_alive, f"第 {index} 个音前面有字可续，不该填占位音节"
        prev_end = svp.to_sv_tick(row["tick"] + row["dur"])


def test_the_manual_version_has_never_used_a_placeholder():
    """人工版从头到尾没有占位音节——这就是这条规则的出处。"""
    manual = _load_svp(MANUAL)
    assert manual, "样本里没有音符"
    assert sum(1 for r in manual if r["lyric"] == "啦") == 0
    assert sum(1 for r in manual if r["lyric"] == "-") == 55
