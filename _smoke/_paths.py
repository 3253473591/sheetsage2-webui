"""``_smoke/`` 与 ``packaging/`` 脚本共用的「本机资源」解析。

这些脚本要指向 ffmpeg、几首测试音频、以及别人机器上根本没有的 SVP 参考样本。
为了不把作者机器的绝对路径写进仓库，统一从这里取：

    **环境变量 → 项目根 ``local_paths.json``（不入库）→ 中性默认值**

``local_paths.json`` 里可选的键（都缺省也能跑，只是相关用例会跳过或走 PATH）::

    {
      "smoke_ffmpeg":      "D:\\\\ffmpeg\\\\bin\\\\ffmpeg.exe",
      "smoke_samples":     "D:\\\\...\\\\demo_audio",
      "smoke_audio":       "D:\\\\...\\\\demo_audio\\\\song.mp3",
      "smoke_song_a":      "D:\\\\...\\\\demo_audio\\\\a.mp3",
      "smoke_song_b":      "D:\\\\...\\\\demo_audio\\\\b.mp3",
      "smoke_extra_audio": "C:\\\\Users\\\\me\\\\Downloads\\\\x.flac",
      "build_models_source": "D:\\\\...\\\\models",
      "build_ffmpeg_source": "D:\\\\ffmpeg\\\\bin\\\\ffmpeg.exe"
    }

用法（脚本在 ``_smoke/`` 下，直接 ``import _paths`` 即可）::

    from _paths import FFMPEG, AUDIO, SONG_A, SONG_B, EXTRA_AUDIO
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LOCAL_PATHS_FILE = ROOT / "local_paths.json"


def _local(key: str) -> str | None:
    """从 ``local_paths.json`` 取一个键；文件不存在或格式不对时返回 None。"""
    try:
        with _LOCAL_PATHS_FILE.open(encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get(key)
    return str(raw) if raw else None


def _resolve(key: str, env_name: str) -> str | None:
    return os.environ.get(env_name) or _local(key)


#: ffmpeg：环境变量 → local_paths.json → PATH → 字面量 "ffmpeg"
FFMPEG: str = _resolve("smoke_ffmpeg", "SMOKE_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"

#: 测试音频目录（默认 <项目根>/samples，通常不存在）
SAMPLES: Path = Path(
    _resolve("smoke_samples", "SMOKE_SAMPLES") or (ROOT / "samples")
).expanduser()


def sample(name: str) -> str:
    """取测试音频的绝对路径（字符串，方便直接喂给 subprocess / Playwright）。"""
    return str(SAMPLES / name)


def _song(key: str, env_name: str, fallback_name: str) -> str:
    return _resolve(key, env_name) or sample(fallback_name)


#: 「随便一首测试歌」——填歌词高亮 / 歌词 LRC 用
AUDIO: str = _song("smoke_audio", "SMOKE_AUDIO", "song.mp3")

#: 需要「同一会话连跑两首不同的歌」的用例
SONG_A: str = _song("smoke_song_a", "SMOKE_SONG_A", "song_a.mp3")
SONG_B: str = _song("smoke_song_b", "SMOKE_SONG_B", "song_b.mp3")

#: 额外的一首真实样本；**没配就是 None**，调用方应跳过该用例
EXTRA_AUDIO: str | None = _resolve("smoke_extra_audio", "SMOKE_EXTRA_AUDIO")
