r"""填词逻辑的回归测试（不需要模型）。

    .\.venv\Scripts\python.exe -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.lyrics import (  # noqa: E402
    CONTINUATION,
    MELISMA,
    assign_lyrics,
    assign_lyrics_split,
    continuation_marker,
    fill_project,
)
from sheetsage2_webui.timeline import from_playback  # noqa: E402


def playback(*measures):
    """最小可用的 playback.json 片段（每小节恰好一个全音符长）。"""
    return {"measures": [
        dict(index=index, start=start, end=end, score_start=float(index),
             score_end=float(index + 1), leading_rest=0.0, trailing_rest=0.0)
        for index, (start, end) in enumerate(measures)
    ], "duration": measures[-1][1] if measures else 0.0}


class Char:
    """替身：只要有 .text / .start / .end（可选 .parts / .confidence）就行。"""

    def __init__(self, text: str, start: float, end: float,
                 parts: tuple[float, ...] = (),
                 confidence: float | None = None) -> None:
        self.text = text
        self.start = start
        self.end = end
        self.parts = parts
        self.confidence = confidence


def spans(*pairs) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in pairs]


# --------------------------------------------------------------- 基本
def test_one_char_per_note():
    notes = spans((0.0, 0.5), (0.5, 1.0), (1.0, 1.5))
    chars = [Char("春", 0.0, 0.5), Char("眠", 0.5, 1.0), Char("不", 1.0, 1.5)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["春", "眠", "不"]
    assert fill.assigned == 3 and fill.melisma == 0
    assert fill.unmatched_notes == 0


def test_char_spanning_several_notes_becomes_melisma():
    """一个字唱了几个音：第一个音挂字，后续写转音标记。"""
    notes = spans((0.0, 0.3), (0.3, 0.6), (0.6, 0.9))
    chars = [Char("长", 0.0, 0.9)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["长", MELISMA, MELISMA]
    assert fill.assigned == 1 and fill.melisma == 2


def test_english_word_without_syllable_info_gets_even_syllable_boundaries():
    """没有音节信息时按粗估音节数均分：hello 2 音节占 3 个音 → `- +`。

    （旧行为是"说不清就全写 `-`"；8 首人工谱统计显示 `+` 被少标 101 处、反向只有 9 处，
    所以改成按音节均分。不同人的标注约定不同，严格 marker 要配 `marker_any` 一起看。）
    """
    notes = spans((0.0, 0.3), (0.3, 0.6), (0.6, 0.9))
    chars = [Char("hello", 0.0, 0.9)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["hello", MELISMA, CONTINUATION]


def test_mixed_song_marks_one_syllable_per_note_as_continuation():
    """混唱（汉字 + 英文词）：一个字一个音的词写 `+`。

    everywhere 占 3 个音、粗估 4 个音节 → 音数 ≤ 音节数，可以认为一个音一个音节，
    三个音里后两个写 `+`（人工谱「everywhere + +」）。
    """
    notes = spans((0.0, 0.3), (0.3, 0.6), (0.6, 0.9), (0.9, 1.2))
    chars = [Char("你", 0.0, 0.3), Char("everywhere", 0.3, 1.2)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["你", "everywhere", CONTINUATION, CONTINUATION]


def test_mixed_song_splits_english_syllables_evenly():
    """混唱里的英文词同样按音节均分（hello 2 音节占 3 个音 → `- +`）。"""
    notes = spans((0.0, 0.3), (0.3, 0.6), (0.6, 0.9), (0.9, 1.2))
    chars = [Char("你", 0.0, 0.3), Char("hello", 0.3, 1.2)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["你", "hello", MELISMA, CONTINUATION]


def test_mixed_english_run_with_parts_reserves_its_syllables():
    """混唱里拿到 `parts` 的英文段交给词级 DP：everywhere 占 3 个音（+ +），

    后面的 `we` 因此从第 5 个音开始——这就是梦不落「everywhere + + we」的人工写法。
    """
    notes = spans(*[(i * 0.3, (i + 1) * 0.3) for i in range(6)])
    chars = [Char("你", 0.0, 0.3),
             Char("everywhere", 0.3, 1.2, parts=(0.6, 0.9)),
             Char("we", 1.2, 1.5)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["你", "everywhere", CONTINUATION, CONTINUATION, "we", ""]


def test_mixed_english_run_without_parts_stays_on_the_char_path():
    """没有 `parts` 就不重排：一个字级 DP，词按自己的时间落（`we` 在第 5 个音）。"""
    notes = spans(*[(i * 0.3, (i + 1) * 0.3) for i in range(6)])
    chars = [Char("你", 0.0, 0.3), Char("everywhere", 0.3, 1.2),
             Char("we", 1.2, 1.5)]
    fill = assign_lyrics(notes, chars)
    heads = [i for i, text in enumerate(fill.lyrics) if text in ("everywhere", "we")]
    assert heads == [1, 4]          # 一个字级 DP：everywhere 一个音，we 按自己的时间


def test_refine_latin_runs_moves_the_next_word_only_with_parts():
    """白盒：`_refine_latin_runs` 只在英文段带 `parts` 时改头。

    没有 `parts` 时词级 DP 会把每个词当单音节，反而更差（《梦不落》英文 token
    50% → 27%），所以加了这道门槛。
    """
    from sheetsage2_webui.lyrics import _refine_latin_runs

    notes = [(i * 0.3, (i + 1) * 0.3) for i in range(8)]
    order = list(range(8))
    without = [Char("你", 0.0, 0.3), Char("everywhere", 0.3, 1.2),
               Char("we", 1.2, 1.2)]
    with_parts = [Char("你", 0.0, 0.3),
                  Char("everywhere", 0.3, 1.2, parts=(0.6, 0.9)),
                  Char("we", 1.2, 1.2)]

    heads, skipped = {0: 0, 1: 1, 2: 2}, []
    _refine_latin_runs(order, notes, without, [0, 1, 2], heads, skipped)
    assert heads == {0: 0, 1: 1, 2: 2}
    assert skipped == []

    heads, skipped = {0: 0, 1: 1, 2: 2}, []
    _refine_latin_runs(order, notes, with_parts, [0, 1, 2], heads, skipped)
    assert heads[1] == 1            # everywhere 的词头不动
    assert heads[2] == 4            # we 让出 everywhere 的两个音节音





def test_english_word_marks_each_new_syllable_with_plus():
    """有音节起点时：延长当前音节写 `-`，进下一个音节写 `+`。

    对应人工修的《The Phoenix》里的 `lighting - +` / `running + -`。
    """
    notes = spans((0.0, 0.3), (0.3, 0.6), (0.6, 0.9))
    fill = assign_lyrics(notes, [Char("lighting", 0.0, 0.9, parts=(0.3, 0.6))])
    assert fill.lyrics == ["lighting", CONTINUATION, CONTINUATION]

    # 第一个音节跨两个音、然后才换音节：`-` 再 `+`
    fill = assign_lyrics(notes, [Char("running", 0.0, 0.9, parts=(0.6,))])
    assert fill.lyrics == ["running", MELISMA, CONTINUATION]


# --------------------------------------------------------------- 英文词-音 DP 对齐
# 对照 samples/对齐/英文/ 的人工谱归纳的规律（词头 = 词开始后第一个音、延音不越过
# 下一个词、ASR 重复词跳过、漂移区按序列结构拉回）。全拉丁字母的词走 DP 路径。

def test_english_word_heads_the_first_note_at_or_after_its_start():
    """词头 = 第一个 onset ≥ 词开始的音，不是"重叠最多"的音。

    TIFA 的词起点略早于音符起点（实测 112/228 同刻、83 个晚 0.1s）：按"重叠最多"
    挑会把 'bubble' 推到下一个音上、前面留一个占位音节（实测旧算法因此错 137/341）。
    """
    notes = spans((7.91, 8.11), (8.11, 8.31), (8.31, 8.51), (8.51, 8.71))
    chars = [Char("a", 7.92, 8.0), Char("bubble", 8.0, 8.54, parts=(8.28,)),
             Char("up", 8.54, 8.69)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["a", "bubble", CONTINUATION, "up"]


def test_english_one_word_per_note_chain_is_not_displaced():
    """一词一音的连续链不被挤位（Poison 开头 "I'm not a bubble up to"）。"""
    notes = spans((7.51, 7.71), (7.71, 7.91), (7.91, 8.11),
                  (8.11, 8.31), (8.31, 8.51), (8.51, 8.71))
    chars = [Char("I'm", 7.51, 7.67), Char("not", 7.67, 7.92),
             Char("a", 7.92, 8.0), Char("bubble", 8.0, 8.54, parts=(8.28,)),
             Char("up", 8.54, 8.69)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["I'm", "not", "a", "bubble", CONTINUATION, "up"]


def test_duplicate_asr_word_is_skipped_and_reported():
    """ASR 重复的词没有合法词头（它的词头区间被下一个词的开始封死）→ 跳过并登记。

    实测 Poison "Cause I know I know you're poison"——重复的 "I know" 不许把
    you're/poison 往后挤。
    """
    notes = spans((38.11, 38.31), (38.31, 38.71), (38.71, 38.91),
                  (38.91, 39.31), (39.31, 39.71))
    chars = [Char("I", 38.11, 38.27), Char("know", 38.27, 38.51),
             Char("i", 38.51, 38.65), Char("know", 38.65, 38.68),
             Char("you're", 38.68, 38.82), Char("poison", 38.82, 39.65, parts=(39.25,))]
    fill = assign_lyrics(notes, chars)
    words = [lyric for lyric in fill.lyrics if lyric.strip() not in ("", MELISMA, CONTINUATION)]
    assert words == ["I", "know", "you're", "poison"]
    assert set(fill.absorbed_chars) >= {"i", "know"}
    assert fill.warnings


def test_inflated_word_does_not_push_the_rest_of_the_phrase_late():
    """TIFA 把 'you' 拉长到 1.4s、后面三个词整体晚 1.1s：按序列结构拉回连续的音。

    实测 Poison "you want me baby that's"——旧算法整句推迟 3 个音。
    `baby` 带双音节 `parts=(57.1,)`，1-to-1 音节匹配把它的第 2 个音正确标为 `+`。
    """
    notes = spans((55.11, 55.31), (55.31, 55.51), (55.51, 55.71),
                  (55.71, 55.91), (56.11, 56.31), (56.51, 56.71))
    chars = [Char("you", 55.05, 56.45), Char("want", 56.45, 56.70),
             Char("me", 56.70, 56.89), Char("baby", 56.89, 57.68, parts=(57.1,)),
             Char("that's", 57.68, 58.05)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["you", "want", "me", "baby", CONTINUATION, "that's"]


def test_english_head_cannot_drift_far_before_the_word():
    """词头不许比词的开始早 1.5s 以上：延音段形状代价不能把词拽到几秒前的音上。

    实测 Poison 'any' 曾被拽早 3.2s（它前面有一段没有词的音阶走句）。
    无缝相接的第二个音归 'ah' 的延音 `-`，'boom' 稳定落在第 3 个音上。
    """
    notes = spans((10.0, 10.2), (10.2, 10.4), (12.0, 12.2))
    chars = [Char("ah", 10.0, 10.15), Char("boom", 11.9, 12.15)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["ah", MELISMA, "boom"]


def test_held_english_word_keeps_distance_gradient():
    """正常唱长超过 0.35s 的英文词不该被抹平距离梯度（旧 `_is_inflated` 会把它推歪）。"""
    notes = spans((53.10, 53.53), (53.53, 53.74), (53.74, 54.10))
    chars = [Char("but", 53.10, 53.55), Char("your", 53.55, 54.06)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["but", "your", MELISMA]


def test_parts_monotonic_dp_matches_late_syllable_and_avoids_double_consume():
    """音节 `parts` 轻微滞后或挤在同一音容差内时，1-to-1 DP 仍能正确标 `+`。"""
    # 2 音节词 1 个续音，part 比音符晚 0.15s（超旧 0.10s 容差）仍应标 `+`
    notes = spans((0.0, 0.3), (0.3, 0.6))
    fill = assign_lyrics(notes, [Char("captain", 0.0, 0.6, parts=(0.45,))])
    assert fill.lyrics == ["captain", CONTINUATION]

    # 3 音节词 2 个续音，两个 parts 都落在第 1 个续音容差内，不该被一次吞光
    notes3 = spans((0.0, 0.3), (0.3, 0.6), (0.6, 0.9))
    fill3 = assign_lyrics(notes3, [Char("everywhere", 0.0, 0.9, parts=(0.28, 0.38))])
    assert fill3.lyrics == ["everywhere", CONTINUATION, CONTINUATION]


def test_continuation_does_not_cross_long_rest_past_word_end():
    """越过词 end 后遇到明显休止（>0.5s），孤立音不该被标成延音。"""
    notes = spans((10.0, 10.2), (10.9, 11.1), (12.6, 12.8))
    chars = [Char("ah", 10.0, 10.15), Char("boom", 12.5, 12.75)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["ah", "", "boom"]


def test_retime_lines_by_chars_anchors_matching_tokens_across_asr_insertion():
    """无时间戳 TXT：ASR 中段多听和声/幻觉词时，序列锚定只局部插值，不拖垮后续整首。"""
    from sheetsage2_webui.asr import LyricLine
    from sheetsage2_webui.lyrics import anchor_lyric_lines, retime_lines_by_chars

    official = [LyricLine(-1.0, "春眠不觉晓"), LyricLine(-1.0, "处处闻啼鸟")]
    # ASR 在两句之间多听了 4 个杂音字 "啊哦嗯哈"，且把 "觉" 听错成 "脚"
    spoken = [
        Char("春", 1.0, 1.2), Char("眠", 1.2, 1.4), Char("不", 1.4, 1.6),
        Char("脚", 1.6, 1.8), Char("晓", 1.8, 2.0),
        Char("啊", 3.0, 3.2), Char("哦", 3.2, 3.4), Char("嗯", 3.4, 3.6), Char("哈", 3.6, 3.8),
        Char("处", 5.0, 5.2), Char("处", 5.2, 5.4), Char("闻", 5.4, 5.6),
        Char("啼", 5.6, 5.8), Char("鸟", 5.8, 6.0),
    ]
    retimed = retime_lines_by_chars(official, spoken)
    assert [c.text for c in retimed] == list("春眠不觉晓处处闻啼鸟")
    # 第二句首字 "处" 必须锚定在 5.0s，而不是被中间 4 个杂音字拖到 3.x 秒
    assert retimed[5].start == pytest.approx(5.0)
    # 错听字 "觉" 在 "不"(1.6) 和 "晓"(1.8) 之间局部插值
    assert 1.59 <= retimed[3].start <= 1.61 and 1.79 <= retimed[3].end <= 1.81

    anchored = anchor_lyric_lines(official, spoken)
    assert [(line.text, round(line.start, 2)) for line in anchored] == [
        ("春眠不觉晓", 1.0), ("处处闻啼鸟", 5.0)]


def test_retime_lines_by_chars_keeps_english_words_intact():
    """无时间戳英文 TXT：按整词切分，不再拆成单字母。"""
    from sheetsage2_webui.asr import LyricLine
    from sheetsage2_webui.lyrics import retime_lines_by_chars

    official = [LyricLine(-1.0, "Hello, world!")]
    spoken = [Char("hello", 1.0, 1.5, parts=(1.25,), confidence=0.88),
              Char("world", 1.6, 2.1, confidence=0.91)]
    retimed = retime_lines_by_chars(official, spoken)
    assert [c.text for c in retimed] == ["Hello", "world"]
    assert retimed[0].parts == (1.25,)
    assert retimed[0].confidence == pytest.approx(0.88)
    assert retimed[1].start == pytest.approx(1.6)


def test_latin_melisma_stops_before_the_next_word_start():
    """人工规则：延音不得越过下一个词的开始（实测 0 反例）。

    老贪心路径（only_empty / 混合内容）里英文词头也吃这条边界。
    """
    notes = spans((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8))
    chars = [Char("la", 0.0, 0.65), Char("dee", 0.45, 0.7)]
    fill = assign_lyrics(notes, chars, existing=[""] * 4, only_empty=True)
    # dee 的头落在包含它的 note2 上；note1 是 la 的延音（onset 0.2 < 0.45 ✓）
    assert fill.lyrics[:2] == ["la", MELISMA]
    assert fill.lyrics[2] == "dee"


def test_a_following_char_is_not_eaten_by_the_previous_melisma():
    """真实 bug：处理第一个字时贪心把下一个音标成 `-`，结果下一个字被吃掉。

    实测《光芒》开头会变成"曾 - 天 空"，"经"丢失。修法是先定所有字的头，再补转音。
    """
    notes = spans((30.0, 30.17), (30.17, 30.33), (30.33, 30.65), (30.65, 30.89))
    chars = [Char("曾", 30.01, 30.17), Char("经", 30.17, 30.33),
             Char("天", 30.33, 30.65), Char("空", 30.65, 30.89)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["曾", "经", "天", "空"]
    assert fill.melisma == 0
    assert fill.assigned == 4


def test_melisma_stops_at_the_next_char():
    """转音延伸到**下一个字的头**为止，不会越过它。

    窗口终点用下一个字的起点而不是本字的 `end`：TIFA 对一部分字给出接近零的
    时长（实测粤语"系" 0.02s），按 end 截断会把延音全丢掉（marker 0/72）。
    """
    notes = spans((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8))
    chars = [Char("长", 0.0, 0.4), Char("短", 0.6, 0.8)]
    fill = assign_lyrics(notes, chars, tolerance=0.0)
    # 长 -> note0 为头；note1、note2 都在"短"的头(note3)之前 -> 都是转音
    assert fill.lyrics == ["长", MELISMA, MELISMA, "短"]


def test_continuation_marker_picks_by_script():
    assert continuation_marker("你") == MELISMA
    assert continuation_marker("hello") == CONTINUATION
    assert continuation_marker("abc") == CONTINUATION
    # 单拍假名（あ / きょ / ラ）只有一拍，拖音必须写 `-`（避免非法 `ラ + +`）；
    # 未拆的多拍假名词（ありがとう）才写 `+`；中粤韩一字一音写 `-`
    assert continuation_marker("あ") == MELISMA
    assert continuation_marker("きょ") == MELISMA
    assert continuation_marker("ラ") == MELISMA
    assert continuation_marker("ありがとう") == CONTINUATION
    assert continuation_marker("한") == MELISMA


def test_single_mora_japanese_melisma_uses_dash_not_plus():
    """单拍假名跨多个音符时必须写 `-` 而不是 `+`（SynthV 日语不接受 `ラ + +`）。"""
    notes = spans((0.0, 0.3), (0.3, 0.6), (0.6, 0.9), (0.9, 1.2))
    fill = assign_lyrics(notes, [Char("か", 0.0, 0.9), Char("ラ", 0.9, 1.2)])
    assert fill.lyrics == ["か", MELISMA, MELISMA, "ラ"]


def test_japanese_word_spans_notes_with_plus():
    """未拆的多拍日语词跨多个音：前 mora-1 个音写 `+`，超出拍数写 `-`。"""
    notes = spans((0.0, 0.3), (0.3, 0.6), (0.6, 0.9))
    fill = assign_lyrics(notes, [Char("ありがとう", 0.0, 0.9)])
    assert fill.lyrics == ["ありがとう", CONTINUATION, CONTINUATION]


def test_mora_japanese_mixed_with_english_uses_dash_for_kana_and_plus_for_en():
    """逐拍平假名 + 英文混唱：假名走一拍一音与 `-`，英文词按音节起点标 `+`。"""
    notes = spans((0.0, 0.3), (0.3, 0.6), (0.6, 0.9), (0.9, 1.2), (1.2, 1.5))
    chars = [
        Char("な", 0.0, 0.3),
        Char("み", 0.3, 0.9),
        Char("baby", 0.9, 1.5, parts=(1.2,)),
    ]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["な", "み", MELISMA, "baby", CONTINUATION]


def test_japanese_lyrics_with_kanji_go_through_the_word_path():
    """日语歌词汉字假名混排：出现假名就整首按词算（DP 路径）。

    汉字是中日共用的；纯汉字（中粤）必须仍走逐字贪心。
    """
    # 汉字词 + 假名词，词头各占一个音、谢谢的拍子铺满
    notes = spans((0.0, 0.3), (0.3, 0.6), (0.6, 0.9), (0.9, 1.2), (1.2, 1.5))
    chars = [Char("夢", 0.0, 0.3), Char("infinite", 0.3, 0.35),
             Char("愛", 0.35, 0.6), Char("ありがとう", 0.6, 1.5)]
    fill = assign_lyrics(notes, chars)
    words = [l for l in fill.lyrics if l.strip() not in ("", MELISMA, CONTINUATION)]
    assert words[0] == "夢" and "ありがとう" in words
    assert fill.reasons.get("dp_head", 0) >= 3


def test_refill_wipes_stale_lyrics_off_non_head_notes():
    """整体重填（only_empty=False）：词头落点变了，旧词不能残留在别的音上。

    实测《スロウリー》重填后出现"てる てる"——旧词留在不再是词头的音上。
    """
    notes = spans((1.35, 1.55), (1.55, 1.75), (1.75, 1.94), (1.94, 2.13))
    chars = [Char("壊れ", 1.30, 1.55), Char("てる", 1.92, 2.00)]
    fill = assign_lyrics(notes, chars, existing=["x", "てる", "", ""])
    # てる 的头按时间戳落在 note2（1.75 晚于 1.92−容差…按 DP 距离挑更近的 1.94）
    heads = [i for i, l in enumerate(fill.lyrics) if l == "てる"]
    assert len(heads) == 1, fill.lyrics
    assert fill.lyrics[heads[0] - 1] != "てる"


def test_chinese_lyrics_stay_on_the_char_path():
    """纯汉字（中粤韩）一字一音：走**字级 DP**，不走词级 DP，延续写 `-`。"""
    notes = spans((0.0, 0.3), (0.3, 0.6), (0.6, 0.9), (0.9, 1.5))
    chars = [Char("爱", 0.0, 0.3), Char("无", 0.3, 0.6), Char("限", 0.6, 0.9),
             Char("大", 0.9, 1.5)]
    fill = assign_lyrics(notes, chars)
    assert fill.lyrics == ["爱", "无", "限", "大"]
    assert fill.reasons.get("dp_head") == 4        # 字级 DP
    assert fill.reasons.get("window") is None      # 不是逐字贪心


def test_zero_length_char_still_gets_a_note():
    """对齐器对约 20% 的字给出零长度时间戳（start == end = "没对齐上"）。

    `[start - 容差, end)` 这时会退化成空窗口，把它们全漏掉——实测《光芒》有 53/324 个字
    是这样。零长度就当"这个字发生在这一刻"，给它一个以它为中心的对称窗口。
    """
    notes = spans((58.900, 59.190), (59.190, 59.770), (60.055, 60.340))
    chars = [Char("雅", 59.130, 59.130), Char("山", 59.130, 59.130)]
    fill = assign_lyrics(notes, chars)
    # 两个点状的字分别落到最近的两个音上，而不是被丢掉
    assert fill.dropped == 0
    assert "雅" in fill.lyrics and "山" in fill.lyrics


def test_zero_length_char_uses_nearest_start_not_overlap():
    notes = spans((10.0, 10.5), (11.0, 11.5))
    fill = assign_lyrics(notes, [Char("点", 10.45, 10.45)], tolerance=0.05)
    # 10.45 离 10.0 更近（0.45）而不是 11.0（0.55）
    assert fill.lyrics == ["点", ""]


def test_zero_length_char_does_not_create_melisma():
    notes = spans((0.0, 0.5), (0.5, 1.0), (1.0, 1.5))
    fill = assign_lyrics(notes, [Char("点", 0.0, 0.0)])
    assert fill.lyrics[0] == "点"
    assert fill.melisma == 0


def test_assignment_is_monotonic_in_note_order():
    """后面的字不能挂到前面的音上。

    实测《光芒》"喜马拉雅山"会输出成"喜 马 拉 山 雅"——雅和山都是零长度、同一时刻，
    退路各自找最近的音就颠倒过来了。
    """
    notes = spans((58.90, 59.19), (59.19, 59.77), (60.05, 60.34), (60.34, 60.92))
    chars = [Char("雅", 59.13, 59.13), Char("山", 59.13, 59.13)]
    fill = assign_lyrics(notes, chars)
    assert fill.dropped == 0
    positions = [i for i, text in enumerate(fill.lyrics) if text in ("雅", "山")]
    assert positions == sorted(positions), "先出现的字必须落在更靠前的音上"
    assert fill.lyrics[positions[0]] == "雅"
    assert fill.lyrics[positions[1]] == "山"


# --------------------------------------------------------------- 下标 bug 回归
def test_chars_are_not_dropped_when_the_sorted_order_differs_from_note_order():
    """回归：`last_pos` 曾经同时被当成"排序后的位置"和"音符下标"用。

    两者只有在音符本来就按起点有序时才相等。一旦某个音更早开始，扫描起点就跳到了
    旋律中段，那个位置之前的音再也扫不到——实测《光芒》因此丢掉 29/324 个字，
    而且全被误报成"没有重叠"（真正原因是它们根本没被扫到）。

    这里的音符故意乱序（note2 比 note1 早），字按时间铺开，一个字都不该丢。
    """
    notes = spans((0.0, 0.25), (0.8, 1.05), (0.25, 0.5), (0.5, 0.8))
    chars = [Char("一", 0.01, 0.24), Char("二", 0.26, 0.49),
             Char("三", 0.51, 0.79), Char("四", 0.81, 1.04)]
    fill = assign_lyrics(notes, chars, tolerance=0.05)

    assert fill.dropped == 0, f"丢了 {fill.dropped_chars}"
    assert set(fill.lyrics) == {"一", "二", "三", "四"}
    # 每个字都挂到了时间上对应的那个音
    assert fill.lyrics == ["一", "四", "二", "三"]


def test_every_char_lands_somewhere_or_is_reported():
    """字只能有三种下场：显示出来、算作转音延续、或者被登记；绝不能悄悄消失。"""
    notes = spans((0.0, 0.4), (0.4, 0.8), (0.8, 1.2))
    chars = [Char("甲", 0.0, 0.2), Char("乙", 0.2, 0.4), Char("丙", 0.4, 0.6),
             Char("丁", 0.6, 0.8), Char("戊", 0.8, 1.0), Char("己", 1.0, 1.2)]
    fill = assign_lyrics(notes, chars)
    accounted = (fill.assigned + fill.dropped)
    assert accounted == len(chars)                       # 每个字都被处理过
    assert fill.dropped + len(fill.absorbed_chars) + fill.melisma <= len(chars)
    if fill.absorbed_chars:
        assert fill.warnings                            # 被挤掉必须出声


def test_chars_competing_for_one_note_are_registered_not_lost():
    """段落的音比字少时，多出来的字被登记（absorbed），而不是悄悄覆盖。"""
    notes = spans((0.0, 1.0))                            # 一个长音
    chars = [Char("一", 0.0, 0.3), Char("二", 0.3, 0.6), Char("三", 0.6, 0.9)]
    fill = assign_lyrics(notes, chars)

    assert fill.dropped == 0
    assert fill.lyrics[0] == "一"                        # 先来的占住音位
    assert set(fill.absorbed_chars) == {"二", "三"}
    assert fill.assigned == 3
    assert any("已经占住" in w for w in fill.warnings)


def test_note_spans_prefer_raw_tick_over_the_quantized_one():
    """填词要对着**量化前**的位置匹配。

    逐字时间戳是沿着模型自己的音符时间轴产生的；量化把 tick 吸到网格上之后，两者会差
    半个网格步长（实测最大 0.86s）。实测影响：《光芒》"显示不出来"的字 29 → 60，
    《相思》18 → 42。所以量化会把原位置存进 `raw_tick`，填词优先用它。
    """
    from sheetsage2_webui.lyrics import note_spans_seconds

    timeline = from_playback(playback((0.0, 2.0)))       # 一全音符 = 2s → 每 tick 1/960 s
    notes = [
        {"tick": 0, "dur": 960, "pitch": 60, "raw_tick": 0, "raw_dur": 960},
        # 量化把它从 940 挪到 960，时长也从 940 变成 1000
        {"tick": 960, "dur": 1000, "pitch": 62, "raw_tick": 940, "raw_dur": 940},
    ]
    spans = note_spans_seconds(timeline, notes)
    assert spans[1][0] == pytest.approx(timeline.tick_to_sec(940))
    assert spans[1][1] == pytest.approx(timeline.tick_to_sec(1880))
    assert spans[1][0] != pytest.approx(timeline.tick_to_sec(960))

    # 没有 raw_* 时退回 tick / dur（老工程 / 没开量化）
    legacy = [{"tick": 0, "dur": 960, "pitch": 60},
              {"tick": 960, "dur": 960, "pitch": 62}]
    assert note_spans_seconds(timeline, legacy)[1] == \
        (pytest.approx(timeline.tick_to_sec(960)),
         pytest.approx(timeline.tick_to_sec(1920)))


def test_fill_project_uses_raw_tick_so_quantization_does_not_break_alignment():
    """端到端：量化把音挪走之后，只要带着 raw_tick，填词结果不变。"""
    project = project_fixture()
    chars = [Char("一", 0.0, 0.25), Char("二", 0.25, 0.5),
             Char("三", 0.5, 0.75), Char("四", 0.75, 1.0)]
    baseline = fill_project(project_fixture(), chars)["lyrics"]

    moved = project_fixture()
    for index, note in enumerate(moved["tracks"][0]["notes"]):
        note["raw_tick"] = note["tick"]
        note["tick"] = note["tick"] + 120 * (index + 1)   # 假装量化把它们挪走了
    assert fill_project(moved, chars)["lyrics"] == baseline


def test_quantize_notes_carry_raw_position_and_keep_lyric():
    """量化留下的 `raw_tick` / `raw_dur` 必须等于量化前的值，并且不动已填的歌词。"""
    from sheetsage2_webui.quantize import quantize_tracks

    track = {"id": "vocal", "notes": [
        {"tick": 1730, "dur": 950, "pitch": 60, "lyric": "一"},
        {"tick": 2900, "dur": 900, "pitch": 62, "lyric": "二"},
    ]}
    new_tracks, stats = quantize_tracks([track], ticks_per_quarter=1920)
    assert stats.failed_tracks == 0
    assert [n["raw_tick"] for n in new_tracks[0]] == [1730, 2900]
    assert [n["raw_dur"] for n in new_tracks[0]] == [950, 900]
    assert [n["lyric"] for n in new_tracks[0]] == ["一", "二"]
    # 落网格的不变量是**网格步长**（一拍/96 = 20 tick）
    assert stats.placement_ticks == 1920 // 96
    assert all(n["tick"] % stats.placement_ticks == 0 for n in new_tracks[0])
    assert all((n["tick"] + n["dur"]) % stats.placement_ticks == 0 for n in new_tracks[0])


# --------------------------------------------------------------- 容差与退化
def test_tolerance_lets_a_slightly_early_char_land():
    """容差是**逐字贪心路径**的参数（`only_empty=True` 时走那条）；字级 DP 用距离代价。"""
    notes = spans((1.00, 1.10))
    chars = [Char("晚", 1.20, 1.30)]           # 字整个落在音符之后，没有重叠
    assert assign_lyrics(notes, chars, tolerance=0.20,
                         only_empty=True).lyrics == ["晚"]
    assert assign_lyrics(notes, chars, tolerance=0.05,
                         only_empty=True).lyrics == [""]


def test_char_with_no_note_is_dropped_and_reported():
    notes = spans((5.0, 5.5))
    chars = [Char("丢", 0.0, 0.3)]
    fill = assign_lyrics(notes, chars)
    assert fill.dropped == 1
    assert fill.dropped_chars == ["丢"]
    assert fill.warnings


def test_falls_back_to_the_most_overlapping_note():
    """没有音符起点落在窗口里时，贪心路径挂到重叠最多的那个音（字级 DP 用距离代价）。"""
    notes = spans((0.0, 1.0), (1.0, 2.0))
    chars = [Char("回", 0.85, 0.95)]           # 起点都不在窗口内
    fill = assign_lyrics(notes, chars, only_empty=True)
    assert fill.lyrics == ["回", ""]           # 与第一个音重叠最多


def test_empty_inputs_are_safe():
    assert assign_lyrics([], []).lyrics == []
    fill = assign_lyrics(spans((0.0, 1.0)), [])
    assert fill.lyrics == [""]
    assert fill.unmatched_notes == 1


def test_blank_char_text_is_ignored():
    fill = assign_lyrics(spans((0.0, 1.0)), [Char("  ", 0.0, 1.0)])
    assert fill.lyrics == [""]


# --------------------------------------------------------------- 既有歌词
def test_only_empty_keeps_hand_written_lyrics():
    notes = spans((0.0, 0.5), (0.5, 1.0))
    chars = [Char("新", 0.0, 0.5), Char("词", 0.5, 1.0)]
    fill = assign_lyrics(notes, chars, existing=["手写", ""], only_empty=True)
    assert fill.lyrics == ["手写", "词"]


def test_existing_is_overwritten_by_default():
    notes = spans((0.0, 0.5))
    fill = assign_lyrics(notes, [Char("新", 0.0, 0.5)], existing=["旧"])
    assert fill.lyrics == ["新"]


# --------------------------------------------------------------- 工程
def project_fixture() -> dict:
    """两个小节、四个音符的极小工程。"""
    return {
        "version": 1,
        "ppq": 1920,
        "tracks": [{"id": "vocal", "role": "vocal-lead", "notes": [
            {"tick": 0, "dur": 960, "pitch": 60, "lyric": ""},
            {"tick": 960, "dur": 960, "pitch": 62, "lyric": ""},
            {"tick": 1920, "dur": 960, "pitch": 64, "lyric": ""},
            {"tick": 2880, "dur": 960, "pitch": 65, "lyric": ""},
        ]}],
        "timeline": {
            "measures": [
                {"index": 0, "start": 0.0, "end": 2.0, "tick_start": 0, "tick_end": 7680,
                 "numerator": 4, "denominator": 4, "is_pickup": False},
            ],
            "pickup_seconds": 0.0,
            "tick_length": 7680,
        },
    }


def test_fill_project_writes_lyrics_into_the_track():
    project = project_fixture()
    # 小节 0–2s 覆盖 0–7680 tick；每个音 960 tick = 0.25s
    chars = [Char("一", 0.0, 0.25), Char("二", 0.25, 0.5),
             Char("三", 0.5, 0.75), Char("四", 0.75, 1.0)]
    result = fill_project(project, chars)
    assert result["found"] is True
    notes = project["tracks"][0]["notes"]
    assert [n["lyric"] for n in notes] == ["一", "二", "三", "四"]
    assert result["fill"]["assigned"] == 4


def test_fill_project_reports_a_missing_track():
    result = fill_project(project_fixture(), [], track_id="nope")
    assert result["found"] is False
    assert "nope" in result["reason"]


def test_fill_project_respects_tick_to_second_mapping():
    """音符时间是从存下来的小节表反算的，不是按 BPM 猜的。"""
    project = project_fixture()
    # 把第二个音往后挪到 1920 tick（=0.5s），于是它不再是 0.25s 那个音
    project["tracks"][0]["notes"][1]["tick"] = 1920
    chars = [Char("甲", 0.0, 0.2), Char("乙", 0.4, 0.6)]
    fill_project(project, chars)
    lyrics = [n["lyric"] for n in project["tracks"][0]["notes"]]
    assert lyrics[0] == "甲"
    # 乙落在 0.25s 那个音上——证明是按小节表反算的秒在匹配
    assert lyrics[1] == "乙"


# --------------------------------------------------------------------- ③ 切分模式
def test_split_mode_gives_each_char_its_own_piece():
    """一音多字：一个长音上唱了两个字 -> 切成两段，两个字都看得见。"""
    spans = [(0.0, 2.0), (2.0, 3.0)]
    chars = [Char("一", 0.2, 1.0), Char("二", 1.2, 1.8)]
    fill = assign_lyrics_split(spans, chars)

    assert [note.text for note in fill.notes] == ["一", "二"]
    assert [note.source for note in fill.notes] == [0, 0]      # 都来自同一个原音
    assert fill.notes[0].start == 0.2 and fill.notes[0].end == 1.0
    assert fill.notes[1].start == 1.2 and fill.notes[1].end == 1.8
    assert all(not note.slur for note in fill.notes)           # 各自是头，不是延音
    assert fill.visible == 2 and fill.absorbed == 0
    # 代价：原音 2 个 -> 3 段；切开 1 个音
    assert fill.cut_notes == 1 and fill.added_notes == 1 and fill.original_notes == 2
    assert fill.ratio == pytest.approx(0.5)


def test_split_mode_marks_following_segments_as_slur():
    """一字多音：一个字跨两个音 -> 头 + 后续段（对应现在的转音标记）。"""
    spans = [(0.0, 1.0), (1.0, 2.0)]
    fill = assign_lyrics_split(spans, [Char("长", 0.2, 1.8)])

    assert [note.text for note in fill.notes] == ["长", ""]
    assert [note.slur for note in fill.notes] == [False, True]
    assert fill.visible == 1 and fill.added_notes == 0        # 没多出音符，只是重新分段
    assert fill.notes[0].end == 1.0 and fill.notes[1].start == 1.0


def test_split_mode_does_not_overlap_or_grow_total_duration():
    """切出来的段不能重叠，总时长也不能超过原音——否则等于凭空加时间。"""
    spans = [(0.0, 3.0)]
    chars = [Char("甲", 0.1, 0.9), Char("乙", 1.0, 2.0), Char("丙", 2.0, 2.8)]
    fill = assign_lyrics_split(spans, chars)

    assert [note.text for note in fill.notes] == ["甲", "乙", "丙"]
    for before, after in zip(fill.notes, fill.notes[1:]):
        assert before.end <= after.start + 1e-9
    assert fill.notes[0].start >= spans[0][0]
    assert fill.notes[-1].end <= spans[0][1] + 1e-9


def test_split_mode_falls_back_for_zero_length_timestamps():
    """零长度时间戳的字没有边界可切，但**不能丢**——退回"离得最近的音"。"""
    spans = [(0.0, 1.0), (1.0, 2.0)]
    # 空文本的字直接跳过，不算"零长度"（它根本不是字）
    assert assign_lyrics_split(spans, [Char("", 1.0, 1.0)]).degenerate == 0

    fill = assign_lyrics_split(spans, [Char("晚", 0.2, 0.8), Char("风", 1.0, 1.0)])
    assert fill.degenerate == 1
    assert fill.fallback == 1 and fill.visible == 2             # 兜底挂上了，没丢字


def test_split_mode_reports_chars_with_no_note_to_attach():
    spans = [(0.0, 1.0), (3.0, 4.0)]
    fill = assign_lyrics_split(spans, [Char("缝", 1.8, 2.2)])
    assert fill.dropped == 1 and fill.visible == 0
    assert fill.warnings and "间隙" in fill.warnings[0]


def test_split_mode_is_not_the_default_path():
    """切分模式目前只是"候选"：默认填词仍然走 onset（音符一个都不动）。

    这条测试是把决定锁住——真要以切分为默认，得先改这里，别悄悄换掉。
    """
    spans = [(0.0, 2.0)]
    chars = [Char("一", 0.2, 1.0), Char("二", 1.2, 1.8)]
    onset = assign_lyrics(spans, chars)
    assert len(onset.lyrics) == len(spans)                      # onset 不新增音符
    assert onset.lyrics == ["一"]                               # "二" 被挤占
    assert len(onset.absorbed_chars) == 1