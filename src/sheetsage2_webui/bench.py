"""挂字评测的纯逻辑：读样本、对齐人工/自动歌词、算 head/marker 指标。

只做**挂字层**，不加载模型、不需要音频：

- `read_svp_notes`：读 `auto.svp` / `manual.svp` 的音符（秒）；
- `read_chars`：读 `lyrics.json` 的逐字时间；
- `read_project_notes`：读 `project.json` 的自动音符（只用于计数/对齐参考）；
- `score_alignment`：用**人工音符**当音符序列，把自动 chars 喂给线上
  `assign_lyrics`，再和人工歌词比较；
- `best_offset`：每首样本的 manual 与 chars 可能有全局时间偏移，
  搜一个 offset 再比（正式评测要报告它，不能当不存在）。

指标刻意分开：

- `content`：人工 head 字序列与生成 head 字序列的匹配数（顺序，不看位置）；
- `exact`：匹配上的 head 在同一个音符下标；
- `pm1`：匹配上的 head 在 ±1 个音符内；
- `missing`：人工有字、生成是空；
- marker：人工 `-`/`+` 与生成同一音符的标记是否一致。

`content` 高而 `exact` 低，说明“字都在、位置/切分不对”，不能当成挂字全错。

**分层**：`meta.json` 里 `split == "stress"` 的样本（对唱/合唱这类音符层就选错旋律的）
不计入主指标，单独列。挂字算法对不了一条不存在的旋律，混进均值只会掩盖它的真实表现。
"""
from __future__ import annotations

import bisect
import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .lyrics import DEFAULT_TOLERANCE, assign_lyrics, note_spans_seconds
from .timeline import from_stored_measures

MARKERS = frozenset({"-", "+", "·", "・", "•"})
DEFAULT_MAX_OFFSET = 3.0
DEFAULT_OFFSET_STEP = 0.02
#: 和声/ad-lib 词：人工谱写了、但 LRC/识别文本里没有时不计入分母（见 `score_alignment`）。
ADLIB_TOKENS = frozenset({
    "oh", "ohh", "ohhh", "ooh", "oohh", "oohhh", "ho", "hoh", "whoa", "woah",
    "wow", "yeah", "yea", "yeh", "hey", "ah", "aah", "mm", "hmm", "mmm",
    "la", "na", "da",
})
#: 人工谱与 `lyrics.json` 文本低于这个相似度就告警（content 会被文本差异压低几分）。
TEXT_WARN_MIN = 0.9
#: 低于这个相似度视为**不同一份歌词**，不进挂字主指标（content/marker 量的不是挂字）。
TEXT_EXCLUDE_MIN = 0.7


@dataclass(frozen=True)
class Note:
    start: float
    end: float
    pitch: int = 0
    lyric: str = ""


@dataclass(frozen=True)
class Char:
    text: str
    start: float
    end: float
    #: 英文词内部「第二、三…个音节」的起点（混唱的 `+` 靠它）。
    parts: tuple[float, ...] = ()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_svp(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes().rstrip(b"\x00").decode("utf-8"))


def read_svp_notes(path: Path) -> list[Note]:
    """SVP -> 秒域音符。按 SVP 自己的 tempo 表逐段换算（与现有 tmp 脚本一致）。"""
    data = _read_svp(path)
    tempos = [(int(item["position"]) / 1_470_000, float(item["bpm"]))
              for item in data["time"]["tempo"]]
    tempos.sort(key=lambda item: item[0])
    if not tempos:
        raise ValueError(f"{path} 没有 tempo")
    if tempos[0][0] > 0:
        tempos = [(0.0, tempos[0][1])] + tempos
    marks = []
    acc = 0.0
    for (t0, b0), (t1, _b1) in zip(tempos, tempos[1:]):
        acc += (t1 - t0) * 60 / (b0 * 480)
        marks.append((t1, acc))
    marks.append((float("inf"), acc))

    def tick_to_sec(tick: float) -> float:
        sec = 0.0
        for (t0, b0), (t1, upto) in zip(tempos, marks):
            if tick <= t1:
                return sec + (tick - t0) * 60 / (b0 * 480)
            sec = upto
        t0, b0 = tempos[-1]
        return sec + (tick - t0) * 60 / (b0 * 480)

    track = next((item for item in data.get("tracks") or []
                  if (item.get("mainGroup") or {}).get("notes")), None)
    if track is None:
        return []
    notes = []
    for note in (track.get("mainGroup") or {}).get("notes") or []:
        onset = note["onset"] / 1_470_000
        notes.append(Note(
            start=tick_to_sec(round(onset)),
            end=tick_to_sec(round(onset + note["duration"] / 1_470_000)),
            pitch=int(note.get("pitch") or 0),
            lyric=str(note.get("lyrics") or ""),
        ))
    return notes


