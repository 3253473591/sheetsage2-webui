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
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..bootstrap import PROJECT_ROOT
from ..backend import detect_backend
from ..fsutil import move_into, write_text_atomic
from ..meta import write_meta
from ..meta_watch import MetaWatcher
from .errors import StageFailure, describe as describe_error
from .eta import estimate as estimate_seconds
from .job_errors import (ImportedProject, JobCancelled, StageNotReady, StaleRevision,
                         UploadRejected)
from .job_files import (AUDIO_SUFFIXES, FILE_KINDS, INTERNAL_DIRNAME, STEM_KINDS,
                        STEM_LABELS, _UNSAFE, _check_audio, _kind_of, _write_stream,
                        claim_project_dir, content_fingerprint, import_audio, is_internal,
                        iter_chunks, project_folder_name, safe_name, scan_files,
                        unique_project_dir, unique_suffix)
from .job_models import (DEFAULT_PLAN, DRAFT_STATE, MANIFEST_NAME, MAX_EVENTS, PHASE_LABELS,
                         PHASES, STAGE_TERMINAL, TERMINAL_STATES, Event, Job, Progress,
                         StageRunner, StageRun)
from .job_options import (ASR_MODEL_PREFIX, PARAM_INDEX, PARAM_OWNER, PARAM_SPECS,
                          _option_label, asr_model_label, default_options, describe_change,
                          option_values, param_choices, validate_options)
from .job_backup import BackupService
from .job_storage import JobStore
from .job_text import bytes_text as _bytes_text, duration_text as _duration_text
from .job_trash import (TRASH_DIRNAME, TRASH_MAX_BYTES, TRASH_MAX_DAYS,
                        TRASH_PURGE_INTERVAL, TrashStore)
from .job_scheduler import (CANCEL_COOLDOWN_SECONDS, HARD_TIMEOUT, STALL_TIMEOUT, STUCK_GRACE,
                            Scheduler,
                            _per_phase, _PhaseWatch)

# 兼容入口：这些名字过去都在 `webui.jobs` 里可直接导入（含 `_per_phase` / `_option_label`
# 这类私有的），调用方正逐步改到各自模块，这里保留转发
# （见 docs/261113 模块化拆分与重构计划.md 第 2 条“保留兼容入口”）。

