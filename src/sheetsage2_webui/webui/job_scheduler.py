"""阶段调度：单队列 + 工作线程 + 阶段看门狗 + 完成门禁。

**状态所有权**：`Job` 的字段归管理器与 `Job` 自己；调度器只做"下一趟跑什么、什么时候判死"，
并把磁盘写入交给 :class:`job_storage.JobStore`。它不认识 FastAPI、不认识路由，也不知道
管理器长什么样——只要一个 `lookup(job_id) -> Job | None`。

为什么要看门狗（G6）：推理卡死（驱动/显存/cuBLAS）时一个进度回调都不会来，用户点「停止」
也没用（协作式取消要等下个检查点），界面就永远停在 62%。这里从**另一个线程**判死并把
`cancel_requested` 置上，让工作线程在下个检查点退出、并把阶段标成失败 + 人话。
"""

from __future__ import annotations

import json
import queue
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from ..meta import write_meta
from .errors import StageFailure, describe as describe_error
from .job_errors import JobCancelled
from .job_files import scan_files
from .job_models import PHASE_LABELS, PHASES, STAGE_TERMINAL, Job, Progress, StageRunner
from .job_storage import JobStore
from .job_text import bytes_text as _bytes_text, duration_text as _duration_text

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

class Scheduler:
    """任务队列 + 工作线程 + 阶段看门狗。"""

    def __init__(self, root: str | Path, store: JobStore, *,
                 lookup: Callable[[str], Job | None],
                 runners: Mapping[str, StageRunner] | None = None,
                 workers: int = 1,
                 stall_timeout: float | Mapping[str, float] | None = None,
                 hard_timeout: float | Mapping[str, float] | None = None,
                 watchdog_interval: float = 1.0) -> None:
        self.root = Path(root)
        self._store = store
        self._lookup = lookup
        self._runners: dict[str, StageRunner] = dict(runners or {})
        self._workers = max(1, int(workers))
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        #: 阶段守护（G6）：无进展超过 `stall_timeout` 或总时长超过 `hard_timeout` 就判失败。
        self.stall_timeout = _per_phase(stall_timeout, STALL_TIMEOUT)
        self.hard_timeout = _per_phase(hard_timeout, HARD_TIMEOUT)
        self.watchdog_interval = max(0.05, float(watchdog_interval))
        self._watch: dict[tuple[str, str], _PhaseWatch] = {}
        self._watchdog: threading.Thread | None = None
        self._lock = threading.RLock()
        #: 派发闸门（批量页的「暂停」）：**只挡"下一项"，不打断正在跑的那一项**。
        #: 用 `cancel()` 实现暂停是不行的——它有 5 秒冷静期，而且会把 plan 清掉。
        self._gate = threading.Event()
        self._gate.set()

    # ---------------------------------------------------------------- 暂停 / 继续
    @property
    def paused(self) -> bool:
        return not self._gate.is_set()

    def pause(self) -> None:
        self._gate.clear()

    def resume(self) -> None:
        self._gate.set()

    # ---------------------------------------------------------------- 配置
    def set_runners(self, runners: Mapping[str, StageRunner]) -> None:
        self._runners.update(runners)

    @property
    def runners(self) -> Mapping[str, StageRunner]:
        return self._runners

    # ---------------------------------------------------------------- 入队 / 关停
    def submit(self, job_id: str) -> None:
        """把工程交给工作线程（先确保线程起来了）。"""
        self.ensure_workers()
        self._queue.put(job_id)

    def ensure_workers(self) -> None:
        """工作线程与看门狗懒启动（第一次有活干时才建；模块导入时不起线程）。"""
        self._ensure_workers()

    def shutdown(self, timeout: float = 5.0) -> None:
        """请求停止；正在跑的阶段不会被粗暴中断。"""
        self._stop.set()
        for _ in self._threads:
            self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()
        if self._watchdog is not None:
            self._watchdog.join(timeout=timeout)
            self._watchdog = None

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
                job = self._lookup(watch.job_id)
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
        job = self._lookup(watch.job_id)
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
        self._store.write_metadata(job)
    # ---------------------------------------------------------------- 工作线程
    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None:
                return
            if not self._wait_gate():
                return
            job = self._lookup(job_id)
            if job is None:
                continue
            try:
                self._run(job)
            except Exception as exc:  # 工作线程绝不能死掉
                self._fail(job, exc)

    def _wait_gate(self) -> bool:
        """暂停时在派发前挡住；返回 `False` 表示服务在关停（线程该退出）。"""
        while not self._gate.is_set():
            if self._stop.wait(0.1):
                return False
        return True
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
                stopped = job.stages[phase].state in ("failed", "cancelled")
            if stopped:
                # 这一阶段没成功：**剩下的阶段一起跳过**（批量下否则 40 首变 80 次失败，
                # 甚至拿上一版 project.json 把旧谱面填上新歌词）。
                self._skip_rest(job, phase)
                break
        self._finish(job)

    def _skip_rest(self, job: Job, phase: str) -> None:
        """把计划里还没跑的阶段标成 `skipped`（人话写在 message 上）。"""
        with job.lock:
            skipped = []
            for rest in list(job.plan):
                run = job.stages.get(rest)
                if run is None or run.state in STAGE_TERMINAL:
                    continue
                run.state = "skipped"
                run.message = f"{PHASE_LABELS.get(phase, phase)}没成功，这一项跳过了"
                run.finished_at = time.time()
                skipped.append(PHASE_LABELS.get(rest, rest))
            job.plan.clear()
        if skipped:
            job.emit("log", phase=phase,
                     message=f"{PHASE_LABELS.get(phase, phase)}没成功，"
                             f"已跳过：{'、'.join(skipped)}")
        self._store.write_metadata(job)
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
        self._store.clean_derived(job)
        self._store.quarantine(job, phase)
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
                # 先写 meta 再做完成门禁：这样读方看到 stage=done 时，
                # meta.json 一定已经在盘上（否则会有一个"done 了但 meta 还没写"的窗口）。
                self._refresh_meta(job, phase)
                self._settle_phase(job, phase, detail)
        finally:
            self._watch_end(job, phase)
            with job.lock:
                job.stage = None
            self._store.write_metadata(job)

    def _refresh_meta(self, job: Job, phase: str) -> None:
        """阶段收尾前刷新 `meta.json`（歌词阶段带歌词，扒谱阶段只有音符）。

        meta 是辅助索引，写失败不能把阶段改回 failed；读方最多看到旧 meta，
        不能看到半截 JSON（`write_meta` 是原子写）。
        """
        try:
            write_meta(job.out_dir, include_lyrics=(phase == "lyrics"))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            job.emit("log", phase=phase, message=f"meta.json 生成失败：{exc}")

    # ---------------------------------------------------------------- 完成门禁
    def _settle_phase(self, job: Job, phase: str, detail: Mapping[str, Any]) -> None:
        """阶段收尾：**先体检产物，通过了才算 done**（G4）。

        写盘（`manifest.json` / `job.json`）也留在同一个临界区里：调用方拿到
        "这一阶段 done"之后就会去备份/下载/删除，此时磁盘上必须已经是这一版
        （`artifacts_rev` 已经 +1）。放在锁外面就会读到上一版的 `job.json`。
        """
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
            # 终态事件先发、再落盘（都在同一把锁里）：
            # - 读方拿 `snapshot()` / `events_since()` 时要么看到"还没 done"，要么看到
            #   "done + 事件都在"，不会只拿到一半；
            # - `job.json` 里的 `events` 计数也就包含了这条终态事件（以前就是这个数）。
            if error is None:
                job.emit("phase_done", phase=phase, detail=run.detail)
            else:
                job.emit("phase_error", phase=phase, **error)
            self._store.write_manifest(job)                  # RLock：重入自己那把锁
            self._store.write_metadata(job)
        if error is None:
            self._store.flush_prev(job)
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
    # ---------------------------------------------------------------- 收尾
    def _finish(self, job: Job) -> None:
        """所有阶段都跑完（或停了）之后：汇总任务级状态 + 扫产物。

        **写盘必须在同一个临界区里**：`snapshot()` 也是拿 `job.lock` 的，所以调用方
        不可能"先看到 done、再去删/备份"，而 `manifest.json` / `job.json` 还没落盘。
        否则删工程会撞上收尾写盘（回收站里的 `files` 也会是 0）。
        """
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
            payload = {"message": "全部完成" if state == "done" else "任务已结束",
                       "state": state}
            # 终态事件先发再落盘（同一把锁里）：`job.json` 的 `events` 计数包含它，
            # 而 `snapshot()` 的读方不会看到"done 了但事件还没发"。
            job.emit(state if state != "failed" else "error", **payload)
            self._store.write_manifest(job)                  # RLock：重入自己那把锁
            self._store.write_metadata(job)

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
        self._store.write_metadata(job)