def read_midi_notes(path: Path) -> list[Note]:
    """从 `ref.mid` 读人工谱：音符时间 + 逐音歌词。

    量化金标（`samples/量化/*/ref.mid`）和部分英文样本只有 MIDI，没有 `manual.svp`。
    MIDI 里的歌词是 `lyrics`/`text` 元事件，按时间顺序与 `note_on` 一一配对。
    """
    import mido

    midi = mido.MidiFile(str(path))
    #: mido 的 `message.time` 是 **tick 差**，不是秒；而且 tempo 常只写在 0 轨，
    #: 所以要先汇总全局 tempo 表，再把绝对 tick 换成秒（按轨默认 120 BPM 会整体偏）。
    ticks_per_beat = midi.ticks_per_beat or 480
    tempo_events: list[tuple[int, int]] = []
    for track in midi.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "set_tempo":
                tempo_events.append((tick, message.tempo))
    tempo_events.sort()

    def to_seconds(target: int) -> float:
        seconds = 0.0
        previous = 0
        tempo = 500000
        for when, value in tempo_events:
            if when > target:
                break
            seconds += mido.tick2second(when - previous, ticks_per_beat, tempo)
            previous = when
            tempo = value
        return seconds + mido.tick2second(target - previous, ticks_per_beat, tempo)

    notes: list[tuple[float, int, int]] = []   # (start_seconds, pitch, track)
    texts: list[tuple[float, str]] = []
    for track_index, track in enumerate(midi.tracks):
        tick = 0
        open_notes: dict[int, list[float]] = {}
        for message in track:
            tick += message.time
            when = to_seconds(tick)
            if message.type == "note_on" and message.velocity > 0:
                open_notes.setdefault(message.note, []).append(when)
            elif message.type == "note_off" or (message.type == "note_on"
                                                 and message.velocity == 0):
                stack = open_notes.get(message.note)
                if stack:
                    start = stack.pop()
                    notes.append((start, message.note, track_index))
            elif message.type in ("lyrics", "text"):
                lyric = str(message.text).strip()
                if lyric:
                    texts.append((when, lyric))
    if not notes:
        return []
    # 旋律轨：歌词元事件最多的那条轨道（都没有就取音最多的）
    counts: dict[int, int] = {}
    for _, _, track_index in notes:
        counts[track_index] = counts.get(track_index, 0) + 1
    melody_track = max(counts, key=counts.get)
    melody = sorted(note for note in notes if note[2] == melody_track)
    if texts:
        # 文本先按时间排；每个文本配给它后面（或最近）的音符
        texts.sort()
        lyrics = [""] * len(melody)
        cursor = 0
        for when, lyric in texts:
            while cursor < len(melody) and melody[cursor][0] < when - 0.05:
                cursor += 1
            if cursor >= len(melody):
                break
            if not lyrics[cursor]:
                lyrics[cursor] = lyric
        result = [Note(start, start, pitch, lyric)
                  for (start, pitch, _), lyric in zip(melody, lyrics)]
    else:
        result = [Note(start, start, pitch, "") for start, pitch, _ in melody]
    # 音长用下一个同轨音的起点兜底
    for index in range(len(result) - 1):
        if result[index].end <= result[index].start:
            result[index] = Note(result[index].start, result[index + 1].start,
                                 result[index].pitch, result[index].lyric)
    return result


def read_manual(folder: Path) -> list[Note]:
    """人工谱：优先 `manual.svp`，其次 `ref.mid` / `ref.midi`。"""
    svp = folder / "manual.svp"
    if svp.is_file():
        return read_svp_notes(svp)
    for name in ("ref.mid", "ref.midi"):
        candidate = folder / name
        if candidate.is_file():
            return read_midi_notes(candidate)
    return []


def read_chars(path: Path) -> list[Char]:
    data = _read_json(path)
    return [Char(str(item["text"]), float(item["start"]), float(item["end"]),
                 tuple(float(value) for value in item.get("parts") or ()))
            for item in data.get("chars") or []
            if str(item.get("text") or "").strip()]


def read_project_notes(path: Path) -> list[Note]:
    data = _read_json(path)
    timeline = from_stored_measures(data["timeline"]["measures"])
    vocal = next((track for track in data.get("tracks") or []
                  if track.get("id") == "vocal"), None)
    if vocal is None:
        return []
    spans = note_spans_seconds(timeline, vocal.get("notes") or [])
    return [Note(start=start, end=end,
                 pitch=int(note.get("pitch") or 0),
                 lyric=str(note.get("lyric") or ""))
            for (start, end), note in zip(spans, vocal.get("notes") or [])]


