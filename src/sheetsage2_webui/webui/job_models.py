"""任务领域对象：`Job` / `StageRun` / `Event` / `Progress` 与状态常量。

只依赖异常、参数定义和布局常量；**不导入管理器、不碰磁盘、不起线程**。
`Progress` 通过 `job.emit` / `job.cancel_requested` 与外界协作，自己不持有
调度器状态。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import describe as describe_error
from .job_errors import JobCancelled
from .job_files import INTERNAL_DIRNAME
from .job_options import default_options

#: 阶段顺序与显示名。界面上的两张卡就是这两个。
PHASES = ("transcribe", "lyrics")
PHASE_LABELS = {"transcribe": "扒谱", "lyrics": "歌词识别"}

#: 新工程默认**一个阶段都不排**（门禁）。
#:
#: 丢音频只做两件事：把文件收下来、把工程建出来。参数面板这时才露出来，用户选完参数
#: 再点「开始扒谱」/「开始识别歌词」，任务才进队列。想一条龙就用「⏵ 自动提取」
#: （它走 `/chain`，明确要求串联）。
DEFAULT_PLAN: tuple[str, ...] = ()

#: 刚建好、还没排任何阶段的工程状态。界面上是"待开始"。
DRAFT_STATE = "draft"

#: 任务状态机：draft -> queued -> running -> (done | failed | cancelled)
TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})
STAGE_TERMINAL = frozenset({"done", "failed", "cancelled", "skipped"})

#: 保留的 SSE 事件条数上限。一个任务的解码事件可能上千条，全留着没意义。
MAX_EVENTS = 4000

#: 产物清单（`.webui/manifest.json`）：只有它列出的文件才是"当前这一版的结果"。
MANIFEST_NAME = "manifest.json"

# --------------------------------------------------------------------- 事件
@dataclass
class Event:
    seq: int
    type: str
    time: float
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "time": self.time, "data": self.data}


# --------------------------------------------------------------------- 阶段
@dataclass
class StageRun:
    """一个阶段（扒谱 / 歌词）的状态。界面上就是一张卡。"""

    key: str
    label: str
    state: str = "idle"          # idle | queued | running | done | failed | cancelled | skipped
    progress: float = 0.0
    step: str = ""               # 当前小步骤（"识别音乐"…）
    message: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    #: 最后一次"有进展"（step/progress/log 任一变化）的时刻，看门狗拿它判卡死。
    last_advance_at: float | None = None
    #: **人话**（横条上显示的那一句），不是异常原文——见 `errors.py`。
    error: str | None = None
    #: 错误码 / 下一步 / 技术原文（`errors.describe()` 的四段，`error` 是第二段）。
    error_code: str | None = None
    error_action: str | None = None
    error_detail: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)   # 该阶段的产物统计

    def fail(self, exc: BaseException) -> None:
        """把一次异常落成"用户能看懂"的三段（调用方持有 `job.lock`）。"""
        error = describe_error(exc, phase=self.key)
        self.state = "failed"
        self.error = error.message
        self.error_code = error.code
        self.error_action = error.action
        self.error_detail = error.detail

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def as_dict(self, eta: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "progress": round(self.progress, 4),
            "step": self.step,
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_advance_at": self.last_advance_at,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "error": self.error,
            "error_code": self.error_code,
            "error_action": self.error_action,
            "error_detail": self.error_detail,
            # 耗时预估（G20）：`{seconds, low, high, label, device, audio_seconds}`
            # 或 null（还不知道音频多长）。只给"还没跑完"的阶段算，跑完了就不用猜了。
            "eta": dict(eta) if eta else None,
            "detail": self.detail,
        }


# --------------------------------------------------------------------- 任务
@dataclass
class Job:
    """一次上传 + 一串阶段运行。所有字段的读写都走 ``lock``。

    **磁盘布局（`output/<歌名>/`）**——工程目录就是产物目录，和命令行跑出来的一模一样::

        output/<歌名>/project.json  report.json  melody_vocal.lab  …   <- 产物（CLI 也写这里）
        output/<歌名>/job.json                                        <- WebUI 元数据
        output/<歌名>/.webui/input/<原音频>  stems/  peaks-*.json
                             .webui/export/*.svp  bundle.zip           <- WebUI 自己的东西

    ``imported=True`` 表示这个目录是**别人跑出来的**（命令行 / 另一个工具）：能看能下载，
    但不能重跑阶段，也不会往里写 `job.json`。
    """

    id: str
    name: str
    audio: str
    root: Path
    upload: Path
    out_dir: Path
    created_at: float
    rev: int = 1
    #: **产物版本戳**：每完成一轮阶段 +1（扒谱 +1、歌词 +1）。和 `rev` 分工不同——
    #: `rev` 管"用户改了参数"（界面上显示 rev N），这里是"磁盘上这批产物是第几版"，
    #: 所有派生产物的缓存键都带上它（zip / SVP / 波形），重跑之后缓存必然失效。
    artifacts_rev: int = 0
    state: str = DRAFT_STATE
    stage: str | None = None                        # 当前正在跑的阶段 key
    plan: list[str] = field(default_factory=list)   # 待跑的阶段队列
    stages: dict[str, StageRun] = field(default_factory=dict)
    message: str = "等待参数"
    files: list[dict[str, Any]] = field(default_factory=list)
    #: 单独上传的音源：kind -> {name, path, size, created_at}。目前只有 `vocal`（干声）。
    #: 有干声且「音源」选了干声就用它，否则用混音原曲——回退规则在 `runner.lyrics`。
    stems: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 上传体检（G10）的结果：时长/采样率/是否静音等，给前端提示"太短/太长/纯静音"。
    audio_info: dict[str, Any] = field(default_factory=dict)
    #: 内容指纹（大小 + 前 1MB 的 hash）：用于"这个文件已经传过"去重提示。
    audio_fingerprint: str | None = None
    #: 命中已有工程（内容指纹一致）时，指向那个工程 id；没有就是 None。
    duplicate_of: str | None = None
    #: 每个工程自己的可调参数（见 `PARAM_SPECS`）。**这是页面上放开给用户的东西**：
    #: 精度 / 设备 / 范围 / 量化 / 识别模型 / 音源 / 语言。
    options: dict[str, Any] = field(default_factory=default_options)
    highlights: dict[str, Any] | None = None
    decisions: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    cancel_requested: bool = False
    #: 最近一次取消的时刻（G14 的"墓碑"）：取消后短时间内的「开始」当作手抖连点拒绝，
    #: 免得取消又被立刻复活。
    canceled_at: float | None = None
    #: `output/` 里本来就有的产物目录（不是 WebUI 建的）——只读。
    imported: bool = False
    #: **墓碑**：这个工程已经从列表里删掉了。收尾线程（`_finish` 之后的写盘）可能还在跑，
    #: 而原子写会 `mkdir(parents=True)`——不认这个标记就会把刚删掉的目录重新建出来。
    #: 只影响写盘，不进快照 / `job.json`。
    removed: bool = False
    #: 耗时预估（G20）的提供者：`(job, phase) -> {seconds,low,high,label,…} | None`。
    #: 由 :class:`JobManager` 在登记工程时挂上——`Job` 自己不知道显卡、音频时长这些上下文。
    eta_provider: Callable[["Job", str], Mapping[str, Any] | None] | None = field(
        default=None, repr=False, compare=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _seq: int = 0

    def __post_init__(self) -> None:
        for phase in PHASES:
            self.stages.setdefault(phase, StageRun(phase, PHASE_LABELS[phase]))

    # ---------------------------------------------------------------- 布局
    @property
    def internal_dir(self) -> Path:
        """WebUI 自己的东西（元数据 / 音频 / 缓存 / 打包）都塞这里，不污染产物。"""
        return self.root / INTERNAL_DIRNAME

    @property
    def stems_dir(self) -> Path:
        return self.internal_dir / "stems"

    @property
    def manifest_path(self) -> Path:
        """产物清单：`/files` 与 `/download` 只信它（见 `JobManager.write_manifest`）。"""
        return self.internal_dir / MANIFEST_NAME

    # ---------------------------------------------------------------- 事件
    def emit(self, type_: str, **data: Any) -> Event:
        with self.lock:
            self._seq += 1
            event = Event(self._seq, type_, time.time(), data)
            self.events.append(event)
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]
            return event

    def events_since(self, seq: int) -> list[Event]:
        with self.lock:
            return [event for event in self.events if event.seq > seq]

    @property
    def terminal(self) -> bool:
        with self.lock:
            return self.state in TERMINAL_STATES

    @property
    def elapsed_seconds(self) -> float:
        with self.lock:
            starts = [s.started_at for s in self.stages.values() if s.started_at]
            ends = [s.finished_at for s in self.stages.values() if s.finished_at]
            if not starts:
                return 0.0
            end = max(ends) if ends else time.time()
            return max(0.0, end - min(starts))

    # ---------------------------------------------------------------- 快照
    def stage_eta(self, run: StageRun) -> Mapping[str, Any] | None:
        """这个阶段的耗时预估（G20）。

        只算**还没跑完**的阶段：`done` 了进度条就是 100%，再挂一句"预计 3 分钟"只会误导。
        provider 抛异常不影响快照——预估是锦上添花，不能把接口带挂。
        """
        if self.eta_provider is None:
            return None
        if run.state in STAGE_TERMINAL:
            return None
        try:
            return self.eta_provider(self, run.key)
        except Exception:                            # noqa: BLE001 - 预估坏了不该影响快照
            return None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            current = self.stages.get(self.stage) if self.stage else None
            stages = {key: run.as_dict(self.stage_eta(run)) for key, run in self.stages.items()}
            return {
                "id": self.id,
                "name": self.name,
                "audio": self.audio,
                "rev": self.rev,
                "artifacts_rev": self.artifacts_rev,
                "state": self.state,
                "stage": self.stage,
                "stages": stages,
                # 下面几个是"当前阶段"的扁平视图，顶栏/列表用起来方便
                "stage_label": current.label if current else "",
                "step": current.step if current else "",
                "progress": round(current.progress, 4) if current else 0.0,
                # 当前阶段的耗时预估（G20，没在跑就是 null）
                "eta": (stages.get(self.stage) or {}).get("eta") if self.stage else None,
                "message": self.message,
                "created_at": self.created_at,
                "elapsed_seconds": round(self.elapsed_seconds, 2),
                "error": next((s.error for s in self.stages.values() if s.error), None),
                "error_code": next((s.error_code for s in self.stages.values()
                                    if s.error_code), None),
                "error_action": next((s.error_action for s in self.stages.values()
                                      if s.error_action), None),
                "error_detail": next((s.error_detail for s in self.stages.values()
                                      if s.error_detail), None),
                "warnings": list(self.warnings),
                "files": list(self.files),
                "stems": {kind: dict(info) for kind, info in self.stems.items()},
                "audio_info": dict(self.audio_info),
                "audio_fingerprint": self.audio_fingerprint,
                "duplicate_of": self.duplicate_of,
                "options": dict(self.options),
                "highlights": self.highlights,
                "decisions": self.decisions,
                "summary": self.summary,
                "events": len(self.events),
                "cancel_requested": self.cancel_requested,
                # `output/` 里本来就有的产物：只读（不能重跑、不能改参数）
                "imported": self.imported,
            }

    def metadata(self) -> dict[str, Any]:
        """落盘的 ``job.json``：比快照多一份完整摘要，够人工复盘。"""
        data = self.snapshot()
        data["output_dir"] = str(self.out_dir)
        data["upload"] = str(self.upload)
        return data


# --------------------------------------------------------------------- 进度
class Progress:
    """runner 汇报进度 + 检查取消的唯一入口（**按阶段**）。

    用法（`runner.py` 里就是这么用的）::

        progress.step("识别音乐", 0.10)
        progress.set(0.42, "窗口 2/6 · 解码 512 tokens")
        progress.check()          # 用户点了取消就在这里抛 JobCancelled
    """

    #: 进度事件的最小间隔（秒）与最小增量的双重节流——解码事件太密，不筛会淹掉日志。
    MIN_INTERVAL = 0.25
    MIN_DELTA = 0.002

    def __init__(self, job: Job, phase: str) -> None:
        self.job = job
        self.phase = phase
        self.run = job.stages[phase]
        self._last_emit = 0.0
        self._last_value = -1.0

    # ---------------------------------------------------------------- 小步骤
    def step(self, label: str, fraction: float | None = None) -> None:
        """切换当前小步骤。**只有 label 变了才发事件**，否则逐窗口回调会刷爆事件流。"""
        with self.job.lock:
            changed = self.run.step != label
            self.run.step = label
            self.run.last_advance_at = time.time()
            if fraction is not None:
                self.run.progress = max(self.run.progress, fraction)
            progress = self.run.progress
        if changed:
            self.job.emit("step", phase=self.phase, step=label, progress=progress)

    # ---------------------------------------------------------------- 进度
    def set(self, fraction: float, message: str | None = None, *, force: bool = False) -> None:
        fraction = min(1.0, max(0.0, float(fraction)))
        with self.job.lock:
            self.run.progress = max(self.run.progress, fraction)
            self.run.last_advance_at = time.time()
            if message is not None:
                self.run.message = message
                self.job.message = message
        now = time.monotonic()
        due = (now - self._last_emit >= self.MIN_INTERVAL
               or fraction - self._last_value >= self.MIN_DELTA)
        if force or due:
            self._last_emit = now
            self._last_value = fraction
            self.job.emit("progress", phase=self.phase, progress=round(fraction, 4),
                          message=message or "")

    def detail(self, **values: Any) -> None:
        """记录该阶段的产物统计（歌词行数、音符数……），进快照与 job.json。"""
        with self.job.lock:
            self.run.detail.update(values)

    def lyrics(self, **payload: Any) -> None:
        """把"已经识别出来的歌词"实时推给界面（模型逐段出结果）。"""
        self.job.emit("lyrics", phase=self.phase, **payload)

    def log(self, message: str, **extra: Any) -> None:
        with self.job.lock:
            self.job.message = message
            self.run.last_advance_at = time.time()
        self.job.emit("log", phase=self.phase, message=message, **extra)

    def check(self) -> None:
        """取消检查点。

        用户点「停止」、或者看门狗判定这一阶段卡死/超时之后，`cancel_requested` 会被置上——
        下一次回调进来就抛 :class:`JobCancelled`，把工作线程放出来（推理本身不能被打断，
        但至少不会永远占着线程）。
        """
        with self.job.lock:
            if self.job.cancel_requested:
                raise JobCancelled("用户取消" if self.run.error_code is None else self.run.error or "任务已中止")


StageRunner = Callable[[Job, Progress], dict[str, Any]]
