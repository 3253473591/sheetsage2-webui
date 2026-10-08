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
    AsrConfig,
    Section,
    asr_load_kwargs,
    parsed_sections,
    sections_from_lab,
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
