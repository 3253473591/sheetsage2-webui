"""双声部（男女合唱）结构分析：逐帧找两个有谐波支持的基频，给出音程关系与时间分布。

**输入必须是"所有人声、无伴奏"的 stem**（例如 MSST 的 big_beta6x vocals 输出）。
拿完整混音来跑，贝斯/钢琴/吉他的基频与泛音会被当成第二声部，结果不可信——
脚本带自诊断，遇到这种情况会判定结论无效并退出。

输出要点
--------
* 逐帧：主声部 f1、可能的第二声部 f2、两者音程（半音）
* 音程直方图：集中在 3/4（三度）、8/9（六度）、12（八度）说明是平行和声
* 时序表：每 2 秒给出 f_low / f_high / 间隔，可直接看出"谁在上面、什么时候换位"
* 自诊断：不像人声声部时拒绝输出结论

用法::

    .venv\\Scripts\\python.exe _smoke\\analyze_two_voices.py "<vocals.wav>" [--out 报告.txt]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

import _paths  # 同目录；本机 ffmpeg 路径见 _smoke/_paths.py

SR = 16000
N = 4096          # 256 ms 窗口；配抛物线插值拿到亚频点精度
HOP = 512         # 32 ms
FMIN, FMAX = 70.0, 500.0   # 人声基频范围（男低 ~80，女高 ~500）
DF = SR / N
HARM = np.arange(1, 9)
FFMPEG = _paths.FFMPEG

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MALE = (75.0, 185.0)
FEMALE = (165.0, 500.0)


def decode_mono(path: Path) -> np.ndarray:
    cmd = [FFMPEG, "-v", "error", "-nostdin", "-i", str(path),
           "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype="<f4").astype(np.float64)


def note_name(f0: float) -> str:
    if f0 <= 0:
        return "—"
    midi = int(round(69 + 12 * np.log2(f0 / 440.0)))
    return f"{NAMES[midi % 12]}{midi // 12 - 1}"


def _refine(score: np.ndarray, i: int, grid: np.ndarray) -> float:
    """对峰值做抛物线插值，拿到比网格更细的基频。"""
    if i <= 0 or i >= len(score) - 1:
        return float(grid[i])
    a, b, c = score[i - 1], score[i], score[i + 1]
    denom = a - 2 * b + c
    if abs(denom) < 1e-12:
        return float(grid[i])
    delta = 0.5 * (a - c) / denom
    delta = max(-0.5, min(0.5, delta))
    step = grid[1] - grid[0]
    return float(grid[i] + delta * step)


def analyze(x: np.ndarray) -> dict:
    win = np.hanning(N)
    grid = np.arange(FMIN, FMAX, 0.5)
    hidx = np.round(grid[:, None] * HARM[None, :] / DF).astype(np.int32)
    hgain = 1.0 / HARM
    frames = max(0, (len(x) - N) // HOP)

    rms = np.empty(frames)
    rows = []
    for i in range(frames):
        seg = x[i * HOP: i * HOP + N]
        rms[i] = np.sqrt(np.mean(seg * seg))
        mag = np.abs(np.fft.rfft(seg * win))
        nb = len(mag)
        idx = np.clip(hidx, 0, nb - 1)
        ok = hidx < nb
        score = (mag[idx] * hgain[None, :] * ok).sum(axis=1)

        k1 = int(np.argmax(score))
        f1, s1 = _refine(score, k1, grid), float(score[k1])

        sup = mag.copy()
        for k in HARM:
            b = int(round(f1 * k / DF))
            sup[max(0, b - 4): b + 5] = 0.0
        score2 = (sup[idx] * hgain[None, :] * ok).sum(axis=1)
        k2 = int(np.argmax(score2))
        f2, s2 = _refine(score2, k2, grid), float(score2[k2])
        rows.append((i * HOP / SR, f1, s1, f2, s2))

    rows = np.array(rows)
    gate = np.percentile(rms, 55)
    v = rows[rms > gate]
    f1, f2, s1, s2 = v[:, 1], v[:, 3], v[:, 2], v[:, 4]
    semi = 12 * np.log2(np.maximum(f2, 1e-6) / np.maximum(f1, 1e-6))
    pair = (s2 >= 0.5 * s1) & (np.abs(semi) >= 1.0) & (np.abs(semi) <= 24.0)
    low = np.minimum(f1, f2)[pair]
    high = np.maximum(f1, f2)[pair]
    return {"n": len(f1), "pair": int(pair.sum()), "f1": f1, "t_all": v[:, 0],
            "f2": f2[pair], "low": low, "high": high, "semi": np.abs(semi[pair]),
            "t": v[pair, 0] if pair.any() else np.array([]), "rms": rms}


def main() -> int:
    argv = sys.argv[1:]
    if "--out" in argv:
        i = argv.index("--out")
        if i + 1 < len(argv):
            raw = open(Path(argv[i + 1]), "w", encoding="utf-8")

            class _Tee:
                def write(self, s):
                    raw.write(s)
                    try:
                        return sys.__stdout__.write(s)
                    except UnicodeEncodeError:
                        enc = getattr(sys.__stdout__, "encoding", None) or "utf-8"
                        return sys.__stdout__.write(s.encode(enc, "replace").decode(enc, "replace"))

                def flush(self):
                    raw.flush()
                    sys.__stdout__.flush()

            sys.stdout = _Tee()
        del argv[i:i + 2]

    if not argv:
        print(__doc__)
        return 2
    path = Path(argv[0])
    if not path.is_file():
        print("找不到音频:", path)
        return 2

    print(f"读取 {path.name} …")
    x = decode_mono(path)
    res = analyze(x)
    n, npair = res["n"], res["pair"]
    print(f"  时长 {len(x)/SR:.1f}s；有声帧 {n}（每帧 {HOP/SR*1000:.0f}ms）")

    # ---- 自诊断 ----
    span = float(np.percentile(res["f1"], 95) / max(1.0, np.percentile(res["f1"], 5)))
    inside = float((res["semi"] <= 12).mean()) if npair else 0.0
    harmset = np.array([12, 19, 24])
    harm = float((np.min(np.abs(res["semi"][:, None] - harmset[None, :]), axis=1) < 0.6).mean()) if npair else 0.0
    pair_ratio = npair / max(1, n)
    print(f"\n自诊断：主声部 5–95% 跨度 {span:.2f}×；成对帧 {pair_ratio*100:.0f}%；"
          f"其中距离 ≤ 八度 {inside*100:.0f}%；谐波关系 {harm*100:.0f}%")
    # 注意：跨度门槛对「男女都在这条 stem 里」的输入要放宽——f1 有时是男、有时是女，
    # 跨 1.5~2 个八度是正常的。全混音那种输入跨度会到 10× 以上（伴奏全在里面）。
    bad = []
    if span > 4.0:
        bad.append(f"跨度 {span:.1f}×（>4×，输入里多半还有伴奏/噪声）")
    if npair and inside < 0.5:
        bad.append(f"只有 {inside*100:.0f}% 的成对帧在一个八度内（不像人声和声）")
    if npair and harm > 0.5:
        bad.append(f"{harm*100:.0f}% 的『第二声部』与第一成谐波关系（是在看泛音）")
    if pair_ratio < 0.15:
        bad.append(f"成对帧仅 {pair_ratio*100:.0f}%（几乎没有双声部）")
    if bad:
        print("  ⚠️ 结论不可信：")
        for b in bad:
            print("     - " + b)
        print("  → 请改用 MSST 的 vocals stem（不含伴奏）再跑。")
        return 0
    print("  ✓ 三项判据通过，下面的音程关系可信。")

    # ---- 音程分布 ----
    uniq, cnt = np.unique(np.round(res["semi"]).astype(int), return_counts=True)
    order = np.argsort(-cnt)
    label = {3: "小三度", 4: "大三度", 5: "纯四度", 6: "减五度", 7: "纯五度",
             8: "小六度", 9: "大六度", 10: "小七度", 11: "大七度", 12: "八度"}
    print(f"\n两个声部的音程（半音）Top：")
    for i in order[:8]:
        s = int(uniq[i])
        print(f"   {s:>3}  {label.get(s, ''):<5} {cnt[i]:>6} 帧 ({cnt[i]/npair*100:5.1f}%)")

    lo, hi = res["low"], res["high"]
    for nm, arr in (("低声部", lo), ("高声部", hi)):
        p5, p50, p95 = np.percentile(arr, [5, 50, 95])
        tags = []
        if MALE[0] <= p50 <= MALE[1]:
            tags.append("男声区")
        if FEMALE[0] <= p50 <= FEMALE[1]:
            tags.append("女声区")
        print(f"   {nm}: 中位 {p50:6.1f} Hz ({note_name(p50)})，5–95% "
              f"{p5:.0f}–{p95:.0f} Hz {'/'.join(tags) or '其他'}")

    # ---- 主声部时序（含独唱段）：用来判断"某一段到底几个人唱" ----
    t, dur = res["t"], len(x) / SR
    print("\n双声部时序（每 2s，只列有双声部的桶）：")
    for a in np.arange(0, dur, 2.0):
        m = (t >= a) & (t < a + 2.0)
        if m.sum() < 4:
            continue
        print(f"   {int(a)//60}:{int(a)%60:02d}  {m.sum():>3} 帧  "
              f"低 {np.median(lo[m]):6.1f} Hz ({note_name(np.median(lo[m])):>4})  "
              f"高 {np.median(hi[m]):6.1f} Hz ({note_name(np.median(hi[m])):>4})  "
              f"间隔 {np.median(res['semi'][m]):4.1f} 半音")

    # ---- 主声部时序（含独唱段）：用来判断"某一段到底几个人在唱" ----
    print("\n主声部时序（每 4s，全曲含独唱）：")
    ta, fa = res["t_all"], res["f1"]
    for a in np.arange(0, dur, 4.0):
        m = (ta >= a) & (ta < a + 4.0)
        if m.sum() < 6:
            continue
        pm = (t >= a) & (t < a + 4.0)
        med = float(np.median(fa[m]))
        print(f"   {int(a)//60}:{int(a)%60:02d}  {m.sum():>3} 帧  主声部 {med:6.1f} Hz "
              f"({note_name(med):>4})   同时双声部 {pm.sum():>3} 帧")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