#: 英文缩写 ↔ 展开的归一化表。ASR 有时写 `would've`、有时写 `would have`，
#: 两侧不统一会让 token 数差一个、整句错位（实测《Would've, Could've, Should've》）。
#: 归一化只用于**比较**，不改产物。
_CONTRACTION_PAIRS = {
    ("would", "have"): "would've", ("could", "have"): "could've",
    ("should", "have"): "should've", ("i", "have"): "i've",
    ("you", "have"): "you've", ("we", "have"): "we've",
    ("they", "have"): "they've", ("i", "am"): "i'm", ("i", "m"): "i'm",
    ("you", "are"): "you're", ("we", "are"): "we're", ("they", "are"): "they're",
    ("it", "is"): "it's", ("that", "is"): "that's", ("let", "us"): "let's",
    ("do", "not"): "don't", ("does", "not"): "doesn't", ("did", "not"): "didn't",
    ("can", "not"): "can't", ("is", "not"): "isn't", ("are", "not"): "aren't",
    ("was", "not"): "wasn't", ("were", "not"): "weren't", ("will", "not"): "won't",
    ("would", "not"): "wouldn't", ("could", "not"): "couldn't",
    ("should", "not"): "shouldn't", ("have", "not"): "haven't",
    ("has", "not"): "hasn't", ("i", "will"): "i'll", ("you", "will"): "you'll",
    ("i", "would"): "i'd", ("going", "to"): "gonna", ("want", "to"): "wanna",
}


def _canonical_latin(text: str) -> str:
    return text.strip().lower().replace("\u2019", "'")


def normalize_latin_heads(sequence: Sequence[tuple[int, str]]
                          ) -> list[tuple[int, str]]:
    """英文 token 归一化：小写 + 弯引号 + 把"展开形"合并回缩写形。

    返回 `(音符下标, 文本)`；合并时保留**第一个** token 的音符下标。
    """
    out: list[tuple[int, str]] = []
    index = 0
    while index < len(sequence):
        note, text = sequence[index]
        current = _canonical_latin(text)
        if index + 1 < len(sequence):
            following = _canonical_latin(sequence[index + 1][1])
            merged = _CONTRACTION_PAIRS.get((current, following))
            if merged:
                out.append((note, merged))
                index += 2
                continue
        out.append((note, current))
        index += 1
    return out


def align_tokens(left: Sequence[str], right: Sequence[str]
                 ) -> list[tuple[int, int]]:
    """把两条 token 序列对齐，返回匹配上的 `(左下标, 右下标)`。

    **不用 `difflib.SequenceMatcher`**：它的 `autojunk` 启发式在长序列上会把高频
    token 当噪声吞掉，而中文和英文受这个影响的方向相反——中文（重复字多）关掉
    autojunk 会掉 8pp，英文（the/we 这类高频词）开着又会让小写归一化后匹配崩掉。
    这里用确定性的全局 DP（匹配 0 / 替换 1 / 跳过 1），与语言、词频无关。
    """
    n, m = len(left), len(right)
    if not n or not m:
        return []
    # 只存方向：0=对角（匹配/替换）、1=上（left 跳过）、2=左（right 跳过）
    direction = [[0] * (m + 1) for _ in range(n + 1)]
    previous = list(range(m + 1))
    for i in range(1, n + 1):
        current = [i] + [0] * m
        left_text = left[i - 1]
        row = direction[i]
        for j in range(1, m + 1):
            if left_text == right[j - 1]:
                cost = previous[j - 1]
            else:
                cost = previous[j - 1] + 1
            up = previous[j] + 1
            across = current[j - 1] + 1
            if cost <= up and cost <= across:
                current[j] = cost
                row[j] = 0
            elif up <= across:
                current[j] = up
                row[j] = 1
            else:
                current[j] = across
                row[j] = 2
        previous = current
    pairs: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        step = direction[i][j]
        if step == 0:
            if left[i - 1] == right[j - 1]:
                pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif step == 1:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def _marker(text: str) -> str | None:
    text = str(text or "").strip()
    if text in ("-", "·", "・", "•"):
        return "-"
    if text == "+":
        return "+"
    return None


def normalize_heads(lyrics: Sequence[str]) -> list[tuple[int, str]]:
    """把“连续重复同一个字”折叠成延音，返回 `(音符下标, 字)`。

    人工谱有两种延音写法：`-`，或者把同一个字重复写在连续音符上（`九 九 乘`）。
    不折叠就会把延音当新头，head 指标会假低。
    """
    heads: list[tuple[int, str]] = []
    previous = None
    for index, raw in enumerate(lyrics):
        text = str(raw or "").strip()
        if not text or text in MARKERS:
            previous = text or None
            continue
        if text == previous and heads:
            previous = text
            continue
        heads.append((index, text))
        previous = text
    return heads


