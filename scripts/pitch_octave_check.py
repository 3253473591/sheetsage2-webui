r"""查"扒谱音高整体高一个八度"：音域统计 + 谱面音高 vs 音频实测 F0。

两种用法（`docs/WebUI问题清单.md` §1 记录的就是这个脚本跑出来的数）：

    # ① 音域统计：output/ 里每个工程的 melody_vocal.lab，最低 / 中位 / 最高（MIDI）
    .\.venv\Scripts\python.exe scripts\pitch_octave_check.py --stats

    # ② 逐音比 F0：取时值 >=0.6s 的长音，在音的中点开 0.4s 窗估 F0，和谱面比差几个半音
    .\.venv\Scripts\python.exe scripts\pitch_octave_check.py ^
        "samples\萧敬腾 - 皮囊.flac" "output\萧敬腾_-_皮囊\melody_vocal.lab"

**⚠️ 全混音上做这件事有八度歧义**：伴奏能量大时自相关会挑到 2 倍周期（少算一个八度），
所以 `--method acf` 与 `--method comb`（自相关峰 + 谐波梳选八度）的**绝对倍数会不一样**。
能信的是：**扣掉整八度后的残差 ≈ 0**（音级对、八度错的结论成立）。
要定案必须用干声（vocal stem）当输入。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

PROJECT_ROOT = Path(__file__).resolve().parents[1]

MIN_DUR = 0.6          # 只量这么长的音（太短估不准）
WIN = 0.4              # 分析窗（秒）
HP_HZ = 130.0          # 高通：压掉鼓/贝斯（不压的话自相关会一路挑到 65Hz）
CONF = 0.40            # 自相关置信度下限
N_HARM = 8             # 谐波梳的谐波数
F0_LO, F0_HI = 65.0, 1200.0


def midi_to_hz(pitch: float) -> float:
    return 440.0 * 2 ** ((pitch - 69) / 12)


def _highpass(x: np.ndarray, sr: int, cutoff: float) -> np.ndarray:
    spec = np.fft.rfft(x)
    spec[np.fft.rfftfreq(len(x), 1 / sr) < cutoff] = 0
    return np.fft.irfft(spec, n=len(x))


def _comb_score(freqs: np.ndarray, spec: np.ndarray, f0: float) -> float:
    """f0 的 1..N 次谐波处的平均能量（落空按 0 计）——用来在整八度里挑真基频。"""
    total = 0.0
    for k in range(1, N_HARM + 1):
        target = f0 * k
        band = (freqs >= target * 0.97) & (freqs <= target * 1.03)
        total += float(spec[band].max()) if band.any() else 0.0
    return total / N_HARM


def estimate_f0(x: np.ndarray, sr: int, *, method: str = "comb") -> tuple[float, float]:
    """返回 `(f0, 自相关置信度)`；估不出来返回 `(0.0, 0.0)`。"""
    x = _highpass(x - x.mean(), sr, HP_HZ)
    if x.std() < 1e-4 or len(x) < sr // 20:
        return 0.0, 0.0
    n = 1 << 15
    windowed = x * np.hanning(len(x))
    spec = np.abs(np.fft.rfft(windowed, n=n))
    freqs = np.fft.rfftfreq(n, 1 / sr)
    ac = np.fft.irfft(np.abs(np.fft.rfft(windowed, n=n)) ** 2, n=n)[: n // 2]
    if ac[0] <= 0:
        return 0.0, 0.0
    ac = ac / ac[0]
    lo, hi = int(sr / F0_HI), int(sr / F0_LO)
    seg = ac[lo:hi]
    if len(seg) < 4:
        return 0.0, 0.0
    lag = lo + int(np.argmax(seg))
    if 0 < lag < len(ac) - 1:
        y0, y1, y2 = ac[lag - 1], ac[lag], ac[lag + 1]
        lag = lag + 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2 + 1e-12)
    f_acf = float(sr / lag)
    conf = float(ac[int(round(lag))])
    if method == "acf":
        return f_acf, conf
    cands = [f_acf * r for r in (1.0, 0.5, 1 / 3, 0.25) if F0_LO <= f_acf * r <= F0_HI]
    if not cands:
        return f_acf, conf
    return max(cands, key=lambda f: _comb_score(freqs, spec, f)), conf


def load_lab(path: Path) -> list[tuple[float, float, int]]:
    """`melody_*.lab`：`start \\t end \\t pitch`。"""
    rows: list[tuple[float, float, int]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            try:
                rows.append((float(parts[0]), float(parts[1]), int(float(parts[2]))))
            except ValueError:
                continue
    return rows


def print_stats(out_root: Path) -> None:
    """音域统计：每首的 melody_vocal.lab / melody_instrumental.lab。"""
    print(f"{'工程':<34} {'人声（min/中位/max）':<26} 器乐")
    for out in sorted(out_root.iterdir()):
        if not out.is_dir():
            continue
        cells = []
        for name in ("melody_vocal.lab", "melody_instrumental.lab"):
            path = out / name
            pitches = [p for _, _, p in load_lab(path)] if path.is_file() else []
            cells.append("—" if not pitches else
                         f"{len(pitches):>4} 音 {min(pitches):>3}/{statistics.median(pitches):>3}/"
                         f"{max(pitches):>3}")
        print(f"{out.name[:32]:<34} {cells[0]:<26} {cells[1]}")
    print("\n（MIDI 音高：60=C4，72=C5；男声旋律通常落在 C4–G4 一带）")


def compare(audio_path: Path, lab_path: Path, *, method: str, shown: int) -> None:
    audio, sr = sf.read(audio_path, dtype="float32", always_2d=True)
    mono = audio.mean(axis=1)
    rows = load_lab(lab_path)
    print(f"{audio_path.name}  {len(mono) / sr:.0f}s  |  {lab_path.name}  {len(rows)} 音  "
          f"|  method={method}\n")
    diffs: list[float] = []
    for start, end, pitch in rows:
        if end - start < MIN_DUR:
            continue
        mid = (start + end) / 2
        a, b = int((mid - WIN / 2) * sr), int((mid + WIN / 2) * sr)
        if a < 0 or b > len(mono):
            continue
        f0, conf = estimate_f0(mono[a:b], sr, method=method)
        if f0 <= 0 or conf < CONF:
            continue
        diff = 12 * np.log2(midi_to_hz(pitch) / f0)
        diffs.append(diff)
        if len(diffs) <= shown:
            print(f"  {start:7.2f}s 谱面 {pitch:>3}={midi_to_hz(pitch):7.1f}Hz | "
                  f"实测 {f0:6.1f}Hz（acf {conf:.2f}） | 差 {diff:+5.1f} 半音")
    if not diffs:
        print("  没有可信样本")
        return
    d = np.array(diffs)
    nearest = np.round(d / 12) * 12
    res = d - nearest
    vals, counts = np.unique(nearest, return_counts=True)
    print(f"\n  可信样本 {len(d)}：整八度分布 "
          f"{ {int(v): int(c) for v, c in zip(vals, counts)} }")
    print(f"  扣掉整八度后的残差：中位 {np.median(res):+.2f} 半音，"
          f"|残差| <= 1 半音占 {np.mean(np.abs(res) <= 1):.0%}")
    print(f"  差值中位 {np.median(d):+.2f} 半音")
    print("  [注意] 全混音上绝对倍数不可信，只有'残差≈0'（音级对）这一条结论可靠。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扒谱音高 vs 音频实测 F0")
    parser.add_argument("audio", nargs="?", type=Path, help="音频（干声最好）")
    parser.add_argument("lab", nargs="?", type=Path, help="output/<工程>/melody_vocal.lab")
    parser.add_argument("--stats", action="store_true", help="只打 output/ 的音域统计")
    parser.add_argument("--out-root", type=Path, default=PROJECT_ROOT / "output")
    parser.add_argument("--method", choices=("acf", "comb"), default="comb")
    parser.add_argument("--shown", type=int, default=10, help="打印前几条明细")
    args = parser.parse_args(argv)

    if args.stats:
        print_stats(args.out_root)
        return 0
    if not (args.audio and args.lab):
        parser.error("要么给 --stats，要么给 <音频> <lab> 两个位置参数")
    compare(args.audio, args.lab, method=args.method, shown=args.shown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
