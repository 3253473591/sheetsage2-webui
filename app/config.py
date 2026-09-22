"""全局配置与设置持久化。

路径解析原则（2026-09 整合包改造）
----------------------------------
一切外部资源都按 **环境变量 → 包内相对路径 → 作者机器历史路径** 三级解析，
这样同一个 ``app/`` 既能在这台开发机上直接跑，也能在别人的整合包里解压即用：

============  ==============================  =================================
资源          环境变量                        包内位置
============  ==============================  =================================
SheetSage2    ``SHEETSAGE2_MODEL_DIR``        ``models/SheetSage2``
MERT 主干     ``MERT_MODEL_DIR``              ``models/MERT-v2-FullSong``
ffmpeg        ``FFMPEG_BIN``（可执行文件）     ``runtime/ffmpeg/bin/ffmpeg.exe``
解释器        —                               ``runtime/python/python.exe``
============  ==============================  =================================

历史路径（开发机上模型放在本项目之外的公共目录时用）由**不入库的**
``local_paths.json`` 或环境变量提供，只在候选都不存在时兜底；
源码分发（别人的机器上）没有这个文件，会正常走前面的包内路径或环境变量。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 目录
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent

#: 整合包随包资源（开发目录里通常不存在）
RUNTIME_DIR = ROOT / "runtime"
MODELS_DIR = ROOT / "models"

#: 本机路径覆盖表，**不入 git**（见 ``.gitignore``）。格式::
#:
#:     {"legacy_models": "D:\\path\\to\\models", "svp_reference": "D:\\path\\to\\svp"}
#:
#: 存在的意义：开发机上模型权重可能放在本项目之外的公共目录，不想把作者机器
#: 的绝对路径写进源码。没有这个文件（别人克隆仓库的情况）就走包内 ``models/``
#: 或环境变量，行为与原来一致。
_LOCAL_PATHS_FILE = ROOT / "local_paths.json"


def _local_paths() -> dict[str, Any]:
    """读取本机路径覆盖表；文件不存在或格式不对时返回空表（绝不抛异常）。"""
    try:
        with _LOCAL_PATHS_FILE.open(encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _optional_path(key: str, *env_names: str) -> Path | None:
    """按「环境变量 → 本机路径覆盖表」取一个可选目录；都没有则返回 None。"""
    for name in env_names:
        raw = os.environ.get(name)
        if raw:
            return Path(raw).expanduser()
    raw = _local_paths().get(key)
    return Path(str(raw)).expanduser() if raw else None


def resolve_dir(env_name: str, *candidates: str | Path) -> Path:
    """按「环境变量 > 第一个存在的候选 > 第一个候选」解析一个资源目录。

    永远返回 Path（不抛异常），缺失由 ``envcheck`` 统一报告，而不是在 import 期炸掉。
    """
    raw = os.environ.get(env_name)
    if raw:
        return Path(raw).expanduser()
    for cand in candidates:
        try:
            if Path(cand).exists():
                return Path(cand)
        except OSError:
            continue
    return Path(candidates[0])


#: SheetSage2 权重 + 推理代码（明文可读，含 infer.py）
_LEGACY_MODELS = _optional_path("legacy_models", "SHEETSAGE2_LEGACY_MODELS")

SHEETSAGE2_DIR = resolve_dir(
    "SHEETSAGE2_MODEL_DIR",
    MODELS_DIR / "SheetSage2",
    *([_LEGACY_MODELS / "SheetSage2"] if _LEGACY_MODELS else []),
)
#: MERT-v2 主干
MERT_FULLSONG_DIR = resolve_dir(
    "MERT_MODEL_DIR",
    MODELS_DIR / "MERT-v2-FullSong",
    *([_LEGACY_MODELS / "MERT-v2-FullSong"] if _LEGACY_MODELS else []),
)
#: SVP 参考样本目录（仅用于校验，不在运行时依赖）
SVP_REFERENCE_DIR = resolve_dir(
    "SVP_REFERENCE_DIR",
    _optional_path("svp_reference", "SVP_REFERENCE_DIR") or (ROOT / "svp-reference"),
)

WEB_DIR = ROOT / "web"
OUTPUT_DIR = ROOT / "output"
TMP_DIR = ROOT / "tmp"
SETTINGS_FILE = ROOT / "settings.json"

# SheetSage2 的信任代码目录要能被 `import` 到
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------
HOST = "127.0.0.1"  # 仅本机，不暴露公网（文档 3.1）
DEFAULT_PORT = 8777

#: 上传限制（文档 4.5）
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".flac"}
MAX_UPLOAD_MB = 300

#: 任务无进度看门狗（秒，文档 4.3）
WATCHDOG_IDLE_SECONDS = 600
#: 停止时等待优雅退出的时间，超时强杀（文档 4.4）
STOP_GRACE_SECONDS = 8.0

#: 显存低于该值（MiB）时警告/拒绝（文档 5.2）
MIN_FREE_GPU_MIB = 2500

# --------------------------------------------------------------------------
# 阶段 → 百分比区间（文档 3.6.3，逐字对齐的 5 项阶段名）
# --------------------------------------------------------------------------
STAGE_ORDER = ("加载模型", "读取音频", "识别音乐", "生成乐谱", "完成")

#: 阶段名 → (起, 止) 百分比
STAGE_RANGES: dict[str, tuple[int, int]] = {
    "加载模型": (0, 2),
    "读取音频": (2, 5),
    # 5–30% 预留给「人声分离 / 提取」；SheetSage2 原生完成人声处理，
    # 该区间直接跳过（文档 3.6.3：未启用分离时直接跳到 30%）。
    "识别音乐": (30, 70),
    "生成乐谱": (70, 95),
    "完成": (100, 100),
}

#: 跳过的百分比区间（人声分离），用于诊断信息说明
SKIPPED_RANGES = (("人声分离 / 提取", 5, 30, "SheetSage2 原生完成人声处理，未启用 Demucs/UVR"),)

# --------------------------------------------------------------------------
# 设置（文档 3.3 设置页）
# --------------------------------------------------------------------------
#: ``default_device`` 取值：``auto``（有 N 卡走 GPU，否则自动降级 CPU）/ ``cuda`` / ``cpu``。
DEFAULT_SETTINGS: dict[str, Any] = {
    "output_dir": str(OUTPUT_DIR),
    "default_preset": "default",
    "default_device": "auto",
    "default_dtype": "bf16",
    "default_only_melody": False,
    "default_drop_intro_outro": False,
    "default_max_seconds": None,
    "theme": "light",
    "port": DEFAULT_PORT,
    "max_upload_mb": MAX_UPLOAD_MB,
    "open_browser": True,
}


def load_settings() -> dict[str, Any]:
    """读取设置；缺失或损坏时回落到默认值。"""
    data = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return data


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """合并并写回设置。"""
    data = load_settings()
    data.update({k: v for k, v in (patch or {}).items() if k in DEFAULT_SETTINGS})
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def ensure_dirs() -> None:
    """确保运行所需目录存在。"""
    for d in (OUTPUT_DIR, TMP_DIR, WEB_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _exe_names(stem: str) -> tuple[str, ...]:
    """同一个可执行文件在不同平台上的名字。"""
    return (f"{stem}.exe", stem) if os.name == "nt" else (stem, f"{stem}.exe")


def ffmpeg_path() -> str | None:
    """定位 ffmpeg 可执行文件。

    音频解码统一走 ffmpeg 而不是 ``soundfile``：实测 ``librosa.load`` 对部分 mp3
    会抛 ``LibsndfileError: Unspecified internal error``，而 ffmpeg 对各种容器/编码
    都能吃。项目本来就依赖 ffmpeg（SheetSage2 转码也用）。

    查找顺序：``FFMPEG_BIN`` 环境变量 → 包内 ``runtime/ffmpeg`` → PATH →
    ``FFMPEG_DIR`` 环境变量 / 本机路径覆盖表 → 常见安装位置。
    """
    raw = os.environ.get("FFMPEG_BIN")
    if raw and Path(raw).expanduser().is_file():
        return str(Path(raw).expanduser())

    for folder in (RUNTIME_DIR / "ffmpeg" / "bin", ROOT / "ffmpeg" / "bin"):
        for name in _exe_names("ffmpeg"):
            candidate = folder / name
            if candidate.is_file():
                return str(candidate)

    found = shutil.which("ffmpeg")
    if found:
        return found
    # 最后兜底：常见安装位置。``C:\ffmpeg\bin`` 是社区惯例，跟具体哪台机器无关；
    # 作者/使用者自己的 ffmpeg 目录走 FFMPEG_DIR 或本机路径覆盖表，不进源码。
    candidates: list[str] = []
    extra = _optional_path("ffmpeg_dir", "FFMPEG_DIR")
    if extra:
        candidates += [str(extra / name) for name in _exe_names("ffmpeg")]
    candidates.append(str(Path(r"C:\ffmpeg\bin") / _exe_names("ffmpeg")[0]))
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


def ffprobe_path() -> str | None:
    """定位 ffprobe（读时常用）。

    与 :func:`ffmpeg_path` 同源：包内 ``runtime/ffmpeg/bin/ffprobe[.exe]`` 优先，
    这样测试者机器上没装 ffmpeg 也能显示音频时长。
    """
    raw = os.environ.get("FFPROBE_BIN")
    if raw and Path(raw).expanduser().is_file():
        return str(Path(raw).expanduser())
    for name in _exe_names("ffprobe"):
        bundled = RUNTIME_DIR / "ffmpeg" / "bin" / name
        if bundled.is_file():
            return str(bundled)
    ff = ffmpeg_path()
    if ff:
        for name in _exe_names("ffprobe"):
            sibling = Path(ff).with_name(name)
            if sibling.is_file():
                return str(sibling)
    return shutil.which("ffprobe")


def ensure_ffmpeg_on_path() -> str | None:
    """把随包 ffmpeg 所在目录塞进 ``PATH``，让**写死 ``"ffmpeg"`` 的代码**也能找到它。

    ⚠️ 这个函数不是锦上添花，是必须的。原因：

    SheetSage2 的 vendored 代码 ``audio_sheetsage2.py::load_audio`` 里是这么写的::

        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is required to read audio files; "
                               "install it and add it to PATH")
        command = ["ffmpeg", "-v", "error", ...]
        subprocess.run(command, ...)

    也就是说它**只认 PATH**，完全不看我们自己的 :func:`ffmpeg_path()`。
    偏偏环境检测（``envcheck``）、时长探测（``server._probe_duration``）、
    八度校正（``octave.py``）走的都是 ``ffmpeg_path()`` —— 于是会出现
    「环境面板显示 ffmpeg 绿勾，一提交任务却报找不到 ffmpeg」这种自相矛盾的现象。

    开发机上一直没暴露，只是因为开发机恰好把 ffmpeg 装在了系统 PATH 里；
    换成任何没装过 ffmpeg 的机器（也就是绝大多数测试者）立刻就炸。

    只在 ``PATH`` 上追加包内目录、且 ``ffmpeg_path()`` 已经把包内路径排在
    PATH 查找之前，所以随包版本优先，行为可预期。
    """
    exe = ffmpeg_path()
    if not exe:
        return None
    folder = str(Path(exe).parent)
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if folder not in parts:
        os.environ["PATH"] = os.pathsep.join([folder, *parts])
    return exe


#: 模块导入即生效：worker / server 一启动就把随包 ffmpeg 接上 PATH。
#: 放在这里（而不是各入口函数里）是因为真正调用 ffmpeg 的是**很深处的 vendored 代码**，
#: 只有 import 期注入才能保证覆盖所有调用路径。
ensure_ffmpeg_on_path()


def python_exe() -> Path:
    """运行本项目的解释器。

    整合包内是 ``runtime/python/python.exe``（便携版，随包分发）；
    开发机上则是 ``.venv\\Scripts\\python.exe``。 ``sys.executable`` 在两者下都正确，
    这里只是提供一个不依赖 ``sys`` 的展示/诊断用值。
    """
    bundled = RUNTIME_DIR / "python" / "python.exe"
    if bundled.is_file():
        return bundled
    return ROOT / ".venv" / "Scripts" / "python.exe"


def describe_paths() -> dict[str, Any]:
    """汇总关键路径与存在性，供环境检测 / 自检脚本直接打印。"""
    checks: list[tuple[str, str, Path]] = [
        ("解释器", "python_exe", python_exe()),
        ("SheetSage2 权重", "SHEETSAGE2_DIR", SHEETSAGE2_DIR),
        ("MERT-v2-FullSong", "MERT_FULLSONG_DIR", MERT_FULLSONG_DIR),
    ]
    out: dict[str, Any] = {"root": str(ROOT)}
    for label, key, path in checks:
        out[key] = {"label": label, "path": str(path), "exists": path.exists()}
    ff = ffmpeg_path()
    out["ffmpeg"] = {"label": "ffmpeg", "path": ff, "exists": bool(ff)}
    fp = ffprobe_path()
    out["ffprobe"] = {"label": "ffprobe", "path": fp, "exists": bool(fp)}
    return out