def score_alignment(manual: Sequence[Note], chars: Sequence[Char],
                    *, offset: float = 0.0,
                    tolerance: float = DEFAULT_TOLERANCE,
                    greedy: bool = False) -> dict[str, Any]:
    """用人工音符 + 自动 chars 跑线上挂字，再和人工歌词比。

    `greedy=True` 强制走逐字贪心（`only_empty=True`）：只给 offset 搜索当廉价代理，
    正常评分走默认的字级 DP。
    """
    spans = [(note.start, note.end) for note in manual]
    shifted = _shifted(chars, offset)
    fill = assign_lyrics(spans, shifted, tolerance=tolerance,
                         only_empty=greedy)

    # 平均「字起点到最近音符起点」的距离：offset 搜索的次级判据。
    # 没有它，DP 路径对 offset 很宽容（差 0.4s 也能全对），搜索会在同分 offset 里
    # 挑绝对值最小的那个，于是真正的全局偏移（如《梦不落》-1.90s）被藏起来。
    onsets = [note.start for note in manual]
    distance = 0.0
    if onsets:
        for char in shifted:
            position = bisect.bisect_left(onsets, char.start)
            candidates = [abs(onsets[k] - char.start)
                          for k in (position - 1, position)
                          if 0 <= k < len(onsets)]
            distance += min(candidates) if candidates else 0.0

    manual_heads = normalize_heads([note.lyric for note in manual])
    generated_heads = normalize_heads(fill.lyrics)
    # 英文歌的「oh / wow / yeah」常被唱出来、人工谱也写了，但 LRC/识别文本里没有——
    # 那不是挂字错，是**文本源**缺了。这类 token 从分母里剔掉并单独报数，
    # 否则会平白拉低 content/exact（实测《Believer》14%、HOYO 那首 12%）。
    char_texts = {str(char.text).strip().lower() for char in chars}
    adlib = 0
    kept = []
    for index, text in manual_heads:
        key = text.strip().lower()
        if key in ADLIB_TOKENS and key not in char_texts:
            adlib += 1
            continue
        kept.append((index, text))
    manual_heads = normalize_latin_heads(kept)
    generated_heads = normalize_latin_heads(generated_heads)
    manual_text = [text for _, text in manual_heads]
    generated_text = [text for _, text in generated_heads]

    matched = 0
    exact = 0
    pm1 = 0
    boundary_gt1s = 0
    for left, right in align_tokens(manual_text, generated_text):
        matched += 1
        manual_index = manual_heads[left][0]
        generated_index = generated_heads[right][0]
        delta = generated_index - manual_index
        exact += int(delta == 0)
        pm1 += int(abs(delta) <= 1)
        if abs(manual[manual_index].start - manual[generated_index].start) > 1.0:
            boundary_gt1s += 1

    missing = sum(1 for index, _text in manual_heads
                  if not str(fill.lyrics[index] if index < len(fill.lyrics)
                             else "").strip())

    markers = sum(1 for note in manual if _marker(note.lyric) is not None)
    marker_hit = 0
    marker_any = 0     # 与写法无关：只问"是不是延续音"，不问 `+` 还是 `-`
    for index, note in enumerate(manual):
        want = _marker(note.lyric)
        if want is None:
            continue
        got = _marker(fill.lyrics[index]) if index < len(fill.lyrics) else None
        marker_hit += int(got == want)
        marker_any += int(got is not None)

    heads = len(manual_heads)
    return {
        "offset": round(offset, 3),
        "heads": heads,
        "content": matched,
        "content_ratio": round(matched / heads, 4) if heads else 0.0,
        "distance": round(distance, 3),
        "exact": exact,
        "exact_ratio": round(exact / heads, 4) if heads else 0.0,
        "pm1": pm1,
        "pm1_ratio": round(pm1 / heads, 4) if heads else 0.0,
        "missing": missing,
        "markers": markers,
        "marker_hit": marker_hit,
        "marker_any": marker_any,
        "boundary_gt1s": boundary_gt1s,
        "adlib_excluded": adlib,
        "absorbed": len(fill.absorbed_chars),
        "dropped": fill.dropped,
        "generated_heads": len(generated_heads),
        "notes_manual": len(manual),
        "notes_auto": None,
        "chars": len(chars),
        "warnings": [],
    }


