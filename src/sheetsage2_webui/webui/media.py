"""音频与波形：给界面播放、画波形用。

界面上两条音轨都要画波形，浏览器只知道 `<audio>`，所以要后端把包络算出来。
`peaks()` 用 ffmpeg 解成 4kHz 单声道再按桶取绝对值最大——一首 5 分钟的歌约 1.2M 采样，
算一次几十毫秒，结果按桶数缓存在任务目录里。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from ..bootstrap import find_ffmpeg
from ..fsutil import write_text_atomic

#: 解码超时（秒）。ffmpeg 卡住时以前会**永久**占着一个线程池线程（对比 `asr.py` 的 300s）。
PEAK_TIMEOUT_SECONDS = 300.0

#: 上传体检（G10）的解码超时：只是"能不能听"的亚秒级探测，别等太久。
PROBE_TIMEOUT_SECONDS = 20.0
#: 体检时最多解码多少秒（算静音用）。抽 20 秒足够判断是不是纯静音。
PROBE_SAMPLE_SECONDS = 20.0
#: 静音判定的 RMS 阈值（f32le 全量程 ±1.0）。低于它 = 基本没声音。
SILENCE_RMS_THRESHOLD = 1e-3

#: 波形包络的采样率。只要形状，不需要音质。
PEAK_SAMPLE_RATE = 4000
#: 默认桶数（和原型里 wave 有 160 根柱子不同：真实波形要密一些才好看）。
DEFAULT_BUCKETS = 1200
MIN_BUCKETS = 32
MAX_BUCKETS = 8000

MEDIA_TYPES = {
    ".flac": "audio/flac", ".wav": "audio/wav", ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/opus", ".aif": "audio/aiff",
    ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma", ".mp4": "audio/mp4",
}


def media_type(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def clamp_buckets(value: int | None) -> int:
    if not value:
        return DEFAULT_BUCKETS
    return max(MIN_BUCKETS, min(MAX_BUCKETS, int(value)))


def compute_peaks(audio: Path, buckets: int, *, sample_rate: int = PEAK_SAMPLE_RATE
                  ) -> dict[str, Any]:
    """把音频折成 ``buckets`` 个 0~1 的振幅。

    解码走 `-f f32le`（**不是** s16le）：轻量包里要塞一份几 MB 的裁剪版 ffmpeg，
    它 `--disable-everything` 之后只留了 `pcm_f32le` 这个 muxer（`s16le` 没留），
    而模型自己的解码路径用的也是 f32le —— 跟着它走，才能用那份小二进制。
    4kHz 单声道下浮点比 16 位只多一点点流量（5 分钟约 4.8MB vs 2.4MB），无所谓。

    ⚠️ **要自己 clip**：`-ac 1` 的下混是 L+R（不除 2），所以解出来的浮点会超过 1.0
    （实测《光芒》峰值 1.694、约 4% 采样越界）。以前走 s16le 时这些值在 16 位里**饱和**了，
    所以显示上一直是 0~1；现在改成 f32 就得显式 clip 回来，视觉与旧行为一致。
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，无法计算波形")
    command = [ffmpeg, "-v", "error", "-i", str(audio), "-ac", "1", "-ar",
               str(sample_rate), "-f", "f32le", "-"]
    # 子进程独立进程组 + 超时：Python 侧被杀时不会留下一个还在吃 CPU 的孤儿 ffmpeg。
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        result = subprocess.run(command, capture_output=True, check=False,
                                timeout=PEAK_TIMEOUT_SECONDS, **options)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"解码音频超过 {PEAK_TIMEOUT_SECONDS:.0f} 秒还没跑完，ffmpeg 可能卡住了"
        ) from exc
    if result.returncode:
        raise RuntimeError("解码音频失败："
                           + result.stderr.decode(errors="replace")[-200:].strip())

    import numpy as np

    samples = np.frombuffer(result.stdout, dtype="<f4")
    if samples.size == 0:
        return {"buckets": 0, "sample_rate": sample_rate, "duration": 0.0, "peaks": []}
    wave = np.clip(np.abs(samples.astype("float32")), 0.0, 1.0)
    if wave.size >= buckets:
        usable = (wave.size // buckets) * buckets
        peaks = wave[:usable].reshape(buckets, -1).max(axis=1)
    else:
        # 音频比桶还短：剩下的桶补 0，形状上仍然对齐（极短的测试音频会走到这里）
        peaks = np.zeros(buckets, dtype="float32")
        peaks[: wave.size] = wave
    return {
        "buckets": int(peaks.size),
        "sample_rate": sample_rate,
        "duration": round(float(samples.size) / sample_rate, 3),
        "peaks": [round(float(value), 4) for value in peaks],
    }


def _fingerprint(audio: Path) -> str:
    """源文件的指纹（大小 + mtime）：换了干声/换了原曲，波形缓存必须跟着作废。"""
    try:
        stat = audio.stat()
    except OSError:
        return ""
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def peaks_for(job_root: Path, audio: Path, buckets: int,
              cache_key: str = "mix") -> dict[str, Any]:
    """带磁盘缓存：同一个任务、同一档桶数、**同一条音频**只算一次。

    缓存里存着源文件指纹（G3）：`attach_stem` 换了干声、或者工程换了原曲之后，
    同名缓存自然失效——以前这里只按文件名做键，换干声之后界面画的还是上一条的波形。
    """
    cache = job_root / f"peaks-{cache_key}-{buckets}.json"
    fingerprint = _fingerprint(audio)
    if cache.is_file():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict) and cached.get("source") == fingerprint:
            return cached
        cache.unlink(missing_ok=True)
    data = dict(compute_peaks(audio, buckets))
    data["source"] = fingerprint
    try:
        write_text_atomic(cache, json.dumps(data))
    except OSError:
        pass                                    # 缓存写不进去不影响返回
    return data


