"""男女合唱：逐音归属 v2（主旋律 vs 和声）。

v1 的问题（见交接文档 §15）：窗太长（256ms）会跨音符；第二个基频里混进泛音/共振峰，
出现 −33 / +19 这种不可能是人声和声的野值。

v2 的改动
--------
1. **锚定已校正的主旋律**：八度已由 `app/octave.py` 修好（交接文档 §14.4），
   所以主旋律音高与音频一致，可以直接在它的 ±1.5 半音内找峰，不再逐音猜八度。
2. **窗缩短**到 128ms（N=2048），并贴着音符中点取样，避免跨音符。
3. **限制音程范围**：只接受 1–12 半音的第二声部；≤1 半音判为**同度**（物理上分不开）；
   >12 半音直接丢掉（人声和声不会那样）。
4. **连续性去野值**：对"音程序列"做中值滤波，偏离局部中位数 > 2 半音的丢掉。
5. 最终和声轨**沿用主旋律的节奏**（用户描述是"贴着主旋律"），音程吸附到局部中位数。

输出：报告 + JSON；可选写出两条 `.lab`（`--lead-out` / `--harmony-out`），
便于接到 `rebuild_exports` 导成两轨 SVP/MIDI。

用法::

    .venv\\Scripts\\python.exe _smoke\\duet_voice_split.py <vocals.wav> <校正后的 melody_vocal.lab> \
        [--window-ms 128] [--out 报告.txt] [--json 结果.json]
"""
from __future__ import annotations

import collections
import json
import pathlib
import subprocess
import sys

import numpy as np

import _paths  # 同目录；本机 ffmpeg 路径见 _smoke/_paths.py

SR = 16000
HARM = np.arange(1, 9)
DF = None  # 由窗长决定
FFMPEG = _paths.FFMPEG
FMIN, FMAX = 70.0, 1100.0
UNISON_TOL = 1.0        # ≤ 这个间隔算同度
MAX_INTERVAL = 12.0     # > 这个间隔不算人声和声
OUTLIER_TOL = 2.0       # 偏离局部中值多少半音算野值

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def nm(midi: float) -> str:
    m = int(round(midi))
    return f"{NAMES[m % 12]}{m // 12 - 1}"


def hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def hz_to_midi(f: float) -> float:
    return 69 + 12 * np.log2(max(f, 1e-6) / 440.0)


