"""进程级环境准备。

必须在 `import torch` 之前调用。本机 conda base 环境自带一份 `libiomp5md.dll`，
而 torch 又静态链接了另一份；两者同时初始化会让解释器在 `import torch` 直接
以 `OMP: Error #15` 中止。官方给出的开关是 `KMP_DUPLICATE_LIB_OK`。

SheetSage2 的默认推理路径还需要 PATH 里有 `ffmpeg` 可执行文件（它用
`ffmpeg -i ... -f f32le pipe:1` 解码音频）。这里按优先级查找并写回 PATH，
让调用方不必手工配置。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 随包自带的 ffmpeg 放哪（**内测包就是靠它**：解压即用，不依赖同好机器上有没有装）。
#: 打包脚本 `scripts/build_alpha_package.py` 会把裁剪版静态 ffmpeg 放到第一个位置。
BUNDLED_FFMPEG_DIRS = (
    PROJECT_ROOT / "runtime" / "ffmpeg" / "bin",
    PROJECT_ROOT / "runtime" / "ffmpeg",
)

#: 开发机上的兜底位置（内置包找不到才轮到它们）。
#: 注意：本机的 ffmpeg 和项目**不在一起**（`D:\ffmpeg\bin`），所以这两条只在开发机上有效。
FFMPEG_DIR_CANDIDATES = (
    Path(r"D:\ffmpeg\bin"),
    Path(r"C:\ffmpeg\bin"),
)


def bundled_ffmpeg() -> str | None:
    """随包自带的那份 ffmpeg（没有就 None）。"""
    for directory in BUNDLED_FFMPEG_DIRS:
        exe = directory / "ffmpeg.exe"
        if exe.is_file():
            return str(exe)
    return None


def ensure_kmp_guard() -> None:
    """允许同一进程内加载多份 OpenMP 运行时（conda + torch）。"""
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")


def find_ffmpeg() -> str | None:
    """返回可用的 ffmpeg 路径，找不到返回 None。

    优先级：**环境变量 → 随包自带 → PATH → 开发机固定路径**。
    随包自带排在 PATH 前面是有意的：内测包带的是实测过的那份裁剪版静态构建，
    不该被同好机器上某个来路不明的 ffmpeg 顶掉（版本/编解码器对不上很难查）。
    """
    override = os.environ.get("SHEETSAGE_FFMPEG_DIR")        # 给目录（文档里那一个）
    if override:
        exe = Path(override) / "ffmpeg.exe"
        if exe.is_file():
            return str(exe)
    explicit = os.environ.get("SHEETSAGE_FFMPEG")            # 直接给 exe（prepare() 自己写的）
    if explicit and Path(explicit).is_file():
        return explicit

    found = bundled_ffmpeg()
    if found:
        _prepend_path(Path(found).parent)
        return found

    found = shutil.which("ffmpeg")
    if found:
        return found

    for directory in FFMPEG_DIR_CANDIDATES:
        exe = directory / "ffmpeg.exe"
        if exe.is_file():
            _prepend_path(directory)
            return str(exe)
    return None


def _prepend_path(directory: Path) -> None:
    """把 ffmpeg 所在目录塞到 PATH 最前面（SheetSage2 自己是按 PATH 找 ffmpeg 的）。"""
    text = str(directory)
    current = os.environ.get("PATH", "")
    if text not in current.split(os.pathsep):
        os.environ["PATH"] = text + os.pathsep + current


def sync_remote_code_cache(model_dir: Path) -> int:
    """把本地模型目录里的 `.py` 全部补齐到 transformers 的模块缓存。

    transformers 4.57.6 对**本地目录**走 `trust_remote_code` 时，只复制它当前那一步需要的
    几个文件；等模型跑到后面再 `import` 别的模块就会 `FileNotFoundError`
    （实测缺 `chord_spelling_sheetsage2.py`，`exports_sheetsage2.py` 会用到它）。
    用仓库 id 加载不会有这个问题——那时的缓存是整个仓库。这里直接把源码补齐，抹平差异。

    返回补了/校对了几个文件。
    """
    import shutil

    # 用 transformers 自己的常量，别自己拼路径——版本之间规则变过。
    # 注意要带上 `transformers_modules` 这一层，否则会同步到隔壁目录去。
    from transformers.utils.hub import HF_MODULES_CACHE, TRANSFORMERS_DYNAMIC_MODULE_NAME

    # 子目录名就是模型目录的名字（连字符和点换成下划线）
    name = model_dir.name.replace("-", "_").replace(".", "_")
    target = Path(HF_MODULES_CACHE) / TRANSFORMERS_DYNAMIC_MODULE_NAME / name
    sources = [p for p in model_dir.rglob("*.py") if "__pycache__" not in p.parts]
    if not sources:
        return 0

    target.mkdir(parents=True, exist_ok=True)
    synced = 0
    for source in sources:
        relative = source.relative_to(model_dir)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if (not destination.is_file()
                or destination.stat().st_size != source.stat().st_size):
            shutil.copy2(source, destination)
            synced += 1
    return synced


def ensure_utf8_stdio() -> None:
    """让 print 走 UTF-8。

    Windows 控制台默认按本地代码页（cp936）编码 stdout，中文日志被转发到
    终端或日志文件时会变成乱码。这里显式改成 UTF-8，写文件的部分本来就已经
    是 UTF-8，两边保持一致。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def huggingface_cache_dir() -> Path:
    """HuggingFace 缓存放哪：**默认落在工程目录下**，不用机器级的 `HF_HOME`。

    为什么：`trust_remote_code` 每次加载 SheetSage2 都会往
    `<HF_HOME>/modules/transformers_modules/...` 写 .py。机器级 `HF_HOME`（比如
    `D:\\cache`）有两个坑——多个安装共用同一份缓存时，两个进程同时写同一个文件会
    报 `PermissionError: [Errno 13]`；换台机器路径也可能不可写。
    放在工程目录里，内测包就是自包含的。
    """
    return PROJECT_ROOT / ".cache" / "huggingface"


def ensure_hf_cache() -> None:
    """把 HF 的缓存钉在工程目录下（`SHEETSAGE_KEEP_HF_HOME=1` 时尊重外部设置）。

    实测踩过：机器级 `HF_HOME=D:\\cache` 被两个安装共用，两个进程同时往
    `modules/transformers_modules/SheetSage2/modeling_sheetsage2.py` 写就会报
    `PermissionError: [Errno 13]`（Windows 的共享冲突）。模型权重我们自己下到
    `models/`，HF 这边只是模块缓存，钉到工程里既不会串，也符合"解压即用"。
    """
    if os.environ.get("SHEETSAGE_KEEP_HF_HOME"):
        return
    root = huggingface_cache_dir()
    os.environ["HF_HOME"] = str(root)
    os.environ["HF_MODULES_CACHE"] = str(root / "modules")
    try:
        (root / "modules").mkdir(parents=True, exist_ok=True)
    except OSError:
        # 建不出来就算了：让 transformers 自己报它那个更明确的错
        pass


def prepare() -> dict[str, str | None]:
    """幂等地准备好运行环境，返回可直接打印的诊断信息。"""
    ensure_kmp_guard()
    ensure_utf8_stdio()
    ensure_hf_cache()
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        os.environ["SHEETSAGE_FFMPEG"] = ffmpeg
    return {"ffmpeg": ffmpeg, "project_root": str(PROJECT_ROOT),
            "hf_home": os.environ.get("HF_HOME")}
