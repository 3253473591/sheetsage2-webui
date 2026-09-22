"""把主旋律（女声）从人声 stem 里抠掉，得到"和声（男声）"参考 stem。

思路
----
逐音贪心找峰已经证明不行（见交接文档 §15）。但**我们知道主旋律是谁**：
SheetSage2 的 Vocal 轨（已由 `app/octave.py` 校正八度）就是女声主旋律。
于是问题从"一起找两个声部"变成"**已知一个声部，把它从时频域抠掉**"：

1. STFT 人声 stem；
2. 每一帧按主旋律当前音高 f0，在 f0 的 1..10 次谐波处下**软性陷波**（高斯凹陷）；
3. 逆变换得到残差 = 和声（男声）+ 抠不干净的残留。

产物是**能直接听的 wav**：如果女声基本消失、剩下一条约等于男声的线，说明抠得动，
下一步就能把这条 stem 丢回 SheetSage2 跑出男声谱面。

用法::

    .venv\\Scripts\\python.exe _smoke\\duet_harmony_stem.py <vocals.wav> <校正后 melody_vocal.lab> \
        --out-wav tmp\\duet_harmony_stem.wav [--sr 22050]
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import numpy as np

import _paths  # 同目录；本机 ffmpeg 路径见 _smoke/_paths.py

FFMPEG = _paths.FFMPEG
N_FFT = 2048
HOP = 512
NOTCH = 0.97          # 陷波深度（1.0 = 完全挖掉）


def decode(path: pathlib.Path, sr: int) -> np.ndarray:
    out = subprocess.run(
        [FFMPEG, "-v", "error", "-nostdin", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"],
        capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype="<f4").astype(np.float64)


def write_wav(path: pathlib.Path, x: np.ndarray, sr: int) -> None:
    import wave

    peak = float(np.max(np.abs(x))) or 1.0
    pcm = (x / peak * 0.9 * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def stft(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    win = np.hanning(N_FFT)
    frames = 1 + (len(x) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(frames)[:, None]
    return np.fft.rfft(x[idx] * win, axis=1), win


def istft(spec: np.ndarray, length: int) -> np.ndarray:
    win = np.hanning(N_FFT)
    frames = np.fft.irfft(spec, n=N_FFT, axis=1) * win
    out = np.zeros(length)
    wsum = np.zeros(length)
    for i in range(frames.shape[0]):
        a = i * HOP
        out[a:a + N_FFT] += frames[i]
        wsum[a:a + N_FFT] += win ** 2
    return out / np.maximum(wsum, 1e-8)


def main() -> int:
    argv = sys.argv[1:]
    sr = 22050
    out_wav = None
    if "--out-wav" in argv:
        i = argv.index("--out-wav"); out_wav = pathlib.Path(argv[i + 1]); del argv[i:i + 2]
    if "--sr" in argv:
        i = argv.index("--sr"); sr = int(argv[i + 1]); del argv[i:i + 2]
    if len(argv) < 2:
        print(__doc__)
        return 2

    vocals, lab = pathlib.Path(argv[0]), pathlib.Path(argv[1])
    for p in (vocals, lab):
        if not p.is_file():
            print("找不到文件:", p)
            return 2

    notes = []
    for line in lab.read_text(encoding="utf-8").splitlines():
        if line.strip():
            a, b, p, *_ = line.split()
            notes.append((float(a), float(b), int(float(p))))
    notes.sort()
    # 每个时刻的主旋律音高（用音符区间查；区间外表示主旋律不出声，不下陷波）
    starts = np.array([a for a, _b, _p in notes])

    def lead_at(t: float) -> int | None:
        k = int(np.searchsorted(starts, t, side="right")) - 1
        if k < 0:
            return None
        a, b, p = notes[k]
        return p if a <= t < b else None

    x = decode(vocals, sr)
    print(f"人声 stem {vocals.name}：{len(x)/sr:.1f}s @ {sr}Hz；主旋律 {len(notes)} 音符")

    spec, _ = stft(x)
    n_frames = spec.shape[0]
    freqs = np.fft.rfftfreq(N_FFT, 1 / sr)
    mask = np.ones_like(spec, dtype=np.float64)
    notch_total = 0.0

    for i in range(n_frames):
        pitch = lead_at(i * HOP / sr)
        if pitch is None:
            continue
        f0 = 440.0 * 2 ** ((pitch - 69) / 12)
        for k in range(1, 11):
            b = f0 * k
            if b > sr / 2 - 100:
                break
            delta = max(18.0, 0.035 * b)
            g = np.exp(-0.5 * ((freqs - b) / delta) ** 2)
            mask[i] *= (1.0 - NOTCH * g)
        notch_total += 1

    masked = spec * mask
    removed = float(np.sum(np.abs(spec)) - np.sum(np.abs(masked)))
    total = float(np.sum(np.abs(spec))) or 1.0
    print(f"  下了陷波的有声帧：{notch_total}/{n_frames} ({notch_total/n_frames*100:.0f}%)")
    print(f"  频谱幅度被移除的比例：{removed/total*100:.1f}%")

    y = istft(masked, len(x))
    print(f"  残差 RMS / 原 RMS = {np.sqrt(np.mean(y**2))/max(1e-9, np.sqrt(np.mean(x**2))):.3f}")
    if out_wav:
        write_wav(out_wav, y, sr)
        print(f"和声参考 stem 已写入 {out_wav}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
