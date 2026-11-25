"""SheetSage2 推理集成层。

对外只暴露三件事：加载模型、跑一次转写、把产物读成结构化摘要。
`bootstrap.prepare()` 会在导入期执行，保证 OpenMP 与 ffmpeg 已经就绪。
"""

from .bootstrap import prepare as _prepare

_ENV = _prepare()

from .engine import SheetSage2Engine, TranscriptionError  # noqa: E402
from .result import ArtifactReport, TranscriptionSummary, inspect_output_dir  # noqa: E402

__all__ = [
    "SheetSage2Engine",
    "TranscriptionError",
    "TranscriptionSummary",
    "ArtifactReport",
    "inspect_output_dir",
    "environment",
]

#: 包版本：**和 `pyproject.toml` / `downloads.json` 保持一致**（`/api/env.version`、
#: FastAPI 的 `version`、备份包版本都读它）。发布前改这一处 + 那两处，别只改一个。
__version__ = "2.0.0"


def environment() -> dict[str, str | None]:
    """当前进程探测到的运行环境（ffmpeg、工程根目录）。"""
    return dict(_ENV)
