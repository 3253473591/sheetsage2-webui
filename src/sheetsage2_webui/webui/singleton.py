"""单实例锁：双击两次 `webui.bat` 不许起两个服务（G1）。

**为什么必须挡**：`webui.bat` 没有任何互斥，第二次双击时 `launch.pick_port` 发现 8000
被占，会**换一个随机端口再起一个服务**——看起来"一切正常"，实际是两个 uvicorn + 两个
`JobManager` 盯同一个 `output/`：同一批工程被两边同时写、`job.json` 互相覆盖、
两个进程同时往显存里加载模型（直接 OOM）、更糟的是 A 窗口删工程删的可能是 B 窗口正在写的目录。

做法：数据根下两个文件。

| 文件 | 作用 |
|---|---|
| `.webui.lock` | **互斥体**：`msvcrt.locking` / `flock` 独占一个字节，跟着进程走，进程没了自动释放 |
| `.webui.port.json` | **名片**：`{pid, port, url, started_at}`，让第二个进程知道"该打开哪个地址" |

分两个文件的原因很实在：Windows 上被字节锁住的文件，另一个进程连打开读都不一定允许，
所以"名片"必须能单独读。

拿不到锁的一方**不是报错退出**，而是按名片把浏览器打开
（"已经有一个在跑了，帮你打开了 → http://127.0.0.1:8000"），退出码 0。

这个模块**不 import 包内任何东西**：`scripts/launch.py` 是按文件路径加载它的，
启动器不该为了一个文件锁去导入 torch 与模型层。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

#: 互斥体文件名（数据根下）。点开头，所以 `JobManager._load_existing` 不会把它当工程。
LOCK_NAME = ".webui.lock"
#: 名片文件名（只有当前持有锁的实例会写它）。
INFO_NAME = ".webui.port.json"


class AlreadyRunning(RuntimeError):
    """已经有一个实例在这个数据根上跑着。

    `info` 是那个实例写进名片的 `{pid, port, url, started_at}`；读不到就是空 dict。
    """

    def __init__(self, info: dict[str, Any], path: Path) -> None:
        self.info = dict(info or {})
        self.path = Path(path)
        url = self.info.get("url") or ""
        super().__init__(f"已经有一个 SheetSage2 Studio 在跑：{url or self.path}")


def _lock_fd(fd: int) -> None:
    """独占加锁（非阻塞）。拿不到抛 OSError。"""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def pid_alive(pid: int | None) -> bool:
    """这个 pid 还活着吗？（名片可能是"上一辈子"留下的）"""
    if not pid:
        return False
    try:
        if os.name == "nt":
            import psutil

            return bool(psutil.pid_exists(int(pid)))
        os.kill(int(pid), 0)
        return True
    except Exception:                           # noqa: BLE001 - 判断存活不该抛
        return False


def read_info_file(path: str | Path) -> dict[str, Any]:
    """读名片（`{}` = 没有/读不懂/正写到一半）。"""
    target = Path(path)
    for _ in range(3):
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            time.sleep(0.05)
            continue
        return dict(data) if isinstance(data, dict) else {}
    return {}


class InstanceLock:
    """一个数据根上的独占锁。用法::

        lock = InstanceLock(data_root)
        try:
            lock.acquire(port=8000)
        except AlreadyRunning as exc:
            print(exc.info.get("url"))
        ...
        lock.release()
    """

    def __init__(self, data_root: str | Path) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / LOCK_NAME
        self.info_path = self.data_root / INFO_NAME
        self._fd: int | None = None
        self._info: dict[str, Any] = {}

    @property
    def held(self) -> bool:
        return self._fd is not None

    # ---------------------------------------------------------------- 加锁
    def acquire(self, *, port: int | None = None, url: str | None = None,
                extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """加锁并写名片；已被占就抛 :class:`AlreadyRunning`。"""
        if self.held:
            return self.update(port=port, url=url, **(extra or {}))
        self.data_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _lock_fd(fd)
        except OSError:
            info = self.read_info()
            os.close(fd)
            raise AlreadyRunning(info, self.path) from None
        self._fd = fd
        return self.update(port=port, url=url, **(extra or {}))

    def try_acquire(self, **fields: Any) -> bool:
        """拿得到锁就 True，拿不到 False（`launch.py` 探活用这个，不抛异常）。"""
        try:
            self.acquire(**fields)
        except AlreadyRunning:
            return False
        return True

    def update(self, *, port: int | None = None, url: str | None = None,
               **fields: Any) -> dict[str, Any]:
        """刷新名片（端口确定之后要再写一次）。"""
        info = dict(self._info)
        info.update({key: value for key, value in fields.items() if value is not None})
        if port is not None:
            info["port"] = int(port)
        if url is not None:
            info["url"] = url
        elif info.get("port"):
            info["url"] = f"http://127.0.0.1:{info['port']}"
        info["pid"] = os.getpid()
        info.setdefault("started_at", time.time())
        info.setdefault("exe", "python -m sheetsage2_webui.webui")
        self._info = info
        payload = json.dumps(info, ensure_ascii=False).encode("utf-8")
        if self._fd is not None:
            try:                                # 互斥体里也留一份，人工看得到
                os.lseek(self._fd, 0, os.SEEK_SET)
                os.ftruncate(self._fd, 0)
                os.write(self._fd, payload)
                os.fsync(self._fd)
            except OSError:
                pass
        try:                                    # 名片：只有持锁的实例会写
            self.info_path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return info

    def read_info(self) -> dict[str, Any]:
        """读名片；名片读不到再退回互斥体里的副本。"""
        info = read_info_file(self.info_path)
        if not info:
            info = read_info_file(self.path)
        return info

    def release(self) -> None:
        """解锁并关闭（两个文件都留着：里面还有"上次是谁在跑"）。"""
        if self._fd is None:
            return
        _unlock_fd(self._fd)
        try:
            os.close(self._fd)
        finally:
            self._fd = None

    # ---------------------------------------------------------------- 生命周期
    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