def best_offset(manual: Sequence[Note], chars: Sequence[Char],
                *, max_abs: float = DEFAULT_MAX_OFFSET,
                step: float = DEFAULT_OFFSET_STEP,
                greedy: bool = False) -> tuple[float, dict[str, Any]]:
    """搜一个全局 offset（加到 chars 上），使 content 最大；同分取距离小、|offset| 小。"""
    steps = int(round(max_abs / step))
    best: tuple[tuple[float, float, float, float, float], float,
                dict[str, Any]] | None = None
    for index in range(-steps, steps + 1):
        offset = index * step
        result = score_alignment(manual, chars, offset=offset, greedy=greedy)
        # 同分时先看「字到最近音的距离」（越小越可信），再看 |offset| 小。
        key = (result["content"], result["exact"], result["pm1"],
               -result["distance"], -abs(offset))
        if best is None or key > best[0]:
            best = (key, offset, result)
    assert best is not None
    return best[1], best[2]


def read_split(folder: Path) -> str:
    """读 `meta.json` 的 `split`；没有就 `unassigned`。

    分层的用途只有一个：**对唱/合唱这类音符层就选错旋律的样本不算挂字层的账**。
    `assign_lyrics` 再好也挂不到一条不存在的旋律上，把它们混进均值只会让
    「挂字算法好不好」这个问题失去答案。所以 `scan()` 默认把 `stress` 单独拿出来。
    """
    path = Path(folder) / "meta.json"
    if not path.is_file():
        return "unassigned"
    try:
        data = _read_json(path)
    except (OSError, ValueError):
        return "unassigned"
    return str(data.get("split") or "unassigned")


def _shifted(chars: Sequence[Char], offset: float) -> list[Char]:
    """平移时间，**连 parts 一起平**（漏了它，混唱的 `+` 在评测里永远测不出来）。"""
    out = []
    for char in chars:
        parts = tuple(getattr(char, "parts", ()) or ())
        out.append(Char(char.text, char.start + offset, char.end + offset,
                        tuple(value + offset for value in parts)))
    return out


def token_records(manual: Sequence[Note], chars: Sequence[Char], *,
                  offset: float = 0.0) -> list[dict[str, Any]]:
    """逐 token 的落点与上下文特征（只算人工音符和生成头能对上的那些）。

    诊断「exact 差在哪」用：每条记录带 `delta = 生成音下标 − 人工音下标`，
    以及这个人工音的上下文（上一/下一音是不是延音、音长、前隙、局部字密度）。
    """
    spans = [(note.start, note.end) for note in manual]
    fill = assign_lyrics(spans, _shifted(chars, offset))
    manual_heads = normalize_heads([note.lyric for note in manual])
    generated_heads = normalize_heads(fill.lyrics)
    starts = sorted(char.start + offset for char in chars)
    records: list[dict[str, Any]] = []
    for left, right in align_tokens([text for _, text in manual_heads],
                                    [text for _, text in generated_heads]):
            manual_index = manual_heads[left][0]
            generated_index = generated_heads[right][0]
            note = manual[manual_index]
            previous = manual[manual_index - 1] if manual_index else None
            following = (manual[manual_index + 1]
                         if manual_index + 1 < len(manual) else None)
            low = bisect.bisect_left(starts, note.start - 5)
            high = bisect.bisect_right(starts, note.start + 5)
            records.append({
                "text": manual_heads[left][1],
                "manual": manual_index,
                "generated": generated_index,
                "delta": generated_index - manual_index,
                "duration": note.end - note.start,
                "gap_before": (note.start - previous.end) if previous else None,
                "prev_marker": bool(previous
                                    and previous.lyric.strip() in MARKERS),
                "next_marker": bool(following
                                    and following.lyric.strip() in MARKERS),
                "first_note": manual_index == 0,
                "density": (high - low) / 10.0,
            })
    return records


def delta_histogram(records: Sequence[dict[str, Any]]) -> dict[str, int]:
    """生成头相对人工音的下标偏差分布。"""
    buckets = {"<=-3": 0, "-2": 0, "-1": 0, "0": 0, "1": 0, "2": 0, ">=3": 0}
    for record in records:
        delta = int(record["delta"])
        key = ("<=-3" if delta <= -3 else ">=3" if delta >= 3 else str(delta))
        buckets[key] += 1
    return buckets


#: 诊断切片：命中率按这些上下文分开看，找「差一个音」集中在哪种结构上。
SLICE_RULES: tuple[tuple[str, Any], ...] = (
    ("上一音是延音", lambda record: record["prev_marker"]),
    ("下一音是延音", lambda record: record["next_marker"]),
    ("音长 <0.25s", lambda record: record["duration"] < 0.25),
    ("音长 >1.0s", lambda record: record["duration"] > 1.0),
    ("前隙 >0.5s", lambda record: (record["gap_before"] or 0.0) > 0.5),
    ("全曲首音", lambda record: record["first_note"]),
    ("密集字 ≥3/s", lambda record: record["density"] >= 3.0),
)


