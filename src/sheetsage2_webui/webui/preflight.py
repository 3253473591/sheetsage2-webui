"""启动预检（G15）：把"缺 ffmpeg / 没下模型 / 没显卡 / 磁盘不够"在开跑前就摆到页面上。

以前这些都拖到用户点「开始」才炸（`launch.py` 只在**控制台**打一行提示，浏览器里什么都
看不到）。这里做一次 `preflight()`，结果进 `/api/env.preflight`，前端据此在顶栏画横幅：

| 级别 | 含义 | 例子 |
|---|---|---|
| `error` | 红色横幅，功能基本用不了 | 缺 ffmpeg / 数据目录写不进去 |
| `warn`  | 黄色横幅，某条功能缺一块 | 没下扒谱权重 / 没下歌词权重 |
| `info`  | 蓝色提示，能用但慢 | 没显卡，CPU 跑会很久 |
| `ok`    | 通过（前端默认不显示） | — |

这个模块**不 import 包内的模型层**（torch / transformers 都懒加载），启动时调用不会因为
缺权重就崩。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ..bootstrap import PROJECT_ROOT, find_ffmpeg

#: 扒谱权重目录（`resolve_model_path` 优先认它）。缺了会自动退到 HuggingFace 仓库 id。
SHEETSAGE_MODEL_DIR = PROJECT_ROOT / "models" / "SheetSage2"
#: 识别模型目录的前缀。**不能写死 1.7B**：最小集下的是 0.6B，1.7B 是可选项，
#: 写死会让只装了最小集的机器误报「歌词权重不完整」。
ASR_MODEL_GLOB = "Qwen3-ASR-*"
#: 逐字对齐器权重（Qwen 版，只有日语才需要）。
ALIGNER_MODEL_DIR = PROJECT_ROOT / "models" / "Qwen3-ForcedAligner-0.6B"
#: 随包带的 TIFA 对齐器（中/粤/英）；在它就在，不必再下 Qwen 对齐器。
TIFA_MODEL_DIR = PROJECT_ROOT / "models" / "TIFA-1.0"

#: 磁盘低于这个字节数就变黄。
DISK_WARN_BYTES = 2 * 1024 ** 3


def _has_model(directory: Path) -> bool:
    return (directory / "config.json").is_file()


def asr_model_dirs() -> list[Path]:
    """本机实际装了的识别模型（`models/Qwen3-ASR-0.6B` / `-1.7B` …）。"""
    root = PROJECT_ROOT / "models"
    return sorted(path for path in root.glob(ASR_MODEL_GLOB) if _has_model(path))


def cuda_available() -> bool | None:
    """有没有可用的 CUDA。返回 None = 读不到（torch 没装/初始化失败），当"无显卡"处理。

    `JobManager` 拿它把「设备 = 自动」解析成实际口径，好让耗时预估（G20）的系数选对——
    CPU 上扒谱的系数是显卡的十几倍，估错了那句话就没意义了。
    """
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:                                   # noqa: BLE001 - 预检不该崩
        return None


def _disk_free(data_root: Path) -> int:
    try:
        return shutil.disk_usage(data_root).free
    except OSError:
        return 0


def _writable(data_root: Path) -> bool:
    """数据目录能不能写（先真写一个临时文件再删，比只看权限可靠）。"""
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        probe = data_root / ".preflight-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def preflight(data_root: str | Path | None = None) -> list[dict[str, Any]]:
    """返回一组检查结果（顺序即优先级，前端按第一个 error/warn 决定横幅主色）。"""
    root = Path(data_root) if data_root is not None else PROJECT_ROOT / "output"
    checks: list[dict[str, Any]] = []

    # 1. ffmpeg：扒谱/波形/SVP 都靠它，缺了基本用不了。
    if find_ffmpeg():
        checks.append({"key": "ffmpeg", "level": "ok", "title": "ffmpeg 就绪"})
    else:
        checks.append({
            "key": "ffmpeg", "level": "error",
            "title": "缺 ffmpeg：扒谱、波形、SVP 导出都会用不了",
            "detail": "跑一次 01_install.bat，或把 ffmpeg 加到 PATH 后重启服务",
        })

    # 2. 数据目录可写：这个最致命，先测。
    if _writable(root):
        checks.append({"key": "writable", "level": "ok", "title": "数据目录可写"})
    else:
        checks.append({
            "key": "writable", "level": "error",
            "title": f"数据目录写不进去：{root}",
            "detail": "检查杀毒软件/权限，或换一个 SHEETSAGE_WEBUI_DATA 目录",
        })

    # 3. 扒谱权重（缺了会退回 HuggingFace 仓库 id，离线时失败）。
    if _has_model(SHEETSAGE_MODEL_DIR):
        checks.append({"key": "sheetsage", "level": "ok", "title": "扒谱权重就绪"})
    else:
        checks.append({
            "key": "sheetsage", "level": "warn",
            "title": "还没下载扒谱权重（SheetSage2）",
            "detail": "点这里下载；没网的话扒谱会失败",
        })

    # 4. 歌词权重（可选：缺了只影响歌词识别）。
    #    识别模型认 `Qwen3-ASR-*`（最小集 0.6B，1.7B 可选）；对齐器有两种——
    #    随包的 TIFA（中/粤/英）和 Qwen3-ForcedAligner（日语才要）。
    asr_models = asr_model_dirs()
    aligner_ready = _has_model(TIFA_MODEL_DIR) or _has_model(ALIGNER_MODEL_DIR)
    if asr_models and aligner_ready:
        checks.append({"key": "lyrics_model", "level": "ok", "title": "歌词识别权重就绪"})
    else:
        missing: list[str] = []
        if not asr_models:
            missing.append("识别模型（Qwen3-ASR）")
        if not aligner_ready:
            missing.append("逐字对齐器")
        checks.append({
            "key": "lyrics_model", "level": "warn",
            "title": "歌词识别权重不完整：" + "、".join(missing),
            "detail": ("跑一次 01_install.bat 把模型下齐（最小集 4.3 GB，对齐器随包带）；"
                       "只想扒谱可以忽略"),
        })

    # 5. 显卡：没卡就 CPU 跑，会很慢，提前说一声。
    cuda = cuda_available()
    if cuda:
        checks.append({"key": "cuda", "level": "ok", "title": "检测到显卡（CUDA）"})
    else:
        checks.append({
            "key": "cuda", "level": "info",
            "title": "没检测到显卡，会用 CPU 跑（一首歌约 30~60 分钟）",
            "detail": "把「设备」留在自动即可；有 N 卡的话检查驱动",
        })

    # 6. 磁盘剩余：< 2GB 变黄。
    free = _disk_free(root)
    if free >= DISK_WARN_BYTES:
        checks.append({"key": "disk", "level": "ok", "title": "磁盘空间充足"})
    elif free > 0:
        checks.append({
            "key": "disk", "level": "warn",
            "title": f"磁盘剩余不多（约 {free / 1024 ** 3:.1f} GB）",
            "detail": "跑几首歌可能就满了，建议先清理",
        })
    else:
        checks.append({
            "key": "disk", "level": "error",
            "title": "读不到磁盘剩余空间",
            "detail": "阶段开跑前会再做一次预检并拦住",
        })

    return checks
