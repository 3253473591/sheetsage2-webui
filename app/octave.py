"""八度校正：修 SheetSage2 输出整体高八度的问题（见交接文档 §14）。

两个部分
--------
1. :func:`shift_abc_octaves` —— 把 ABC 文本里的**音符八度**整体移 ±12。
   只动音符的「字母大小写 + ``'``/``,`` 标记」，头字段、和弦符号 ``"Cm"``、
   行内字段 ``[K:Em]``、装饰 ``!f!``、注释 ``%``、休止符 ``z/x/Z`` 一律不碰。

   我们的解析器（:mod:`app.abcp`）的约定是：
   大写字母基准 MIDI 60，小写 +12，每个 ``'`` +12、每个 ``,`` −12。
   于是「八度序号」= ``(0 if 大写 else 1) + ' 数 - , 数``，移调就是改这个序号，
   再按规范形式写回（序数 ≥1 用小写 + 若干 ``'``；≤0 用大写 + 若干 ``,``）。

2. :func:`estimate_octave_shift` —— 用**音频**投票判断模型到底偏了几个八度。
   对每个音符，在其余中点开窗，比较谐波求和得分在 f、f/2、2f 三者上谁更强。
   只有压倒性多数（默认 ≥70%）指向同一方向才返回移位，否则返回 0（不动）。

**为什么必须用音频判**：不能在代码里写死 -12 —— 万一以后模型修好了，
写死会把本来正确的输出改坏。这里始终以"音频里基频究竟在哪"为准。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.config import ffmpeg_path

__all__ = [
    "shift_abc_octaves",
    "estimate_octave_shift",
    "read_lab_notes",
    "shift_lab_pitches",
    "ABC_NOTE_RE",
]

ABC_NOTE_RE = __import__("re").compile(
    r"""
    (?P<acc>\^{1,2}|_{1,2}|=)?          # 临时记号
    (?P<letter>[A-Ga-g])                 # 音名
    (?P<octave>[',]*)                    # 八度标记
    (?P<dur>(?:\d+)?(?:/+)?(?:\d+)?)?    # 时值
    """,
    __import__("re").VERBOSE,
)


def _octave_index(letter: str, marks: str) -> int:
    """解析器里的八度序号：大写基准 0，小写 +1，``'`` +1，``,`` −1。"""
    return (0 if letter.isupper() else 1) + marks.count("'") - marks.count(",")


def _encode(letter: str, index: int) -> str:
    """把八度序号写成规范形式（保留音名，只换大小写与标记）。"""
    name = letter.upper()
    if index >= 1:
        return name.lower() + "'" * (index - 1)
    return name + "," * (-index)


def shift_abc_octaves(abc: str, octaves: int) -> str:
    """把 ABC 里所有音符的八度移动 ``octaves`` 个八度（通常 -1 或 +1）。

    非音符内容原样保留。``octaves == 0`` 时原样返回。
    """
    if not abc or octaves == 0:
        return abc

    out_lines: list[str] = []
    for line in abc.split("\n"):
        stripped = line.lstrip()
        # 头字段行（X:/T:/M:/L:/Q:/K:/V:/w: …）整行跳过
        if len(stripped) > 1 and stripped[1] == ":" and stripped[0].isalpha():
            out_lines.append(line)
            continue
        if stripped.startswith("%"):          # 注释
            out_lines.append(line)
            continue

        body = line
        i, n = 0, len(body)
        pieces: list[str] = []
        while i < n:
            ch = body[i]
            if ch == '"':                      # 行内和弦符号 "Cm"
                end = body.find('"', i + 1)
                end = n if end < 0 else end + 1
                pieces.append(body[i:end])
                i = end
                continue
            if ch == "!":                      # 装饰 !f! / !pp!
                end = body.find("!", i + 1)
                end = n if end < 0 else end + 1
                pieces.append(body[i:end])
                i = end
                continue
            if ch in "[{":                     # 行内字段 [K:Em] / 和弦 [CEG] / 装饰 {..}
                close = "]" if ch == "[" else "}"
                end = body.find(close, i + 1)
                end = n if end < 0 else end + 1
                inner = body[i + 1: end - 1] if end <= n else body[i + 1:]
                m = ABC_NOTE_RE.match(inner)
                if m and m.group("letter"):
                    shifted = _encode(
                        m.group("letter"),
                        _octave_index(m.group("letter"), m.group("octave") or "") + octaves,
                    )
                    inner = (m.group("acc") or "") + shifted + (m.group("dur") or "") + inner[m.end():]
                pieces.append(body[i] + inner + (close if end <= n else ""))
                i = end
                continue
            if ch == "%":                      # 行中注释，其余不管
                pieces.append(body[i:])
                break
            m = ABC_NOTE_RE.match(body, i)
            if m and m.group("letter"):
                pieces.append(
                    (m.group("acc") or "")
                    + _encode(m.group("letter"),
                              _octave_index(m.group("letter"), m.group("octave") or "") + octaves)
                    + (m.group("dur") or "")
                )
                i = m.end()
                continue
            pieces.append(ch)
            i += 1
        out_lines.append("".join(pieces))

    text = "\n".join(out_lines)
    return text


# --------------------------------------------------------------------------
# 模型原始产物（.lab）的读写
# --------------------------------------------------------------------------
def read_lab_notes(path: str | Path) -> list[tuple[float, float, int]]:
    """读 ``melody_*.lab``：每行 ``start end pitch track``，返回前 3 列。"""
    notes: list[tuple[float, float, int]] = []
    p = Path(path)
    if not p.is_file():
        return notes
    for line in p.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            notes.append((float(parts[0]), float(parts[1]), int(float(parts[2]))))
        except ValueError:
            continue
    return notes


def shift_lab_pitches(path: str | Path, semitones: int) -> int:
    """把 ``.lab`` 第 3 列（音高）整体移调，返回改动的行数。

    只动「≥3 列且第 3 列是 0–127 的整数」的行——``beat.lab`` 之类列义不同的文件不会被误改。
    """
    p = Path(path)
    if semitones == 0 or not p.is_file():
        return 0
    out_lines: list[str] = []
    changed = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            try:
                pitch = int(float(parts[2]))
            except ValueError:
                pitch = None
            if pitch is not None and 0 <= pitch <= 127:
                new_pitch = max(0, min(127, pitch + semitones))
                if new_pitch != pitch:
                    changed += 1
                parts[2] = str(new_pitch)
                line = " ".join(parts)
        out_lines.append(line)
    if changed:
        p.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return changed


# --------------------------------------------------------------------------
# 音频投票：模型到底偏了几个八度
# --------------------------------------------------------------------------
_SR = 16000
_N = 4096
_FFMPEG_FALLBACKS = (r"D:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\bin\ffmpeg.exe")


def _ffmpeg() -> str:
    p = ffmpeg_path()
    if p:
        return p
    for cand in _FFMPEG_FALLBACKS:
        if Path(cand).is_file():
            return cand
    raise RuntimeError("找不到 ffmpeg")


def _decode(audio: str | Path) -> Any:
    import numpy as np

    out = subprocess.run(
        [_ffmpeg(), "-v", "error", "-nostdin", "-i", str(audio),
         "-vn", "-ac", "1", "-ar", str(_SR), "-f", "f32le", "pipe:1"],
        capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype="<f4").astype(np.float64)


def _harm_score(mag: Any, f0: float, df: float, n_harm: int = 8) -> float:
    """f0 及其 1..n 次谐波的幅度加权和（1/k 权重）。"""
    if f0 <= 0:
        return 0.0
    nb = len(mag)
    total = 0.0
    for k in range(1, n_harm + 1):
        b = int(round(f0 * k / df))
        if b >= nb:
            break
        lo, hi = max(0, b - 1), min(nb, b + 2)
        total += float(mag[lo:hi].max()) / k
    return total


def estimate_octave_shift(
    audio: str | Path,
    notes: Sequence[tuple[float, float, int]] | Iterable[Any],
    *,
    sample_every: int = 3,
    majority: float = 0.70,
    margin: float = 1.25,
    min_votes: int = 25,
) -> dict[str, Any]:
    """用音频判断模型输出整体偏了几个八度。

    Args:
        audio: 模型当时听到的**同一个音频**（原始输入即可，不必做人声分离）。
        notes: ``[(start秒, end秒, midi音高), ...]``；通常是 ``melody_vocal.lab``。
        sample_every: 每几个音符取一个窗（省时间）。
        majority: 同一方向票数占比达到该值才采纳。
        margin: ``score(f/2)`` 要超过 ``score(f)`` 的这个倍数才算一票。
        min_votes: 有效投票数下限，太少就放弃判断。

    Returns:
        ``{"shift": 0/-12/+12, "votes": {...}, "ratio": float, "reason": str}``。
    """
    import numpy as np

    notes = [tuple(n[:3]) for n in notes]
    if len(notes) < min_votes:
        return {"shift": 0, "votes": {}, "ratio": 0.0,
                "reason": f"音符太少（{len(notes)} < {min_votes}），不判断"}

    try:
        x = _decode(audio)
    except Exception as exc:  # noqa: BLE001 - 判断失败就不动
        return {"shift": 0, "votes": {}, "ratio": 0.0, "reason": f"解码失败：{exc}"}

    win = np.hanning(_N)
    df = _SR / _N
    votes = {"down": 0, "up": 0, "none": 0}
    ratios: list[float] = []

    for idx, (start, end, pitch) in enumerate(notes):
        if idx % sample_every:
            continue
        mid = (float(start) + float(end)) / 2
        i0 = int(round(mid * _SR)) - _N // 2
        if i0 < 0 or i0 + _N > len(x):
            continue
        mag = np.abs(np.fft.rfft(x[i0:i0 + _N] * win))
        fc = 440.0 * 2 ** ((int(pitch) - 69) / 12)
        s_here = _harm_score(mag, fc, df)
        s_half = _harm_score(mag, fc / 2, df)
        s_dbl = _harm_score(mag, fc * 2, df)
        if s_here <= 0:
            continue
        ratios.append(s_half / s_here)
        if s_half > margin * s_here and s_half > s_dbl:
            votes["down"] += 1
        elif s_dbl > margin * s_here and s_dbl > s_half:
            votes["up"] += 1
        else:
            votes["none"] += 1

    total = sum(votes.values())
    if total < min_votes:
        return {"shift": 0, "votes": votes, "ratio": 0.0,
                "reason": f"有效窗口太少（{total} < {min_votes}），不判断"}

    med = float(np.median(ratios)) if ratios else 0.0
    if votes["down"] / total >= majority:
        return {"shift": -12, "votes": votes, "ratio": med,
                "reason": f"{votes['down']}/{total} 个窗口显示真实基频在 f/2（中位比 {med:.2f}）"}
    if votes["up"] / total >= majority:
        return {"shift": +12, "votes": votes, "ratio": med,
                "reason": f"{votes['up']}/{total} 个窗口显示真实基频在 2f（中位比 {med:.2f}）"}
    return {"shift": 0, "votes": votes, "ratio": med,
            "reason": f"投票不集中（down={votes['down']} up={votes['up']} none={votes['none']}），不改"}