# --------------------------------------------------------------------- 上传体检（G10）
def probe_audio(path: str | Path) -> dict[str, Any] | None:
    """上传后的**亚秒级**体检：只靠 ffmpeg 判断"是不是能播的音频、是不是纯静音"。

    不加载模型、不解码整首（抽前 20 秒算 RMS）。返回 ``None`` = 本机没有 ffmpeg，
    无法体检（退回只查后缀的老行为，不拦）。

    返回 dict：
      ``ok``         是否可当音频用（False = 读不出 / 纯静音）
      ``code``       失败时的错误码（`E_AUDIO_UNREADABLE` / `E_AUDIO_SILENT`）
      ``reason``     给人看的失败原因（中文）
      ``duration_seconds`` / ``sample_rate`` / ``silent`` / ``rms``：体检到的信息
      ``detail``     技术原文（ffmpeg stderr 尾部）
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None

    container = _probe_container(ffmpeg, path)
    if container is None or not container.get("has_audio"):
        tail = (container or {}).get("stderr", "")[-300:].strip()
        return {"ok": False, "code": "E_AUDIO_UNREADABLE",
                "reason": "读不出音频内容：可能不是有效的音频文件", "detail": tail}

    silence = _probe_silence(ffmpeg, path)
    out: dict[str, Any] = {
        "ok": True,
        "duration_seconds": container.get("duration_seconds"),
        "sample_rate": container.get("sample_rate"),
        "silent": silence["silent"],
        "rms": silence["rms"],
    }
    if silence["silent"]:
        out.update({"ok": False, "code": "E_AUDIO_SILENT",
                    "reason": "听起来是纯静音（没有内容可识别），换一首有声音的再传"})
    return out


def _probe_container(ffmpeg: str, path: str | Path) -> dict[str, Any] | None:
    """`ffmpeg -i` 只探测容器头（不真正解码），解析出时长 / 采样率 / 有没有音频流。"""
    command = [ffmpeg, "-hide_banner", "-i", str(path)]
    try:
        result = subprocess.run(command, capture_output=True, check=False,
                                timeout=PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"has_audio": False, "stderr": "ffmpeg 探测超时"}
    text = result.stderr.decode(errors="replace")
    info: dict[str, Any] = {"stderr": text, "has_audio": False}
    duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if duration:
        hours, minutes, seconds = duration.groups()
        info["duration_seconds"] = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    for line in text.splitlines():
        if "Stream #" in line and "Audio:" in line:
            info["has_audio"] = True
            sample_rate = re.search(r"(\d+)\s*Hz", line)
            if sample_rate:
                info["sample_rate"] = int(sample_rate.group(1))
            break
    return info if info["has_audio"] or info.get("duration_seconds") is not None else None


def _probe_silence(ffmpeg: str, path: str | Path) -> dict[str, Any]:
    """解码前 20 秒算 RMS：判断是不是纯静音。"""
    command = [ffmpeg, "-v", "error", "-i", str(path), "-t", str(PROBE_SAMPLE_SECONDS),
               "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
    try:
        result = subprocess.run(command, capture_output=True, check=False,
                                timeout=PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"silent": False, "rms": 0.0}
    if result.returncode != 0 or not result.stdout:
        return {"silent": False, "rms": 0.0}
    import numpy as np

    samples = np.frombuffer(result.stdout, dtype="<f4")
    if samples.size == 0:
        return {"silent": False, "rms": 0.0}
    rms = float(np.sqrt(np.mean(np.square(samples.astype("float32")))))
    return {"silent": rms < SILENCE_RMS_THRESHOLD, "rms": round(rms, 6)}


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """解析 ``Range: bytes=...``，返回闭区间 ``(start, end)``。

    - 没有 Range 头 -> ``None``（调用方回 200 全量）
    - 语法不认识或越界 -> 抛 :class:`ValueError`（调用方回 416）
    """
    if not header:
        return None
    spec = header.strip()
    if not spec.lower().startswith("bytes="):
        raise ValueError(f"不支持的 Range：{header!r}")
    first = spec[6:].split(",")[0].strip()
    start_text, _, end_text = first.partition("-")
    try:
        if not start_text:                      # bytes=-500：最后 500 字节
            length = int(end_text)
            if length <= 0:
                raise ValueError("后缀长度必须是正数")
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError as exc:
        raise ValueError(f"Range 解析失败：{header!r}") from exc
    end = min(end, size - 1)
    if start > end or start >= size:
        raise ValueError(f"Range 越界：{header!r}（文件只有 {size} 字节）")
    return start, end
