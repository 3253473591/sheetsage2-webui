"""平行和声：把一条旋律按**调性**平移成和声轨。

为什么必须按调性（而不是固定半音）
--------------------------------
固定半音平移（每个音都 +N 个半音）听着不像和声：

* C 大调里 ``C + 3 半音 = Eb`` —— **直接出调**，跟伴奏打架；
* 更致命的是**音程会随音级变化**：旋律从 C 走到 D，固定 +3 得到 Eb → F，
  一个是小三度一个是小二度（F 到 Eb 是减四度）；而人的耳朵要的是
  "每个音都往上一个三度"——C→E、D→F，**绝对半音数本来就不一样**。

所以"三度"这种说法**只有在调性里才有意义**，这也正是 :mod:`app.abcp`
的 :func:`~app.abcp.scale_pitch_classes` 存在的理由。

度数记法
--------
``degree=1`` 是同音（一度），``degree=3`` 是往上走一个三度，以此类推。
**调内平移会自动跨八度**（C 大调里 B 往上三度要升八度到 D），
这是固定半音做不到的。

调外音怎么办
------------
旋律里难免有调外音（临时记号）。做法是把它**归到最近的一个音级**，
并把"相对那一级的半音偏移"原样带过去 —— 所以调外音不会被抹平成调内音，
只是跟着它依附的那一级一起平移。

认不出调号时（``scale`` 为空）调用方应回落到固定半音，见
:func:`chromatic_semitones`。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "DEGREE_CN",
    "MAX_DEGREE",
    "chromatic_semitones",
    "degree_steps",
    "diatonic_shift",
    "nearest_degree",
    "build_harmony_notes",
]

#: 音级中文名（一度 = 同音）
DEGREE_CN = {1: "一度", 2: "二度", 3: "三度", 4: "四度", 5: "五度", 6: "六度", 7: "七度"}

#: 允许的音级（一度~七度）
MAX_DEGREE = 7

#: 固定半音回退时，各音级按**大调模板**折算成半音数。
#: 只在「认不出调号」或调用方明确要求固定半音时使用。
_MAJOR_TEMPLATE = {1: 0, 2: 2, 3: 4, 4: 5, 5: 7, 6: 9, 7: 11}


def nearest_degree(scale: Sequence[int], pitch_class: int) -> tuple[int, int]:
    """把半音 ``pitch_class`` 归到 ``scale`` 里最近的一级。

    **一切都在「相对主音」的半音空间里算**：``scale[0]`` 是主音，
    所以先把它折算成 0。本项目踩过的坑：直接用 ``pitch - pitch_class``
    当八度基准，等于假设主音是 C —— 主音不是 C 的调（小调尤其）会整体错一个八度。

    Returns:
        ``(级序号, 与那一级的半音差)``，差落在 ``-6..+6``（调外音时非 0）。
        正好卡在两级中间时取**较低的一级**（结果稳定、可复现）。
    """
    tonic = int(scale[0])
    relative = (int(pitch_class) - tonic) % 12
    best_index, best_delta = 0, None
    for index, semitone in enumerate(scale):
        delta = relative - (int(semitone) - tonic) % 12
        if delta > 6:
            delta -= 12
        elif delta < -6:
            delta += 12
        if best_delta is None or abs(delta) < abs(best_delta):
            best_index, best_delta = index, delta
    return best_index, int(best_delta or 0)


def degree_steps(degree: int) -> int:
    """**度数 → 音级步数**：一度 = 0 步、二度 = 1 步、三度 = 2 步 …… 七度 = 6 步。

    负号表示下行。这一步换算很容易写错（把"三度"直接当 2 步、或把"六度"当 6 步），
    而错一位的表现是"听起来像和声但总差一个音"，很难一眼看出来 —— 所以单独成函数。

    Raises:
        ValueError: 度数超出 ±1..±7。
    """
    magnitude = abs(int(degree))
    if not 1 <= magnitude <= MAX_DEGREE:
        raise ValueError(f"度数必须在 ±1..±{MAX_DEGREE} 之间，收到 {degree!r}")
    step = magnitude - 1
    return step if degree > 0 else -step


def diatonic_shift(pitch: int, scale: Sequence[int], degree: int) -> int:
    """把音高按**度数**平移（``3`` = 上行三度，``-3`` = 下行三度），自动跨八度。

    Raises:
        ValueError: ``scale`` 为空（没有调性可用），或度数超范围。
    """
    if not scale:
        raise ValueError("没有可用音阶：调用方应回落到固定半音平移")
    pitch = int(pitch)
    if degree == 0:
        return pitch
    step = degree_steps(degree)
    tonic = int(scale[0])
    relative = (pitch % 12 - tonic) % 12
    # 八度基准取「该音所在八度里，不高于它的那个主音」
    octave_base = pitch - relative
    index, offset = nearest_degree(scale, pitch % 12)
    octave, target_index = divmod(index + step, 7)
    target = (int(scale[target_index]) - tonic) % 12
    return octave_base + target + 12 * octave + offset


def chromatic_semitones(degree: int) -> int:
    """把音级按**大调模板**折算成固定半音数（一度=0、三度=+4、五度=+7 …）。"""
    magnitude = abs(int(degree))
    if magnitude not in _MAJOR_TEMPLATE:
        raise ValueError(f"音级必须在 1..{MAX_DEGREE} 之间，收到 {degree!r}")
    step = _MAJOR_TEMPLATE[magnitude]
    return step if degree > 0 else -step


def build_harmony_notes(
    notes: Sequence[Mapping[str, Any]],
    *,
    scale: Sequence[int] | None = None,
    degree: int = 0,
    semitones: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """由主人声音符造出和声音符。

    **时值、起点、歌词一律照搬**——平行和声唱的是同一套词、同一个节奏，
    只有音高被平移。留空歌词会在导出时回落到占位词 ``la``，那就不是平行和声了。

    Args:
        scale: 调内平移用的七声音阶（:func:`~app.abcp.scale_pitch_classes`）。
            给了它就按 **degree** 做调内平移；为空则按 **semitones** 固定半音。
        degree: 音级（±1..±7）。
        semitones: 固定半音数（调内不可用时使用）。

    Returns:
        ``(音符表, 统计)``；统计里 ``mode`` 说明实际走了哪条路，
        ``clamped`` 是贴到 0/127 边界的音数，``out_of_scale`` 是调外音数。
    """
    use_scale = bool(scale) and degree != 0
    step = int(degree) if use_scale else int(semitones)
    out: list[dict[str, Any]] = []
    clamped = 0
    out_of_scale = 0
    for note in notes or ():
        try:
            pitch = int(note["pitch"])
            start = float(note["start"])
            end = float(note["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        if use_scale:
            new_pitch = diatonic_shift(pitch, scale, step)
            if nearest_degree(scale, pitch % 12)[1] != 0:
                out_of_scale += 1
        else:
            new_pitch = pitch + step
        if new_pitch < 0:
            new_pitch, clamped = 0, clamped + 1
        elif new_pitch > 127:
            new_pitch, clamped = 127, clamped + 1
        out.append({
            "start": round(start, 6),
            "end": round(end, 6),
            "pitch": new_pitch,
            "lyric": "" if note.get("lyric") is None else str(note["lyric"]),
        })
    return out, {
        "mode": "diatonic" if use_scale else "chromatic",
        "degree": step,
        "semitones": 0 if use_scale else step,
        "notes": len(out),
        "clamped": clamped,
        "out_of_scale": out_of_scale,
    }
