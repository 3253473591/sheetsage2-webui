"""下载器：断点续传 + sha256 校验 + **一行刷新的进度**。只用标准库。

为什么坚持标准库：它要在**环境还没建起来**的时候跑——内测者双击 `webui.bat` 的那一刻，
机器上只有包里那份便携 Python，`requests`/`tqdm` 都还不存在。所以这里自己写：

- `download()`：要下 1.8 GB 的 torch，断线/关机后重跑必须能**续上**（HTTP Range + `.part`）；
- 每个文件下完都要过 **sha256**，对不上就删掉重来（清单里的 sha256 来自镜像站/ModelScope）；
- **进度要让人看见**：内测者的第一反应是"是不是卡住了"，所以
  - 进度行给 百分比 / 已下 / 总量 / 速度 / 剩余秒数；
  - **校验 sha256 也报进度**（1.8 GB 的轮子要算好几秒，没输出就像死了）；
  - **连不上/中途断流**会主动喊一声（默认 20 s 没有数据就提示，别干等）；
  - 输出被重定向（不是终端）时改成每 5% 打一行，免得日志里塞满 `\\r`。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

CHUNK = 1 << 20                      # 1 MiB
USER_AGENT = "sheetsage2-webui-alpha2"
DEFAULT_TIMEOUT = 60
RETRIES = 3
#: 多久没收到数据就喊一声（秒）。
STALL_SECONDS = 20.0


def human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def human_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class Progress:
    """一行刷新的进度：`标签: 42.1%  767.9 MB/1.7 GB  9.8 MB/s  剩 1m35s`。

    - 终端里用 `\\r` 原地刷新（**不会**刷屏）；
    - 输出被重定向时改打整行（每 5% 一次），日志照样看得懂；
    - `message()` 用来插一句普通提示，插完进度行会重新画。
    """

    def __init__(self, label: str, *, stream: TextIO | None = None,
                 extra: str = "", clock=time.monotonic) -> None:
        self.label = label
        self.extra = extra
        self.stream = stream if stream is not None else sys.stdout
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.clock = clock                       # 注入时钟，方便测试节流
        self.started = self.clock()
        self._last_draw = 0.0
        self._last_bucket = -1
        self._dirty = False                      # 当前行上有内容

    # ---------------------------------------------------------------- 画
    def update(self, done: int, total: int | None = None, *, extra: str | None = None) -> None:
        now = self.clock()
        if now - self._last_draw < 0.2:
            return
        self._last_draw = now
        elapsed = max(1e-6, now - self.started)
        speed = done / elapsed
        text = self._line(done, total, speed, extra if extra is not None else self.extra)
        if self.tty:
            self.stream.write("\r" + text.ljust(110))
            self.stream.flush()
            self._dirty = True
            return

        bucket = int(done / total * 20) if total else 0      # 非终端：每 5% 一行
        if bucket != self._last_bucket:
            self._last_bucket = bucket
            self.stream.write("    " + text + "\n")
            self.stream.flush()

    def _line(self, done: int, total: int | None, speed: float, extra: str) -> str:
        if total:
            percent = done / total * 100
            eta = human_seconds((total - done) / speed) if speed > 1 else "?"
            text = (f"{self.label}: {percent:5.1f}%  {human_bytes(done)}/{human_bytes(total)}"
                    f"  {human_bytes(speed)}/s  剩 {eta}")
        else:
            text = f"{self.label}: {human_bytes(done)}  {human_bytes(speed)}/s"
        return f"{text}  {extra}".rstrip()

    # ---------------------------------------------------------------- 说话
    def message(self, text: str) -> None:
        """插一句普通信息（先把进度行清干净）。"""
        self._clear()
        self.stream.write(text + "\n")
        self.stream.flush()

    def finish(self, text: str | None = None) -> None:
        elapsed = human_seconds(self.clock() - self.started)
        self._clear()
        self.stream.write(f"    {text or (self.label + ' 完成')}（用了 {elapsed}）\n")
        self.stream.flush()

    def _clear(self) -> None:
        if self._dirty and self.tty:
            self.stream.write("\r" + " " * 110 + "\r")
            self.stream.flush()
        self._dirty = False

    def __enter__(self) -> "Progress":
        return self

    def __exit__(self, *exc: object) -> None:
        self._clear()


class _StallWatcher(threading.Thread):
    """下载中途"没动静"就喊一声——内测者最怕的就是窗口一动不动。"""

    def __init__(self, progress: Progress, *, timeout: float = STALL_SECONDS) -> None:
        super().__init__(daemon=True)
        self.progress = progress
        self.timeout = timeout
        self._touched = time.monotonic()
        self._stop = threading.Event()
        self._warned = False

    def touch(self) -> None:
        self._touched = time.monotonic()
        self._warned = False

    def run(self) -> None:
        while not self._stop.wait(2.0):
            idle = time.monotonic() - self._touched
            if idle >= self.timeout and not self._warned:
                self._warned = True
                self.progress.message(
                    f"      …已经 {int(idle)} 秒没收到数据了（网络卡住？）。别急着关："
                    f"Ctrl+C 之后重跑同一条命令，已经下完的部分会接着下。")

    def stop(self) -> None:
        self._stop.set()


def sha256_of(path: Path, *, progress: Progress | None = None) -> str:
    """算文件的 sha256；给了进度条就报进度（大轮子要算好几秒）。"""
    digest = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
            done += len(block)
            if progress is not None and total:
                progress.update(done, total, extra="校验中")
    return digest.hexdigest()


def fetch_json(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def download(url: str, target: str | Path, *, sha256: str | None = None,
             size: int | None = None, label: str | None = None,
             extra: str = "", retries: int = RETRIES, progress: bool = True) -> Path:
    """把 `url` 下到 `target`（可续传），返回路径；已存在且校验通过就直接用。"""
    target = Path(target)
    label = label or target.name
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_file():
        ok, why = _verify(target, sha256, size, progress=progress, label=label)
        if ok:
            if progress:
                print(f"    {label}: 已存在（{human_bytes(target.stat().st_size)}），跳过")
            return target
        if progress:
            print(f"    {label}: 已有文件不对（{why}），重新下")

    part = target.with_name(target.name + ".part")
    for attempt in range(1, retries + 1):
        bar = Progress(label, extra=extra) if progress else None
        watcher = _StallWatcher(bar) if bar is not None else None
        if watcher is not None:
            watcher.start()
        try:
            _download_once(url, part, target, size=size, bar=bar, watcher=watcher)
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            if watcher is not None:
                watcher.stop()
            if attempt >= retries:
                raise
            if progress:
                got = part.stat().st_size if part.is_file() else 0
                print(f"\n    {label}: 断了（{type(exc).__name__}），{attempt}/{retries} 重试"
                      f"——已下 {human_bytes(got)}，会接着下")
            time.sleep(2 * attempt)
            continue
        finally:
            if watcher is not None:
                watcher.stop()

        ok, why = _verify(part, sha256, size, progress=progress, label=label,
                          on_progress=bar)
        if ok:
            os.replace(part, target)
            if bar is not None:
                bar.finish(f"{label} 完成（{human_bytes(target.stat().st_size)}）")
            return target

        if progress:
            print(f"    {label}: 校验不过（{why}），整份重下")
        part.unlink(missing_ok=True)
    raise RuntimeError(f"{label}: 试了 {retries} 次都没下成功")


def _download_once(url: str, part: Path, target: Path, *, size: int | None,
                   bar: Progress | None, watcher: _StallWatcher | None) -> None:
    offset = part.stat().st_size if part.is_file() else 0
    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    if bar is not None and offset:
        bar.message(f"      {bar.label}: 从 {human_bytes(offset)} 接着下")
    elif bar is not None:
        bar.message(f"      {bar.label}: 连接中…（{url.split('/')[2]}）")

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
        if response.status != 206:                  # 服务器没认 Range -> 从头下
            offset = 0
        if offset:
            content_range = response.headers.get("Content-Range") or ""
            if not content_range.startswith(f"bytes {offset}-"):
                offset = 0                          # 从别的位置续的，不可靠，重来
        total = size
        if not total:
            length = response.headers.get("Content-Length")
            total = (int(length) + offset) if length else None
        mode = "ab" if offset else "wb"
        done = offset
        with open(part, mode) as handle:
            if bar is not None:
                bar.update(done, total)
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                handle.write(block)
                done += len(block)
                if watcher is not None:
                    watcher.touch()
                if bar is not None:
                    bar.update(done, total)


def _verify(path: Path, sha256: str | None, size: int | None, *,
            progress: bool = True, label: str = "", on_progress: Progress | None = None
            ) -> tuple[bool, str]:
    if not path.is_file():
        return False, "文件不存在"
    actual = path.stat().st_size
    if size and actual != size:
        return False, f"大小 {human_bytes(actual)} != {human_bytes(size)}"
    if sha256:
        bar = on_progress
        if bar is not None:
            bar.message(f"      {label or path.name}: 下完，正在校验 sha256…"
                        f"（几百 MB 要几秒，没动静是正常的）")
            digest = sha256_of(path, progress=bar)
        else:
            bar = Progress(f"{label or path.name} 校验 sha256") if progress else None
            digest = sha256_of(path, progress=bar)
            if bar is not None:
                bar.finish()
        if digest.lower() != sha256.lower():
            return False, f"sha256 {digest[:12]}… != {sha256[:12]}…"
    return True, "ok"


def free_space(path: Path) -> int:
    """可用磁盘空间（字节）；路径不存在就往上看。"""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free


@dataclass
class Plan:
    """一次下载计划：给内测者先看一眼要下多少。"""

    items: list[tuple[str, str, int]]        # (label, url, bytes)

    @property
    def total_bytes(self) -> int:
        return sum(item[2] for item in self.items)