def slice_rates(records: Sequence[dict[str, Any]]
                ) -> dict[str, tuple[int, int]]:
    """每个诊断切片的（exact, total）。"""
    out: dict[str, tuple[int, int]] = {}
    for name, rule in SLICE_RULES:
        subset = [record for record in records if rule(record)]
        out[name] = (sum(1 for record in subset if record["delta"] == 0),
                     len(subset))
    return out


def map_notes(auto: Sequence[Note], manual: Sequence[Note], *,
              tolerance: float = 0.3) -> dict[int, int]:
    """自动音符下标 → 人工音符下标（按 onset 单调匹配）。

    product 模式要先有这层映射，才能把「自动音符上挂的字」换算成人工谱里的位置。
    """
    starts = [note.start for note in manual]
    mapping: dict[int, int] = {}
    last = -1
    for index, note in enumerate(auto):
        position = bisect.bisect_left(starts, note.start - tolerance)
        while position < len(starts) and starts[position] <= note.start + tolerance:
            if position > last:
                mapping[index] = position
                last = position
                break
            position += 1
    return mapping


def product_score(manual: Sequence[Note], auto: Sequence[Note],
                  chars: Sequence[Char], *, offset: float = 0.0,
                  tolerance: float = 0.3) -> dict[str, Any]:
    """端到端（product）口径：**自动音符**上挂字，映射回人工谱再比。

    与 `score_alignment` 的区别只在音符序列：那边用人工音（对齐-only），
    这边用 `project.json` 的音（用户真正拿到的）。音符本身错了也会算进来。
    """
    spans = [(note.start, note.end) for note in auto]
    fill = assign_lyrics(spans, _shifted(chars, offset))
    manual_heads = normalize_heads([note.lyric for note in manual])
    generated_heads = normalize_heads(fill.lyrics)
    mapping = map_notes(auto, manual, tolerance=tolerance)
    matched = exact = pm1 = 0
    for left, right in align_tokens([text for _, text in manual_heads],
                                    [text for _, text in generated_heads]):
            manual_index = manual_heads[left][0]
            generated_index = generated_heads[right][0]
            matched += 1
            target = mapping.get(generated_index)
            if target is None:
                continue
            delta = target - manual_index
            exact += int(delta == 0)
            pm1 += int(abs(delta) <= 1)
    heads = len(manual_heads)
    return {
        "heads": heads,
        "content": matched,
        "exact": exact,
        "pm1": pm1,
        "notes_auto": len(auto),
        "notes_manual": len(manual),
        "note_match_rate": round(len(mapping) / max(1, len(auto)), 4),
    }


def text_similarity(manual: Sequence[Note], chars: Sequence[Char]) -> float:
    """人工谱歌词 vs `lyrics.json` 文字的序列相似度（0~1）。

    `content` 比的是**两边歌词的文字序列**，所以文本不是同一份时它量的是
    「ASR 跟人工词差多少」，不是挂字好坏：巴拉莱卡人工是「轰鸣声」、ASR 是「后面是」，
    相似度 51.9%，content 只有 219/546。低于 0.9 的样本不能进挂字主指标。
    """
    manual_text = [text for _, text in normalize_latin_heads(
        normalize_heads([note.lyric for note in manual]))]
    char_text = [text for _, text in normalize_latin_heads(
        [(0, str(char.text)) for char in chars if str(char.text or "").strip()])]
    if not manual_text or not char_text:
        return 0.0
    pairs = align_tokens(manual_text, char_text)
    return 2.0 * len(pairs) / (len(manual_text) + len(char_text))


def manual_triplet_share(manual: Sequence[Note], bpm: float | None) -> float:
    """人工谱里"纯三连音间隔"的占比（判断这首歌是不是三连音歌）。

    只看相邻音起点的间隔：落在 1/3 或 1/6 拍上、且不落在直线位上就算。
    **不需要估相位**（相位坑见 `docs/音符量化规格.md` §8.1）。
    """
    if not bpm or bpm <= 0 or len(manual) < 3:
        return 0.0
    beat = 60.0 / float(bpm)
    starts = sorted(note.start for note in manual)
    gaps = [b - a for a, b in zip(starts, starts[1:]) if b - a > 0]
    if not gaps:
        return 0.0

    def distance(value: float, step: float) -> float:
        rest = value % step
        return min(rest, step - rest)

    hits = sum(
        1 for gap in gaps
        if (distance(gap, beat / 3) <= 0.02 or distance(gap, beat / 6) <= 0.02)
        and distance(gap, beat / 4) > 0.02 and distance(gap, beat / 2) > 0.02
    )
    return hits / len(gaps)


