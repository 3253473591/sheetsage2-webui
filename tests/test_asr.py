r"""`asr` 模块里不依赖模型的那些逻辑（区间划分）。

    .\.venv\Scripts\python.exe -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.asr import (  # noqa: E402
    AlignedChar,
    AsrConfig,
    AsrEngine,
    JaAlignerMissing,
    Section,
    asr_load_kwargs,
    interpolate_degenerate,
    parsed_sections,
    sections_from_lab,
    widen_to_vocals,
)


def lab(*rows) -> str:
    return "".join("\t".join(str(x) for x in row) + "\n" for row in rows)


def test_merges_adjacent_sections_with_the_same_label():
    text = lab((0.0, 10.0, "verse"), (10.0, 20.0, "verse"), (20.0, 30.0, "chorus"))
    sections = sections_from_lab(text, max_seconds=100.0, min_seconds=1.0)
    assert len(sections) == 1                      # 全部能塞进 100s，于是并成一段
    assert sections[0].start == 0.0 and sections[0].end == 30.0


def test_labels_are_translated_to_chinese_and_merged():
    """模型的标签是按乐句给的（`verse verse`），合并 + 翻译之后才是"一段主歌"。"""
    text = lab((0.0, 10.0, "verse"), (10.0, 20.0, "verse"), (20.0, 30.0, "chorus"),
               (30.0, 40.0, "chorus"), (40.0, 50.0, "interlude"))
    merged = parsed_sections(text)
    assert [(row.label, row.start, row.end) for row in merged] == \
        [("主歌", 0.0, 20.0), ("副歌", 20.0, 40.0), ("间奏", 40.0, 50.0)]


def test_same_label_apart_stays_apart():
    """只并"连着"的：副歌—主歌—副歌 还是三段。"""
    text = lab((0.0, 10.0, "chorus"), (10.0, 20.0, "verse"), (20.0, 30.0, "chorus"))
    assert [row.label for row in parsed_sections(text)] == ["副歌", "主歌", "副歌"]


def test_skip_instrumental_drops_intro_interlude_outro():
    text = lab((0.0, 10.0, "intro"), (10.0, 30.0, "verse"), (30.0, 45.0, "interlude"),
               (45.0, 70.0, "chorus"), (70.0, 80.0, "outro"))
    every = sections_from_lab(text, max_seconds=100.0, min_seconds=1.0)
    assert [s.label for s in every] == [None]              # 打包混在一起 -> 标签清空
    skipped = sections_from_lab(text, max_seconds=100.0, min_seconds=1.0,
                                skip_instrumental=True)
    # 只剩唱段：10-45s 与 45-70s 连着，打包成一段
    assert len(skipped) == 1
    assert (skipped[0].start, skipped[0].end) == (10.0, 70.0)


def test_skip_instrumental_returns_nothing_when_all_instrumental():
    """整首都是器乐段时不猜、不硬跑——返回空，由调用方决定（runner 会回退到全部段落）。"""
    text = lab((0.0, 10.0, "intro"), (10.0, 20.0, "interlude"), (20.0, 30.0, "outro"))
    assert sections_from_lab(text, max_seconds=100.0, min_seconds=1.0,
                             skip_instrumental=True) == []
    assert len(sections_from_lab(text, max_seconds=100.0, min_seconds=1.0)) == 1


def test_packs_short_sections_up_to_the_limit():
    text = lab(*[(i * 10.0, (i + 1) * 10.0, "verse") for i in range(10)])
    sections = sections_from_lab(text, max_seconds=45.0, min_seconds=1.0)
    assert all(s.seconds <= 45.0 for s in sections)
    # 10 个 10s 的段，上限 45s -> 至少 3 段
    assert len(sections) >= 3
    assert sections[0].start == 0.0
    assert sections[-1].end == 100.0


def test_splits_a_section_longer_than_the_limit():
    sections = sections_from_lab(lab((0.0, 300.0, "verse")), max_seconds=90.0,
                                 min_seconds=1.0)
    assert len(sections) >= 4
    assert all(s.seconds <= 90.0 + 1e-6 for s in sections)
    assert sections[0].start == pytest.approx(0.0)
    assert sections[-1].end == pytest.approx(300.0)


def test_section_labels_survive_and_get_cleared_on_mixed_packs():
    text = lab((0.0, 10.0, "chorus"), (10.0, 20.0, "verse"))
    sections = sections_from_lab(text, max_seconds=100.0, min_seconds=1.0)
    assert sections[0].label is None               # 混合段不再声称自己是某一种

    solo = sections_from_lab(lab((0.0, 10.0, "chorus")), max_seconds=100.0,
                             min_seconds=1.0)
    assert solo[0].label == "副歌"


def test_garbage_and_empty_input_is_safe():
    assert sections_from_lab("") == []
    assert sections_from_lab("not a lab\n") == []
    assert sections_from_lab(lab((5.0, 5.0, "x"))) == []      # 零长度丢掉
    assert sections_from_lab(lab((10.0, 5.0, "x"))) == []     # 倒过来也丢掉


def test_space_separated_lab_is_accepted():
    sections = sections_from_lab("0.0 10.0 verse\n", max_seconds=100.0, min_seconds=1.0)
    assert len(sections) == 1
    assert sections[0].label == "主歌"


def test_unknown_labels_are_kept_as_is():
    """认不出来的标签原样保留——别乱猜成"主歌"。"""
    assert sections_from_lab("0.0 10.0 weirdname\n", max_seconds=100.0,
                             min_seconds=1.0)[0].label == "weirdname"


def test_unsorted_input_is_sorted():
    text = lab((10.0, 20.0, "b"), (0.0, 10.0, "a"))
    sections = sections_from_lab(text, max_seconds=100.0, min_seconds=1.0)
    assert sections[0].start == 0.0
    assert sections[-1].end == 20.0


def test_section_seconds_property():
    assert Section(2.0, 5.5, "x").seconds == pytest.approx(3.5)


# --------------------------------------------------------------------- 零长度字插值
def chars(*rows) -> list[AlignedChar]:
    return [AlignedChar(text, start, end) for text, start, end in rows]


def test_interpolate_spreads_a_zero_run_across_the_gap():
    """连续两个零长度字，均分在前一个字的 end 和后一个字的 start 之间。"""
    rows = chars(("a", 0.0, 1.0), ("b", 1.0, 1.0), ("c", 2.0, 2.0), ("d", 3.0, 4.0))
    fixed, repaired = interpolate_degenerate(rows)
    assert repaired == 2
    assert [(c.start, c.end) for c in fixed] == [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0),
                                                 (3.0, 4.0)]
    # 入参不被改写
    assert (rows[1].start, rows[1].end) == (1.0, 1.0)


def test_interpolate_leaves_measured_chars_alone():
    rows = chars(("a", 0.0, 0.7), ("b", 1.2, 1.9))
    fixed, repaired = interpolate_degenerate(rows)
    assert repaired == 0
    assert [(c.start, c.end) for c in fixed] == [(0.0, 0.7), (1.2, 1.9)]


def test_interpolate_caps_one_char_in_a_huge_gap():
    """35 秒的空档通常是一段间奏，不是一个字拖了 35 秒——锚在左边按上限排。"""
    rows = chars(("a", 0.0, 1.0), ("b", 1.0, 1.0), ("c", 40.0, 41.0))
    fixed, _ = interpolate_degenerate(rows)
    assert fixed[1].start == pytest.approx(1.0)
    assert fixed[1].end == pytest.approx(1.0 + 1.5)


def test_interpolate_stays_monotonic_and_never_overlaps():
    rows = chars(("a", 0.0, 1.0), *[(str(i), 1.0, 1.0) for i in range(6)],
                 ("z", 30.0, 31.0))
    fixed, repaired = interpolate_degenerate(rows, max_char_seconds=0.5)
    assert repaired == 6
    for previous, current in zip(fixed, fixed[1:]):
        assert current.start >= previous.end - 1e-9
    assert all(current.end > current.start for current in fixed)


def test_interpolate_handles_an_all_degenerate_track():
    rows = chars(("a", 5.0, 5.0), ("b", 5.0, 5.0))
    fixed, repaired = interpolate_degenerate(rows)
    assert repaired == 2 and all(c.end > c.start for c in fixed)


def test_interpolate_handles_a_run_at_the_very_end():
    rows = chars(("a", 0.0, 1.0), ("b", 2.0, 2.0))
    fixed, _ = interpolate_degenerate(rows)
    assert fixed[1].start == pytest.approx(1.0)
    assert fixed[1].end == pytest.approx(2.5)          # 没有右边的字就按上限顺排


# --------------------------------------------------------------------- 边界让人声
def test_widen_moves_an_open_boundary_out_to_the_vocal_phrase():
    """前奏边界切在人声里：往外挪到这一句的起点。"""
    sections = [Section(23.87, 105.0)]
    vocals = [(22.49, 23.62), (23.90, 24.20), (104.9, 105.4)]
    widened = widen_to_vocals(sections, vocals)
    assert widened[0].start == pytest.approx(22.49)
    assert widened[0].end == pytest.approx(105.4)


def test_widen_keeps_boundaries_between_two_kept_sections():
    """相邻切片之间的边界切在里面不丢音频，不许动。"""
    sections = [Section(23.87, 60.0), Section(60.0, 100.0)]
    vocals = [(22.49, 23.62), (30.0, 59.5), (59.9, 90.0)]
    widened = widen_to_vocals(sections, vocals)
    assert widened[0].start == pytest.approx(22.49)     # 只有全曲最前面那侧敞口
    assert (widened[0].end, widened[1].start) == (60.0, 60.0)


def test_widen_respects_the_maximum_extension():
    sections = [Section(100.0, 120.0)]
    vocals = [(80.0, 101.0)]
    widened = widen_to_vocals(sections, vocals, max_extend=12.0)
    assert widened[0].start == pytest.approx(88.0)


def test_widen_splits_the_gap_when_both_sides_reach_it():
    """两侧都往同一段间奏里挪会撞上：在原来的边界中间对半分。"""
    sections = [Section(0.0, 50.0), Section(60.0, 100.0)]
    vocals = [(50.5, 59.5)]
    widened = widen_to_vocals(sections, vocals)
    assert widened[0].end == pytest.approx(55.0)
    assert widened[1].start == pytest.approx(55.0)


def test_widen_is_a_no_op_without_vocals():
    sections = [Section(0.0, 10.0)]
    assert widen_to_vocals(sections, []) == sections


# --------------------------------------------------------------------- TIFA 后端
def test_slice_limit_shrinks_for_tifa():
    """TIFA 上下文约 1 分钟，90s 的默认切片要压到 50s。"""
    qwen = AsrConfig(asr_model="m", max_slice_seconds=90.0, aligner="qwen")
    tifa = AsrConfig(asr_model="m", max_slice_seconds=90.0, aligner="tifa")
    assert qwen.slice_limit == 90.0
    assert tifa.slice_limit == 50.0
    assert tifa.uses_tifa and not qwen.uses_tifa
    # 本来就更短的切片不被放大
    assert AsrConfig(asr_model="m", max_slice_seconds=30.0,
                     aligner="tifa").slice_limit == 30.0


def test_tifa_language_mapping_falls_back_to_chinese():
    from sheetsage2_webui.tifa_align import MAX_ALIGN_SECONDS, guess_language, tifa_language

    assert MAX_ALIGN_SECONDS == 50.0
    assert tifa_language("Chinese") == "zh"
    assert tifa_language("Japanese") == "ja"
    assert tifa_language("Cantonese") == "zh"   # yue 覆盖太差，默认走中文 G2P
    assert tifa_language("English") == "en"
    # TIFA 1.0 没有韩语：映射成 `ko`，由路由层转 Qwen 对齐器。
    # （以前这里返回 "zh"，韩文会被中文 G2P 处理，一个时间戳都出不来）
    assert tifa_language("Korean") == "ko"
    assert tifa_language(None) == "zh"
    # 文字兜底：假名 -> ja，拉丁为主 -> en，其余 -> zh
    assert guess_language("君が僕の心臓じゃない") == "ja"
    assert guess_language("사랑해요 그대") == "ko"
    assert guess_language("The Wellerman came across the sea") == "en"
    assert guess_language("阳光在身上流转") == "zh"


def test_mixed_latin_word_in_a_chinese_song_gets_syllable_onsets():
    """中文歌里夹的英文词也要切音节（`parts` 只在整首英文时才给，是旧的门槛）。

    TIFA 的 `language=[zh, en]` 让拉丁词走英文 G2P，phones 层里就是 ARPAbet；
    汉字词是拼音（`ao`/`en`/`er` 与 ARPAbet 撞名），只能按词面判断，一律不切。
    """
    from sheetsage2_webui.tifa_align import syllable_onsets, word_syllable_onsets

    phones = [(0.0, 0.1, "b"), (0.1, 0.2, "ax"), (0.2, 0.3, "n"),
              (0.3, 0.4, "t"), (0.4, 0.5, "uw")]
    # 最大开头原则：两个元音之间只有一个辅音组时，最后一个辅音归后一个音节
    assert syllable_onsets(phones, 0.0, 0.5) == (0.3,)

    assert word_syllable_onsets(phones, "captain", 0.0, 0.5,
                                language="zh") == (0.3,)
    assert word_syllable_onsets(phones, "captain", 0.0, 0.5,
                                language="en") == (0.3,)

    pinyin = [(0.0, 0.1, "h"), (0.1, 0.2, "ao"), (0.2, 0.3, "x"),
              (0.3, 0.4, "iao")]
    assert word_syllable_onsets(pinyin, "好", 0.0, 0.4, language="zh") == ()


def test_parse_text_grid_skips_silence_and_zero_width():
    from sheetsage2_webui.tifa_align import parse_text_grid

    text = (
        'File type = "ooTextFile"\n'
        'item [1]:\n\tclass = "IntervalTier"\n\tname = "texts"\n'
        '\tintervals: size = 4\n'
        '\t\tintervals [1]:\n\t\t\txmin = 0.0\n\t\t\txmax = 0.5\n\t\t\ttext = "SP"\n'
        '\t\tintervals [2]:\n\t\t\txmin = 0.5\n\t\t\txmax = 0.9\n\t\t\ttext = "流"\n'
        '\t\tintervals [3]:\n\t\t\txmin = 0.9\n\t\t\txmax = 0.9\n\t\t\ttext = "转"\n'
        '\t\tintervals [4]:\n\t\t\txmin = 0.9\n\t\t\txmax = 1.3\n\t\t\ttext = "等"\n'
        'item [2]:\n\tclass = "IntervalTier"\n\tname = "phones"\n'
    )
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".TextGrid", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(text)
        path = Path(handle.name)
    try:
        assert parse_text_grid(path) == [("流", 0.5, 0.9), ("等", 0.9, 1.3)]
    finally:
        path.unlink(missing_ok=True)


def test_vendored_tifa_sources_are_present():
    """vendored 的是 MIT 推理子集；模型权重不在这里（另放 models/TIFA-1.0）。"""
    vendor = PROJECT_ROOT / "src" / "sheetsage2_webui" / "vendor" / "tifa"
    assert (vendor / "LICENSE").is_file()
    assert (vendor / "inference" / "api.py").is_file()
    assert "MIT License" in (vendor / "LICENSE").read_text(encoding="utf-8")


def test_tifa_is_the_default_aligner():
    from sheetsage2_webui.asr import AsrConfig as Config

    assert Config(asr_model="m").aligner == "tifa"


def test_missing_tifa_weights_reports_and_falls_back():
    """缺权重不该让整条链路失败——返回 (False, 原因)，调用方退回 Qwen。"""
    from sheetsage2_webui.asr import AsrEngine

    engine = AsrEngine(AsrConfig(asr_model="m", aligner="tifa",
                                 tifa_model="D:/nope/model.pt"))
    ready, reason = engine._tifa_ready()
    assert ready is False
    assert "TIFA" in reason and "Qwen" in reason


def test_english_words_keep_our_text_not_tifa_lowercase():
    """TIFA 的 G2P 会 lowercase，英文词要用我们自己的写法。"""
    from sheetsage2_webui.asr import AsrEngine

    spans = [("the", 0.0, 0.3), ("wellerman", 0.3, 0.9)]
    assert AsrEngine._labels_for("The Wellerman", spans, "en") == ["The", "Wellerman"]
    # 数量对不上就退回 TIFA 给的字面
    assert AsrEngine._labels_for("The Wellerman", spans[:1], "en") == []
    # 中文按字切
    zh = [("流", 0.0, 0.3), ("转", 0.3, 0.6)]
    assert AsrEngine._labels_for("流转", zh, "zh") == ["流", "转"]


# --------------------------------------------------------------------- 加载参数

def test_load_kwargs_without_offload_puts_everything_on_the_device():
    config = AsrConfig(asr_model="m", aligner_model="a", device="cuda:0")
    kwargs = asr_load_kwargs(config, "bf16")
    assert kwargs["device_map"] == "cuda:0"
    assert "max_memory" not in kwargs
    assert kwargs["forced_aligner"] == "a"
    assert kwargs["forced_aligner_kwargs"]["device_map"] == "cuda:0"
    assert kwargs["max_inference_batch_size"] == 1


def test_load_kwargs_with_a_vram_budget_offloads_to_ram():
    """给了预算就走 accelerate 的 auto + max_memory：放不下的层留内存里。"""
    config = AsrConfig(asr_model="m", aligner_model="a", device="cuda:0",
                       max_vram="3GiB", max_ram="64GiB")
    kwargs = asr_load_kwargs(config, "bf16")
    assert kwargs["device_map"] == "auto"
    assert kwargs["max_memory"] == {0: "3GiB", "cpu": "64GiB"}
    # 对齐器是**另一个** from_pretrained，它得拿同一份预算，否则照样全塞进卡里
    assert kwargs["forced_aligner_kwargs"]["device_map"] == "auto"
    assert kwargs["forced_aligner_kwargs"]["max_memory"] == {0: "3GiB", "cpu": "64GiB"}


def test_load_kwargs_without_an_aligner_stays_clean():
    kwargs = asr_load_kwargs(AsrConfig(asr_model="m"), "bf16")
    assert "forced_aligner" not in kwargs and "forced_aligner_kwargs" not in kwargs

    offloaded = asr_load_kwargs(AsrConfig(asr_model="m", max_vram="2GiB"), "bf16")
    assert offloaded["max_memory"] == {0: "2GiB", "cpu": "96GiB"}     # 默认给内存留 96GiB


# ------------------------------------------------- 日语：没有对齐器就停下来
def test_qwen_aligner_ready_needs_config_json(tmp_path):
    engine = AsrEngine.__new__(AsrEngine)          # 不 load，只测这一个判断
    engine.config = AsrConfig(asr_model=tmp_path, aligner_model=tmp_path / "missing")
    assert engine._qwen_aligner_ready() is False

    (tmp_path / "yes").mkdir()
    (tmp_path / "yes" / "config.json").write_text("{}", encoding="utf-8")
    engine.config.aligner_model = tmp_path / "yes"
    assert engine._qwen_aligner_ready() is True

    engine.config.aligner_model = None
    assert engine._qwen_aligner_ready() is False


def test_chars_from_stamps_are_moved_onto_the_song_timeline():
    class Stamp:
        def __init__(self, text, start, end):
            self.text, self.start_time, self.end_time = text, start, end

    chars = AsrEngine._chars_from_stamps([Stamp("こ", 0.2, 0.8)], 30.0)
    assert [(c.text, round(c.start, 2), round(c.end, 2)) for c in chars] == [
        ("こ", 30.2, 30.8)]


def test_ja_without_the_aligner_maps_to_an_actionable_error():
    """日语 + 没装对齐器：给一张说清"去 02_models.bat 选 2"的错误卡。"""
    from sheetsage2_webui.webui.errors import describe

    error = describe(JaAlignerMissing("识别出来是日语，但没装日语对齐器"))
    assert error.code == "E_JA_ALIGNER"
    assert "02_models" in error.action

def test_read_lrc_strips_speaker_prefix(tmp_path):
    """LRC 行内的 `男：/合：` 不能当歌词挂在音符上。"""
    from sheetsage2_webui.asr import read_lrc

    path = tmp_path / "x.lrc"
    path.write_text("[00:07.95]男：One two three\n[00:10.00]合：一起唱\n",
                    encoding="utf-8")
    text = read_lrc(path)
    assert "男：" not in text and "合：" not in text
    assert "One two three" in text and "一起唱" in text

def test_pick_align_mode_routes_korean_to_qwen():
    """韩文不能被当成中文交给 TIFA。"""
    from sheetsage2_webui.asr import AsrEngine

    assert AsrEngine._pick_align_mode("사랑해요") == "ko"
    assert AsrEngine._pick_align_mode("君が僕") == "ja"
    assert AsrEngine._pick_align_mode("你好世界") == "tifa"

def test_lrc_mode_refuses_korean_instead_of_using_chinese():
    """韩文 LRC 不能退化成"按中文 G2P 跑"——必须报错让用户走 Qwen 对齐器。"""
    from sheetsage2_webui.asr import (AsrConfig, AsrEngine, JaAlignerMissing,
                                      LyricLine, Section)

    engine = AsrEngine(AsrConfig(asr_model="x", aligner_model=None, language="Korean"))
    try:
        engine._transcribe_with_lrc(Path("x.wav"), [Section(0.0, 10.0)],
                                    [LyricLine(0.0, "사랑해")], language="Korean")
    except JaAlignerMissing as exc:
        assert "韩语" in str(exc)
    else:
        raise AssertionError("韩语 LRC 应该抛 JaAlignerMissing")

def test_explode_cjk_items_splits_hangul_words():
    """Qwen 对齐器对韩语给词级时间戳，必须拆成音节（否则 69% 的字没时间）。"""
    from types import SimpleNamespace
    from sheetsage2_webui.asr import AsrEngine

    items = [SimpleNamespace(text="사랑해", start_time=1.0, end_time=2.5),
             SimpleNamespace(text="love", start_time=3.0, end_time=3.5)]
    out = AsrEngine._explode_cjk_items(items)
    assert [text for text, _s, _e in out] == ["사", "랑", "해", "love"]
    assert out[0][1] == 1.0 and abs(out[2][2] - 2.5) < 1e-9
    assert out[3] == ("love", 3.0, 3.5)


# --------------------------------------------------------------- LRC 走哪套对齐
def test_lrc_aligner_kind_picks_tifa_for_chinese_and_english(tmp_path):
    """中/英/粤的 LRC 走 TIFA（独立小模型），**不必加载 6.3G 的识别模型**。"""
    engine = AsrEngine(AsrConfig(asr_model=tmp_path))
    assert engine.lrc_aligner_kind("晚风停在旧站台", "Chinese") == "tifa"
    assert engine.lrc_aligner_kind("The wind stops here", "English") == "tifa"


def test_lrc_aligner_kind_picks_qwen_for_japanese_and_korean(tmp_path):
    """日/韩没有 TIFA G2P，要对齐器（挂在 ASR 模型里）——这两种必须加载识别模型。"""
    engine = AsrEngine(AsrConfig(asr_model=tmp_path))
    assert engine.lrc_aligner_kind("君が僕の心臓じゃないせいで", None) == "qwen"
    # 用户显式指定日语时也要走对齐器（哪怕字面是汉字）
    assert engine.lrc_aligner_kind("晚风停在旧站台", "Japanese") == "qwen"

def test_read_lrc_decodes_html_entities(tmp_path):
    """韩文 LRC 常把整行存成 &#45432; 这种数字实体，不解码会变成一串数字挂到音符上。"""
    from sheetsage2_webui.asr import read_lrc

    path = tmp_path / "k.lrc"
    path.write_text("[00:10.00]&#45432;&#48124;&#51648; @ SM LVYIN Studio\n",
                    encoding="utf-8")
    text = read_lrc(path)
    assert "노민지 @ SM LVYIN Studio" in text
    assert "&#" not in text

def test_unidic_dir_detection_and_ja_routing(tmp_path, monkeypatch):
    """有本地 UniDic 时：日语走 TIFA（不要求 pip 装 unidic 包）。"""
    from sheetsage2_webui import tifa_align
    from sheetsage2_webui.asr import AsrConfig, AsrEngine

    monkeypatch.delenv("SHEETSAGE_UNIDIC_DIR", raising=False)
    assert tifa_align.unidic_dir() is None
    assert tifa_align.ja_g2p_available() is False

    dicdir = tmp_path / "unidic"
    dicdir.mkdir()
    (dicdir / "sys.dic").write_bytes(b"x")
    (dicdir / "mecabrc").write_text("", encoding="utf-8")
    monkeypatch.setenv("SHEETSAGE_UNIDIC_DIR", str(dicdir))

    assert tifa_align.unidic_dir() == dicdir
    assert tifa_align.ja_g2p_available() is True
    engine = AsrEngine(AsrConfig(asr_model="x", aligner="tifa", language="Japanese"))
    assert engine._lrc_route("こんにちは", "Japanese")[0] == "tifa"
    assert AsrEngine._pick_align_mode("こんにちは") == "tifa"
