"""落盘日志（G12）：崩溃 / 蓝屏 / 驱动重置之后，现场不会只剩一句"它转圈了"。

以前日志只在内存里（`job.json` 只留最后快照、事件流上限 4000 条），进程被强杀后全丢。
这里用 `logging` + `RotatingFileHandler` 写 `<数据根>/.logs/studio.log`（10MB × 5），
顺带把 uvicorn 的 access/error 也接进去，保留最近 7 天。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: 日志目录名（点开头 = `JobManager._load_existing` 不会把它当工程）。
LOG_DIRNAME = ".logs"
LOGGER_NAME = "sheetsage2"

#: 10MB × 5，超过自动轮转。
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def log_dir(data_root: str | Path) -> Path:
    return Path(data_root) / LOG_DIRNAME


def setup_logging(data_root: str | Path) -> Path:
    """幂等地把 `sheetsage2` logger 接到滚动文件上，返回日志文件路径。"""
    directory = log_dir(data_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "studio.log"

    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return path
    logger.setLevel(logging.INFO)

    handler = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES,
                                  backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    # 别往根 logger 上冒（uvicorn 已经在控制台打了，重复只会刷屏）
    logger.propagate = False
    return path


def attach_uvicorn(log_path: Path, level: str = "info") -> None:
    """把 uvicorn 的 access/error 也接到同一份日志文件（G12）。"""
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if not any(getattr(h, "baseFilename", None) == str(log_path) for h in logger.handlers):
            logger.addHandler(handler)
            logger.setLevel(getattr(logging, level.upper(), logging.INFO))


def install_crash_hook(data_root: str | Path) -> None:
    """崩溃时写 `logs/crash-<时间>.txt`（G12）：线程里没接住的异常也有现场。"""
    import sys
    import threading
    import time
    import traceback

    directory = log_dir(data_root)
    directory.mkdir(parents=True, exist_ok=True)

    def dump(prefix: str, exc: BaseException) -> None:
        try:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = directory / f"crash-{prefix}-{stamp}.txt"
            path.write_text("".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)), encoding="utf-8")
        except Exception:                                # noqa: BLE001 - 兜底本身不能再炸
            pass

    def excepthook(exc_type, exc, tb):
        dump("main", exc)
        sys.__excepthook__(exc_type, exc, tb)

    def thread_excepthook(args):
        exc = getattr(args, "exc_value", None) or getattr(args, "exc_type", None)
        if exc is not None:
            dump("thread", exc)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
