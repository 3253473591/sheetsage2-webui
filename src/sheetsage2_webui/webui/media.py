"""音频与波形：给界面播放、画波形用。

界面上两条音轨都要画波形，浏览器只知道 `<audio>`，所以要后端把包络算出来。
`peaks()` 用 ffmpeg 解成 4kHz 单声道再按桶取绝对值最大——一首 5 分钟的歌约 1.2M 采样，
算一次几十毫秒，结果按桶数缓存在任务目录里。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ..bootstrap import find_ffmpeg

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
    result = subprocess.run(command, capture_output=True, check=False)
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


def peaks_for(job_root: Path, audio: Path, buckets: int,
              cache_key: str = "mix") -> dict[str, Any]:
    """带磁盘缓存：同一个任务、同一档桶数、同一条音源只算一次。"""
    cache = job_root / f"peaks-{cache_key}-{buckets}.json"
    if cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache.unlink(missing_ok=True)
    data = compute_peaks(audio, buckets)
    try:
        cache.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass                                    # 缓存写不进去不影响返回
    return data


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
