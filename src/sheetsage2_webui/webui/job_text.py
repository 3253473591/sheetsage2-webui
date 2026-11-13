"""给用户看的数字说法：秒 → "12 分钟"、字节 → "1.2 GB"。

单独一层是因为**写提示的地方比写逻辑的地方多**：调度器的超时提示、磁盘预检、清理
汇总、路由的错误信息都要用它；留在 `jobs.py` 里会让 `job_storage` / `job_scheduler`
反过来依赖门面（那就成环了）。
"""

from __future__ import annotations


def duration_text(seconds: float) -> str:
    """秒 -> 人话（"35 秒" / "12 分钟" / "1.5 小时"），只用在超时提示里。"""
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    if seconds < 5400:
        return f"{seconds / 60:.0f} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def bytes_text(size: float) -> str:
    """字节 -> 人话（"1.2 GB" / "340 MB"），磁盘预检与清理提示用。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
