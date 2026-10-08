r"""`sections` 模块：标签翻译与"相邻同名合并"（纯函数，不碰模型）。

    .\.venv\Scripts\python.exe -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.sections import (  # noqa: E402
    CHINESE_SECTIONS,
    canonical_label,
    canonical_labels,
    is_instrumental,
    merge_consecutive,
)


def test_the_seven_labels_the_model_actually_emits():
    """实测 `output/` 里 10 个工程只用过这 7 种标签，全部要能翻成中文。"""
    assert canonical_label("chorus") == "副歌"
    assert canonical_label("verse") == "主歌"
    assert canonical_label("pre-chorus") == "预副歌"
    assert canonical_label("intro") == "前奏"
    assert canonical_label("interlude") == "间奏"
    assert canonical_label("bridge") == "桥段"
    assert canonical_label("outro") == "尾奏"
    assert set(CHINESE_SECTIONS) == {"前奏", "主歌", "预副歌", "副歌", "间奏", "桥段", "尾奏"}


def test_aliases_and_case_are_folded():
    assert canonical_label("  CHORUS ") == "副歌"
    assert canonical_label("PreChorus") == "预副歌"
    assert canonical_label("instrumental") == "间奏"
    assert canonical_label("ending") == "尾奏"
    assert canonical_label("REFRAIN") == "副歌"


def test_unknown_labels_are_kept_verbatim_and_empty_is_none():
    assert canonical_label("weirdname") == "weirdname"
    assert canonical_label("") is None
    assert canonical_label(None) is None
    assert canonical_label("   ") is None


def test_is_instrumental_covers_intro_interlude_outro():
    for label in ("intro", "interlude", "outro", "前奏", "间奏", "尾奏"):
        assert is_instrumental(label) is True
    for label in ("verse", "chorus", "bridge", "主歌", "副歌", None):
        assert is_instrumental(label) is False


def test_merge_consecutive_only_joins_touching_same_labels():
    spans = [(0.0, 1.0, "前奏"), (1.0, 2.0, "前奏"), (2.0, 3.0, "主歌"),
             (3.0, 4.0, "副歌"), (5.0, 6.0, "前奏")]
    assert merge_consecutive(spans) == [
        (0.0, 2.0, "前奏"), (2.0, 3.0, "主歌"), (3.0, 4.0, "副歌"), (5.0, 6.0, "前奏"),
    ]


def test_merge_consecutive_keeps_a_gap_apart():
    # 中间断了 0.5s：即使同名也不并（那不是"连续的一段"）
    assert merge_consecutive([(0.0, 1.0, "主歌"), (1.5, 2.0, "主歌")]) == [
        (0.0, 1.0, "主歌"), (1.5, 2.0, "主歌"),
    ]


def test_merge_consecutive_drops_empty_and_keeps_input_order():
    assert merge_consecutive([(1.0, 1.0, "主歌"), (0.0, 1.0, "主歌"),
                              (1.0, 2.0, "主歌")]) == [(0.0, 2.0, "主歌")]


def test_canonical_labels_dedupes_and_keeps_order():
    assert canonical_labels(["chorus", "verse", "chorus", None, "verse"]) == \
        ["副歌", "主歌"]
    assert canonical_labels([]) == []
