"""把音符序列序列化回 ABC 记谱文本（钢琴卷帘编辑后的持久化方向）。

为什么需要它
------------
项目正在把「乐谱编辑」里的 ABC 文本框换成**图形化钢琴卷帘**：用户在卷帘上改音符，
但这些改动必须仍然以 **ABC** 落盘 —— 导出管线（:mod:`app.rebuild`）吃的就是 ABC
文本，保持「卷帘 → ABC → 导出」这条链不断，导出侧一行都不用改。

于是本模块是 :mod:`app.abcp` 的**逆运算**：输入「声部名 + (起点, 时值, 音高)」，
输出能被 ``parse_abc`` **无损读回**的 ABC 文本。

四条硬约束（都是为了无损往返）
------------------------------
1. **休止符补空**：ABC 是顺序流，要把音符放在某个起点上，只能先把前面的静默写成
   ``z``。这是正确性的一部分，不是排版。
2. **每个音都带显式临时记号**：``^`` / ``_`` / ``=`` 必须写在每个音符上。理由有二：

   * ``K:`` 的调号会参与 ``_pitch_of`` 的换算，不写记号就会被调号改音高；
   * 更隐蔽的是 ``_pitch_of`` 里 ``^``/``_`` 是**按调号叠加**的
     （先 ``base += acc_map[letter]``，再 ``base += len(acc)``），所以「记号 + 调号」
     才等于最终音高。本模块按调号反算出需要的记号，保证结果与 ``K:`` 无关。

3. **时值一律显式写出**：每个音符/休止都带时值数字，绝不依赖 ``L:`` 的默认值 ——
   虽然本模块自己会写 ``L:``，但显式写出来以后即使有人改了 ``L:`` 也不会错位。
4. **``L:`` 取「能整除所有起点/时值/空隙的 1/2^k」**（见 :func:`base_unit_of`），于是
   每个长度都是整数倍的基本单位，可以写成单个数字（如 ``C7``、``z3``）。

关于连音线（为什么完全不用它）
------------------------------
``parse_abc`` 默认 ``merge_ties=True`` 会把连音线**合并成一个音符**。调用方拿到的
音符就是这么合并过的；一次「解析 → 序列化 → 再解析」如果中途引入连音线，再解析时
音符个数就对不上了。所以本模块**一个连音线都不写**：``L:`` 取得足够细，所有长度都能
写成单个数字；万一某段长度不是基本单位的整数倍（内部不变量被破坏），直接 ``ValueError``
炸出来，绝不静默近似。

同样地，**小节线只是插在事件之间的排版**，绝不切开音符 —— ABC 允许一个记号跨越小节线，
而切开一个音会让它被读回成两个音（实测过：714 个音变 749 个）。

自校验
------
:func:`score_to_abc` 在返回前会把自己写出的文本**重新解析一遍**并逐音比对，不一致就
``ValueError``。多花这一次解析很值：本模块的核心是在猜（``L:`` 选多细、临时记号写哪个），
猜错的症状是导出谱悄悄错音而不是崩溃；宁可在这里炸。

约定
----
* ``onset`` / ``duration`` 的单位是**全音符**，与 :attr:`app.abcp.AbcNote.onset` 同一族。
* 每个声部**必须单音**（同声部内不允许时间重叠）。检测到重叠直接 ``ValueError``，
  交给调用方去拆轨（例如 :func:`app.rebuild.monophonic_groups`），本模块不截短、不丢弃。
* **空声部（没有任何音符）不输出** ``V:`` 行：它一个音都没有，往返本来也无从校验，
  留着只会让 ABC 变长；声部内全是休止的整段也一样不输出。
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Any, Mapping, Sequence

from app.abcp import _LETTER_SEMITONE, _key_accidentals

__all__ = [
    "DEFAULT_BASE_DENOM",
    "MAX_BASE_DENOM",
    "score_to_abc",
    "header_from_abc",
    "base_unit_of",
    "voice_has_overlap",
]

#: 输入里一个正长度都没有（空声部列表、或全零长度）时用的基本单位分母。
#: 与 abcp 的默认 ``L:1/16`` 保持一致，纯粹是取个像样的默认值。
DEFAULT_BASE_DENOM = 16
#: ``L:`` 分母上限。超过它说明输入时值细得不像乐谱，宁可报错也不写出一堆 1/32768。
MAX_BASE_DENOM = 4096
#: 单个时值数字的上限（挡住病态输入造出天文数字）
_MAX_MULTIPLIER = 1_000_000

#: 头部字段的书写顺序；``K`` 放最后是 ABC 惯例（它之后才是音乐）
_HEADER_ORDER = ("X", "T", "M", "L", "Q", "K")
#: 已由前面统一处理的 header 键，避免在「其它字段」里重复写一遍。
#: ``v`` 也在内：见 :func:`_write_header` —— 从解析结果抄 ``V:`` 会造出幻影音符。
_HEADER_ALIASES = {"meter", "meter_tuple", "unit", "unit_whole", "q", "key", "v"}

#: ``M:`` 取值：``4/4`` / ``3+2/8``（additive 只取前半段，够用）
_METER_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)")
#: 单字母信息字段（``X:`` / ``K:`` / ``V:`` ...）
_FIELD_RE = re.compile(r"^([A-Za-z]):\s*(.*)$")


# --------------------------------------------------------------------------
# 基本单位
# --------------------------------------------------------------------------
def base_unit_of(values: Sequence[Fraction]) -> Fraction:
    """求一个能**整除**所有给定长度的基本单位，形式固定为 ``1/2^k``。

    只取「分母的 2 的幂 + 分子固定为 1」，**不**取最大公约数的分子。两个原因：

    1. ``L:`` 的惯例就是 ``1/2^k``。早期版本取了 GCD（含分子），于是「一个 3 个全音符
       的长音」会把基本单位算成 ``3``，写出 ``L:3/1`` —— 音乐上毫无意义，还会把
       别的长度（休止、小节补白）逼成非整数倍而直接报错。
    2. 这样得到的单位**天然整除**所有输入：任何以 2 的幂为分母的有理数都能整除
       ``1/2^k``（k 取够大即可）。调用方把所有「会被写出来」的长度都传进来
       （起点、时值、空隙），就能保证后面永远不需要近似。

    Raises:
        ValueError: 需要的精度超过 :data:`MAX_BASE_DENOM`。
    """
    den = 1
    for value in values:
        f = abs(Fraction(value))
        if f == 0:
            continue
        # 先约分再取分母：``0.25`` 可能是 1/4，也可能是 2/8。
        # 上限判断必须在循环**里面**：分母含 2 以外的质因子时（例如 3），``den % 3``
        # 永远不会变成 0，没有这个判断就会一路翻倍到内存耗尽 —— 实测卡死过。
        while den % f.denominator:
            den *= 2
            if den > MAX_BASE_DENOM:
                raise ValueError(
                    f"时值精度过高：基本单位需要 1/{den}，超过上限 1/{MAX_BASE_DENOM}"
                    f"（输入里存在比 1/{MAX_BASE_DENOM} 全音符还细的音符或空隙；"
                    f"分母含 2 以外的质因子时无法用 1/2^k 表示）"
                )
    return Fraction(1, den)


def _format_multiplier(num: int, den: int) -> str:
    """把「多少个 L」写成 ABC 时值记号（``3`` / ``/2`` / ``3/2``）。"""
    if den == 1:
        return str(num)
    if num == 1:
        return f"/{den}"
    return f"{num}/{den}"


def _duration_text(value: Fraction, base: Fraction) -> str:
    """时值 → ABC 时值记号（**只写单个数字**）。

    不变量：``base`` 是按「所有会被写出来的长度」选出来的，所以 ``value / base`` 一定
    是正整数。写不出单个数字说明 ``score_to_abc`` 的长度收集漏了东西（早期就是漏了
    「空隙」和「小节补白」，于是冒出 ``时值 1.0 无法用基本单位 3.0 表示`` 这种崩溃）
    —— 那种情况必须炸得很响，绝不能悄悄退回连音线：连音线会被 ``parse_abc`` 的默认
    ``merge_ties=True`` 合并，音符个数就对不上了。

    Raises:
        ValueError: 长度不是 ``base`` 的整数倍（内部不变量被破坏），或时值大得离谱。
    """
    units = Fraction(value) / base
    if units.denominator != 1:
        raise ValueError(
            f"内部错误：时值 {value} 不是基本单位 {base} 的整数倍"
            f"（应改成基本单位的 {float(units):.6g} 倍）—— 说明有长度没纳入基本单位计算"
        )
    num = int(units)
    if num > _MAX_MULTIPLIER:
        raise ValueError(f"时值 {float(value)} 太大（{num} 个基本单位），拒绝生成")
    return _format_multiplier(num, 1)


# --------------------------------------------------------------------------
# 音高 → ABC 记号
# --------------------------------------------------------------------------
def _key_marks_of(key: str) -> dict[str, int]:
    """调号 → 每个音名的默认升降（``+1`` 升 / ``-1`` 降）。

    直接复用 :func:`app.abcp._key_accidentals`：那是**解析侧真正的调号实现**，
    自己再写一份就可能和 ``_pitch_of`` 走岔 —— 那才是真正致命的隐性不一致。
    """
    return dict(_key_accidentals(key or "C"))


def _pitch_from_token(token: str, key_marks: Mapping[str, int]) -> int:
    """本模块自己拼的记号 → 音高反算（只在 :func:`_note_token` 里做自校验）。

    必须与 :func:`app.abcp._pitch_of` 的语义**逐字一致**，两处细节都踩过坑：

    * ``=`` 是「还原成该音名的**自然音**」（把调号影响整个抹掉），不是「抵消一个」；
    * 调号的那个升降**加在八度基准之前**，所以 ``K:E`` 下 ``^C`` = 62 而不是 61
      （E 大调的 C 本来就被调号升了，再写 ``^`` 才是 C## = 62）。
    """
    m = re.fullmatch(r"(?P<acc>\^{1,2}|_{1,2}|=)?(?P<letter>[A-Ga-g])(?P<octave>[',]*)", token)
    if m is None:                              # pragma: no cover - 记号由本模块自己拼
        return -1
    letter = m.group("letter")
    upper = letter.upper()
    value = _LETTER_SEMITONE[upper] + int(key_marks.get(upper, 0))
    acc = m.group("acc") or ""
    if acc.startswith("^"):
        value += len(acc)
    elif acc.startswith("_"):
        value -= len(acc)
    else:
        # 无记号 / ``=``：都回到该音名的自然音，调号那一份要扣掉
        value -= int(key_marks.get(upper, 0))
    value += 60 if letter.isupper() else 72
    value += 12 * m.group("octave").count("'")
    value -= 12 * m.group("octave").count(",")
    return value


def _pitch_token_attempts(
    target: int, letter: str, semitone: int, mark: int, base_midi: int, mark_char: str
) -> list[tuple[str, int, int]]:
    """枚举一个 ``(音名, 大小写)`` 在某音高上的**所有**可写八度（``-5..5`` 个标点：
    负数用逗号往下走，正数用撇号往上走，两个方向都合法）。

    对每个八度算 ``差值 = 目标 - (该八度自然音 + 调号)``，只留下差值在 ``[-2, 2]``
    内的 —— 显式记号只有 ``= ^ ^^ _ __`` 五种，超出就真写不出来。全部枚举完，
    MIDI 0..127 都能覆盖到。

    ``mark`` 是调号给这个音名的默认升降。写显式记号等于**覆盖**调号，所以 ``=`` 天然
    产出自然音，不需要为它开特例路径。
    """
    out: list[tuple[str, int, int]] = []
    for octaves in range(-5, 6):
        natural = base_midi + semitone + mark + 12 * octaves
        delta = target - natural
        if not -2 <= delta <= 2:
            continue
        acc_text = {2: "^^", 1: "^", 0: "=", -1: "_", -2: "__"}[delta]
        note_letter = letter if mark_char == "," else letter.lower()
        out.append((acc_text + note_letter + mark_char * abs(octaves), abs(delta), abs(octaves)))
    return out


def _note_token(pitch: int, key_marks: Mapping[str, int]) -> str:
    """MIDI 音高 → 带显式临时记号的 ABC 音符记号。

    枚举所有 ``(临时记号, 音名, 大小写)`` 写法，挑**记号最少**的那个；记号数相同时
    偏好 ``=``（还原）> 单升/单降 > 双升/双降，再偏好更少的八度标点。

    Args:
        key_marks: 调号对每个音名的默认升降（:func:`_key_marks_of` 的结果）。它进
            计算只是为了让自校验能覆盖调号叠加的坑，**结果本身与调号无关**。

    大小写两个写法都试：大写以中央 C（60）为基准往低处用逗号，小写以 72 为基准往
    高处用撇号；两者都不写标点同样有效。于是中央 C 就是 ``=C``、一个八度以下
    ``C,``、高音区自然落到 ``c'`` —— 与 ABC 惯例一致。
    """
    target = int(pitch)
    best: tuple[tuple[int, int], str] | None = None
    for letter, semitone in _LETTER_SEMITONE.items():
        mark = int(key_marks.get(letter, 0))
        for base_midi, mark_char in ((60, ","), (72, "'")):
            for token, delta, octaves in _pitch_token_attempts(
                target, letter, semitone, mark, base_midi, mark_char
            ):
                if _pitch_from_token(token, key_marks) != target:
                    continue                   # 交叉校验，挡住取整/取模的边界错误
                costs = (delta, octaves)
                if best is None or costs < best[0]:
                    best = (costs, token)
    if best is None:
        raise ValueError(f"音高 {target} 超出 ABC 可写范围")
    return best[1]


# --------------------------------------------------------------------------
# 头部
# --------------------------------------------------------------------------
def header_from_abc(text: str) -> dict[str, str]:
    """从 ABC 原文抄出 ``X/T/M/L/Q/K`` 等字段，可直接喂给 :func:`score_to_abc`。

    这是「读原文 → 改音符 → 写回原文」的正规入口，比 :func:`app.abcp.parse_abc` 的
    ``score.header`` 更完整（后者只留 key/q/meter/unit，``X:`` 与 ``T:`` 会被丢掉）。

    ``V:`` **不抄**：声部行由 :func:`score_to_abc` 自己写；照抄 ``V: ... clef=...``
    会被解析器当音乐行扫出幻影音符（见 :func:`_write_header`）。

    同一字段出现多次取第一次（与 ABC 惯例一致：``V:`` 之外的字段声明一次）。
    """
    header: dict[str, str] = {}
    for raw in text.split("\n"):
        m = _FIELD_RE.match(raw.strip())
        if m is None or m.group(1).upper() == "V":
            continue
        header.setdefault(m.group(1), m.group(2))
    return header


def _header_value(header: Mapping[str, Any], field: str) -> str | None:
    """从 header 取字段，兼容 ``{"K": "Cm"}`` 与 ``{"key": "Cm"}`` 两种写法。"""
    for key in (field, field.lower()):
        value = header.get(key)
        if value is not None and str(value) != "":
            return str(value)
    return None


def _meter_of(header: Mapping[str, Any]) -> tuple[int, int]:
    """取出拍号 ``(分子, 分母)``，认不出来就 ``(4, 4)``。"""
    raw: Any = header.get("meter_tuple")
    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        try:
            num, den = int(raw[0]), int(raw[1])
            if num > 0 and den > 0:
                return num, den
        except (TypeError, ValueError):
            pass
    m = _METER_RE.match(_header_value(header, "M") or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    return 4, 4


def _field_text(value: Any) -> str:
    """``T:`` 之类的自由文本：``%`` 会被解析器当注释截断，换成全角百分号保义。"""
    return str(value).replace("%", "％")


def _write_header(header: Mapping[str, Any], base: Fraction) -> list[str]:
    """生成头部行：``X/T/M/L/Q/K`` 按惯例顺序 + 原样保留的其它字段。

    ``V:`` **一律丢掉**（也记在 :data:`_HEADER_ALIASES` 之外的单独特判里）：声部行由
    :func:`score_to_abc` 自己按 ``voices`` 写。从解析结果里原样抄一条
    ``V: Vocal clef=treble name="Vocal Melody"`` 出来是**有害**的 —— ``parse_abc``
    会把它当音乐行扫，``clef``/``name`` 里的字母被读成音符（实测凭空多出 35 个音、
    位置还都错）。少了声部声明解析器会用默认声部名，随后我们自己写的 ``V:`` 会接管。
    """
    meter = _meter_of(header)
    lines: list[str] = []
    for field in _HEADER_ORDER:
        if field == "L":
            # L 必须由 base 决定：本模块的正确性依赖「每个时值都是 base 的整数倍」
            lines.append(f"L:{base.numerator}/{base.denominator}")
        elif field == "M":
            lines.append(f"M:{meter[0]}/{meter[1]}")
        elif field == "K":
            lines.append(f"K:{_header_value(header, 'K') or 'C'}")
        elif field == "X":
            lines.append(f"X:{_header_value(header, 'X') or '1'}")
        else:
            value = _header_value(header, field)
            lines.append(f"{field}:{_field_text(value) if value else ''}")

    for key, value in header.items():
        field = str(key)
        if not re.fullmatch(r"[A-Za-z]", field) or field.upper() in _HEADER_ORDER:
            continue
        if field.lower() in _HEADER_ALIASES:
            continue
        lines.append(f"{field}:{_field_text(value)}")
    return lines


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def _as_fraction(value: Any) -> Fraction:
    """用十进制字面量而不是二进制浮点来取有理数，避免 0.1 → 3602879701896397/2^55。"""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    return Fraction(str(value))


#: 判定"重叠"的容差（全音符单位）。
#:
#: **必须有**：`app.abcp.seconds_to_score()` 把秒换回记谱位置时，时值会带上浮点噪声
#: （实测 ``0.125`` 变成 ``0.12499999999999911``），于是"首尾相接"的相邻音算出来的
#: 结束点会比下一个音的起点大 ~1e-16。用精确有理数比较就会把它判成重叠 ——
#: 实测 **22 个真实样本里 21 个**因此被本模块拒绝，整条卷帘保存链路直接不可用。
#:
#: 1e-9 个全音符在 120 BPM 下约 2 微秒：比浮点噪声高 7 个数量级，又远低于任何
#: 音乐意义。**这个值必须与 `app.rebuild.monophonic_groups()` 的容差一致** ——
#: 两处若不一致，就会出现"这里说有重叠要拆轨、那里说没重叠不用拆"的自相矛盾。
OVERLAP_TOLERANCE = Fraction(1, 10**9)


def voice_has_overlap(
    notes: Sequence[tuple[float, float, int]],
    *,
    tolerance: Fraction | float = OVERLAP_TOLERANCE,
) -> bool:
    """同一时刻是否有多于一个音（**首尾相接不算重叠**）。

    小于 ``tolerance`` 的"重叠"按首尾相接处理（浮点噪声），不算重叠。
    """
    tol = _as_fraction(tolerance)
    spans = sorted(
        (_as_fraction(start), _as_fraction(start) + _as_fraction(dur)) for start, dur, _ in notes
    )
    return any(next_start < end - tol for (_, end), (next_start, _) in zip(spans, spans[1:]))


def _coerce_notes(voice: str, notes: Sequence[Sequence[Any]]) -> list[tuple[Fraction, Fraction, int]]:
    """校验并归一化一个声部的音符；不合法直接 ``ValueError``。"""
    out: list[tuple[Fraction, Fraction, int]] = []
    for item in notes:
        if len(item) != 3:
            raise ValueError(f"声部 {voice!r} 的音符必须是 (onset, duration, pitch)，收到 {item!r}")
        raw_onset, raw_duration, raw_pitch = item
        start = _as_fraction(raw_onset)
        length = _as_fraction(raw_duration)
        if length <= 0:
            raise ValueError(f"声部 {voice!r} 在 {float(start)} 处时值为 {float(length)}，必须为正")
        if start < 0:
            raise ValueError(f"声部 {voice!r} 的起点 {float(start)} 为负，ABC 无法表示")
        pitch = int(raw_pitch)
        if float(raw_pitch) != pitch:
            raise ValueError(f"声部 {voice!r} 的音高 {raw_pitch!r} 不是整数 MIDI 音高")
        if not 0 <= pitch <= 127:
            raise ValueError(f"声部 {voice!r} 的音高 {pitch} 超出 MIDI 范围 0..127")
        out.append((start, length, pitch))
    out.sort(key=lambda n: (n[0], n[2]))
    if voice_has_overlap([(float(s), float(d), p) for s, d, p in out]):
        raise ValueError(
            f"声部 {voice!r} 内存在时间重叠的音符：本模块只接受单音声部，"
            f"请先用 app.rebuild.monophonic_groups() 把它拆成互不重叠的若干条"
        )
    return out


def _voice_token(name: str) -> str:
    """声部名 → 可安全写进 ``V:`` 的 token。

    ``parse_abc`` 的 ``V:`` 只取行内**第一个空白分隔的词**，而写音乐的声部名又只能
    来自 ``V:``，所以名字里带空格就永远往返不回去 —— 这里换成下划线。仍然非 ASCII 则
    直接报错，绝不生成一个读不回来的声部名（调用方应改用 ``Vocal`` / ``Ins`` 这类名字）。
    """
    token = re.sub(r"\s+", "_", str(name).strip())
    if not token:
        raise ValueError("声部名不能为空")
    if not token.isascii():
        raise ValueError(f"声部名 {name!r} 含非 ASCII 字符，写进 V: 后 parse_abc 读不回来")
    if token.startswith("[") or token.startswith('"') or "|" in token:
        raise ValueError(f"声部名 {name!r} 含 ABC 保留字符（[ / \" / |），无法写进 V:")
    return token


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def score_to_abc(
    header: Mapping[str, Any],
    voices: Sequence[tuple[str, Sequence[tuple[float, float, int]]]],
) -> str:
    """把音符序列写成能被 :func:`app.abcp.parse_abc` 无损读回的 ABC 文本。

    Args:
        header: ABC 头部字段。既接受「真正的 ABC 字段」（``{"X": "1", "M": "4/4",
            "Q": "1/4=120", "K": "Cm"}``），也接受 :func:`app.abcp.parse_abc` 解析出来的
            ``{"meter": "4/4", "meter_tuple": (4, 4), "q": 120, "key": "Cm"}``。
            缺字段用默认值（``M:4/4`` / ``L:1/8`` / ``K:C``）。**``L:`` 一律由本模块
            按最大公约数重新决定**，传进来的会被覆盖 —— 正确性优先于照抄。
        voices: ``[(声部名, [(onset, duration, pitch), ...]), ...]``；onset/duration
            单位是全音符，pitch 是 MIDI 音高。声部内必须单音（重叠即 ``ValueError``）。

    Returns:
        ABC 文本（UTF-8 可直接落盘）。**没有音符的声部不输出**，见模块文档。

    Raises:
        ValueError: 声部内重叠、时值非正、音高越界、时值精度超过 ``MAX_BASE_DENOM``，
            或声部名无法在 ABC 里表达。
    """
    prepared: list[tuple[str, list[tuple[Fraction, Fraction, int]]]] = []
    for name, notes in voices:
        # 先校验再算基本单位：非法输入不该先污染出一个诡异的 L:
        prepared.append((_voice_token(name), _coerce_notes(str(name), notes)))

    # 基本单位 = 所有起点、时值、以及它们之间空隙的最大公约数。
    # 算上「空隙」是让休止符也是整数倍；算上「起点」是让第一个音之前的空档同样对齐。
    values: list[Fraction] = []
    for _, notes in prepared:
        cursor = Fraction(0)
        for start, length, _ in notes:
            if start > cursor:
                values.append(start - cursor)      # 空隙（要写成休止）
            values.append(start)                   # 起点本身（第一个音之前也要对齐）
            values.append(length)
            cursor = start + length
    base = base_unit_of(values)
    if base.numerator != 1:
        # 只会出现在 base_unit_of 被改坏的时候：``L:3/1`` 不是合法惯例的 ABC 基本时值
        raise ValueError(f"内部错误：基本单位必须是 1/2^k，实得 {base}")

    meter = _meter_of(header)
    measure_len = Fraction(meter[0], meter[1])
    key_marks = _key_marks_of(_header_value(header, "K") or "C")

    lines = _write_header(header, base)
    for name, notes in prepared:
        if not notes:
            continue                                # 空声部不输出（模块文档里写明）
        lines.append(f"V:{name}")
        lines.extend(_voice_lines(name, notes, base, measure_len, key_marks))
    text = "\n".join(lines) + "\n"

    # 自校验：重新解析一遍，确认写出去的东西真能读回**一模一样的**音符序列。
    # 为什么值得多花这一次解析：本模块存在的唯一理由是「无损往返回去」，而它的核心
    # 恰恰是在猜 — L: 选多细、临时记号写哪个。一旦猜错（历史上错过两次：``=`` 的语义、
    # 跨小节音符被切开），症状是导出结果悄悄错音，不是崩溃。宁可在这里炸，也不要让
    # 用户拿到一份听着不对的谱子。
    _verify_roundtrip(text, prepared, str(base))
    return text


def _verify_roundtrip(
    text: str,
    prepared: Sequence[tuple[str, Sequence[tuple[Fraction, Fraction, int]]]],
    base_text: str,
) -> None:
    """把刚生成的 ABC 重新解析，逐音比对；不一致就 ``ValueError``。

    Raises:
        ValueError: 生成的文本读回来与原音符不一致（本模块的内部 bug）。
    """
    from app.abcp import parse_abc            # 局部导入：abcp 不依赖本模块，放顶部也行，
                                              # 但这样能让「解析」这件事在文件里一眼可见
    got: dict[str, list[tuple[float, float, int]]] = {}
    for note in parse_abc(text, merge_ties=False).notes:
        if note.pitch is None:
            continue
        got.setdefault(note.voice, []).append((note.onset, note.duration, note.pitch))

    problems: list[str] = []
    for name, notes in prepared:
        if not notes:
            continue
        mine = sorted(got.get(name, []))
        want = sorted((float(o), float(d), p) for o, d, p in notes)
        if len(mine) != len(want):
            problems.append(f"{name}: 音符数 {len(want)} → {len(mine)}")
            continue
        for index, (a, b) in enumerate(zip(want, mine)):
            if a[2] != b[2]:
                problems.append(f"{name}[{index}]: 音高 {a[2]} → {b[2]}")
            elif abs(a[0] - b[0]) > 1e-6 or abs(a[1] - b[1]) > 1e-6:
                problems.append(f"{name}[{index}]: onset/dur {a[:2]} → {b[:2]}")
            if len(problems) >= 3:
                break
    expected = {name for name, notes in prepared if notes}
    for voice in got:
        if voice not in expected:
            problems.append(f"多出声部 {voice!r}")
    if problems:
        raise ValueError(
            f"内部错误：生成的 ABC 读回来与原音符不一致（基本单位 {base_text}）："
            + "；".join(problems[:3])
        )


def _voice_lines(
    name: str,
    notes: Sequence[tuple[Fraction, Fraction, int]],
    base: Fraction,
    measure_len: Fraction,
    key_marks: Mapping[str, int],
) -> list[str]:
    """一个声部的音乐行：每小节一行、行内以 ``|`` 收尾（末小节残句不补小节线）。

    做法：先把空隙铺成休止，得到一个从 0 开始、逐步前进且互不重叠的事件流；
    然后**逐个事件整体写出**，沿途在跨过小节线的地方插 ``|`` 并换行。

    关键：**一个小节线永远不切开一个音符**。早期版本为了「小节内音符对齐」把跨小节的
    音符切成两段分别写时值（还不加连音线，因为连音线会被 ``parse_abc`` 合并、破坏
    ``merge_ties=False`` 下的音符个数），结果一个音被读回成两个（实测 224343 那个样本
    714 → 749 个音）。ABC 本身允许一个记号跨越小节线，所以正确做法是让音符整段写完，
    小节线只是插在事件之间、纯做排版。
    """
    offset = min(Fraction(0), notes[0][0])              # 负起点整体右移到 0
    events: list[tuple[Fraction, Fraction, int | None]] = []
    if offset < 0:
        events.append((Fraction(0), -offset, None))
    filled_to = offset                                  # 已经铺到的时间点（前一个事件的右端）
    for start, length, pitch in notes:
        if start > filled_to:
            events.append((filled_to, start - filled_to, None))   # 空隙 → 休止
        events.append((start, length, pitch))
        filled_to = start + length

    out: list[str] = []
    tokens: list[str] = []
    closed: set[int] = set()       # 已经画过 ``|`` 的小节，避免重复落线
    previous: int | None = None    # 上一个事件所在的小节

    def flush() -> None:
        if tokens:
            out.append(" ".join(tokens))
            tokens.clear()

    for start, length, pitch in events:
        # 时间轴从 0 起，第 n 小节 = [n*小节长, (n+1)*小节长)
        start_measure = int(start / measure_len)
        if previous is not None and start_measure != previous and start_measure not in closed:
            tokens.append("|")
            closed.add(start_measure)
            flush()
        previous = start_measure
        token = "z" if pitch is None else _note_token(pitch, key_marks)
        tokens.append(token + _duration_text(length, base))

    flush()
    return out or ["|"]
