"""回收站：删除 = 把工程目录 `rename` 进数据根下的 `.trash/`，能恢复、也有保留策略。

**为什么不是直接 `rmtree`**：删除按钮就在「下载 ▾」旁边，手指偏 20 像素就是几小时的活
全没（连原音频都一起没了）。`rename` 在同一卷上是原子的，用户感知和"删了"一样。

这一层只管**目录怎么进出回收站**；"从列表里摘掉 / 重新登记"是管理器的事，所以
:meth:`TrashStore.entry_dir` 只回答路径，注册由 `JobManager.restore` 做。
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from ..fsutil import move_into
from .job_files import safe_name

#: 软删除的回收站（数据根下的 `.trash/`，点开头所以启动恢复不会扫它）。
TRASH_DIRNAME = ".trash"
#: 回收站保留策略：超过这些就真删（启动、每次删除、以及定时三处跑）。
TRASH_MAX_DAYS = 7.0
TRASH_MAX_BYTES = 2 * 1024 ** 3
#: 定时清理的节拍。一小时扫一次目录很便宜；没有这条的话，**七天不删东西也不重启**
#: 过期条目就一直躺着——「7 天自动清」就成了空话。
TRASH_PURGE_INTERVAL = 3600.0


class TrashStore:
    """数据根下的 `.trash/`：挪进去、列出来、挪回来、清掉。"""

    def __init__(self, root: str | Path, *, enabled: bool = True,
                 max_days: float = TRASH_MAX_DAYS,
                 max_bytes: float = TRASH_MAX_BYTES,
                 purge_interval: float = TRASH_PURGE_INTERVAL) -> None:
        self.root = Path(root)
        #: `False` = 硬删（测试与"不想留回收站"的部署用）。
        self.enabled = bool(enabled)
        self.max_days = float(max_days)
        self.max_bytes = float(max_bytes)
        self.purge_interval = max(0.05, float(purge_interval))
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    # ---------------------------------------------------------------- 布局
    @property
    def trash_root(self) -> Path:
        return self.root / TRASH_DIRNAME

    def entry_dir(self, entry_id: str) -> Path | None:
        """回收站里某一项的目录；不存在（或越界）就是 `None`。

        `entry_id` 来自前端，必须收敛成 basename 再拼——不能让它指到 `.trash/` 外面。
        """
        try:
            target = self.trash_root / safe_name(entry_id)
        except (TypeError, ValueError):
            return None
        if not target.is_dir() or TRASH_DIRNAME not in str(target):
            return None
        return target

    # ---------------------------------------------------------------- 读写
    def entries(self) -> list[dict[str, Any]]:
        """回收站列表（新的在前）。每项就是一个被挪进来的工程目录。"""
        if not self.trash_root.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for entry in sorted(self.trash_root.iterdir(), reverse=True):
            if not entry.is_dir():
                continue
            meta: dict[str, Any] = {}
            try:
                meta = json.loads((entry / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            size = 0
            for path in entry.rglob("*"):
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except OSError:
                    continue
            stat = entry.stat()
            entries.append({
                "id": entry.name,
                "name": str(meta.get("name") or entry.name),
                "job_id": meta.get("id"),
                "deleted_at": stat.st_mtime,
                "size": size,
                "files": len(meta.get("files") or []),
            })
        return entries

    def move_in(self, job_id: str, name: str, source: Path) -> Path:
        """把工程目录挪进回收站，返回落点。挪不动抛 `OSError`（调用方负责回滚登记）。"""
        self.trash_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        entry = self.trash_root / f"{safe_name(name)}-{job_id}-{stamp}"
        move_into(source, entry)
        self.purge_expired()
        return entry

    def drop(self, entry_id: str) -> bool:
        """彻底删掉回收站里的一项（不可恢复）。"""
        target = self.entry_dir(entry_id)
        if target is None:
            return False
        shutil.rmtree(target, ignore_errors=True)
        return True

    # ---------------------------------------------------------------- 保留策略
    def purge_expired(self) -> None:
        """超过 `max_days` 天、或总共超过 `max_bytes`，就真删最旧的。"""
        if not self.trash_root.is_dir():
            return
        entries = []
        for entry in self.trash_root.iterdir():
            if not entry.is_dir():
                continue
            try:
                entries.append((entry.stat().st_mtime, entry))
            except OSError:
                continue
        entries.sort()
        deadline = time.time() - self.max_days * 86400.0
        total = 0
        sizes: list[tuple[float, int, Path]] = []
        for mtime, entry in entries:
            size = 0
            for path in entry.rglob("*"):
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except OSError:
                    continue
            sizes.append((mtime, size, entry))
            total += size
        for mtime, size, entry in sizes:
            if mtime < deadline or total > self.max_bytes:
                shutil.rmtree(entry, ignore_errors=True)
                total -= size

    # ---------------------------------------------------------------- 定时清理
    def start(self) -> None:
        """起一个定时清理线程（一小时一扫，见 `TRASH_PURGE_INTERVAL`）。"""
        self.purge_expired()
        self._worker = threading.Thread(target=self._loop, name="sheetsage-trash",
                                       daemon=True)
        self._worker.start()

    def _loop(self) -> None:
        """定时清回收站。清理失败只跳过这一轮，绝不把服务带走。"""
        while not self._stop.wait(self.purge_interval):
            try:
                self.purge_expired()
            except Exception:      # noqa: BLE001 - 目录被占用/权限问题都不该影响识别
                continue

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            self._worker = None