def decode_mono(path: pathlib.Path) -> np.ndarray:
    out = subprocess.run(
        [FFMPEG, "-v", "error", "-nostdin", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"],
        capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype="<f4").astype(np.float64)


def _refine(score: np.ndarray, i: int, grid: np.ndarray) -> float:
    if i <= 0 or i >= len(score) - 1:
        return float(grid[i])
    a, b, c = score[i - 1], score[i], score[i + 1]
    den = a - 2 * b + c
    if abs(den) < 1e-12:
        return float(grid[i])
    d = max(-0.5, min(0.5, 0.5 * (a - c) / den))
    return float(grid[i] + d * (grid[1] - grid[0]))


class Detector:
    def __init__(self, n: int) -> None:
        self.n = n
        self.df = SR / n
        self.win = np.hanning(n)
        self.grid = np.arange(FMIN, FMAX, 0.5)
        self.hidx = np.round(self.grid[:, None] * HARM[None, :] / self.df).astype(np.int32)
        self.hgain = 1.0 / HARM

    def _score(self, mag: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
        nb = len(mag)
        idx = np.clip(self.hidx, 0, nb - 1)
        ok = self.hidx < nb
        src = mag if mask is None else np.where(mask, 0.0, mag)
        return (src[idx] * self.hgain[None, :] * ok).sum(axis=1)

    def analyze(self, seg: np.ndarray, lead_f: float) -> tuple[float, float, float, float]:
        """返回 (f_lead, s_lead, f_other, s_other)。f_lead 锚定在 lead_f 附近。"""
        mag = np.abs(np.fft.rfft(seg * self.win))
        score = self._score(mag)

        # 1) 在 lead_f ±1.5 半音内取峰，锚定到模型给的主旋律
        lo, hi = lead_f * 2 ** (-1.5 / 12), lead_f * 2 ** (1.5 / 12)
        band = np.where((self.grid >= lo) & (self.grid <= hi))[0]
        if len(band) == 0:
            return lead_f, 0.0, 0.0, 0.0
        k1 = int(band[np.argmax(score[band])])
        f_lead, s_lead = _refine(score, k1, self.grid), float(score[k1])

        # 2) 抹掉主旋律的谐波（含 ±5 bin，覆盖基频估计的小误差）
        mask = np.zeros(len(mag), dtype=bool)
        for k in HARM:
            b = int(round(f_lead * k / self.df))
            mask[max(0, b - 5): b + 6] = True
        score2 = self._score(mag, mask)
        k2 = int(np.argmax(score2))
        return f_lead, s_lead, _refine(score2, k2, self.grid), float(score2[k2])


def median_filter(vals: list[float], k: int = 7) -> list[float]:
    half = k // 2
    out = []
    for i in range(len(vals)):
        lo, hi = max(0, i - half), min(len(vals), i + half + 1)
        out.append(float(np.median(vals[lo:hi])))
    return out


def render_midi(path: pathlib.Path, tracks: list[tuple[str, list[tuple[float, float, int]]]]) -> None:
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(initial_tempo=120)
    for name, notes in tracks:
        inst = pretty_midi.Instrument(program=0, name=name)
        for s, e, p in notes:
            if e > s:
                inst.notes.append(pretty_midi.Note(velocity=100, pitch=int(p), start=float(s), end=float(e)))
        if inst.notes:
            pm.instruments.append(inst)
    pm.write(str(path))


def merge_same_pitch(notes: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    """把音高相同、时间重叠（或相接）的音符合并成一个。

    **为什么必须合并**：简易合成器每个音符都从 ``sin(0)`` 起振，同音高的两个音符
    若时间上错开，相位不同就会**互相抵消**（用户实测 0:46–1:03 听到相位抵消）。
    合并后每个音高在任一时刻只有一个振荡器，抵消消失。
    """
    by_pitch: dict[int, list[list[float]]] = {}
    for s, e, p in notes:
        by_pitch.setdefault(int(p), []).append([float(s), float(e)])
    out: list[tuple[float, float, int]] = []
    for p, ivs in by_pitch.items():
        ivs.sort()
        cur: list[float] | None = None
        for s, e in ivs:
            if cur is not None and s <= cur[1] + 1e-6:
                cur[1] = max(cur[1], e)
            else:
                if cur is not None:
                    out.append((cur[0], cur[1], p))
                cur = [s, e]
        if cur is not None:
            out.append((cur[0], cur[1], p))
    return sorted(out)


def _synth(notes: list[tuple[float, float, int]], total: int, sr: int) -> np.ndarray:
    buf = np.zeros(total)
    for s, e, p in merge_same_pitch(notes):
        f = hz(p)
        i0, i1 = int(s * sr), min(total, int(e * sr))
        if i1 <= i0 or f > sr / 2 - 200:
            continue
        t = np.arange(i1 - i0) / sr
        env = np.minimum(1.0, t / 0.02) * np.exp(-t / 1.2)
        seg = sum(a * np.sin(2 * np.pi * f * k * t) / k
                  for k, a in ((1, 1.0), (2, 0.5), (3, 0.28), (4, 0.15), (5, 0.08)))
        buf[i0:i1] += seg * env
    return buf


def render_wav(path: pathlib.Path, notes: list[tuple[float, float, int]], sr: int = 22050) -> None:
    """把音符渲染成能直接双击试听的 wav（加法合成，不需要音色库）。"""
    import wave

    if not notes:
        return
    total = int((max(e for _s, e, _p in notes) + 0.5) * sr)
    buf = _synth(notes, total, sr)
    _write(path, buf, sr)


def render_wav_stereo(path: pathlib.Path, left: list[tuple[float, float, int]],
                      right: list[tuple[float, float, int]], sr: int = 22050) -> None:
    """左=一条声部、右=另一条。两声部同度时不会互相抵消，也方便分辨谁是谁。"""
    import wave

    if not left and not right:
        return
    span = max([e for _s, e, _p in left + right] or [0.0]) + 0.5
    total = int(span * sr)
    a = _synth(left, total, sr)
    b = _synth(right, total, sr)
    peak = float(np.max(np.abs(np.concatenate([a, b])))) or 1.0
    inter = np.empty(total * 2)
    inter[0::2] = a / peak * 0.9
    inter[1::2] = b / peak * 0.9
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((inter * 32767).astype("<i2").tobytes())


def _write(path: pathlib.Path, buf: np.ndarray, sr: int) -> None:
    import wave

    peak = float(np.max(np.abs(buf))) or 1.0
    pcm = (buf / peak * 0.9 * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def main() -> int:
    argv = sys.argv[1:]
    out_path = json_path = None
    window_ms = 128.0
    midi_path = wav_path = lead_wav_path = None
    if "--out" in argv:
        i = argv.index("--out"); out_path = argv[i + 1]; del argv[i:i + 2]
    if "--json" in argv:
        i = argv.index("--json"); json_path = argv[i + 1]; del argv[i:i + 2]
    if "--window-ms" in argv:
        i = argv.index("--window-ms"); window_ms = float(argv[i + 1]); del argv[i:i + 2]
    if "--midi" in argv:
        i = argv.index("--midi"); midi_path = pathlib.Path(argv[i + 1]); del argv[i:i + 2]
    if "--harmony-wav" in argv:
        i = argv.index("--harmony-wav"); wav_path = pathlib.Path(argv[i + 1]); del argv[i:i + 2]
    if "--lead-wav" in argv:
        i = argv.index("--lead-wav"); lead_wav_path = pathlib.Path(argv[i + 1]); del argv[i:i + 2]
    if len(argv) < 2:
        print(__doc__)
        return 2

    raw = open(out_path, "w", encoding="utf-8") if out_path else None

    class _Tee:
        def write(self, s):
            if raw:
                raw.write(s)
            try:
                return sys.__stdout__.write(s)
            except UnicodeEncodeError:
                enc = getattr(sys.__stdout__, "encoding", None) or "utf-8"
                return sys.__stdout__.write(s.encode(enc, "replace").decode(enc, "replace"))

        def flush(self):
            if raw:
                raw.flush()
            sys.__stdout__.flush()

    sys.stdout = _Tee()

    vocals, lab = pathlib.Path(argv[0]), pathlib.Path(argv[1])
    for p in (vocals, lab):
        if not p.is_file():
            print("找不到文件:", p)
            return 2

    n = int(round(window_ms / 1000 * SR))
    n = 1 << (n - 1).bit_length()      # 取 2 的幂，FFT 更快
    det = Detector(n)
    print(f"人声 stem: {vocals.name}   窗 {n} 样本（{n/SR*1000:.0f} ms）")
    x = decode_mono(vocals)
    print(f"  时长 {len(x)/SR:.1f}s")

    notes = []
    for line in lab.read_text(encoding="utf-8").splitlines():
        if line.strip():
            a, b, p, *_ = line.split()
            notes.append((float(a), float(b), int(float(p))))
    notes.sort()
    print(f"主旋律（已校正）: {len(notes)} 音符，中位 {nm(np.median([p for _,_,p in notes]))}")

    rows = []
    for start, end, pitch in notes:
        mid = (start + end) / 2
        i0 = int(round(mid * SR)) - n // 2
        if i0 < 0 or i0 + n > len(x):
            continue
        f_lead, s_lead, f_other, s_other = det.analyze(x[i0:i0 + n], hz(pitch))
        if s_lead <= 0:
            continue
        interval = hz_to_midi(f_other) - hz_to_midi(f_lead) if f_other > 0 else 0.0
        rows.append({
            "start": round(start, 3), "end": round(end, 3), "lead_pitch": pitch,
            "lead_hz": round(f_lead, 2), "other_hz": round(f_other, 2),
            "conf": round(float(s_other / s_lead), 3) if s_lead else 0.0,
            "interval": round(float(interval), 2),
            "usable": bool(s_other / s_lead >= 0.45 if s_lead else False),
        })

    print(f"可判定音符 {len(rows)}")

    # ---- 只看置信度够、间隔合理的 ----
    cand = [r for r in rows if r["usable"] and 1.0 <= abs(r["interval"]) <= MAX_INTERVAL]
    print(f"  第二声部置信度达标且音程合理：{len(cand)}（{len(cand)/max(1,len(rows))*100:.0f}%）")
    unison = sum(1 for r in rows if r["usable"] and abs(r["interval"]) < UNISON_TOL)
    print(f"  判为同度（≤{UNISON_TOL} 半音，物理上分不开）：{unison}")
    far = sum(1 for r in rows if r["usable"] and abs(r["interval"]) > MAX_INTERVAL)
    print(f"  间隔 >{MAX_INTERVAL} 半音（不是人声和声，丢弃）：{far}")

    if not cand:
        print("\n→ 没有可用的第二声部。")
        return 0

    # ---- 连续性：对音程做中值滤波，丢野值 ----
    ivs = [r["interval"] for r in cand]
    sm = median_filter(ivs, k=7)
    kept = []
    for r, m in zip(cand, sm):
        if abs(r["interval"] - m) <= OUTLIER_TOL:
            r["interval_smooth"] = round(m, 2)
            kept.append(r)
    print(f"  中值滤波后保留：{len(kept)}（丢掉野值 {len(cand)-len(kept)}）")

    kept_iv = np.array([r["interval"] for r in kept])
    print(f"\n和声相对主旋律的音程（半音，正=更高）：中位 {np.median(kept_iv):+.1f}，"
          f"范围 {kept_iv.min():+.1f} ~ {kept_iv.max():+.1f}，"
          f"标准差 {kept_iv.std():.2f}")
    uniq, cnt = np.unique(np.round(kept_iv).astype(int), return_counts=True)
    lbl = {3: "小三度", 4: "大三度", 5: "纯四度", 7: "纯五度", 8: "小六度", 9: "大六度",
           -3: "小三度↓", -4: "大三度↓", -5: "纯四度↓", -7: "纯五度↓",
           -8: "小六度↓", -9: "大六度↓", -12: "八度↓"}
    print("  分布：")
    for i in np.argsort(-cnt)[:10]:
        s = int(uniq[i])
        print(f"    {s:+3d} 半音 {lbl.get(s, ''):<10} {cnt[i]:>4} 音 ({cnt[i]/len(kept)*100:5.1f}%)")
    above = float((kept_iv > 0).mean())
    print(f"  和声在主旋律上方 {above*100:.0f}% / 下方 {(1-above)*100:.0f}%")

    print("\n时序（每 4s）：主旋律 / 和声 / 音程")
    for a in np.arange(0, len(x) / SR, 4.0):
        sel = [r for r in kept if a <= r["start"] < a + 4.0]
        if len(sel) < 2:
            continue
        lead = float(np.median([r["lead_pitch"] for r in sel]))
        iv = float(np.median([r["interval"] for r in sel]))
        print(f"   {int(a)//60}:{int(a)%60:02d}  {len(sel):>3} 音  "
              f"主旋律 {nm(lead):>4}   和声 {nm(lead + iv):>4}   音程 {iv:+5.1f} 半音")

    if json_path:
        pathlib.Path(json_path).write_text(
            json.dumps({"vocals": str(vocals), "lab": str(lab), "window": n,
                        "notes": rows, "kept": kept}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"\n逐音结果已写入 {json_path}")

    # ---- 导出可试听的产物：光看统计分不清"真实浮动"与"检测噪声"，得听 ----
    harm_notes = [(r["start"], r["end"], int(round(r["lead_pitch"] + r["interval_smooth"])))
                  for r in kept]
    if midi_path or wav_path or lead_wav_path:
        lead_notes = [(a, b, int(p)) for a, b, p in notes]
        if midi_path:
            # MIDI 的轨名走 Latin-1，中文会抛 UnicodeEncodeError（坑 #6）
            render_midi(midi_path, [("Lead", lead_notes), ("Harmony", harm_notes)])
            print(f"两轨 MIDI 已写入 {midi_path}")
        if wav_path:
            render_wav(wav_path, harm_notes)
            print(f"和声试听 wav 已写入 {wav_path}（{len(harm_notes)} 个音）")
        if lead_wav_path:
            render_wav(lead_wav_path, lead_notes)
            print(f"主旋律试听 wav 已写入 {lead_wav_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
