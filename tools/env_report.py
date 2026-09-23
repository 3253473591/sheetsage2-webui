"""环境快照 / 自检报告生成器（整合包售后专用）。

用法（包根目录）::

    runtime\\python\\python.exe tools\\env_report.py

会往包根目录写一份 UTF-8 的 ``环境快照.txt``，同时打印到控制台。
测试者遇到任何问题，只要把这份文件整个粘给 AI，对方就能知道：
这台机器有什么、缺什么、权重在哪、torch/CUDA 是什么版本、跑过什么。

设计约束
--------
* **绝不在本进程里 import torch**：import 一次就要 1–2 秒，还会初始化 CUDA
  上下文、常驻占显存（与 ``app/envcheck.py`` 的理由相同）。一律走子进程探测。
* **绝不抛异常退出**：自检脚本自己崩掉是最没用的自检脚本。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 允许以 `python tools\env_report.py` 的方式直接运行
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, OSError):
    pass

REPORT_NAME = "环境快照.txt"


def _section(title: str) -> str:
    return f"\n{'=' * 70}\n{title}\n{'=' * 70}\n"


def _run(cmd: list[str], timeout: float = 20.0) -> str:
    """执行外部命令并返回合并后的输出（失败也返回可读文本，不抛异常）。"""
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"<执行失败：{type(exc).__name__}: {exc}>"
    text = ((out.stdout or "") + (out.stderr or "")).strip()
    return text or f"<无输出，退出码 {out.returncode}>"


def _memory_gb() -> str:
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MemStatus()
        stat.dwLength = ctypes.sizeof(_MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        total = stat.ullTotalPhys / 1024**3
        avail = stat.ullAvailPhys / 1024**3
        return f"共 {total:.1f} GB，可用 {avail:.1f} GB"
    except Exception as exc:  # noqa: BLE001
        return f"<读取失败：{type(exc).__name__}: {exc}>"


def _disk_free() -> str:
    lines = []
    for drive in ("C:\\", "D:\\", "E:\\"):
        try:
            if Path(drive).exists():
                usage = shutil.disk_usage(drive)
                lines.append(f"  {drive} 可用 {usage.free / 1024**3:.1f} GB / 共 {usage.total / 1024**3:.1f} GB")
        except OSError:
            continue
    return "\n".join(lines) or "  <读不到>"


def collect() -> str:
    parts: list[str] = []
    parts.append("SheetSage2 扒谱 · 环境快照")
    parts.append(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    parts.append(f"包根目录：{ROOT}")

    # ---------------- 版本信息 ----------------
    parts.append(_section("1. 整合包版本"))
    info_file = ROOT / "版本信息.json"
    if info_file.is_file():
        try:
            parts.append(info_file.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeDecodeError) as exc:
            parts.append(f"<版本信息.json 读取失败：{exc}>")
    else:
        parts.append("<没有 版本信息.json — 可能是开发目录或文件被删了>")

    # ---------------- 操作系统 ----------------
    parts.append(_section("2. 操作系统与硬件"))
    parts.append(f"系统      ：{platform.platform()}")
    parts.append(f"版本      ：{platform.version()}")
    parts.append(f"架构      ：{platform.machine()}")
    parts.append(f"CPU       ：{platform.processor()}（{os.cpu_count()} 逻辑核）")
    parts.append(f"内存      ：{_memory_gb()}")
    parts.append("磁盘：")
    parts.append(_disk_free())

    # ---------------- Python ----------------
    parts.append(_section("3. Python"))
    parts.append(f"解释器    ：{sys.executable}")
    parts.append(f"版本      ：{sys.version.replace(chr(10), ' ')}")
    parts.append(f"sys.prefix：{sys.prefix}")

    # ---------------- GPU / 驱动 ----------------
    parts.append(_section("4. 显卡与驱动（nvidia-smi）"))
    smi = shutil.which("nvidia-smi") or r"C:\WINDOWS\system32\nvidia-smi.exe"
    if Path(smi).exists():
        parts.append(f"路径      ：{smi}")
        parts.append(_run([smi, "--query-gpu=name,driver_version,memory.total,memory.used,memory.free",
                           "--format=csv"]))
    else:
        parts.append("<没有 nvidia-smi：这台机器大概率没有 NVIDIA 显卡（或驱动没装）>")

    # ---------------- torch / CUDA ----------------
    parts.append(_section("5. PyTorch / CUDA（子进程探测，避免占用显存）"))
    try:
        from app import envcheck

        probe = envcheck.probe_torch()
        parts.append(json.dumps(probe, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        parts.append(f"<探测失败：{type(exc).__name__}: {exc}>")

    # ---------------- 关键路径 ----------------
    parts.append(_section("6. 关键路径（权重 / 运行时 / ffmpeg）"))
    try:
        from app import config

        paths = config.describe_paths()
        for key, item in paths.items():
            if not isinstance(item, dict):
                continue
            flag = "存在" if item.get("exists") else "**缺失**"
            parts.append(f"[{flag}] {item.get('label')}\n         {item.get('path')}")

        for label, path in (
            ("SheetSage2/model.safetensors", config.SHEETSAGE2_DIR / "model.safetensors"),
            ("SheetSage2/config.json", config.SHEETSAGE2_DIR / "config.json"),
            ("MERT/model.safetensors", config.MERT_FULLSONG_DIR / "model.safetensors"),
        ):
            if path.is_file():
                size_gb = path.stat().st_size / 1024**3
                parts.append(f"[存在] {label}  {size_gb:.2f} GB")
            else:
                parts.append(f"[**缺失**] {label}  ({path})")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"<路径检查失败：{type(exc).__name__}: {exc}>")

    # ---------------- ffmpeg ----------------
    parts.append(_section("7. ffmpeg"))
    try:
        from app import config as _cfg

        ff = _cfg.ffmpeg_path()
        parts.append(f"路径：{ff or '<未找到>'}")
        if ff:
            parts.append(_run([ff, "-version"]).splitlines()[0])
    except Exception as exc:  # noqa: BLE001
        parts.append(f"<检查失败：{type(exc).__name__}: {exc}>")

    # ---------------- 环境检测汇总 ----------------
    parts.append(_section("8. 内置环境检测（app.envcheck）"))
    try:
        from app import envcheck

        result = envcheck.run_checks(include_torch=False)
        for item in result["checks"]:
            parts.append(
                f"[{item['status']:4}] {item['label']}：{item.get('value') or ''}"
                + (f"\n        说明：{item['detail']}" if item.get("detail") else "")
                + (f"\n        建议：{item['fix']}" if item.get("fix") else "")
            )
        parts.append(f"\n汇总：{result['summary']}  可开工={result['ready']}")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"<检测失败：{type(exc).__name__}: {exc}>")

    # ---------------- 设置与产物 ----------------
    parts.append(_section("9. settings.json"))
    settings_file = ROOT / "settings.json"
    if settings_file.is_file():
        try:
            parts.append(settings_file.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeDecodeError) as exc:
            parts.append(f"<读取失败：{exc}>")
    else:
        parts.append("<不存在 — 会用默认设置（端口 8777）>")

    parts.append(_section("10. 已产出的任务（最近 10 个）"))
    try:
        out_root = ROOT / "output"
        dirs = sorted((p for p in out_root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        if not dirs:
            parts.append("<还没有跑过任何任务>")
        for d in dirs[:10]:
            summary = d / "summary.json"
            mark = "有 summary.json" if summary.is_file() else "**没有 summary.json（可能是失败的任务）**"
            parts.append(f"  {d.name}  {mark}")
    except OSError as exc:
        parts.append(f"<读取 output 失败：{exc}>")

    return "\n".join(parts)


def _banner() -> str:
    """给测试者的使用提示。

    ⚠️ 这里**故意用列表 join 而不是相邻字符串字面量**。
    原来是这么写的::

        banner = "\\n" + "#" * 70 + "\\n"
                 "# 把下面这份…\\n"
                 "# 并说明…\\n"
                 "#" * 70 + "\\n"

    Python 的相邻字符串字面量会在**编译期**直接拼成一个字面量，于是
    ``"\\n# 把下面…\\n# 并说明…\\n#"`` 变成了一个整体，紧接着的 ``* 70``
    作用在**这一整块**上 —— 报告里那句提示被原样重复了 70 遍（实测 18 KB
    的报告里 70 个 banner）。别再用相邻字面量拼接。
    """
    return "\n".join([
        "",
        "#" * 70,
        "# 把下面这份「环境快照」整个复制给 AI（ChatGPT / Claude / DeepSeek 都行），",
        "# 并说明你遇到的问题，它就能帮你定位。",
        "#" * 70,
        "",
    ]) + "\n"


def main() -> int:
    report = _banner() + collect() + "\n"
    print(report)

    target = ROOT / REPORT_NAME
    try:
        target.write_text(report, encoding="utf-8")
        print(f"\n[已写入] {target}")
    except OSError as exc:
        print(f"\n[写入失败] {exc}（把上面的内容手动复制给 AI 也一样）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
