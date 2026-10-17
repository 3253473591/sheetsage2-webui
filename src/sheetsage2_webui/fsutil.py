"""原子落盘：先写同目录的 `.part`，再 `os.replace` 顶上去。

**为什么**：`project.json` / `report.json` / `summary.json` / `job.json` 以前都是
`write_text` 直接盖。进程在写 JSON 时被杀（Ctrl+C、任务管理器、断电）就留下半截文件，
而 `/lanes`、`/exports`、`/download?kind=svp` 全靠 `project.json` —— 用户看到的是 500
或"还没扒谱"。`os.replace` 在同一个卷上是原子的：**要么是旧文件，要么是新文件**，
不会有第三种状态。

临时文件必须和目标是**同一个目录**（同卷），否则 `os.replace` 会退化成复制+删除，
那就不原子了。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def temp_sibling(path: str | Path) -> Path:
    """目标旁边的一个临时名（同目录 = 同卷，`os.replace` 才是原子的）。"""
    target = Path(path)
    return target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.part")


def write_bytes_atomic(path: str | Path, data: bytes) -> Path:
    """原子写字节。失败时临时文件会被清掉，目标文件保持原样。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = temp_sibling(target)
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target


def write_text_atomic(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    """原子写文本（工程的 JSON 产物都走它）。"""
    return write_bytes_atomic(path, text.encode(encoding))


def move_into(path: str | Path, target: str | Path) -> Path:
    """把目录/文件挪到 `target`（软删除用）。

    同一个卷上 `os.replace` 是原子改名；跨卷会失败，那就退回 `shutil.move`。
    """
    import shutil

    source = Path(path)
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
    except OSError:
        shutil.move(str(source), str(destination))
    return destination