def estimate_offset(manual: Sequence[Note], chars: Sequence[Char], *,
                    max_abs: float = DEFAULT_MAX_OFFSET,
                    step: float = DEFAULT_OFFSET_STEP,
                    tolerance: float = 0.05) -> float:
    """估计 manual 与 `lyrics.json` 的整体时间基准差。

    做法是**不依赖任何对齐算法**：找那个让最多的「字起点」正好落在「音符起点」上的
    offset。用挂字结果去搜 offset 是循环论证——贪心会把 offset 拉到自己偏好的位置
    （实测《赵本山》因此偏 +0.42s，《梦不落》偏 0.2s），换算法后整首歌就崩。
    """
    onsets = sorted(note.start for note in manual)
    if not onsets or not chars:
        return 0.0
    steps = int(round(max_abs / step))
    best_hits = -1
    best_offset = 0.0
    for index in range(-steps, steps + 1):
        offset = index * step
        hits = 0
        for char in chars:
            start = char.start + offset
            position = bisect.bisect_left(onsets, start - tolerance)
            if position < len(onsets) and onsets[position] <= start + tolerance:
                hits += 1
        if hits > best_hits or (hits == best_hits
                                and abs(offset) < abs(best_offset)):
            best_hits = hits
            best_offset = offset
    return best_offset


def score_folder(folder: Path, *, offset: float | None = None,
                 max_abs: float = DEFAULT_MAX_OFFSET,
                 step: float = DEFAULT_OFFSET_STEP,
                 diagnose: bool = False,
                 product: bool = False) -> dict[str, Any] | None:
    """评一个样本目录；缺 project/lyrics/manual 返回 None。

    `diagnose` 额外算逐 token 偏差与切片；`product` 额外算端到端口径
    （自动音符 + 自动字时间）。
    """
    folder = Path(folder)
    project_path = folder / "project.json"
    lyrics_path = folder / "lyrics.json"
    manual = read_manual(folder)
    if not (project_path.is_file() and lyrics_path.is_file() and manual):
        return None
    chars = read_chars(lyrics_path)
    if not manual or not chars:
        return None
    if offset is None:
        offset = estimate_offset(manual, chars, max_abs=max_abs, step=step)
    result = score_alignment(manual, chars, offset=offset)
    result["name"] = folder.name
    result["offset"] = round(offset, 3)
    result["split"] = read_split(folder)
    result["text_similarity"] = round(text_similarity(manual, chars), 4)
    try:
        meta = _read_json(folder / "project.json")
        bpm = (meta.get("meta") or {}).get("beat_bpm")
        result["triplet_share"] = round(manual_triplet_share(manual, bpm), 4)
    except (OSError, ValueError, KeyError, TypeError):
        result["triplet_share"] = None
    auto_notes: list[Note] | None = None
    try:
        auto_notes = read_project_notes(project_path)
        result["notes_auto"] = len(auto_notes)
    except (OSError, ValueError, KeyError, TypeError):
        result["notes_auto"] = None
    if diagnose:
        records = token_records(manual, chars, offset=offset)
        result["delta_hist"] = delta_histogram(records)
        result["slices"] = slice_rates(records)
        result["tokens"] = len(records)
    if product and auto_notes:
        result["product"] = product_score(manual, auto_notes, chars,
                                          offset=offset)
    if abs(offset) > 0.35:
        result["warnings"].append(f"manual 与 lyrics.json 有 {offset:+.2f}s 全局偏移")
    if result["text_similarity"] < TEXT_WARN_MIN:
        result["warnings"].append(
            f"manual 与 lyrics.json 文本只有 {result['text_similarity']:.1%} 相同"
            "（content 会被文本差异压低；<70% 不进主指标）")
    if result["notes_auto"]:
        ratio = abs(result["notes_auto"] - result["notes_manual"]) / max(
            1, result["notes_auto"])
        if ratio > 0.05:
            result["warnings"].append(
                f"音符数差 {result['notes_manual']}/{result['notes_auto']}（{ratio:.1%}）")
    if result["content_ratio"] >= 0.9 and result["exact_ratio"] < 0.7:
        result["warnings"].append("内容对但位置/切分差异大（看 exact/pm1）")
    return result


def scan(root: Path, *, exclude: Iterable[str] = ("新建文件夹",),
         offset: float | None = None, max_abs: float = DEFAULT_MAX_OFFSET,
         step: float = DEFAULT_OFFSET_STEP,
         diagnose: bool = False,
         product: bool = False) -> list[dict[str, Any]]:
    root = Path(root)
    if not root.is_dir():
        return []
    excluded = set(exclude)
    rows = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name in excluded or folder.name.startswith("."):
            continue
        row = score_folder(folder, offset=offset, max_abs=max_abs, step=step,
                           diagnose=diagnose, product=product)
        if row is not None:
            rows.append(row)
    return rows


