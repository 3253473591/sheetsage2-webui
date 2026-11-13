"""任务管理：上传落到磁盘 → 等用户选参数 → 按阶段跑 runner → 产物可下载。

一个任务有**两个阶段**（和界面上的两张卡一一对应）：

| phase | 界面 | 谁跑 |
|---|---|---|
| `transcribe` | 扒谱 / SheetSage2 | `runner.RunnerSession.transcribe` → `pipeline.run` |
| `lyrics` | 歌词识别 / Qwen3-ASR | `runner.RunnerSession.lyrics` → `asr` + `lyrics` |

设计约束（都是踩过的）：

1. **丢音频不立刻开跑（门禁）。** 上传只建工程，状态是 :data:`DRAFT_STATE`；用户在页面上
   选完参数点「开始扒谱」才排队。参数本身挂在 :attr:`Job.options` 上（见 ``PARAM_SPECS``）。
2. **一次只跑一个阶段。** GPU 只有一块，并发只会一起变慢、还容易 OOM；
   队列本身也是给用户看的（"排队 2"）。
3. **进度按阶段分开记。** 界面是两张独立的卡，各有进度条和"当前小步骤"，
   所以 `stage/progress/step` 都挂在阶段上，不能只留一个全局数。
4. **事件必须能续传。** 每个事件带单调递增的 `seq`，客户端断线重连时带 `since`，
   后端从下一条补发——所以事件列表要留着。
5. **取消是协作式的。** 推理不可中断，只能在下一次回调里抛 :class:`JobCancelled`。

**模块边界**（重构见 `docs/261113 模块化拆分与重构计划.md`）：本文件是**兼容门面**，
只负责组装 :class:`JobManager` 并转发旧符号；领域对象在 `job_models`、参数在
`job_options`、文件工具在 `job_files`、异常在 `job_errors`。新代码请直接导入那些模块。
"""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..bootstrap import PROJECT_ROOT
from ..fsutil import move_into, write_text_atomic
from .errors import StageFailure, describe as describe_error
from .eta import estimate as estimate_seconds
from .job_errors import (ImportedProject, JobCancelled, StageNotReady, StaleRevision,
                         UploadRejected)
from .job_files import (AUDIO_SUFFIXES, FILE_KINDS, INTERNAL_DIRNAME, STEM_KINDS,
                        STEM_LABELS, _check_audio, _kind_of, _write_stream,
                        claim_project_dir, content_fingerprint, is_internal, iter_chunks,
                        project_folder_name, safe_name, scan_files, unique_project_dir,
                        unique_suffix)
from .job_models import (DEFAULT_PLAN, DRAFT_STATE, MANIFEST_NAME, MAX_EVENTS, PHASE_LABELS,
                         PHASES, STAGE_TERMINAL, TERMINAL_STATES, Event, Job, Progress,
                         StageRunner, StageRun)
from .job_options import (PARAM_INDEX, PARAM_OWNER, PARAM_SPECS, asr_model_label,
                          default_options, describe_change, param_choices, validate_options)

# 兼容入口：这些名字过去都在 `webui.jobs` 里可直接导入，调用方（app / runner / 测试）
# 正逐步改到各自模块，这里保留转发（见 docs/261113 模块化拆分与重构计划.md 第 2 条）。

#: 软删除的回收站（数据根下的 `.trash/`，点开头所以 `_load_existing` 不扫它）。
#: 删除 = 把工程目录 `rename` 进去（同卷原子），用户感知和"删了"一样，但能恢复。
TRASH_DIRNAME = ".trash"
#: 回收站保留策略：超过这些就真删（`_purge_trash` 在启动、每次删除、以及定时三处跑）。
TRASH_MAX_DAYS = 7.0
TRASH_MAX_BYTES = 2 * 1024 ** 3
#: 定时清理的节拍。一小时扫一次目录很便宜；没有这条的话，**七天不删东西也不重启**
#: 过期条目就一直躺着——「7 天自动清」就成了空话。
TRASH_PURGE_INTERVAL = 3600.0

#: 取消后的"冷静期"（秒，G14）：这段时间里的「开始」当作手抖连点，直接 409。
CANCEL_COOLDOWN_SECONDS = 5.0

#: 阶段级超时（秒）。`STALL` = 多久**没有任何进展**算卡死；`HARD` = 总时长上限。
#: 实测：一首 4 分钟的歌 GPU 扒谱约 2~4 分钟、CPU 30~90 分钟——CPU 退路靠 HARD 兜。
STALL_TIMEOUT = {"transcribe": 900.0, "lyrics": 600.0}
HARD_TIMEOUT = {"transcribe": 7200.0, "lyrics": 3600.0}

@dataclass
class _PhaseWatch:
    """看门狗盯的一趟运行（G6）。

    `fired` 一旦为真，说明这一阶段已经被判定失败（卡死/超时）——工作线程回来之后
    **不许再把它改回 done**（那正是"进度条永远停在 62%"的另一面：看起来失败，实际成功了）。
    """

    job_id: str
    phase: str
    started_at: float
    fired: bool = False


def _per_phase(value: float | Mapping[str, float] | None,
               defaults: Mapping[str, float]) -> dict[str, float]:
    """超时参数：一个数 = 两个阶段都用它；一个映射 = 分别给。"""
    if value is None:
        return dict(defaults)
    if isinstance(value, Mapping):
        return {phase: float(value.get(phase, defaults[phase])) for phase in defaults}
    return {phase: float(value) for phase in defaults}