# --------------------------------------------------------------------- 管理器
class JobManager:
    """任务管理器的门面：工程登记 / 参数与音源 / 删除与回收站 / 备份。

    真正干活的分在四处，管理器只负责**把它们接起来并维护工程列表这一份状态**：

    | 职责 | 在哪 |
    |---|---|
    | 队列、worker、看门狗、阶段生命周期 | :class:`job_scheduler.Scheduler` |
    | 恢复 / 元数据 / 清单 / 打包 / 清理 | :class:`job_storage.JobStore` |
    | 回收站 | :class:`job_trash.TrashStore` |
    | 整库备份 / 恢复 | :class:`job_backup.BackupService` |

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
        stuck_grace: float = STUCK_GRACE,
        trash_max_days: float = TRASH_MAX_DAYS,
        trash_max_bytes: float = TRASH_MAX_BYTES,
        trash_purge_interval: float = TRASH_PURGE_INTERVAL,
        use_trash: bool = True,
        probe_upload: bool = False,
        meta_watch: bool = False,
        meta_watch_dirs: Iterable[str | Path] = (),
    ) -> None:
        self.root = Path(root) if root is not None else PROJECT_ROOT / "tmp" / "webui"
        self.max_upload_mb = float(max_upload_mb)
        #: 「4G 特供」模式：参数被服务端锁死（页面只读），PATCH `/options` 直接 409。
        self.lock_params = bool(lock_params)
        #: 新工程的参数默认值。服务端用 `--asr-model` 指定了别的识别模型时，
        #: 「模型」那一栏的默认值要跟着它走，否则页面上的默认值会把它悄悄覆盖掉。
        self.option_defaults: dict[str, Any] = dict(option_defaults or default_options())
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []            # 新的在前
        self._lock = threading.RLock()
        #: 软删除（G2）：删除 = 挪进 `.trash/`，能恢复；`use_trash=False` 是硬删（测试用）
        self.use_trash = bool(use_trash)
        #: 回收站（挪进/列出/恢复/保留策略）交给它；管理器只负责"从列表里摘掉/登记回来"。
        self._trash = TrashStore(self.root, enabled=self.use_trash,
                                 max_days=trash_max_days, max_bytes=trash_max_bytes,
                                 purge_interval=trash_purge_interval)
        #: 上传后是否做内容体检（G10）。默认关——直接 `JobManager(...)` 的测试不上 ffmpeg；
        #: 产品入口 `create_app` 会显式打开。
        self.probe_upload = bool(probe_upload)
        #: 这台机器实际会跑在什么设备上（`"cuda"` / `"cpu"` / `None` = 不知道）。
        #: `create_app` 用 `RunnerSession.describe()` 的结果填它；**只影响耗时预估的系数**（G20），
        #: 不影响真跑哪台设备（那是 `EngineProvider.config_for` 的事）。
        self.device_hint: str | None = None
        self.root.mkdir(parents=True, exist_ok=True)
        #: 磁盘这边的读写（恢复 / 元数据 / 清单 / 隔离 / 打包 / 清理）都交给它；
        #: 管理器自己只管排队与状态。
        self._store = JobStore(self.root, option_defaults=self.option_defaults,
                               eta_provider=self.eta_for)
        #: 队列 / 工作线程 / 看门狗 / 阶段生命周期：`job_scheduler.Scheduler`。
        #: 它只要一个按 id 取 job 的回调，不认识管理器本身。
        self._scheduler = Scheduler(self.root, self._store, lookup=self.get,
                                    runners=runners, workers=workers,
                                    stall_timeout=stall_timeout, hard_timeout=hard_timeout,
                                    watchdog_interval=watchdog_interval,
                                    stuck_grace=stuck_grace)
        #: 整库备份 / 恢复：归档格式在 `backup.py`，协调逻辑在 `job_backup.py`。
        self._backup = BackupService(self.root, list_jobs=self.list,
                                     audio_seconds=self.audio_seconds,
                                     restore_dir=self._store.restore_dir,
                                     register=self._register_restored)
        # **重启不丢**：把 output/ 里已有的工程读回来（WebUI 建的读 job.json，
        # 命令行跑出来的当"只读产物"收进来）
        self._load_existing()
        # 手动精修只改/加 auto.svp / manual.svp：轮询这些目录，源文件一变就刷新
        # meta.json（write_meta 自己会看 mtime，没变不写盘）。生产入口默认开；
        # 测试里 create_app 不传就不起线程。
        self._meta_watch = (MetaWatcher([self.root, *meta_watch_dirs])
                            if meta_watch else None)
        if self._meta_watch is not None:
            self._meta_watch.start()
        self._trash.start()

    # ---------------------------------------------------------------- 启动恢复
    def _load_existing(self) -> None:
        """把数据根里已有的工程登记进来（扫描与恢复的细节在 `JobStore.load_existing`）。"""
        restored = self._store.load_existing()
        for job in restored:
            # 手动精修往往是在识别完成之后再加/改 auto.svp / manual.svp 的；
            # 启动扫描时顺手刷新，meta 就不会停在旧统计上。
            try:
                write_meta(job.out_dir)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        with self._lock:
            for job in restored:
                self._jobs[job.id] = job
                self._order.append(job.id)
        if restored:
            logos = [job.name for job in restored[:5]]
            print(f"  已载入 {len(restored)} 个工程：{', '.join(logos)}"
                  + (" …" if len(restored) > 5 else ""), flush=True)

    # ---------------------------------------------------------------- 存储（委托 JobStore）
    # 拆分后这些职责在 `job_storage.py`；名字保留，路由 / 调度 / 测试不用改。

    def bundle(self, job: Job) -> Path:
        """把 ``output/`` 打成一个 zip（懒生成，落盘缓存）。"""
        return self._store.bundle(job)

    def manifest_files(self, job: Job) -> dict[str, Any] | None:
        return self._store.manifest_files(job)

    def write_manifest(self, job: Job) -> None:
        self._store.write_manifest(job)

    def _write_metadata(self, job: Job) -> None:
        self._store.write_metadata(job)

    def _attach_eta(self, job: Job) -> Job:
        return self._store._attach_eta(job)

    def _restore(self, root: Path) -> Job | None:
        return self._store.restore_dir(root)

    def _clean_derived(self, job: Job) -> None:
        self._store.clean_derived(job)

    def _quarantine_stage_outputs(self, job: Job, phase: str) -> None:
        self._store.quarantine(job, phase)

    def _flush_prev(self, job: Job) -> None:
        self._store.flush_prev(job)

    def cleanup(self, job_id: str) -> dict[str, Any]:
        """清掉一个工程的可重算缓存（整包 zip / SVP / 波形 / 旧版本 prev）。"""
        job = self.get(job_id)
        if job is None:
            return {"freed": 0}
        return self._store.cleanup(job)




    # ---------------------------------------------------------------- 调度（委托 Scheduler）
    # 队列、worker、看门狗与阶段生命周期都在 `job_scheduler.py`；名字保留给测试与路由用。

    def _ensure_workers(self) -> None:
        self._scheduler.ensure_workers()

    def shutdown(self, timeout: float = 5.0) -> None:
        """请求停止；正在跑的阶段不会被粗暴中断。"""
        if self._meta_watch is not None:
            self._meta_watch.stop()
        self._scheduler.shutdown(timeout=timeout)
        self._trash.stop(timeout=timeout)

    @property
    def stall_timeout(self) -> dict[str, float]:
        """阶段守护的"多久没进展算卡死"（秒，按阶段）。改它就是改调度器的阈值。"""
        return self._scheduler.stall_timeout

    @stall_timeout.setter
    def stall_timeout(self, value: float | Mapping[str, float] | None) -> None:
        self._scheduler.stall_timeout = _per_phase(value, STALL_TIMEOUT)

    @property
    def hard_timeout(self) -> dict[str, float]:
        """阶段守护的总时长上限（秒，按阶段）。"""
        return self._scheduler.hard_timeout

    @hard_timeout.setter
    def hard_timeout(self, value: float | Mapping[str, float] | None) -> None:
        self._scheduler.hard_timeout = _per_phase(value, HARD_TIMEOUT)

    @property
    def watchdog_interval(self) -> float:
        """看门狗扫一遍的间隔（秒）。"""
        return self._scheduler.watchdog_interval

    @watchdog_interval.setter
    def watchdog_interval(self, value: float) -> None:
        self._scheduler.watchdog_interval = max(0.05, float(value))

    # ---------------------------------------------------------------- 配置
    def set_runners(self, runners: Mapping[str, StageRunner]) -> None:
        self._scheduler.set_runners(runners)

    # ---------------------------------------------------------------- 暂停 / 继续
    @property
    def paused(self) -> bool:
        return self._scheduler.paused

    def pause(self) -> bool:
        """不再派发新的工程（正在跑的那一首不打断）。批量页的「暂停」。"""
        self._scheduler.pause()
        return True

    def resume(self) -> bool:
        self._scheduler.resume()
        return True

    @property
    def runners(self) -> Mapping[str, StageRunner]:
        return self._scheduler.runners

    def env(self) -> dict[str, Any]:
        with self._lock:
            running = next((j.id for j in self._jobs.values() if j.state == "running"), None)
            pending = sum(1 for j in self._jobs.values() if j.state == "queued")
            return {"data_root": str(self.root), "max_upload_mb": self.max_upload_mb,
                    "params_locked": self.lock_params,
                    # 诊断包（env.json）里也带上后端：A 卡用户发回日志时，`hip` 有没有值
                    # 决定了是"装成 CUDA 版了"还是"ROCm 跑起来了"。
                    "backend": detect_backend(),
                    "queue": {"pending": pending, "running": running,
                              "paused": self.paused,
                              # 判死后线程还没回收：队列实际停摆，界面要提示重启
                              "stuck": self._scheduler.stuck},
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

    # ---------------------------------------------------------------- 生命周期
    # ---------------------------------------------------------------- 查询
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]

    # ---------------------------------------------------------------- 建任务
    def create(self, filename: str, stream: Any,
               plan: Iterable[str] = DEFAULT_PLAN, *,
               options: Mapping[str, Any] | None = None,
               batch: Mapping[str, Any] | None = None) -> Job:
        """把上传流写到 ``output/<歌名>/.webui/input/``，**默认什么都不排**。

        ``stream`` 只要有 ``read(n)`` 就行（`UploadFile.file` 就是）。超限会立刻中断并
        删掉半截文件——磁盘不留给失败的上传。

        ``plan`` 非空才排队（`/chain` 与显式 ``plan=`` 用得上）；空 plan 时工程停在
        :data:`DRAFT_STATE`，等用户在页面上选完参数点「开始扒谱」——这就是门禁。

        ``options`` 是"建工程时就定好参数"（批量导入一次设定跟着走）；``batch`` 是批量
        归属（`{"id", "title"}`），单曲不传。
        """
        audio = _check_audio(filename)
        wanted = self._plan_of(plan)
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

        audio_info = self._probe_audio(upload, root)
        fingerprint = content_fingerprint(upload)

        job = Job(id=job_id, name=Path(audio).stem or audio, audio=audio, root=root,
                  upload=upload, out_dir=root, created_at=created,
                  plan=list(wanted),
                  options=dict(self.option_defaults, **(options or {})),
                  batch=dict(batch) if batch else None,
                  audio_info=audio_info, audio_fingerprint=fingerprint or None)
        self._register(job, wanted, fingerprint)
        return job

    def create_from_path(self, source: str | Path,
                         plan: Iterable[str] = DEFAULT_PLAN, *,
                         options: Mapping[str, Any] | None = None,
                         batch: Mapping[str, Any] | None = None,
                         link: bool = True) -> Job:
        """从**本机路径**建工程（批量导入用）：不经过上传流。

        与 :meth:`create` 的差别只有"音频怎么进来"：校验（后缀 / 大小 / 体检）、工程目录
        认领、去重、排队全都一样。``link=True`` 时同卷**硬链接**（一首 40MB 的歌不复制
        第二份），跨卷或文件系统不支持时静默退化成复制；``link=False`` 强制复制。
        """
        source = Path(source)
        if not source.is_file():
            raise UploadRejected(f"找不到音频：{source}")
        audio = _check_audio(source.name)
        size = source.stat().st_size
        limit = int(self.max_upload_mb * 1024 * 1024)
        if size == 0:
            raise UploadRejected(f"{audio} 是空文件")
        if size > limit:
            raise UploadRejected(f"{audio} 超过 {self.max_upload_mb:g} MB 上限")

        wanted = self._plan_of(plan)
        job_id = uuid.uuid4().hex[:12]
        created = time.time()
        root = claim_project_dir(self.root, project_folder_name(Path(audio).stem))
        upload = root / INTERNAL_DIRNAME / "input" / f"{job_id}-{audio}"
        try:
            import_audio(source, upload, link=link)
        except OSError as exc:
            shutil.rmtree(root, ignore_errors=True)
            raise UploadRejected(f"把 {audio} 放进工程失败：{exc}") from exc

        audio_info = self._probe_audio(upload, root)
        fingerprint = content_fingerprint(upload)

        job = Job(id=job_id, name=Path(audio).stem or audio, audio=audio, root=root,
                  upload=upload, out_dir=root, created_at=created,
                  plan=list(wanted),
                  options=dict(self.option_defaults, **(options or {})),
                  batch=dict(batch) if batch else None,
                  audio_info=audio_info, audio_fingerprint=fingerprint or None)
        self._register(job, wanted, fingerprint)
        return job

    @staticmethod
    def _plan_of(plan: Iterable[str]) -> list[str]:
        return [phase for phase in plan if phase in PHASES]

    def _probe_audio(self, upload: Path, root: Path) -> dict[str, Any]:
        """内容体检（G10）：ffmpeg 探一下"是不是能播的音频、是不是纯静音"。没 ffmpeg 就跳过。"""
        if not self.probe_upload:
            return {}
        from .media import probe_audio

        audio_info = probe_audio(upload) or {}
        if audio_info.get("ok") is False:
            reason = str(audio_info.get("reason") or "这个音频用不了")
            shutil.rmtree(root, ignore_errors=True)
            raise UploadRejected(reason)
        return audio_info

    def _register(self, job: Job, wanted: Sequence[str], fingerprint: str) -> None:
        """登记一个新工程：状态、去重、元数据、排队。上传与"从路径导入"共用。"""
        self._attach_eta(job)
        job.state = "queued" if wanted else DRAFT_STATE
        job.message = "等待开始" if wanted else "等待参数"
        for phase in wanted:
            job.stages[phase].state = "queued"
        job.emit("log", phase=None, message=f"新建工程 · {job.audio}"
                 + ("" if wanted else "（选好参数后点「开始扒谱」）"))
        if fingerprint:
            with self._lock:
                for other in self._jobs.values():
                    if other is not job and other.audio_fingerprint == fingerprint:
                        job.duplicate_of = other.id
                        break
        with self._lock:
            self._jobs[job.id] = job
            self._order.insert(0, job.id)
        self._write_metadata(job)
        if wanted:
            self._scheduler.submit(job.id)

    def run_stage(self, job_id: str, phase: str, *, chain: bool = False,
                  force: bool = False) -> Job | None:
        """单独（重）跑一个阶段：清掉它的旧状态，塞回待跑队列。

        工作流是 **扒谱 → 歌词**（原型也是这么写的），所以手动跑歌词**必须先有扒谱**：
        没扒谱就抛 :class:`StageNotReady`（API 回 409"请先完成扒谱"）。

        ``chain=True`` 是例外——那是「⏵ 自动提取（扒谱 → 歌词）」：它**故意**在扒谱还没跑完时
        就把歌词排进队列，所以跳过前置检查（计划里本来就先跑扒谱）。

        ``force=True`` 跳过"刚取消"的冷静期：批量的「继续剩余」是一次明确的批量动作，
        不该被 5 秒墓碑挡住（手抖连点的保护仍然对单工程生效）。
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
            # **但终态工程不算**：崩溃/取消可能留下"job 已 cancelled、某阶段还挂着 queued"
            # 的僵尸组合——不重排的话「继续剩余」会空转（`run_stage` 每次都在这里早退）。
            if (job.stages[phase].state in ("queued", "running")
                    and job.state not in TERMINAL_STATES):
                return job
            # 取消后的"墓碑"（G14）：刚取消又立刻点开始，多半是手抖——直接拒绝，
            # 别让"取消"被这次点击抹掉。
            if not force and job.canceled_at is not None and \
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
        self._scheduler.submit(job_id)
        return job

    def run_chain(self, job_id: str, *, force: bool = False) -> Job | None:
        """「自动提取」：扒谱 → 歌词，一条龙排进队列（两个都重跑）。"""
        job = self.run_stage(job_id, "transcribe", force=force)
        if job is None:
            return None
        return self.run_stage(job_id, "lyrics", chain=True, force=force)

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

    # ---------------------------------------------------------------- 删除 / 回收站
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
        # 先立"已删除"的墓碑再动磁盘：收尾线程（`_finish`）可能正在写 job.json /
        # manifest.json，而原子写会 `mkdir(parents=True)`——它会把这个目录**重新建出来**。
        with job.lock:
            job.removed = True
        if permanent or not self.use_trash:
            shutil.rmtree(job.root, ignore_errors=True)
            return True
        try:
            self._trash.move_in(job.id, job.name, job.root)
        except OSError:
            # 挪不进回收站就别硬删：宁可报错让用户重试，也不能把东西弄丢
            with job.lock:
                job.removed = False
            with self._lock:
                self._jobs[job_id] = job
                self._order.insert(0, job_id)
            raise RuntimeError(f"删除失败：工程目录被占用（{job.root}），关掉相关程序再试")
        return True

    @property
    def trash_root(self) -> Path:
        """回收站目录（`<数据根>/.trash/`）。"""
        return self._trash.trash_root

    @property
    def trash_purge_interval(self) -> float:
        """回收站定时清理的节拍（秒）。"""
        return self._trash.purge_interval

    @trash_purge_interval.setter
    def trash_purge_interval(self, value: float) -> None:
        self._trash.purge_interval = max(0.05, float(value))

    @property
    def trash_max_days(self) -> float:
        return self._trash.max_days

    @trash_max_days.setter
    def trash_max_days(self, value: float) -> None:
        self._trash.max_days = float(value)

    @property
    def trash_max_bytes(self) -> float:
        return self._trash.max_bytes

    @trash_max_bytes.setter
    def trash_max_bytes(self, value: float) -> None:
        self._trash.max_bytes = float(value)

    def trash(self) -> list[dict[str, Any]]:
        """回收站列表（新的在前）。每项就是一个被挪进来的工程目录。"""
        return self._trash.entries()

    def restore(self, entry_id: str) -> Job | None:
        """把回收站里的工程挪回 `output/`（重名就 `-2`），并重新登记。"""
        source = self._trash.entry_dir(entry_id)
        if source is None:
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
        return self._trash.drop(entry_id)

    def _purge_trash(self) -> None:
        """回收站保留策略：超过 7 天、或总共超过 2GB，就真删最旧的。"""
        self._trash.purge_expired()

    # ---------------------------------------------------------------- 看门狗
    # ---------------------------------------------------------------- 工作线程
    # ---------------------------------------------------------------- 完成门禁
    # ---------------------------------------------------------------- 磁盘（G11）
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
        """把所有工程打成一个备份 zip，返回 `(临时文件路径, 摘要)`（细节见 `job_backup`）。"""
        return self._backup.write(include_audio=include_audio,
                                  include_cache=include_cache)

    def restore_backup(self, archive: str | Path) -> dict[str, Any]:
        """把备份包里的工程收进来（**不覆盖**已有的，重名自动 `-2`）。"""
        return self._backup.restore(archive)

    def _register_restored(self, job: Job) -> None:
        """把一个从包里恢复出来的工程登记进列表（id 撞了就换一个）。"""
        with self._lock:
            if job.id in self._jobs:             # 同一个包导两次：换个 id
                job.id = f"{job.id}-{uuid.uuid4().hex[:4]}"
            self._jobs[job.id] = job
            self._order.insert(0, job.id)
        job.emit("log", phase=None, message=f"已从备份导入：{job.name}")
        self._write_metadata(job)
        self.write_manifest(job)

    # ---------------------------------------------------------------- 收尾