"""ABC 记谱解析与时间换算（支撑「编辑 ABC 后导出也用编辑结果」）。

为什么需要它
------------
用户可以在「乐谱编辑」里改 ABC，改完导出的 SVP / MIDI 必须用改后的结果。
而 SVP/MIDI 需要**秒**为单位的时间，ABC 里只有**记谱时间**。因此必须：

1. 把 ABC 解析成带记谱时值的音符序列；
2. 把记谱时间换算成与原曲对齐的秒。

关于第 2 步
-----------
SheetSage2 的 ABC 并非等速铺排：诊断信息里会出现
``measure 0: inferred 2/4 from downbeat span`` / ``padded leading 2/4 span to 4/4``
这类补白。**用固定 BPM 累加时长会和原曲越走越偏。**

``playback.json`` 提供了真实的小节表::

    {"index":0, "start":0.01, "end":0.22, "score_start":0.0, "score_end":1.0,
     "leading_rest":0.875, "trailing_rest":0.0}

其中 ``score_*`` 的单位是**全音符**，``start``/``end`` 是原曲秒数。据此可在小节内
线性插值，把任意记谱位置映射到与原曲对齐的秒。

用户改动 ABC 后小节数可能与原表不符：此时按小节序号对齐到表的**尾部速率**外推，
并把降级情况写进诊断信息，避免静默出错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = [
    "AbcNote",
    "AbcScore",
    "parse_abc",
    "notes_to_seconds",
    "to_simple_notes",
    "to_simple_note_objs",
]

# --------------------------------------------------------------------------
# 音名表
# --------------------------------------------------------------------------
_LETTER_SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

#: 升号顺序 / 降号顺序
_SHARP_ORDER = ("F", "C", "G", "D", "A", "E", "B")
_FLAT_ORDER = ("B", "E", "A", "D", "G", "C", "F")

#: 调号 → 升降号数量（正数=升，负数=降）
_MAJOR_KEYS = {
    "C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5, "F#": 6, "C#": 7,
    "F": -1, "BB": -2, "EB": -3, "AB": -4, "DB": -5, "GB": -6, "CB": -7,
}
_MINOR_KEYS = {
    "A": 0, "E": 1, "B": 2, "F#": 3, "C#": 4, "G#": 5, "D#": 6, "A#": 7,
    "D": -1, "G": -2, "C": -3, "F": -4, "BB": -5, "EB": -6, "AB": -7,
}

_NOTE_RE = re.compile(
    r"""
    (?P<acc>\^{1,2}|_{1,2}|=)?          # 临时记号
    (?P<letter>[A-Ga-g])                 # 音名
    (?P<octave>[',]*)                    # 八度标记
    (?P<dur>(?:\d+)?(?:/+)?(?:\d+)?)?    # 时值
    """,
    re.VERBOSE,
)
_REST_RE = re.compile(r"(?P<kind>[zx])(?P<dur>(?:\d+)?(?:/+)?(?:\d+)?)?")
_MULTIREST_RE = re.compile(r"(?P<kind>Z)(?P<count>\d+)?")
_FIELD_RE = re.compile(r"^([A-Za-z]):\s*(.*)$")
_CHORD_SYM_RE = re.compile(r'"([^"]*)"')


@dataclass
class AbcNote:
    """一个音符或休止（记谱时值）。"""

    voice: str
    onset: float          # 起始位置，单位=全音符
    duration: float       # 时值，单位=全音符
    pitch: int | None     # MIDI 音高；None 表示休止
    chord: str | None = None
    measure: int = 0
    #: 该音符首字符在 ABC 文本中的绝对偏移。
    #: abcjs 渲染出的音符元素带 ``data-index``（同为字符偏移），据此可把
    #: 页面上的音符元素与后端音符**精确对应**——不受连音线合并、休止符、
    #: 多小节休止 ``Z`` 等造成数量不一致的影响（实测 abcjs 32 个元素 vs
    #: 解析 28 个音符，按序号对应是不可靠的）。
    char_index: int = -1
    #: 换算后的秒（由 :func:`notes_to_seconds` 填充）
    start: float | None = None
    #: 逐音歌词。只有**卷帘路径**会填（音符直接从卷帘的音符表构造，时间已经是秒）；
    #: ABC 文本路径不填，歌词仍由 ``lyrics.json`` / 占位词在导出时分配。
    lyric: str | None = None

    @property
    def is_rest(self) -> bool:
        return self.pitch is None

    @property
    def end(self) -> float | None:
        return None if self.start is None else self.start + self.duration_seconds

    @property
    def duration_seconds(self) -> float:
        return getattr(self, "_dur_sec", 0.0)


@dataclass
class AbcScore:
    """解析结果。"""

    header: dict[str, Any] = field(default_factory=dict)
    notes: list[AbcNote] = field(default_factory=list)
    voices: list[str] = field(default_factory=list)
    measure_count: int = 0
    warnings: list[str] = field(default_factory=list)
    #: 歌词是否为**逐音**自带（卷帘路径）。
    #: 为真时导出直接用 ``AbcNote.lyric``，**不再**去跑 LRC/占位词分配 ——
    #: 那些分配按时间窗对齐，会把用户逐音填好的词冲掉。
    lyrics_per_note: bool = False

    @property
    def bpm(self) -> float | None:
        return self.header.get("q")

    @property
    def meter(self) -> tuple[int, int]:
        return self.header.get("meter_tuple", (4, 4))

    def voice_notes(self, voice: str) -> list[AbcNote]:
        key = voice.strip().lower()
        return [n for n in self.notes if n.voice.strip().lower() == key]


# --------------------------------------------------------------------------
# 时值解析
# --------------------------------------------------------------------------
def _parse_duration(text: str | None, default: float) -> float:
    """把 ABC 时值记号解析成「多少个 L」。"""
    if not text:
        return default
    if "/" not in text:
        try:
            return float(text)
        except ValueError:
            return default
    num, _, den = text.partition("/")
    # `e/` = 1/2；`e//` = 1/4；`e/2` = 1/2；`e3/2` = 3/2
    n = float(num) if num else 1.0
    if den == "":
        # 形如 `//` 时 partition 会留下一个空 den，实际是 1/4
        return n / 2.0
    try:
        d = float(den)
    except ValueError:
        d = 2.0
    return n / d


def _parse_fraction(text: str, default: tuple[float, float] = (0.0, 1.0)) -> tuple[float, float]:
    if not text:
        return default
    num, _, den = text.partition("/")
    try:
        return (float(num) if num else default[0], float(den) if den else default[1])
    except ValueError:
        return default


#: 调式后缀（可带冒号也可不带）。模型产出的是 **ABC 简写**：`K:Cm`、`K:F#m`、`K:Em`，
#: 都没有冒号 —— 早期只按 `root:mode` 切分，于是把 `Cm` 当成「C 大调」，
#: 该降的 B/E/A 全变还原音，整条旋律听起来跑调（且配着 C 小调的和弦更明显）。
_KEY_MODE_RE = re.compile(
    r"(?i)(major|minor|maj|min|ionian|dorian|phrygian|lydian|mixolydian|aeolian|locrian|m)$"
)
#: 这些小调式按小调处理，其余（ionian/mixolydian/lydian 等）按大调
_MINOR_MODES = {"m", "min", "minor", "aeo", "aeolian"}


def split_key(key: str) -> tuple[str, str]:
    """把调号拆成 ``(根音, 调式)``，同时兼容 ``C:min`` 与 ABC 简写 ``Cm``。"""
    s = (key or "").strip()
    root, sep, mode = s.partition(":")
    if sep:
        return root.strip(), (mode.strip().lower() or "major")
    m = _KEY_MODE_RE.search(s)
    if m:
        return s[: m.start()].strip(), (m.group(0).lower() or "major")
    return s, "major"


def _key_accidentals(key: str) -> dict[str, int]:
    """根据调号得到每个音名的默认升降（-1 降 / 0 还原 / +1 升）。"""
    acc = {letter: 0 for letter in _LETTER_SEMITONE}
    if not key:
        return acc
    root, mode = split_key(key)
    mode = mode.strip().lower()
    table = _MINOR_KEYS if mode in _MINOR_MODES else _MAJOR_KEYS
    # ABC 里降号写作 b（如 Eb、Bb），统一成大写 B 形式查表
    root_clean = re.sub(r"b", "B", root, flags=re.IGNORECASE).upper()
    count = table.get(root_clean)
    if count is None:
        # 试着手动拆根音 + 升降号
        m = re.match(r"^([A-Ga-g])([#b]?)$", root)
        if not m:
            return acc
        letter = m.group(1).upper()
        mark = m.group(2)
        base = _LETTER_SEMITONE[letter]
        if mark == "#":
            base = (base + 1) % 12
        elif mark == "b":
            base = (base - 1) % 12
        name = {v: k for k, v in _LETTER_SEMITONE.items()}[base]
        count = table.get(name, 0)
    if count > 0:
        for letter in _SHARP_ORDER[:count]:
            acc[letter] = 1
    elif count < 0:
        for letter in _FLAT_ORDER[:-count]:
            acc[letter] = -1
    return acc


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def parse_abc(abc: str, *, merge_ties: bool = True) -> AbcScore:
    """解析 ABC 文本，返回按声部独立累计的记谱音符序列。

    Args:
        merge_ties: 是否把连音线（``-``）连接的同音高音符合并成一个长音符。
            默认 ``True``（音乐上正确，SVP/MIDI 需要）。
            ``False`` 时保留每个**写出来的**音符——abcjs 渲染谱面时一个连音线
            会画出两个音符头，页面上做「播放高亮」需要按这个粒度对应。
    """
    score = AbcScore()
    if not abc:
        score.warnings.append("ABC 文本为空")
        return score

    default_len = (1.0, 16.0)     # L: 默认时值，单位=全音符
    meter = (4, 4)
    key = ""
    tempo: float | None = None
    pending_chord: str | None = None

    positions: dict[str, float] = {}     # 每个声部的当前位置（全音符）
    measures: dict[str, int] = {}        # 每个声部的小节计数
    current_voice = "Vocal"
    last_note: dict[str, AbcNote] = {}
    seen_voice_decls: list[str] = []

    def pos(voice: str) -> float:
        return positions.get(voice, 0.0)

    def advance(voice: str, amount: float) -> None:
        positions[voice] = pos(voice) + amount

    _tie_pending: dict[str, bool] = {}
    _skip_bar_count: set[str] = set()

    def add_note(
        voice: str,
        dur_units: float,
        pitch: int | None,
        *,
        tie: bool = False,
        char_index: int = -1,
    ) -> None:
        nonlocal pending_chord
        dur = dur_units * (default_len[0] / default_len[1])
        note = AbcNote(
            voice=voice,
            onset=pos(voice),
            duration=dur,
            pitch=pitch,
            chord=pending_chord,
            measure=measures.get(voice, 0),
            char_index=char_index,
        )
        pending_chord = None
        # 连音线：`tie=True` 表示本音符是上一个音符的延续。要求同音高且首尾相接，
        # 满足则合并时值而不是新增音符。实测漏掉这一步会让 471 个音符错误地变成 531 个。
        prev = last_note.get(voice)
        if (
            merge_ties
            and tie
            and pitch is not None
            and prev is not None
            and prev.pitch == pitch
            and abs(prev.onset + prev.duration - note.onset) < 1e-9
        ):
            prev.duration += note.duration
            advance(voice, dur)
            return
        score.notes.append(note)
        last_note[voice] = note
        advance(voice, dur)
    acc_state: dict[str, dict[int, int]] = {}   # 小节内临时记号（简化：全曲生效到小节末）

    # 逐行解析，并记录每行的绝对字符偏移（供 char_index 使用）。
    # 用 split("\n") 而不是 splitlines()，这样能精确累加偏移（splitlines 会吞掉
    # \r\n 的长度信息，导致每行漂移 1 个字符）。
    line_offset = 0
    for raw_line in abc.split("\n"):
        line = raw_line.rstrip("\r")
        this_offset = line_offset
        line_offset += len(raw_line) + 1
        if not line.strip():
            continue

        # 注释行（含 % intro / % verse 等段落标记）
        if line.lstrip().startswith("%"):
            continue

        # 信息字段
        m = _FIELD_RE.match(line)
        if m:
            fld, val = m.group(1), m.group(2)
            if fld == "L":
                default_len = _parse_fraction(val, (1.0, 8.0))
                continue
            if fld == "M":
                mm = re.match(r"(\d+)\s*/\s*(\d+)", val)
                if mm:
                    meter = (int(mm.group(1)), int(mm.group(2)))
                continue
            if fld == "Q":
                qm = re.search(r"=\s*(\d+(?:\.\d+)?)", val)
                if qm:
                    tempo = float(qm.group(1))
                continue
            if fld == "K":
                key = val.split()[0] if val.split() else ""
                continue
            if fld == "V":
                # `V: Vocal` 切换声部；`V: Vocal clef=treble name="..."` 是声明
                name = val.split()[0] if val.split() else "Vocal"
                current_voice = name
                positions.setdefault(name, 0.0)
                measures.setdefault(name, 0)
                _tie_pending.setdefault(name, False)
                if "=" in val and name not in seen_voice_decls:
                    seen_voice_decls.append(name)
                continue
            if fld in ("X", "T", "R", "C", "O", "N", "S", "G", "H", "I", "W", "w", "U", "P", "Z", "B", "D", "F"):
                continue

        # 音乐行
        voice = current_voice
        positions.setdefault(voice, 0.0)
        measures.setdefault(voice, 0)
        _tie_pending.setdefault(voice, False)

        # 先吃掉和弦标记与括号
        body = line
        pending_for_line: str | None = None

        i = 0
        n = len(body)
        while i < n:
            ch = body[i]

            if ch.isspace():
                i += 1
                continue

            # 和弦标记 "Cm" / "A#/C##"
            if ch == '"':
                end = body.find('"', i + 1)
                if end == -1:
                    break
                pending_chord = body[i + 1:end] or None
                i = end + 1
                continue

            # 注释
            if ch == "%":
                break

            # 行内字段 [K:Em] / 和弦 [CEG]
            # 必须在下面 "|:]" 那条前面处理：早期把 '[' 也当成小节线跳过，
            # 扫描于是继续走进括号内部，把 [K:Em] 里的 E 当成音符（幻影音符），
            # 真遇到 [CEG] 也会把三个音全塞进主旋律。
            if ch == "[":
                end = body.find("]", i + 1)
                if end == -1:
                    i += 1
                    continue
                inner = body[i + 1:end]
                fm = _FIELD_RE.match(inner)
                if fm:
                    # 行内转调对**其后**的音符生效，必须更新 key，否则临时记号算错
                    if fm.group(1).upper() == "K":
                        key = fm.group(2).strip() or key
                    i = end + 1
                    continue
                # 和弦：只记录第一个音（按既有设计），其余音不放进主旋律
                first = _NOTE_RE.match(inner)
                if first and first.group("letter"):
                    pitch = _pitch_of(first, key)
                    if pitch is not None:
                        add_note(voice, _parse_duration(first.group("dur"), 1.0), pitch)
                i = end + 1
                continue

            # 小节线
            if ch in "|:]":
                if ch == "|":
                    # `Z4|` 这类多小节休止已经计过数，避免紧随的 `|` 重复计
                    if voice in _skip_bar_count:
                        _skip_bar_count.discard(voice)
                    else:
                        measures[voice] = measures.get(voice, 0) + 1
                i += 1
                continue

            # 装饰音 {...}
            if ch == "{":
                end = body.find("}", i + 1)
                i = (end + 1) if end != -1 else (i + 1)
                continue

            # 多小节休止 Z / Z4
            if ch == "Z":
                zr = _MULTIREST_RE.match(body, i)
                cnt = int(zr.group("count") or 1) if zr else 1
                whole_per_measure = meter[0] / meter[1] if meter[1] else 1.0
                total = cnt * whole_per_measure
                dur_units = total / (default_len[0] / default_len[1])
                add_note(voice, dur_units, None)
                measures[voice] = measures.get(voice, 0) + cnt
                _skip_bar_count.add(voice)
                i = zr.end() if zr else i + 1
                continue

            # 普通休止 z / x
            if ch in "zx":
                rr = _REST_RE.match(body, i)
                add_note(voice, _parse_duration(rr.group("dur") if rr else None, 1.0), None)
                i = rr.end() if rr else i + 1
                continue

            # 连音线
            if ch == "-":
                _tie_pending[voice] = True
                i += 1
                continue

            # 破碎节奏 > <
            if ch in "><":
                i += 1
                continue

            # 音符
            nm = _NOTE_RE.match(body, i)
            if nm and nm.group("letter"):
                tie = _tie_pending.get(voice, False)
                _tie_pending[voice] = False
                pitch = _pitch_of(nm, key)
                dur = _parse_duration(nm.group("dur"), 1.0)
                add_note(voice, dur, pitch, tie=tie, char_index=this_offset + i)
                i = nm.end()
                # 紧跟 `-` 表示本音符与下一个同音高音符相连
                if i < n and body[i] == "-":
                    _tie_pending[voice] = True
                    i += 1
                continue

            i += 1

    score.header = {
        "key": key,
        "q": tempo,
        "meter": f"{meter[0]}/{meter[1]}",
        "meter_tuple": meter,
        "unit": f"{default_len[0]:g}/{default_len[1]:g}",
        "unit_whole": default_len[0] / default_len[1],
    }
    score.voices = seen_voice_decls or sorted({n.voice for n in score.notes})
    score.measure_count = max(measures.values(), default=0)
    return score


def _pitch_of(match: re.Match, key: str) -> int | None:
    """把 ABC 音符记号转成 MIDI 音高。"""
    letter = match.group("letter")
    if not letter:
        return None
    upper = letter.upper()
    base = _LETTER_SEMITONE[upper]
    acc_map = _key_accidentals(key)
    base += acc_map.get(upper, 0)

    acc = match.group("acc")
    if acc:
        if acc.startswith("^"):
            base += len(acc)
        elif acc.startswith("_"):
            base -= len(acc)
        # `=` 为还原，已在调号基础上归零
        elif acc == "=":
            base -= acc_map.get(upper, 0)

    octave = match.group("octave") or ""
    # 大写 = 中央 C 所在八度（MIDI 60 起），小写高一个八度
    midi = base + (60 if letter.isupper() else 72)
    midi += 12 * octave.count("'")
    midi -= 12 * octave.count(",")
    return midi


# --------------------------------------------------------------------------
# 记谱时间 → 秒
# --------------------------------------------------------------------------
def notes_to_seconds(
    notes: Sequence[AbcNote],
    *,
    bpm: float,
    measures: Sequence[dict[str, Any]] | None = None,
) -> list[str]:
    """把记谱位置换算成秒，就地写入 ``note.start``（``duration`` 同步换算）。

    Args:
        notes: 已解析的音符。
        bpm: 速度，用于没有小节表时的等速回退。
        measures: ``playback.json`` 的 ``measures`` 列表；提供时按小节线性插值，
            可与原曲严格对齐。

    Returns:
        诊断信息列表（换算降级等），供「诊断信息」展示。
    """
    warnings: list[str] = []
    table = _normalize_measures(measures) if measures else []

    if not table:
        # 等速回退
        whole_seconds = 240.0 / bpm if bpm else 2.0
        for note in notes:
            note.start = note.onset * whole_seconds
            note._dur_sec = note.duration * whole_seconds
        warnings.append("缺少小节表，已按等速换算（可能与原曲有偏移）")
        return warnings

    table_end = table[-1]["score_end"]
    tail_rate = (table[-1]["end"] - table[-1]["start"]) / max(1e-9, table[-1]["score_end"] - table[-1]["score_start"])
    if any(n.onset > table_end + 1e-6 for n in notes):
        warnings.append("记谱长度超出原小节表，超出部分按末小节速度外推（与原曲可能有偏移）")

    for note in notes:
        note.start = _score_to_seconds(note.onset, table, tail_rate)
        end_pos = note.onset + note.duration
        note._dur_sec = _score_to_seconds(end_pos, table, tail_rate) - note.start
    return warnings


def _normalize_measures(measures: Sequence[dict[str, Any]]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for m in measures:
        try:
            out.append(
                {
                    "start": float(m["start"]),
                    "end": float(m["end"]),
                    "score_start": float(m["score_start"]),
                    "score_end": float(m["score_end"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["score_start"])
    return out


def _score_to_seconds(position: float, table: Sequence[dict[str, float]], tail_rate: float) -> float:
    """记谱位置（全音符）→ 秒。"""
    if position <= 0:
        return table[0]["start"]
    for m in table:
        ss, se = m["score_start"], m["score_end"]
        if se - ss <= 0:
            continue
        if ss <= position <= se:
            t = (position - ss) / (se - ss)
            return m["start"] + t * (m["end"] - m["start"])
    last = table[-1]
    return last["end"] + (position - last["score_end"]) * tail_rate


def to_simple_note_objs(
    notes: Iterable[AbcNote],
    *,
    voice: str | None = None,
    min_duration: float = 0.0,
) -> list[AbcNote]:
    """筛出有声音符，返回**音符对象**（按 ``(起点, 音高)`` 排序）。

    为什么需要对象版：卷帘路径下歌词是**逐音**的，必须按同一个顺序把每个音符的歌词
    带出去；只返回 ``(start, end, pitch)`` 元组就丢掉了这个对应关系。

    :func:`to_simple_notes` 就是本函数的结果去取元组，两者顺序**必然一致**。
    """
    out: list[AbcNote] = []
    want = voice.strip().lower() if voice else None
    for note in notes:
        if note.pitch is None or note.start is None:
            continue
        if want and note.voice.strip().lower() != want:
            continue
        if note.duration_seconds < min_duration:
            continue
        out.append(note)
    out.sort(key=lambda n: (n.start, n.pitch))
    return out


def to_simple_notes(
    notes: Iterable[AbcNote],
    *,
    voice: str | None = None,
    min_duration: float = 0.0,
) -> list[tuple[float, float, int]]:
    """筛出有声音符，返回 ``[(start秒, end秒, midi), ...]``。"""
    return [
        (n.start, n.start + n.duration_seconds, n.pitch)
        for n in to_simple_note_objs(notes, voice=voice, min_duration=min_duration)
    ]
