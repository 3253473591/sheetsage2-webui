r"""SVP 导出的接线测试：tick 换算、和弦拆轨、弹窗可选项、写出来的 `.svp` 能读回来。

libresvip 是**可选依赖**（`pip install -e .[svp]`），没装时跳过真正 build 的那几条，
其余纯函数照测。

    .\.venv\Scripts\python.exe -m pytest tests -q
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

libresvip = pytest.importorskip("libresvip", reason="可选依赖：pip install -e .[svp]")

#: 两小节 4/4（首小节当弱起）/ 120 BPM / 一个 8 分音符 = 960 tick 的假工程。
PROJECT = {
    "ppq": 1920,
    "duration_seconds": 4.0,
    "timeline": {
        "measures": [
            {"index": 0, "tick_start": 0, "tick_end": 960, "numerator": 1,
             "denominator": 8, "bpm": 120.0, "is_pickup": True},
            {"index": 1, "tick_start": 960, "tick_end": 8640, "numerator": 4,
             "denominator": 4, "bpm": 120.0, "is_pickup": False},
            {"index": 2, "tick_start": 8640, "tick_end": 16320, "numerator": 4,
             "denominator": 4, "bpm": 140.0, "is_pickup": False},
        ],
    },
    "tracks": [
        {"id": "vocal", "notes": [
            {"tick": 960, "dur": 480, "pitch": 60, "lyric": "啦"},
            {"tick": 1440, "dur": 480, "pitch": 62, "lyric": "-"},
            {"tick": 1920, "dur": 480, "pitch": 64, "lyric": "+"},
        ]},
        {"id": "instrumental", "notes": [
            {"tick": 960, "dur": 960, "pitch": 48},
        ]},
    ],
}


@pytest.fixture()
def out_dir(tmp_path: Path) -> Path:
    (tmp_path / "project.json").write_text(json.dumps(PROJECT, ensure_ascii=False),
                                           encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------- 纯函数
def test_tick_scale_is_four():
    """我们 PPQ=1920，libresvip 基准 480 -> ÷4；32 分网格 240 正好整除。"""
    assert svp.PPQ_SCALE == 4
    assert svp.to_sv_tick(0) == 0
    assert svp.to_sv_tick(240) == 60
    assert svp.to_sv_tick(1920) == 480
    assert svp.to_sv_tick(960) == 240


def test_meter_list_only_keeps_the_change_points():
    """拍号只写变化点：整首 4/4 一条就够，中途转拍号（3/4、6/8）必须保留。

    逐小节抄进去的话，SynthV 会在**每个小节**上标一次 4/4（实测《光芒》45s 版 20 条全一样）。
    """
    api = svp._require_libresvip()
    block = {"measures": [
        {"numerator": 1, "denominator": 8, "is_pickup": True},   # 弱起 -> 前置小节
        {"numerator": 4, "denominator": 4},
        {"numerator": 4, "denominator": 4},
        {"numerator": 4, "denominator": 4},
        {"numerator": 3, "denominator": 4},                      # 转拍号
        {"numerator": 3, "denominator": 4},
        {"numerator": 6, "denominator": 8},
    ]}
    meters = svp._meter_list(block, api)
    assert [(meter.bar_index, meter.numerator, meter.denominator)
            for meter in meters] == [(0, 1, 8), (1, 4, 4), (4, 3, 4), (6, 6, 8)]


def test_tempo_list_writes_one_marker_for_a_constant_song():
    """恒速曲只写一条速度：逐小节那张表只是同一个速度加网格抖动。

    （否则 SynthV 会在每个小节上都标一次 125；实测《光芒》45s 版：文件里 20 条。）
    """
    api = svp._require_libresvip()
    block = {
        "tempo_map": [{"tick": 0, "bpm": 125.0}],
        "measures": [
            {"tick_start": 0, "bpm": 125.0},
            {"tick_start": 7680, "bpm": 124.8},
            {"tick_start": 15360, "bpm": 125.3},
            {"tick_start": 23040, "bpm": 124.9},
        ],
    }
    tempos = svp._tempo_list(block, api)
    assert [(tempo.position, tempo.bpm) for tempo in tempos] == [(0, 125.0)]


def test_tempo_list_keeps_changes_and_collapses_repeats():
    """变速曲保留真的转速度点；相邻数值一样的点折叠掉（跟拍号一个道理）。"""
    api = svp._require_libresvip()
    block = {
        "tempo_map": [{"tick": 0, "bpm": 120.0}, {"tick": 15360, "bpm": 90.0}],
        "measures": [
            {"tick_start": 0, "bpm": 120.0},
            {"tick_start": 7680, "bpm": 120.0},        # 同上一条 -> 折叠
            {"tick_start": 15360, "bpm": 90.0},        # 真的转速度 -> 保留
            {"tick_start": 23040, "bpm": 90.1},        # 差 0.1% -> 折叠
            {"tick_start": 30720, "bpm": 60.0},        # 又转 -> 保留
        ],
    }
    tempos = svp._tempo_list(block, api)
    assert [(tempo.position, tempo.bpm) for tempo in tempos] == \
        [(svp.to_sv_tick(0), 120.0), (svp.to_sv_tick(15360), 90.0),
         (svp.to_sv_tick(30720), 60.0)]


def test_tick_conversion_keeps_touching_notes_touching():
    """未量化的 tick 不是 4 的倍数：**起点和终点各自换算**，不能叠音。

    真事故（《旷古回响》bar 76，量化关掉时）：我们 tick `576478+478` 与 `576956`
    正好头尾相接，旧写法 `round(tick/4) + round(dur/4)` 得到 `144120+120 = 144240`，
    而下一个音的起点是 `144239` —— 差 1 tick，libresvip 的 `PitchSimulator` 直接抛
    `NotesOverlappedError: Notes overlapped near bar 76`（HTTP 500）。
    """
    raw = [{"tick": 576478, "dur": 478, "pitch": 60},
           {"tick": 576956, "dur": 3824, "pitch": 62}]
    rows, stats = svp._note_rows(raw)
    assert stats == {"dropped": 0, "shifted": 0, "trimmed": 0}
    assert rows[0][1] == rows[1][0]                       # 还是头尾相接
    assert svp.to_sv_window(raw[0]) == (svp.to_sv_tick(576478),
                                        svp.to_sv_tick(576956))
    # 旧算法确实会叠 1 tick —— 留在测试里，防止有人改回去
    assert svp.to_sv_tick(576478) + svp.to_sv_tick(478) > svp.to_sv_tick(576956)


def test_note_rows_push_and_trim_real_overlaps():
    """真叠音（模型自己给的重叠）：后面那个往后推、前面那个截短。"""
    rows, stats = svp._note_rows([{"tick": 0, "dur": 4000, "pitch": 60},
                                  {"tick": 1000, "dur": 1000, "pitch": 62}])
    assert [(start, end) for start, end, _ in rows] == \
        [(svp.to_sv_tick(0), svp.to_sv_tick(1000)),
         (svp.to_sv_tick(1000), svp.to_sv_tick(2000))]
    assert stats == {"dropped": 0, "shifted": 0, "trimmed": 1}


def test_note_rows_drop_notes_squeezed_to_nothing():
    """同 tick 起两个音：SVP 摆不下，只能丢一个（宁少不叠）。"""
    rows, stats = svp._note_rows([{"tick": 0, "dur": 1920, "pitch": 60},
                                  {"tick": 0, "dur": 1920, "pitch": 62}])
    assert len(rows) == 1
    assert stats["dropped"] == 1


def test_build_svp_survives_a_track_that_would_overlap(out_dir: Path):
    """端到端：拿会叠音的轨道去 build，不许再抛 NotesOverlappedError。"""
    from libresvip.plugins.svp.synthv_studio_converter import SynthVStudioConverter

    project = json.loads((out_dir / "project.json").read_text(encoding="utf-8"))
    project["tracks"][0]["notes"] = [
        {"tick": 576478, "dur": 478, "pitch": 60, "lyric": "啊"},
        {"tick": 576956, "dur": 3824, "pitch": 62, "lyric": "-"},
        {"tick": 576956, "dur": 480, "pitch": 64, "lyric": "-"},   # 同 tick，注定被丢
    ]
    (out_dir / "project.json").write_text(json.dumps(project, ensure_ascii=False),
                                          encoding="utf-8")

    payload, stats = svp.build_svp(out_dir, parts=("vocal",))
    assert stats["notes"] == 2
    assert stats["adjust"]["dropped"] == 1
    assert any("不叠音" in warning for warning in stats.get("warnings") or [])

    target = out_dir / "overlap.svp"
    target.write_bytes(payload)
    back = SynthVStudioConverter.load(target, {})
    starts = [note.start_pos for note in back.track_list[0].note_list]
    lengths = [note.length for note in back.track_list[0].note_list]
    assert starts == [svp.to_sv_tick(576478), svp.to_sv_tick(576956)]
    assert all(length > 0 for length in lengths)
    assert starts[0] + lengths[0] <= starts[1]              # 读回来也没叠


def test_real_project_that_used_to_fail_exports():
    """真事故回归：《旷古回响》（量化关掉）以前在 bar 76 抛叠音 500。"""
    out = PROJECT_ROOT / "output" / "忘川风华录_-_旷古回响"
    if not (out / "project.json").is_file():
        pytest.skip("没有 output/忘川风华录_-_旷古回响（产物目录是忽略的）")
    payload, stats = svp.build_svp(out, parts=("vocal", "instrumental"))
    assert stats["notes"] > 600
    assert payload.startswith(b"{")


def test_filler_lyric_and_sv_language():
    assert svp.filler_lyric("Chinese") == "啦"
    assert svp.filler_lyric("Japanese") == "ラ"
    assert svp.filler_lyric("English") == "la"
    assert svp.filler_lyric("") == "la"                 # 认不出就用拉丁音素
    assert svp.sv_language("Japanese") == "japanese"
    assert svp.sv_language("") == "mandarin"            # 和 libresvip 的默认一致


def test_resolve_language_falls_back_to_what_asr_judged(out_dir: Path):
    assert svp.resolve_language({"language": "Japanese"}, out_dir) == "Japanese"
    assert svp.resolve_language({"language": ""}, out_dir) == ""      # 没有 lyrics.json
    (out_dir / "lyrics.json").write_text(json.dumps({"language_used": "Chinese"}),
                                         encoding="utf-8")
    assert svp.resolve_language({"language": ""}, out_dir) == "Chinese"
    assert svp.resolve_language({"language": "English"}, out_dir) == "English"


def test_chord_voices_split_by_simultaneous_notes():
    """**一轨只能有一个音**：4 个同时发声的音 -> 4 轨，低音进第 1 轨。"""
    notes = [
        {"start": 0.0, "end": 1.0, "pitch": 38}, {"start": 0.0, "end": 1.0, "pitch": 50},
        {"start": 0.0, "end": 1.0, "pitch": 53}, {"start": 0.0, "end": 1.0, "pitch": 57},
        {"start": 1.0, "end": 2.0, "pitch": 36}, {"start": 1.0, "end": 2.0, "pitch": 48},
        {"start": 1.0, "end": 2.0, "pitch": 52}, {"start": 1.0, "end": 2.0, "pitch": 55},
    ]
    voices = svp.chord_voices(notes)
    assert len(voices) == 4
    assert [voice[0]["pitch"] for voice in voices] == [38, 50, 53, 57]
    assert all(len(voice) == 2 for voice in voices)
    # 每轨内部按时间有序，且绝不重叠
    for voice in voices:
        for earlier, later in zip(voice, voice[1:]):
            assert earlier["end"] <= later["start"]


def test_chord_voices_reuse_a_line_and_truncate_overlaps():
    """音数变少时**复用**已有的声部线（不是每个和弦都新开一轨），重叠处截断。"""
    voices = svp.chord_voices([{"start": 0.0, "end": 5.0, "pitch": 60},
                               {"start": 2.0, "end": 4.0, "pitch": 62}])
    assert len(voices) == 1                             # 只有一个音，落在第 1 线上
    assert voices[0][0]["end"] == 2.0                   # 截到下一个音开始
    assert voices[0][1]["start"] == 2.0


def test_chord_voices_handles_ragged_chords():
    """后面的和弦音数变多/变少都不该炸。"""
    voices = svp.chord_voices([{"start": 0.0, "end": 1.0, "pitch": 60},
                               {"start": 1.0, "end": 2.0, "pitch": 60},
                               {"start": 1.0, "end": 2.0, "pitch": 64},
                               {"start": 1.0, "end": 2.0, "pitch": 67}])
    assert len(voices) == 3
    assert [len(voice) for voice in voices] == [2, 1, 1]


def test_available_parts_reports_counts_and_defaults(out_dir: Path):
    rows = svp.available_parts(out_dir, PROJECT)
    by_key = {row["key"]: row for row in rows}
    assert [row["key"] for row in rows] == ["vocal", "instrumental", "chords"]
    # 默认只勾人声
    assert by_key["vocal"]["default"] is True
    assert by_key["instrumental"]["default"] is False
    assert by_key["chords"]["default"] is False
    assert by_key["vocal"]["notes"] == 3 and by_key["vocal"]["tracks"] == 1
    assert by_key["instrumental"]["notes"] == 1
    assert by_key["chords"]["available"] is False       # 这个假工程没有 chords.mid
    assert "歌词" in by_key["vocal"]["detail"]


# --------------------------------------------------------------------- 编码
def test_build_svp_default_only_vocal(out_dir: Path):
    payload, stats = svp.build_svp(out_dir, parts=svp.DEFAULT_PARTS)
    assert payload and len(payload) > 100
    assert stats["parts"] == ["vocal"]
    assert stats["notes"] == 3
    assert [track["title"] for track in stats["tracks"]] == ["人声"]


def test_build_svp_can_include_instrumental(out_dir: Path):
    _payload, stats = svp.build_svp(out_dir, parts=("vocal", "instrumental"))
    assert stats["notes"] == 4
    assert [track["title"] for track in stats["tracks"]] == ["人声", "乐器"]


def test_build_svp_rejects_an_empty_selection(out_dir: Path):
    with pytest.raises(ValueError):
        svp.build_svp(out_dir, parts=("chords",))       # 没有 chords.mid


def test_build_svp_round_trips_notes_tempo_and_lyrics(out_dir: Path):
    """写出来的 `.svp`：音符/歌词用 LibreSVIP 读回来比，**变速直接看文件内容**。

    为什么变速看文件而不是看读回来的：`dump()` 会按"前置小节"把速度轴整体前移
    （`first_bar_tick` = 弱起长度 240），而 `load()` 复原时加回的是**第一个完整小节**
    （1920）——两者不是严格互逆。**Synthesizer V 读的是文件**，所以契约按文件定。
    """
    from libresvip.plugins.svp.constants import TICK_RATE
    from libresvip.plugins.svp.synthv_studio_converter import SynthVStudioConverter

    payload, _stats = svp.build_svp(out_dir, parts=("vocal", "instrumental"))

    # ① 文件里的速度轴：弱起那一小节的 tempo 被丢掉（它是 SV 的前置小节），
    #    第一个完整小节的 120 BPM 落在位置 0，第二个小节的 140 BPM 落在正确的音乐位置。
    raw = json.loads(payload.rstrip(b"\x00").decode("utf-8"))
    tempos = [(item["position"], item["bpm"]) for item in raw["time"]["tempo"]]
    assert tempos == [(0, 120.0), (1920 * TICK_RATE, 140.0)]
    # 拍号：文件里的第 0 条就是我们的第一个**完整**小节；**整首同拍号只写一条**
    assert [(item["index"], item["numerator"], item["denominator"])
            for item in raw["time"]["meter"]] == [(0, 4, 4)]

    # ② 音符 / 歌词：读回来逐项比（实测位置零漂移）
    target = out_dir / "roundtrip.svp"
    target.write_bytes(payload)
    back = SynthVStudioConverter.load(target, {})

    vocal = back.track_list[0]
    assert [note.start_pos for note in vocal.note_list] == [240, 360, 480]
    assert [note.length for note in vocal.note_list] == [120, 120, 120]
    assert [note.key_number for note in vocal.note_list] == [60, 62, 64]
    # 延续标记原样保留：`-` 是延长当前音节，`+` 是进下一个音节
    assert [note.lyric for note in vocal.note_list] == ["啦", "-", "+"]
    assert len(back.track_list) == 2                    # 人声 + 乐器
    assert len(back.track_list[1].note_list) == 1


def test_build_svp_uses_a_filler_lyric_for_instrumental(out_dir: Path):
    from libresvip.plugins.svp.synthv_studio_converter import SynthVStudioConverter

    payload, _stats = svp.build_svp(out_dir, parts=("instrumental",),
                                    options={"language": "Japanese"})
    target = out_dir / "inst.svp"
    target.write_bytes(payload)
    back = SynthVStudioConverter.load(target, {})
    assert back.track_list[0].note_list[0].lyric == "ラ"


def test_cache_key_changes_with_the_selection(out_dir: Path):
    vocal = svp.cache_key(("vocal",), "1.9", {"language": ""})
    more = svp.cache_key(("vocal", "chords"), "1.9", {"language": ""})
    other_version = svp.cache_key(("vocal",), "2.0", {"language": ""})
    assert len({vocal, more, other_version}) == 3
    assert vocal == svp.cache_key(("vocal",), "1.9", {"language": ""})