def pending(root: Path, *, exclude: Iterable[str] = ("新建文件夹",)) -> list[str]:
    """有 `project.json` + `lyrics.json`、只差 `manual.svp` 的目录。

    「已经跑过、只缺人工精修」的样本是补评测集最便宜的一档，单独列出来，
    免得它们安静地躺在目录里不被算进任何一次基线。
    """
    root = Path(root)
    if not root.is_dir():
        return []
    excluded = set(exclude)
    names = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name in excluded or folder.name.startswith("."):
            continue
        if not (folder / "project.json").is_file():
            continue
        if not (folder / "lyrics.json").is_file():
            continue
        # 只判文件在不在，别在这里解析（`pending` 会被很轻的夹具调用）
        if (folder / "manual.svp").is_file():
            continue
        if any((folder / name).is_file() for name in ("ref.mid", "ref.midi")):
            continue
        names.append(folder.name)
    return names


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """两组口径都给：pooled（按 head 加权）与歌级均值（每首等权）。

    两者的差就是「聚合被大歌吃掉了多少」——现在 15 首里前 3 首占 32% 的 head，
    只报 pooled 会让 684 个 head 的一首比 76 个 head 的一首重 9 倍。
    """
    heads = sum(row["heads"] for row in rows)
    content = sum(row["content"] for row in rows)
    exact = sum(row["exact"] for row in rows)
    pm1 = sum(row["pm1"] for row in rows)
    markers = sum(row["markers"] for row in rows)
    marker_hit = sum(row["marker_hit"] for row in rows)
    marker_any = sum(row.get("marker_any", 0) for row in rows)
    missing = sum(row.get("missing", 0) for row in rows)
    boundary_gt1s = sum(row.get("boundary_gt1s", 0) for row in rows)
    adlib_excluded = sum(row.get("adlib_excluded", 0) for row in rows)
    ratios = [row["exact"] / row["heads"] for row in rows if row["heads"]]
    mean = sum(ratios) / len(ratios) if ratios else 0.0
    variance = (sum((ratio - mean) ** 2 for ratio in ratios) / (len(ratios) - 1)
                if len(ratios) > 1 else 0.0)
    return {
        "songs": len(rows),
        "heads": heads,
        "content": content,
        "exact": exact,
        "pm1": pm1,
        "markers": markers,
        "marker_hit": marker_hit,
        "marker_any": marker_any,
        "content_ratio": content / heads if heads else 0.0,
        "exact_ratio": exact / heads if heads else 0.0,
        "pm1_ratio": pm1 / heads if heads else 0.0,
        "marker_ratio": marker_hit / markers if markers else 0.0,
        "marker_any": marker_any,
        "marker_any_ratio": marker_any / markers if markers else 0.0,
        "missing": missing,
        "hidden_ratio": missing / heads if heads else 0.0,
        "boundary_gt1s": boundary_gt1s,
        "boundary_ratio": boundary_gt1s / heads if heads else 0.0,
        "adlib_excluded": adlib_excluded,
        "exact_song_mean": mean,
        "exact_song_sd": variance ** 0.5,
        "exact_song_min": min(ratios) if ratios else 0.0,
    }


def partition(rows: Sequence[dict[str, Any]], *,
              include_stress: bool = False,
              min_text: float = TEXT_EXCLUDE_MIN
              ) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                         list[dict[str, Any]]]:
    """分成（主指标, stress, 文本不一致）。

    默认把两类样本拿出来单列：

    - `split == "stress"`：对唱/合唱这类音符层就选了另一条旋律的样本
      （《灯火里的中国》自动 356 音 / 人工 330 音），挂字层再准也挂不对；
    - `text_similarity < min_text`（默认 0.7）：人工谱与 `lyrics.json` 根本不是
      同一份歌词（巴拉莱卡 51.9%、皮囊 55.2%），`content` 量的是文本差异。

    0.7~0.9 之间（元若蓝 87.8%、蒋大为 89.8%）仍然进主指标：文本差异只在个位数字，
    压低几分但仍能比大小，`text_similarity` 会一并报出来（<0.9 有告警）。
    """
    if include_stress:
        stress: list[dict[str, Any]] = []
        rest = list(rows)
    else:
        stress = [row for row in rows if row.get("split") == "stress"]
        rest = [row for row in rows if row.get("split") != "stress"]
    text_off = [row for row in rest
                if float(row.get("text_similarity", 1.0)) < min_text]
    main = [row for row in rest if row not in text_off]
    return main, stress, text_off