def _duration_text(seconds: float) -> str:
    """秒 -> 人话（"35 秒" / "12 分钟" / "1.5 小时"），只用在超时提示里。"""
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    if seconds < 5400:
        return f"{seconds / 60:.0f} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def _bytes_text(size: float) -> str:
    """字节 -> 人话（"1.2 GB" / "340 MB"），磁盘预检与清理提示用。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"

# --------------------------------------------------------------------- 管理器
class JobManager:
    """任务队列 + 单工作线程。

    测试里把 ``runners`` 换成立刻返回假 payload 的函数，就能在不加载任何模型的情况下
    跑通 HTTP 层、SSE 与打包逻辑。
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        runners: Mapping[str, StageRunner] | None = None,
        max_upload_mb: float = 512.0,
        workers: int = 1,
        lock_params: bool = False,
        option_defaults: Mapping[str, Any] | None = None,
        stall_timeout: float | Mapping[str, float] | None = None,
        hard_timeout: float | Mapping[str, float] | None = None,
        watchdog_interval: float = 1.0,
        trash_max_days: float = TRASH_MAX_DAYS,
        trash_max_bytes: float = TRASH_MAX_BYTES,
        trash_purge_interval: float = TRASH_PURGE_INTERVAL,
        use_trash: bool = True,
        probe_upload: bool = False,
    ) -> None:
        self.root = Path(root) if root is not None else PROJECT_ROOT / "tmp" / "webui"
        self.max_upload_mb = float(max_upload_mb)
        #: 「4G 特供」模式：参数被服务端锁死（页面只读），PATCH `/options` 直接 409。
        self.lock_params = bool(lock_params)
        #: 新工程的参数默认值。服务端用 `--asr-model` 指定了别的识别模型时，
        #: 「模型」那一栏的默认值要跟着它走，否则页面上的默认值会把它悄悄覆盖掉。
        self.option_defaults: dict[str, Any] = dict(option_defaults or default_options())
        self._runners: dict[str, StageRunner] = dict(runners or {})
        self._workers = max(1, int(workers))
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []            # 新的在前
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.RLock()
        self._bundle_lock = threading.Lock()
        self._stop = threading.Event()
        #: 阶段守护（G6）：无进展超过 `stall_timeout` 或总时长超过 `hard_timeout`
        #: 就把阶段判失败。测试里传一个很小的数就能验证（见 `test_watchdog_*`）。
        self.stall_timeout = _per_phase(stall_timeout, STALL_TIMEOUT)
        self.hard_timeout = _per_phase(hard_timeout, HARD_TIMEOUT)
        self.watchdog_interval = max(0.05, float(watchdog_interval))
        self._watch: dict[tuple[str, str], _PhaseWatch] = {}
        self._watchdog: threading.Thread | None = None
        #: 软删除（G2）：删除 = 挪进 `.trash/`，能恢复；`use_trash=False` 是硬删（测试用）
        self.use_trash = bool(use_trash)
        self.trash_max_days = float(trash_max_days)
        self.trash_max_bytes = float(trash_max_bytes)
        #: 回收站也定时扫（见 `TRASH_PURGE_INTERVAL`）：只靠"启动 + 删除"的话，
        #: 服务连开一周、用户又没删过东西，过期条目是清不掉的。
        self.trash_purge_interval = max(0.05, float(trash_purge_interval))
        self._trash_stop = threading.Event()
        self._trash_worker: threading.Thread | None = None
        #: 上传后是否做内容体检（G10）。默认关——直接 `JobManager(...)` 的测试不上 ffmpeg；
        #: 产品入口 `create_app` 会显式打开。
        self.probe_upload = bool(probe_upload)
        #: 这台机器实际会跑在什么设备上（`"cuda"` / `"cpu"` / `None` = 不知道）。
        #: `create_app` 用 `RunnerSession.describe()` 的结果填它；**只影响耗时预估的系数**（G20），
        #: 不影响真跑哪台设备（那是 `EngineProvider.config_for` 的事）。
        self.device_hint: str | None = None
        self.root.mkdir(parents=True, exist_ok=True)
        # **重启不丢**：把 output/ 里已有的工程读回来（WebUI 建的读 job.json，
        # 命令行跑出来的当"只读产物"收进来）
        self._load_existing()
        self._purge_trash()
        self._trash_worker = threading.Thread(target=self._purge_loop,
                                              name="sheetsage-trash", daemon=True)
        self._trash_worker.start()

    # ---------------------------------------------------------------- 启动恢复
    def _load_existing(self) -> None:
        """扫描数据根（默认 `<项目根>/output/`），一个子目录 = 一个工程。

        - 有 `job.json` → WebUI 建的工程，按元数据恢复（上次没跑完的阶段标成中断）；
        - 没有 `job.json` 但有 `project.json`/`report.json` → 别处跑出来的产物，
          作为**只读**工程收进列表（能看卷帘 / 下载 / 导 SVP）；
        - 其它目录（比如空的、放别的东西的）跳过，不动它。
        """
        if not self.root.is_dir():
            return
        restored: list[Job] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            job = self._restore(entry)
            if job is not None:
                restored.append(job)
        restored.sort(key=lambda item: item.created_at, reverse=True)
        with self._lock:
            for job in restored:
                self._jobs[job.id] = job
                self._order.append(job.id)
        if restored:
            logos = [job.name for job in restored[:5]]
            print(f"  已载入 {len(restored)} 个工程：{', '.join(logos)}"
                  + (" …" if len(restored) > 5 else ""), flush=True)

    def _restore(self, root: Path) -> Job | None:
        meta_path = root / "job.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = None
            if isinstance(meta, dict):
                return self._job_from_meta(root, meta)
        if (root / "project.json").is_file() or (root / "report.json").is_file():
            return self._import(root)
        return None

    def _job_from_meta(self, root: Path, meta: Mapping[str, Any]) -> Job:
        audio = str(meta.get("audio") or "")
        # `job.json` 里存的是绝对路径；**包（或数据目录）被整体搬走**时就对不上了，
        # 这时候按布局重新算一遍（工程目录 + `.webui/input/<文件名>`）。
        upload = Path(str(meta.get("upload") or ""))
        if not upload.is_file():
            candidate = root / INTERNAL_DIRNAME / "input" / audio
            if not candidate.is_file():
                # 输入文件带 uuid 前缀（G9）：绝对路径对不上时按"任意前缀 + 展示名"再找一次。
                input_dir = root / INTERNAL_DIRNAME / "input"
                if input_dir.is_dir():
                    for item in sorted(input_dir.iterdir()):
                        if item.is_file() and item.name.endswith(f"-{audio}"):
                            candidate = item
                            break
            upload = candidate if candidate.is_file() else root / INTERNAL_DIRNAME / "input"
        job = Job(
            id=str(meta.get("id") or root.name),
            name=str(meta.get("name") or root.name),
            audio=audio,
            root=root,
            upload=upload,
            out_dir=root,
            created_at=float(meta.get("created_at") or root.stat().st_mtime),
            options=dict(self.option_defaults, **(meta.get("options") or {})),
        )
        for phase in PHASES:
            saved = (meta.get("stages") or {}).get(phase) or {}
            run = job.stages[phase]
            run.label = str(saved.get("label") or run.label)
            run.progress = float(saved.get("progress") or 0.0)
            run.detail = dict(saved.get("detail") or {})
            run.started_at = saved.get("started_at")
            run.finished_at = saved.get("finished_at")
            state = str(saved.get("state") or "idle")
            if state in {"queued", "running"}:
                # 上次是在这个进程里跑的，进程没了就永远不会再推进——别装成"还在跑"
                run.state = "idle"
                run.progress = 0.0
                run.step = ""
                run.message = "上次运行被中断（程序重启），可以重跑"
            else:
                run.state = state
                run.error = saved.get("error")
                run.error_code = saved.get("error_code")
                run.error_action = saved.get("error_action")
                run.error_detail = saved.get("error_detail")
        job.files = scan_files(root)
        job.highlights = meta.get("highlights")
        job.decisions = meta.get("decisions")
        job.summary = meta.get("summary")
        job.warnings = list(meta.get("warnings") or [])
        job.rev = int(meta.get("rev") or 1)
        job.artifacts_rev = int(meta.get("artifacts_rev") or 0)
        interrupted = any(run.message.startswith("上次运行被中断")
                          for run in job.stages.values())
        job.state = DRAFT_STATE if interrupted else str(meta.get("state") or "done")
        job.message = "上次运行被中断（程序重启），可以重跑" if interrupted else "已恢复"
        job.emit("log", phase=None, message="工程已恢复（重启后从 output/ 载入）"
                 + ("；上次没跑完，可以重跑" if interrupted else ""))
        return self._attach_eta(job)

    def _import(self, root: Path) -> Job:
        """`output/` 里本来就有的产物目录：只读收编。"""
        created = root.stat().st_mtime
        job = Job(
            id=root.name, name=root.name, audio="", root=root,
            upload=root / INTERNAL_DIRNAME / "input",
            out_dir=root, created_at=created,
            options=dict(self.option_defaults), imported=True,
        )
        transcribed = (root / "project.json").is_file() or (root / "report.json").is_file()
        if transcribed:
            job.stages["transcribe"].state = "done"
            job.stages["transcribe"].label = PHASE_LABELS["transcribe"]
        if (root / "lyrics.json").is_file():
            job.stages["lyrics"].state = "done"
        job.state = "done" if transcribed else DRAFT_STATE
        job.message = "已有产物（只读）"
        job.files = scan_files(root)
        job.highlights = self._highlights_from_disk(root)
        job.emit("log", phase=None,
                 message="从 output/ 载入已有产物（只读：能看卷帘 / 下载 / 导 SVP，不能重跑）")
        return self._attach_eta(job)

    @staticmethod
    def _highlights_from_disk(root: Path) -> dict[str, Any] | None:
        """给只读工程补一份 `highlights`（产物里有 project/report/summary 就能补）。"""

        def load(name: str) -> Any:
            path = root / name
            if not path.is_file():
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

        project = load("project.json")
        if not isinstance(project, dict):
            return None
        payload = {"project": project, "summary": load("summary.json") or {},
                   "decisions": ((load("report.json") or {}).get("decisions")
                                 if isinstance(load("report.json"), dict) else {})}
        try:
            from .runner import build_highlights
        except ImportError:                      # pragma: no cover - 只有拆包用了才发生
            return None
        try:
            return build_highlights(payload)
        except Exception:                        # noqa: BLE001 - 产物不齐时别把恢复搞崩
            return None

    # ---------------------------------------------------------------- 配置
    def set_runners(self, runners: Mapping[str, StageRunner]) -> None:
        self._runners.update(runners)

    @property
    def runners(self) -> Mapping[str, StageRunner]:
        return self._runners

    def env(self) -> dict[str, Any]:
        with self._lock:
            running = next((j.id for j in self._jobs.values() if j.state == "running"), None)
            pending = sum(1 for j in self._jobs.values() if j.state == "queued")
            return {"data_root": str(self.root), "max_upload_mb": self.max_upload_mb,
                    "params_locked": self.lock_params,
                    "queue": {"pending": pending, "running": running},
                    "disk": self.disk_info()}

    def disk_info(self) -> dict[str, int]:
        """数据根所在磁盘的剩余/总量（G11：顶栏显示"剩余 12.3 GB"，< 2GB 变黄）。"""
        try:
            usage = shutil.disk_usage(self.root)
        except OSError:
            return {"free": 0, "total": 0, "used": 0}
        return {"free": usage.free, "total": usage.total, "used": usage.used}

    # ---------------------------------------------------------------- 耗时预估（G20）
    def set_device_hint(self, device: str | None) -> None:
        """告诉管理器"这台机器实际跑在哪"（`create_app` 用 `RunnerSession.describe()` 填）。"""
        self.device_hint = str(device) if device else None

    def audio_seconds(self, job: Job) -> float | None:
        """音频有多长（秒）。三个来源按可信度排：上传体检 > 产物摘要 > 概要。

        都没有就返回 `None`：那时不给预估（界面不显示），比编一个数字强。
        """
        for source in (job.audio_info, job.summary, job.highlights):
            if not isinstance(source, Mapping):
                continue
            try:
                value = float(source.get("duration_seconds") or 0.0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    def _device_for(self, job: Job) -> str:
        """这一趟按什么口径估：工程自己选的「设备」优先，其次是本机的实际情况。

        `auto` + 本机没检测到显卡时，`create_app` 已经把 `device_hint` 设成 `cpu` 了；
        两个都不知道就按显卡估（有卡的机器远多于没卡的，且**低估**比高估更不吓人——
        真跑慢了，进度条上的"上次进展 xx 秒前"会自己说明问题）。
        """
        wanted = str(job.options.get("device") or "auto").strip().lower()
        if wanted.startswith("cpu"):
            return "cpu"
        if wanted.startswith("cuda"):
            return "cuda"
        hint = str(self.device_hint or "").strip().lower()
        if hint.startswith("cpu"):
            return "cpu"
        return "cuda"

    def eta_for(self, job: Job, phase: str) -> Mapping[str, Any] | None:
        """某个阶段的耗时预估（G20）。纯计算，不读写磁盘。"""
        return estimate_seconds(phase, self.audio_seconds(job), self._device_for(job),
                                job.options)

    def _attach_eta(self, job: Job) -> Job:
        """给工程挂上预估提供者（`Job.snapshot()` 里按阶段调它）。"""
        job.eta_provider = self.eta_for
        return job

    # ---------------------------------------------------------------- 生命周期
    def _ensure_workers(self) -> None:
        with self._lock:
            if self._threads:
                return
            for index in range(self._workers):
                thread = threading.Thread(target=self._work, name=f"sheetsage-worker-{index}",
                                          daemon=True)
                thread.start()
                self._threads.append(thread)
            if self._watchdog is None:
                self._watchdog = threading.Thread(target=self._watch_stages,
                                                  name="sheetsage-watchdog", daemon=True)
                self._watchdog.start()

    def _purge_loop(self) -> None:
        """定时清回收站。清理失败只跳过这一轮，绝不把服务带走。"""
        while not self._trash_stop.wait(self.trash_purge_interval):
            try:
                self._purge_trash()
            except Exception:      # noqa: BLE001 - 目录被占用/权限问题都不该影响识别
                continue

    def shutdown(self, timeout: float = 5.0) -> None:
        """请求停止；正在跑的阶段不会被粗暴中断。"""
        self._stop.set()
        self._trash_stop.set()
        if self._trash_worker is not None:
            self._trash_worker.join(timeout=timeout)
            self._trash_worker = None
        for _ in self._threads:
            self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()
        if self._watchdog is not None:
            self._watchdog.join(timeout=timeout)
            self._watchdog = None

    # ---------------------------------------------------------------- 查询
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]

    # ---------------------------------------------------------------- 建任务
    def create(self, filename: str, stream: Any,
               plan: Iterable[str] = DEFAULT_PLAN) -> Job:
        """把上传流写到 ``output/<歌名>/.webui/input/``，**默认什么都不排**。

        ``stream`` 只要有 ``read(n)`` 就行（`UploadFile.file` 就是）。超限会立刻中断并
        删掉半截文件——磁盘不留给失败的上传。

        ``plan`` 非空才排队（`/chain` 与显式 ``plan=`` 用得上）；空 plan 时工程停在
        :data:`DRAFT_STATE`，等用户在页面上选完参数点「开始扒谱」——这就是门禁。
        """
        audio = _check_audio(filename)
        wanted = [phase for phase in plan if phase in PHASES]
        job_id = uuid.uuid4().hex[:12]
        created = time.time()
        # 工程目录 = `output/<歌名>/`（和命令行一个形状；重名绝不覆盖）。
        # **原子认领**（G9）：`mkdir(exist_ok=False)`，并发同名上传各得各的目录，不会互相踩。
        root = claim_project_dir(self.root, project_folder_name(Path(audio).stem))
        upload_dir = root / INTERNAL_DIRNAME / "input"
        upload_dir.mkdir(parents=True, exist_ok=True)
        # 输入文件带 uuid 前缀落盘（G9）：同名文件也绝不互相覆盖；展示名仍存 `job.audio`。
        upload = upload_dir / f"{job_id}-{audio}"

        limit = int(self.max_upload_mb * 1024 * 1024)
        try:
            written = _write_stream(upload, stream, limit, self.max_upload_mb)
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
        if written == 0:
            shutil.rmtree(root, ignore_errors=True)
            raise UploadRejected("上传的是空文件")

        # 内容体检（G10）：ffmpeg 探一下"是不是能播的音频、是不是纯静音"。没 ffmpeg 就跳过。
        audio_info: dict[str, Any] = {}
        if self.probe_upload:
            from .media import probe_audio

            audio_info = probe_audio(upload) or {}
            if audio_info.get("ok") is False:
                reason = str(audio_info.get("reason") or "这个音频用不了")
                shutil.rmtree(root, ignore_errors=True)
                raise UploadRejected(reason)
        fingerprint = content_fingerprint(upload)

        job = Job(id=job_id, name=Path(audio).stem or audio, audio=audio, root=root,
                  upload=upload, out_dir=root, created_at=created,
                  plan=list(wanted), options=dict(self.option_defaults),
                  audio_info=audio_info, audio_fingerprint=fingerprint or None)
        self._attach_eta(job)
        job.state = "queued" if wanted else DRAFT_STATE
        job.message = "等待开始" if wanted else "等待参数"
        for phase in wanted:
            job.stages[phase].state = "queued"
        job.emit("log", phase=None, message=f"新建工程 · {audio}"
                 + ("" if wanted else "（选好参数后点「开始扒谱」）"))
        if fingerprint:
            with self._lock:
                for other in self._jobs.values():
                    if other is not job and other.audio_fingerprint == fingerprint:
                        job.duplicate_of = other.id
                        break
        with self._lock:
            self._jobs[job_id] = job
            self._order.insert(0, job_id)
        self._write_metadata(job)
        if wanted:
            self._ensure_workers()
            self._queue.put(job_id)
        return job

    def run_stage(self, job_id: str, phase: str, *, chain: bool = False) -> Job | None:
        """单独（重）跑一个阶段：清掉它的旧状态，塞回待跑队列。

        工作流是 **扒谱 → 歌词**（原型也是这么写的），所以手动跑歌词**必须先有扒谱**：
        没扒谱就抛 :class:`StageNotReady`（API 回 409"请先完成扒谱"）。

        ``chain=True`` 是例外——那是「⏵ 自动提取（扒谱 → 歌词）」：它**故意**在扒谱还没跑完时
        就把歌词排进队列，所以跳过前置检查（计划里本来就先跑扒谱）。
        """
        job = self.get(job_id)
        if job is None:
            return None
        if phase not in PHASES:
            raise ValueError(f"未知阶段 {phase}")
        if job.imported:
            raise ImportedProject(
                f"{job.name} 是 output/ 里已有的产物（没有原音频），不能在这里重跑；"
                f"要重新扒谱请拖入音频新建工程")
        if phase == "lyrics" and not chain:
            self._assert_transcribed(job)
        with job.lock:
            # 重复入队去重（G14）：已经在排队 / 正在跑就不重新排，否则连点两次会把同一个
            # job_id 塞进队列两次（`--workers 2` 时甚至会并发跑两份）。
            if job.stages[phase].state in ("queued", "running"):
                return job
            # 取消后的"墓碑"（G14）：刚取消又立刻点开始，多半是手抖——直接拒绝，
            # 别让"取消"被这次点击抹掉。
            if job.canceled_at is not None and \
                    time.time() - job.canceled_at < CANCEL_COOLDOWN_SECONDS:
                raise RuntimeError("刚刚取消，稍等几秒再开始")
            run = job.stages[phase]
            run.state = "queued"
            run.progress = 0.0
            run.error = None
            run.step = ""
            run.message = ""
            run.started_at = None
            run.finished_at = None
            job.stages[phase] = run
            if phase not in job.plan:
                job.plan.append(phase)
            # 重跑 = 影响结果，rev +1（G8）；扒谱重跑还连带歌词对不上，必须一起作废
            job.rev += 1
            if phase == "transcribe":
                self._invalidate_lyrics(job, "扒谱重跑")
            job.state = "queued"
            job.cancel_requested = False
            job.canceled_at = None
        job.emit("log", phase=phase, message=f"{PHASE_LABELS[phase]}：已加入队列")
        self._write_metadata(job)
        self._ensure_workers()
        self._queue.put(job_id)
        return job

    def run_chain(self, job_id: str) -> Job | None:
        """「自动提取」：扒谱 → 歌词，一条龙排进队列（两个都重跑）。"""
        job = self.run_stage(job_id, "transcribe")
        if job is None:
            return None
        return self.run_stage(job_id, "lyrics", chain=True)

    def _assert_transcribed(self, job: Job) -> None:
        """歌词识别的前置条件：扒谱已经完成。

        "有扒谱但没填词"是完全正常的状态——那正是要做歌词识别的时候，所以这里只看扒谱。
        """
        with job.lock:
            state = job.stages["transcribe"].state
        if state != "done":
            raise StageNotReady("请先完成扒谱")

    # ---------------------------------------------------------------- 可调项
    def set_options(self, job_id: str, expected_rev: int | None = None,
                    **values: Any) -> Job | None:
        """改工程级的可调参数（页面上的参数面板，见 :data:`PARAM_SPECS`）。

        改参数 = 换口径，旧结果就不作数了（和换音源、重跑扒谱一个道理）：

        - 扒谱参数（精度 / 设备 / 范围 / 量化）变了 -> 扒谱退回「待开始」，歌词一并作废；
        - 歌词参数（模型 / 音源 / 语言）变了 -> 只作废歌词。

        值不合法抛 :class:`ValueError`（API 回 400）；参数所属的阶段正在跑抛
        :class:`RuntimeError`（API 回 409）；4G 特供模式一律拒绝。

        **乐观锁**（G8）：传了 `expected_rev` 而它和工程当前的 `rev` 不一致，说明调用方是
        基于旧快照改的，抛 :class:`StaleRevision`（API 回 409 + 当前快照），而不是静默覆盖。
        """
        job = self.get(job_id)
        if job is None:
            return None
        if job.imported:
            raise ImportedProject(
                f"{job.name} 是 output/ 里已有的产物（没有原音频、也没有当时的参数），"
                f"不能在这里改；要重跑请拖入音频新建工程")
        if self.lock_params:
            raise RuntimeError("当前是 4G 特供模式，参数已由服务端锁定")
        values = validate_options(values)
        self._assert_stages_idle(job, {PARAM_OWNER[key] for key in values}, "改参数")
        with job.lock:
            if expected_rev is not None and int(expected_rev) != job.rev:
                raise StaleRevision(job.rev)
            before = dict(job.options)
            job.options.update(values)
            changed = {key for key, value in values.items()
                       if before.get(key) != job.options.get(key)}
            if not changed:
                return job
            # 任何影响结果的变更都 +1（G8）：rev 管"两个标签页会不会互相覆盖"，
            # 和 G3 的 artifacts_rev（管产物缓存失效）是两码事。
            job.rev += 1
            why = describe_change(before, job.options)
            invalidated = False
            if any(PARAM_OWNER[key] == "transcribe" for key in changed):
                invalidated = self._invalidate_transcribe(job, why)
            invalidated = self._invalidate_lyrics(job, why) or invalidated
            if not invalidated:
                job.emit("log", phase=None, message=f"参数：{why}")
        self._write_metadata(job)
        return job

    def _assert_stages_idle(self, job: Job, phases: Iterable[str], action: str) -> None:
        """参数所属的阶段正在跑就不让改——那一趟用的是改动前的值，改了界面会和结果对不上。"""
        with job.lock:
            for phase in phases:
                if phase in PHASES and job.stages[phase].state == "running":
                    raise RuntimeError(f"{PHASE_LABELS[phase]}正在跑，{action}请等它结束或先取消")

    # ---------------------------------------------------------------- 音源
    def attach_stem(self, job_id: str, kind: str, filename: str, stream: Any) -> Job | None:
        """给工程补一个音源（目前只有歌词用的 `vocal` 干声）。

        **不做内容校验**：用户故意把混音原曲（甚至别的歌）传进"干声"栏也是允许的——
        那是他自己的选择，我们只如实记录用了哪条。有干声就用干声，没有就用混音原曲，
        页面上不暴露这个选择。

        换音源会让已经识别出来的歌词对不上（音源变了），所以跟重跑扒谱一样作废重来。
        """
        job = self.get(job_id)
        if job is None:
            return None
        if job.imported:
            raise ImportedProject(f"{job.name} 是只读的已有产物，不能补音源")
        if kind not in STEM_KINDS:
            raise ValueError(f"未知音源 {kind}")
        self._assert_lyrics_idle(job, "换音源")
        audio = _check_audio(filename)
        target_dir = job.stems_dir / kind
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / audio
        limit = int(self.max_upload_mb * 1024 * 1024)
        try:
            written = _write_stream(target, stream, limit, self.max_upload_mb)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        if written == 0:
            target.unlink(missing_ok=True)
            raise UploadRejected("上传的是空文件")

        with job.lock:
            previous = job.stems.get(kind)
            job.stems[kind] = {"kind": kind, "name": audio, "path": str(target),
                               "size": written, "created_at": time.time()}
            if previous and previous.get("name") != audio:
                (target_dir / str(previous["name"])).unlink(missing_ok=True)
            job.rev += 1                                # 音源变了 = 影响结果（G8）
            self._invalidate_lyrics(job, f"歌词音源换成 {audio}")
        job.emit("log", phase="lyrics",
                 message=f"{STEM_LABELS[kind]}已就位：{audio}"
                         if not previous else f"{STEM_LABELS[kind]}已更换：{audio}")
        self._write_metadata(job)
        return job

    def detach_stem(self, job_id: str, kind: str) -> Job | None:
        """移除音源，歌词识别自动回退到混音原曲。"""
        job = self.get(job_id)
        if job is None:
            return None
        if job.imported:
            raise ImportedProject(f"{job.name} 是只读的已有产物，不能改音源")
        if kind not in STEM_KINDS:
            raise ValueError(f"未知音源 {kind}")
        self._assert_lyrics_idle(job, "改音源")
        with job.lock:
            info = job.stems.pop(kind, None)
            if info:
                Path(str(info["path"])).unlink(missing_ok=True)
            job.rev += 1                                # 音源变了 = 影响结果（G8）
            self._invalidate_lyrics(job, f"已移除{STEM_LABELS[kind]}，回退到混音原曲")
        if info:
            job.emit("log", phase="lyrics",
                     message=f"已移除{STEM_LABELS[kind]}：歌词识别将用混音原曲")
            self._write_metadata(job)
        return job

    def _assert_lyrics_idle(self, job: Job, action: str) -> None:
        """只有**歌词阶段正在跑**的时候才不许动音源——它正拿这条音频在切片。

        不用 `job.state == "running"` 来判：阶段跑完到任务收尾之间有一个很短的窗口，
        那时任务还是 running，但歌词早就写完了，拦下来只会让人莫名其妙。
        """
        with job.lock:
            if job.stages["lyrics"].state == "running":
                raise RuntimeError(f"歌词识别正在跑，{action}请等它结束或先取消")

    def _invalidate_transcribe(self, job: Job, why: str) -> bool:
        """扒谱参数变了：已经扒出来的谱（连带歌词）不再是"当前结果"，退回待跑。

        返回是否真的作废了什么——没有任何已完成的结果时就不必刷日志。
        """
        run = job.stages["transcribe"]
        if run.state not in STAGE_TERMINAL:
            return False
        job.stages["transcribe"] = StageRun("transcribe", PHASE_LABELS["transcribe"])
        job.emit("log", phase="transcribe", message=f"{why}：已有的扒谱作废，需要重新扒谱")
        return True

    def _invalidate_lyrics(self, job: Job, why: str) -> bool:
        """音源/谱面/参数变了，已识别的歌词就不作数了（调用方持有 job.lock）。"""
        run = job.stages["lyrics"]
        if run.state not in STAGE_TERMINAL:
            return False
        job.stages["lyrics"] = StageRun("lyrics", PHASE_LABELS["lyrics"])
        job.emit("log", phase="lyrics", message=f"{why}：已识别的歌词作废，需要重跑")
        return True

    # ---------------------------------------------------------------- 取消/删除
    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return None
        with job.lock:
            if job.state in TERMINAL_STATES:
                return job
            job.cancel_requested = True
            job.canceled_at = time.time()
            if job.state in (DRAFT_STATE, "queued"):
                # 还没开跑（或停在「等参数」），直接落终态，工作线程拿到时会跳过
                job.state = "cancelled"
                for phase in list(job.plan):
                    job.stages[phase].state = "cancelled"
                job.plan.clear()
                job.stage = None
        job.emit("cancelled", phase=job.stage, message="已在排队阶段取消")
        self._write_metadata(job)
        return job

    def delete(self, job_id: str) -> bool:
        """删除任务与它的全部产物。正在跑的任务必须先取消。

        `output/` 里**本来就有的**产物不在这里删：那是命令行 / 别处跑出来的东西，
        删它等于删用户的作品——要让列表干净请手动删目录（提示里会写清楚）。
        """
        job = self.get(job_id)
        if job is None:
            return False
        if job.imported:
            raise ImportedProject(
                f"{job.name} 是 output/ 里已有的产物，不能从这里删"
                f"（要删请手动删目录 {job.root}）")
        if job.state == "running":
            raise RuntimeError("任务正在运行，请先取消")
        with self._lock:
            self._jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)
        shutil.rmtree(job.root, ignore_errors=True)
        return True

    # ---------------------------------------------------------------- 打包
    def bundle(self, job: Job) -> Path:
        """把 ``output/`` 打成一个 zip（懒生成，落盘缓存）。

        缓存**跟着产物版本走**（G3）：`bundle.meta.json` 里记着打包时的 `artifacts_rev`，
        重跑过（rev 变了）就一定重打，不会把上一版的结果发出去。

        打包含一把独立的锁：它跑在请求线程里，不能占用 ``job.lock``——那个锁还要给
        工作线程汇报进度，一个几百 MB 的产物目录会把进度卡住。
        """
        target = job.internal_dir / "bundle.zip"
        meta = job.internal_dir / "bundle.meta.json"
        with self._bundle_lock:
            if target.is_file() and self._bundle_is_fresh(job, meta):
                return target
            listed = self.manifest_files(job)
            files = [item for item in scan_files(job.out_dir)
                     if listed is None or item["path"] in listed]
            if not files:
                raise FileNotFoundError("还没有产物可打包")
            job.internal_dir.mkdir(parents=True, exist_ok=True)
            temp = job.internal_dir / "bundle.zip.part"
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6) as zf:
                for item in files:
                    path = job.out_dir / item["path"]
                    if not path.is_file():          # 清单里列着但磁盘上没了：跳过
                        continue
                    zf.write(path, arcname=f"{safe_name(job.name)}/{item['path']}")
            temp.replace(target)
            try:
                write_text_atomic(meta, json.dumps({
                    "artifacts_rev": job.artifacts_rev,
                    "files": len(files),
                    "built_at": time.time(),
                }, ensure_ascii=False))
            except OSError:
                pass
        return target

    @staticmethod
    def _bundle_is_fresh(job: Job, meta: Path) -> bool:
        """缓存还认不认：版本戳一致 + 清单在 + 包里有东西。"""
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        try:
            stamped = int(data.get("artifacts_rev", -1))
        except (TypeError, ValueError):
            return False
        return stamped == job.artifacts_rev and int(data.get("files") or 0) > 0

    # ---------------------------------------------------------------- 删除 / 回收站
    @property
    def trash_root(self) -> Path:
        return self.root / TRASH_DIRNAME

    def delete(self, job_id: str, *, permanent: bool = False) -> bool:
        """删除工程。默认是**软删除**：目录 rename 进 `.trash/`，能恢复（G2）。

        为什么不是直接 `rmtree`：删除按钮就在「下载 ▾」旁边，手指偏 20 像素就是几小时的
        活全没（连原音频都一起没了）。`rename` 是原子的，用户感知和"删了"一样。

        `output/` 里**本来就有的**产物不在这里删：那是命令行 / 别处跑出来的东西，
        删它等于删用户的作品。
        """
        job = self.get(job_id)
        if job is None:
            return False
        if job.imported:
            raise ImportedProject(
                f"{job.name} 是 output/ 里已有的产物，不能从这里删"
                f"（要删请手动删目录 {job.root}）")
        if job.state == "running":
            raise RuntimeError("任务正在运行，请先取消")
        with self._lock:
            self._jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)
        if permanent or not self.use_trash:
            shutil.rmtree(job.root, ignore_errors=True)
            return True
        self.trash_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        entry = self.trash_root / f"{safe_name(job.name)}-{job.id}-{stamp}"
        try:
            move_into(job.root, entry)
        except OSError:
            # 挪不进回收站就别硬删：宁可报错让用户重试，也不能把东西弄丢
            with self._lock:
                self._jobs[job_id] = job
                self._order.insert(0, job_id)
            raise RuntimeError(f"删除失败：工程目录被占用（{job.root}），关掉相关程序再试")
        self._purge_trash()
        return True

    def trash(self) -> list[dict[str, Any]]:
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

    def restore(self, entry_id: str) -> Job | None:
        """把回收站里的工程挪回 `output/`（重名就 `-2`），并重新登记。"""
        source = self.trash_root / safe_name(entry_id)
        if not source.is_dir() or TRASH_DIRNAME not in str(source):
            return None
        try:
            meta = json.loads((source / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        base = str(meta.get("name") or source.name)
        target = unique_project_dir(self.root, project_folder_name(base))
        move_into(source, target)
        job = self._restore(target)
        if job is None:
            return None
        with self._lock:
            if job.id in self._jobs:                  # id 撞了（极少见）：挪个名字
                job.id = f"{job.id}-{uuid.uuid4().hex[:4]}"
            self._jobs[job.id] = job
            self._order.insert(0, job.id)
        job.emit("log", phase=None, message=f"已从回收站恢复：{job.name}")
        self._write_metadata(job)
        return job

    def purge_from_trash(self, entry_id: str) -> bool:
        """彻底删掉回收站里的一项（不可恢复）。"""
        target = self.trash_root / safe_name(entry_id)
        if not target.is_dir() or TRASH_DIRNAME not in str(target):
            return False
        shutil.rmtree(target, ignore_errors=True)
        return True

    def _purge_trash(self) -> None:
        """回收站保留策略：超过 7 天、或总共超过 2GB，就真删最旧的。"""
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
        deadline = time.time() - self.trash_max_days * 86400.0
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
            if mtime < deadline or total > self.trash_max_bytes:
                shutil.rmtree(entry, ignore_errors=True)
                total -= size

    # ---------------------------------------------------------------- 看门狗
    def _watch_begin(self, job: Job, phase: str) -> _PhaseWatch:
        watch = _PhaseWatch(job.id, phase, time.time())
        with self._lock:
            self._watch[(job.id, phase)] = watch
        return watch

    def _watch_end(self, job: Job, phase: str) -> None:
        with self._lock:
            self._watch.pop((job.id, phase), None)

    def _watch_stages(self) -> None:
        """每秒扫一遍在跑的阶段：**无进展**太久或总时长超上限就判失败（G6）。

        为什么要它：推理卡死（驱动/显存/cuBLAS）时一个进度回调都不会来，用户点「停止」
        也没用（协作式取消要等下个检查点），界面就永远停在 62%。这里从**另一个线程**
        判死并把 `cancel_requested` 置上，让工作线程在下个检查点退出、并把阶段标成
        失败 + 人话（"卡住了 N 分钟没有进展"）。
        """
        while not self._stop.wait(self.watchdog_interval):
            with self._lock:
                watches = list(self._watch.values())
            for watch in watches:
                if watch.fired:
                    continue
                job = self.get(watch.job_id)
                if job is None:
                    continue
                with job.lock:
                    run = job.stages.get(watch.phase)
                    if run is None or run.state != "running":
                        continue
                    now = time.time()
                    started = run.started_at or watch.started_at
                    last = run.last_advance_at or started
                    idle = max(0.0, now - last)
                    elapsed = max(0.0, now - started)
                stall = self.stall_timeout.get(watch.phase, 0.0)
                hard = self.hard_timeout.get(watch.phase, 0.0)
                if stall and idle >= stall:
                    self._abort_phase(
                        watch, "E_STALLED",
                        f"卡住了 {_duration_text(idle)}没有任何进展",
                        f"最后一次进展 {_duration_text(idle)}前；"
                        f"这一阶段已运行 {_duration_text(elapsed)}")
                elif hard and elapsed >= hard:
                    self._abort_phase(
                        watch, "E_TIMEOUT",
                        f"这一阶段跑了 {_duration_text(elapsed)}，超过上限",
                        f"启动于 {_duration_text(elapsed)}前，最后一次进展 "
                        f"{_duration_text(idle)}前")

    def _abort_phase(self, watch: _PhaseWatch, code: str, message: str,
                     detail: str = "") -> None:
        """看门狗判死一个阶段：落失败态 → 发事件 → 让工作线程退出。"""
        job = self.get(watch.job_id)
        if job is None:
            return
        watch.fired = True
        with job.lock:
            run = job.stages.get(watch.phase)
            if run is None or run.state != "running":
                return
            run.fail(StageFailure(code, message, detail=detail))
            run.finished_at = time.time()
            run.message = message
            job.cancel_requested = True          # 让 runner 在下个检查点退出
            job.stage = None
            error = {"code": run.error_code, "message": run.error,
                     "action": run.error_action, "detail": run.error_detail}
        job.emit("phase_error", phase=watch.phase, **error)
        job.emit("log", phase=watch.phase, message=f"已强制中止：{message}")
        self._write_metadata(job)

    # ---------------------------------------------------------------- 工作线程
    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None:
                return
            job = self.get(job_id)
            if job is None:
                continue
            try:
                self._run(job)
            except Exception as exc:  # 工作线程绝不能死掉
                self._fail(job, exc)

    def _next_phase(self, job: Job) -> str | None:
        with job.lock:
            while job.plan:
                phase = job.plan.pop(0)
                if job.stages[phase].state in STAGE_TERMINAL:
                    continue
                return phase
        return None

    def _run(self, job: Job) -> None:
        with job.lock:
            if job.cancel_requested:
                job.state = "cancelled"
                return
            job.state = "running"
        while True:
            phase = self._next_phase(job)
            if phase is None:
                break
            self._run_phase(job, phase)
            with job.lock:
                if job.cancel_requested:
                    break
        self._finish(job)

    def _run_phase(self, job: Job, phase: str) -> None:
        run = job.stages[phase]
        with job.lock:
            run.state = "running"
            run.started_at = time.time()
            run.last_advance_at = run.started_at
            run.finished_at = None
            run.error = None
            run.error_code = None
            run.error_action = None
            run.error_detail = None
            job.stage = phase
        # 这一阶段会重写产物：上一版的派生缓存（zip / SVP / 波形）一定过期了，
        # 旧产物先挪进 `.webui/prev/`——`output/` 永远只表示"当前这一版"。
        self._clean_derived(job)
        self._quarantine_stage_outputs(job, phase)
        # 开跑就把预估推给界面（G20）：不然用户要等下一次快照拉取才看到"预计几分钟"。
        job.emit("phase", phase=phase, state="running", label=run.label,
                 eta=job.stage_eta(run))
        runner = self._runners.get(phase)
        if runner is None:
            from .runner import default_runners

            self.set_runners(default_runners())
            runner = self._runners[phase]
        progress = Progress(job, phase)
        watch = self._watch_begin(job, phase)
        try:
            self._assert_disk(job, phase)
            payload = runner(job, progress) or {}
        except JobCancelled:
            if not watch.fired:                 # 看门狗判过死就别覆盖它的失败态
                with job.lock:
                    run.state = "cancelled"
                    run.finished_at = time.time()
                job.emit("phase_cancelled", phase=phase, message="阶段已取消")
        except Exception as exc:
            if not watch.fired:                 # 同上：卡死/超时的结论优先
                with job.lock:
                    run.fail(exc)
                    run.finished_at = time.time()
                    error = {"code": run.error_code, "message": run.error,
                             "action": run.error_action, "detail": run.error_detail}
                job.emit("phase_error", phase=phase, **error)
        else:
            detail = payload.get("detail") or {}
            if watch.fired:
                # 判死之后 runner 才回来：保留失败态，但把这一轮的真实统计收下
                with job.lock:
                    run.detail.update(detail)
            else:
                self._settle_phase(job, phase, detail)
        finally:
            self._watch_end(job, phase)
            with job.lock:
                job.stage = None
            self._write_metadata(job)

    # ---------------------------------------------------------------- 完成门禁
    def _settle_phase(self, job: Job, phase: str, detail: Mapping[str, Any]) -> None:
        """阶段收尾：**先体检产物，通过了才算 done**（G4）。"""
        failure: StageFailure | None = None
        with job.lock:
            run = job.stages[phase]
            run.detail.update(detail)
            failure = self._completion_gate(job, phase, run.detail)
            if failure is None:
                run.state = "done"
                run.progress = 1.0
                run.finished_at = time.time()
                # 产物换了一版：所有派生缓存（zip / SVP / peaks）都靠它失效
                job.artifacts_rev += 1
                error = None
            else:
                run.fail(failure)
                run.finished_at = time.time()
                error = {"code": run.error_code, "message": run.error,
                         "action": run.error_action, "detail": run.error_detail}
        if error is None:
            job.emit("phase_done", phase=phase, detail=run.detail)
            self._flush_prev(job)
        else:
            job.emit("phase_error", phase=phase, **error)
        self.write_manifest(job)

    def _completion_gate(self, job: Job, phase: str,
                         detail: Mapping[str, Any]) -> StageFailure | None:
        """"完成"的依据不是"runner 没抛异常"，而是**产物真的齐了**。

        扒谱要 `project.json` / `report.json` / `summary.json` 都在、都可解析；
        歌词要 `lyrics.json` 可解析且工程还在。runner 自己做的体检
        （`detail["artifacts"]`）不通过也算失败——那是最细的一层（15 个文本 + 6 个 MIDI）。
        """
        report = detail.get("artifacts")
        if isinstance(report, Mapping) and not report.get("ok", True):
            missing = list(report.get("missing") or [])
            problems = list(report.get("problems") or [])
            text = "产物自检没通过"
            if missing:
                text += f"：缺 {len(missing)} 个文件（{', '.join(missing[:4])}"
                text += " 等）" if len(missing) > 4 else "）"
            elif problems:
                text += f"：{problems[0]}"
            return StageFailure(
                "E_ARTIFACTS_INCOMPLETE", text,
                "重跑这一阶段；如果反复出现，点顶栏「诊断包」发给任意 AI 帮你看",
                "；".join(problems) or f"missing={missing}")

        required = ("project.json", "report.json", "summary.json") \
            if phase == "transcribe" else ("project.json", "lyrics.json")
        missing = [name for name in required
                   if not (job.out_dir / name).is_file()
                   or (job.out_dir / name).stat().st_size == 0]
        if missing:
            return StageFailure(
                "E_ARTIFACTS_INCOMPLETE", f"产物不完整：缺 {', '.join(missing)}",
                "重跑这一阶段；如果反复出现，点顶栏「诊断包」发给任意 AI 帮你看",
                f"{phase} 结束时缺 {missing}")
        for name in required:
            if not name.endswith(".json"):
                continue
            try:
                json.loads((job.out_dir / name).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return StageFailure(
                    "E_ARTIFACTS_INCOMPLETE", f"{name} 读不出来（可能只写了一半）",
                    "重跑这一阶段；如果反复出现，点顶栏「诊断包」发给任意 AI 帮你看",
                    f"{name}: {type(exc).__name__}: {exc}")
        return None

    # ---------------------------------------------------------------- 产物版本
    @staticmethod
    def _derived_patterns() -> tuple[str, ...]:
        return ("bundle.zip", "bundle.meta.json", "peaks-*.json")

    def _clean_derived(self, job: Job) -> None:
        """阶段开始前清掉上一版的派生缓存（它们都能重算，留着只会发旧货）。"""
        internal = job.internal_dir
        for pattern in self._derived_patterns():
            for path in internal.glob(pattern):
                path.unlink(missing_ok=True)
        export = internal / "export"
        if export.is_dir():
            for path in export.glob("*.svp*"):
                path.unlink(missing_ok=True)

    # ---------------------------------------------------------------- 磁盘（G11）
    def _disk_estimate(self, job: Job) -> int:
        """一次阶段大概需要多少字节（够粗就行：音频 × 3 + 50MB）。"""
        try:
            audio_size = job.upload.stat().st_size if job.upload.is_file() else 0
        except OSError:
            audio_size = 0
        return int(audio_size * 3 + 50 * 1024 * 1024)

    def _assert_disk(self, job: Job, phase: str) -> None:
        """阶段开跑前的磁盘预检：不够就别白跑，直接给人话（而不是写一半 `OSError`）。"""
        try:
            usage = shutil.disk_usage(self.root)
        except OSError:
            return
        needed = self._disk_estimate(job)
        if usage.free < needed:
            raise StageFailure(
                "E_DISK_FULL",
                f"磁盘空间不够：还剩 {_bytes_text(usage.free)}，这一阶段大约需要 "
                f"{_bytes_text(needed)}",
                "清理 output/ 里不要的工程或缓存，或把数据目录换到大一点的盘，再重跑",
                f"free={usage.free} needed={needed} phase={phase}")

    def cleanup(self, job_id: str) -> dict[str, Any]:
        """清掉一个工程的可重算缓存（整包 zip / SVP / 波形 / 旧版本 prev），返回释放了多少。

        「清理」页（G11）用：这些都是能从产物再算出来的东西，删了不影响结果，只影响下次
        下载/画波形时要重算。
        """
        job = self.get(job_id)
        if job is None:
            return {"freed": 0}
        internal = job.internal_dir
        before = 0
        for path in internal.rglob("*"):
            try:
                if path.is_file():
                    before += path.stat().st_size
            except OSError:
                continue
        # bundle / peaks / prev 都能重算；export 里的 SVP 也能重算（产物还在）。
        for pattern in self._derived_patterns():
            for path in internal.glob(pattern):
                path.unlink(missing_ok=True)
        export = internal / "export"
        if export.is_dir():
            shutil.rmtree(export, ignore_errors=True)
        prev = internal / "prev"
        if prev.is_dir():
            shutil.rmtree(prev, ignore_errors=True)
        after = 0
        for path in internal.rglob("*"):
            try:
                if path.is_file():
                    after += path.stat().st_size
            except OSError:
                continue
        freed = max(0, before - after)
        job.emit("log", phase=None,
                 message=f"已清理缓存，释放 {_bytes_text(freed)}（产物本身没动）")
        return {"freed": freed}

    # ---------------------------------------------------------------- 诊断包（G12）
    def diagnose_bundle(self, job_id: str | None = None) -> bytes:
        """把诊断信息打成 zip（`job.json` + 工程 `summary.json` + `/api/env` + 日志）。

        崩溃之后用户能一键发回来的是这个包，而不是一句"它转圈了"。
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            jobs = [self.get(job_id)] if job_id is not None else self.list()
            for job in jobs:
                if job is None:
                    continue
                base = f"jobs/{safe_name(job.id)}/"
                for name in ("job.json", "summary.json"):
                    path = job.root / name
                    if path.is_file():
                        zf.write(path, base + name)
                manifest = job.manifest_path
                if manifest.is_file():
                    zf.write(manifest, base + ".webui/manifest.json")
            zf.writestr("env.json", json.dumps(self.env(), ensure_ascii=False, indent=2))
            from .logs import log_dir

            for log_file in sorted(log_dir(self.root).glob("*.log*")):
                zf.write(log_file, f"logs/{log_file.name}")
        return buffer.getvalue()

    # ---------------------------------------------------------------- 备份 / 迁移（G22）
    def backup_file(self, *, include_audio: bool = True,
                    include_cache: bool = False) -> tuple[Path, dict[str, Any]]:
        """把所有工程打成一个备份 zip，返回 `(临时文件路径, 摘要)`。

        落在系统临时目录而不是数据根里：数据根是用户的东西，别在旁边拉一坨几百 MB 的包
        （而且它会被磁盘预检算进去）。调用方（API）负责把临时目录清掉。
        """
        from . import backup as backup_mod

        projects: list[dict[str, Any]] = []
        pairs: list[tuple[str, Path]] = []
        used: set[str] = set()
        for job in self.list():
            folder = safe_name(job.root.name) or "工程"
            if folder in used:                      # 目录名撞了（极少见）：带一段 id
                folder = f"{folder}-{safe_name(job.id)[:8]}"
            used.add(folder)
            pairs.append((folder, job.root))
            projects.append({
                "id": job.id, "name": job.name, "folder": folder,
                "created_at": job.created_at, "state": job.state,
                "rev": job.rev, "artifacts_rev": job.artifacts_rev,
                "imported": job.imported, "audio": job.audio,
                "duration_seconds": self.audio_seconds(job),
            })
        manifest = backup_mod.build_manifest(
            projects, data_root=self.root,
            include_audio=include_audio, include_cache=include_cache)
        folder = Path(tempfile.mkdtemp(prefix="sheetsage2-backup-"))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = folder / f"sheetsage2-backup-{stamp}.zip"
        summary = backup_mod.write_backup(target, pairs, manifest,
                                          include_audio=include_audio,
                                          include_cache=include_cache)
        summary["projects"] = len(projects)
        return target, summary

    def restore_backup(self, archive: str | Path) -> dict[str, Any]:
        """把备份包里的工程收进来（**不覆盖**已有的，重名自动 `-2`）。

        包里每个工程都会经过和"重启恢复"完全相同的那条路（`_restore`）：带 `job.json` 的
        按元数据恢复（原音频绝对路径对不上就按布局重找），只有产物的当只读工程收编。
        """
        from . import backup as backup_mod

        staging = Path(tempfile.mkdtemp(prefix="sheetsage2-restore-"))
        restored: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        try:
            manifest, projects = backup_mod.extract_backup(Path(archive), staging)
            if not projects:
                raise backup_mod.BackupRejected("备份包里没有工程")
            for source in projects:
                name = safe_name(source.name) or "工程"
                try:
                    target = claim_project_dir(self.root, project_folder_name(name))
                except OSError as exc:
                    skipped.append({"name": name, "reason": f"目录建不出来：{exc}"})
                    continue
                try:
                    for child in source.iterdir():
                        move_into(child, target / child.name)
                except OSError as exc:
                    shutil.rmtree(target, ignore_errors=True)
                    skipped.append({"name": name, "reason": f"文件写不进去：{exc}"})
                    continue
                job = self._restore(target)
                if job is None:
                    shutil.rmtree(target, ignore_errors=True)
                    skipped.append({"name": name, "reason": "包里没有可识别的工程"})
                    continue
                with self._lock:
                    if job.id in self._jobs:         # id 撞了（同一个包导两次）：换个 id
                        job.id = f"{job.id}-{uuid.uuid4().hex[:4]}"
                    self._jobs[job.id] = job
                    self._order.insert(0, job.id)
                job.emit("log", phase=None, message=f"已从备份导入：{job.name}")
                self._write_metadata(job)
                self.write_manifest(job)
                restored.append({"id": job.id, "name": job.name, "files": len(job.files)})
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return {
            "restored": restored,
            "skipped": skipped,
            "count": len(restored),
            "backup": {
                "created_at": manifest.get("created_at"),
                "app_version": manifest.get("app_version"),
                "projects": len(manifest.get("projects") or []),
            },
        }

    def _quarantine_stage_outputs(self, job: Job, phase: str) -> None:
        """把这阶段上一轮写的产物挪进 `.webui/prev/<rev>-<时间>/`。

        重跑中途失败/取消时，`output/` 里不会出现"上一轮的歌词 + 这一轮的项目"这种
        混搭；旧版本在 `prev/` 里还留了一份，跑成功之后再清掉（`_flush_prev`）。
        """
        from ..result import EXPECTED_MIDI, EXPECTED_TEXT

        if phase == "transcribe":
            # 重跑扒谱会连带作废歌词（音符位置变了），所以旧 `lyrics.json` 也一起挪走——
            # 不然会留下"这一轮的新谱面 + 上一轮的旧歌词"这种混搭，下载包里也会带上旧歌词。
            names: list[str] = (["project.json", "report.json", "summary.json", "lyrics.json"]
                                + list(EXPECTED_TEXT) + list(EXPECTED_MIDI))
        else:
            names = ["lyrics.json"]
        present = [name for name in names if (job.out_dir / name).is_file()]
        if not present:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = job.internal_dir / "prev" / f"{job.artifacts_rev:04d}-{stamp}"
        for name in present:
            try:
                move_into(job.out_dir / name, target / name)
            except OSError:
                # 挪不动（被占用/权限）就算了：宁可留着旧文件，也不能把这一阶段搞挂
                pass

    @staticmethod
    def _flush_prev(job: Job) -> None:
        """阶段成功后清掉 `prev/`（旧版本已经没有用了）。"""
        prev = job.internal_dir / "prev"
        if prev.is_dir():
            shutil.rmtree(prev, ignore_errors=True)

    def manifest_files(self, job: Job) -> dict[str, Any] | None:
        """读产物清单；没有就是 `None`（老工程 / 只读工程按目录扫描走）。"""
        try:
            data = json.loads(job.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        files = data.get("files")
        return dict(files) if isinstance(files, dict) else None

    def write_manifest(self, job: Job) -> None:
        """落 `<工程>/.webui/manifest.json`：`/files` 与 `/download` 只信它。

        这样"上一轮残留但这一轮没产出"的文件不会混进下载包——磁盘上留着是一回事，
        当成这一版的结果发出去是另一回事。
        """
        if job.imported:
            return
        try:
            with job.lock:
                payload = {
                    "artifacts_rev": job.artifacts_rev,
                    "rev": job.rev,
                    "updated_at": time.time(),
                    "options": dict(job.options),
                    "stages": {key: run.state for key, run in job.stages.items()},
                    "files": {item["path"]: {"size": item["size"]}
                              for item in scan_files(job.out_dir)},
                }
            write_text_atomic(job.manifest_path,
                              json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError:
            pass

    # ---------------------------------------------------------------- 收尾
    def _finish(self, job: Job) -> None:
        """所有阶段都跑完（或停了）之后：汇总任务级状态 + 扫产物。"""
        with job.lock:
            states = [job.stages[phase].state for phase in PHASES]
            if job.cancel_requested and "failed" not in states:
                job.state = "cancelled"
            elif "cancelled" in states:
                job.state = "cancelled"
            elif "failed" in states:
                job.state = "failed"
            else:
                job.state = "done"
            job.files = scan_files(job.out_dir)
            job.warnings = list(job.stages["transcribe"].detail.get("warnings") or [])
            job.stage = None
            state = job.state
        payload = {"message": "全部完成" if state == "done" else "任务已结束", "state": state}
        job.emit(state if state != "failed" else "error", **payload)
        self.write_manifest(job)
        self._write_metadata(job)

    def _fail(self, job: Job, exc: BaseException | str) -> None:
        """工作线程兜底：连 `_run` 都炸了，至少给一句人话（不是异常类名）。"""
        error = describe_error(exc) if isinstance(exc, BaseException) else None
        with job.lock:
            job.state = "failed"
        payload: dict[str, Any] = {
            "message": error.message if error else str(exc),
        }
        if error is not None:
            payload.update(code=error.code, action=error.action, detail=error.detail)
        job.emit("error", phase=job.stage, **payload)
        self._write_metadata(job)

    def _write_metadata(self, job: Job) -> None:
        # 只读工程（output/ 里本来就有的产物）不写元数据：那会把别人跑出来的目录
        # 变成"WebUI 工程"，下次重启就按 WebUI 工程恢复了
        if job.imported:
            return
        try:
            with job.lock:
                payload = job.metadata()
            write_text_atomic(job.root / "job.json",
                              json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError:
            # 元数据只是诊断用，写不进去不该影响任务本身
            pass
