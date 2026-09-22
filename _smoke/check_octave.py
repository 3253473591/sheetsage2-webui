"""回归：八度校正。

三组断言
--------
1. **ABC 移调**：对真实 `score.abc` 做 -1 八度后，`parse_abc` 的
   音高必须**逐音正好 -12**，且音符数、起点、时值完全不变；
   再移回来必须与原始音高集合一致（允许规范写法变化，不允许音高变化）。
2. **判定器的特异性**：用模型自己的音高合成一段音频，判定器必须返回 0（不改）；
   把它降一个八度合成，判定器必须返回 -12。**只测"能改"是不够的**——
   不能证明"不该改时不改"，就会把本来正确的歌改坏。
3. **真实样本**：对已跑过的任务，报告判定结果。

    .venv\\Scripts\\python.exe _smoke\\check_octave.py
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np

import _paths  # 同目录；额外真实样本路径见 _smoke/_paths.py

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.abcp import parse_abc  # noqa: E402
from app.octave import estimate_octave_shift, shift_abc_octaves  # noqa: E402

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
    if not ok:
        FAIL.append(name)


def notes_of(abc: str):
    s = parse_abc(abc)
    return sorted((round(n.onset, 6), round(n.duration, 6), n.pitch)
                  for n in s.notes if n.pitch is not None)


# ---------------- 1. ABC 移调 ----------------
abcs = sorted(p for p in (ROOT / "output").rglob("score.abc") if p.stat().st_size)
if not abcs:
    print("SKIP 第 1 组：output/ 下没有 score.abc")
for p in abcs:
    text = p.read_text(encoding="utf-8")
    down = shift_abc_octaves(text, -1)
    up_again = shift_abc_octaves(down, +1)
    a, b, c = notes_of(text), notes_of(down), notes_of(up_again)
    tag = f"{p.parent.name}/score.abc"

    check(f"[{tag}] 音符数不变", len(a) == len(b) == len(c), f"{len(a)}/{len(b)}/{len(c)}")
    check(f"[{tag}] 起点与时值完全不变",
          all(x[0] == y[0] and x[1] == y[1] for x, y in zip(a, b)) and len(a) == len(b))
    check(f"[{tag}] 音高逐音 -12", all(x[2] - 12 == y[2] for x, y in zip(a, b)),
          str([(x[2], y[2]) for x, y in zip(a, b) if x[2] - 12 != y[2]][:5]))
    check(f"[{tag}] 移回来音高还原", [x[2] for x in a] == [z[2] for z in c])
    # 头字段/和弦符号/注释行必须原样
    keep = lambda t: [ln for ln in t.split("\n")
                      if ln.lstrip()[:1].isalpha() and ln.lstrip()[1:2] == ":"
                      or ln.lstrip().startswith("%")]
    check(f"[{tag}] 头字段与注释行未被动", keep(text) == keep(down))
    chords = lambda t: [w for w in t.split('"')[1::2]]
    check(f"[{tag}] 和弦符号未被动", chords(text) == chords(down))
    check(f"[{tag}] 移调后仍可解析出音符", len(b) > 0)

# ---------------- 2. 判定器的特异性 ----------------
fixture = ROOT / "output" / "_duet_probe"
lab = fixture / "melody_vocal.lab"
if not lab.is_file():
    print("SKIP 第 2 组：缺少 output/_duet_probe/melody_vocal.lab")
else:
    notes = []
    for line in lab.read_text(encoding="utf-8").splitlines():
        if line.strip():
            a, b, pt, *_ = line.split()
            notes.append((float(a), float(b), int(float(pt))))
    notes.sort()
    dur = max(b for _a, b, _p in notes) + 1.0
    sr = 16000
    x = np.zeros(int(dur * sr))

    def synth(pitch_offset: int) -> np.ndarray:
        sig = np.zeros_like(x)
        for a, b, pt in notes:
            f = 440.0 * 2 ** ((pt + pitch_offset - 69) / 12)
            if f <= 0 or f > sr / 2 - 100:
                continue
            i0, i1 = int(a * sr), min(len(sig), int(b * sr))
            if i1 <= i0:
                continue
            t = np.arange(i1 - i0) / sr
            env = np.minimum(1.0, t / 0.02) * np.exp(-t / 0.6)
            seg = sum(amp * np.sin(2 * np.pi * f * k * t) / k
                      for k, amp in ((1, 1.0), (2, 0.6), (3, 0.35), (4, 0.2)))
            sig[i0:i1] += seg * env
        return sig

    import subprocess
    import tempfile
    import wave

    def write_wav(path: pathlib.Path, data: np.ndarray) -> None:
        peak = np.max(np.abs(data)) or 1.0
        pcm = (data / peak * 0.9 * 32767).astype("<i2")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="octcheck_"))
    for off, expect, label in ((0, 0, "按模型音高合成（应当判定为不改）"),
                               (-12, -12, "降一个八度合成（应当判定 -12）")):
        wav = tmp / f"synth{off}.wav"
        write_wav(wav, synth(off))
        res = estimate_octave_shift(wav, notes)
        print(f"   {label}: shift={res['shift']} votes={res['votes']} ratio={res['ratio']:.2f}")
        check(label, res["shift"] == expect, str(res))
        wav.unlink(missing_ok=True)
    tmp.rmdir()

# ---------------- 3. 真实样本 ----------------
print("\n真实样本判定：")
cases = [
    ("_duet_probe（全混音）", ROOT / "output" / "_duet_probe",
     _paths.EXTRA_AUDIO),
    ("_duet_vocals_probe（干净人声 stem）", ROOT / "output" / "_duet_vocals_probe",
     str(ROOT / "tmp" / "duet_vocals" / "duet_vocals.wav")),
    ("20260914-171656（另一首歌）", ROOT / "output" / "20260914-171656_2a0db6d74f1e4721b046ca29421d7cce",
     None),
]
for label, d, audio in cases:
    lab = d / "melody_vocal.lab"
    if not lab.is_file():
        print(f"   {label}: 缺 melody_vocal.lab，跳过")
        continue
    if audio is None:
        import json
        meta = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        audio = meta.get("audio")
    if not audio or not pathlib.Path(audio).is_file():
        print(f"   {label}: 源音频不在（{audio}），跳过")
        continue
    ns = []
    for line in lab.read_text(encoding="utf-8").splitlines():
        if line.strip():
            a, b, pt, *_ = line.split()
            ns.append((float(a), float(b), int(float(pt))))
    res = estimate_octave_shift(audio, ns)
    print(f"   {label}: shift={res['shift']}  {res['reason']}")

print("\nFAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)
