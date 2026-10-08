"""WebUI 后端：FastAPI + 两阶段任务队列 + SSE 进度 + 卷帘数据 + 产物下载。

对外只有一个入口：`create_app()`。想换掉真实的推理流程（测试里就是这么干的），
把 `runners` 换成自己的 dict 即可——见 `jobs.JobManager`。

    python -m sheetsage2_webui.webui            # 起服务（默认 127.0.0.1:8000）
"""

from .app import STATIC_DIR, create_app
from .jobs import PHASES, Job, JobCancelled, JobManager, Progress, StageNotReady, StageRun
from .runner import RunnerSession, build_highlights, group_lyric_lines

__all__ = [
    "create_app", "STATIC_DIR", "RunnerSession", "JobManager", "Job", "StageRun",
    "Progress", "JobCancelled", "StageNotReady", "PHASES", "build_highlights",
    "group_lyric_lines",
]
