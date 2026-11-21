"""后台刷新 `meta.json`：手动加/改 `auto.svp` / `manual.svp` 后不用再跑命令。

它不是文件系统事件驱动（不引入 watchdog 依赖），而是一个很轻的轮询：
每 `interval` 秒扫一遍工程目录，只对 `project.json` 所在目录调用
:func:`sheetsage2_webui.meta.write_meta`；后者自己会看 mtime，源文件没变就不写盘。

默认只由 WebUI 的生产入口启动（`create_app(meta_watch=True)`），测试里的
`create_app` 不传就不起线程。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterable

from .meta import write_meta


class MetaWatcher:
    """轮询若干根目录，给其中的工程刷新 meta。"""

    def __init__(self, roots: Iterable[str | Path], *, interval: float = 2.0) -> None:
        self.roots = [Path(root) for root in roots]
        self.interval = max(0.05, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="meta-watch",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.scan_once()
            except Exception:
                # 一个坏目录/坏文件不能把后台线程打死；下一轮继续。
                pass

    def scan_once(self) -> None:
        """扫一遍；可单独调用（测试/一次性刷新）。"""
        seen: set[Path] = set()
        for root in self.roots:
            if not root.is_dir():
                continue
            try:
                projects = list(root.rglob("project.json"))
            except OSError:
                continue
            for project in projects:
                try:
                    parts = project.relative_to(root).parts
                except ValueError:
                    continue
                if any(part.startswith(".") for part in parts):
                    continue                    # .trash / .webui / prev 之类不碰
                folder = project.parent
                if folder.name == "auto":          # 嵌套布局：auto/project.json
                    folder = folder.parent
                folder = folder.resolve()
                if folder in seen:
                    continue
                seen.add(folder)
                try:
                    write_meta(folder)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
