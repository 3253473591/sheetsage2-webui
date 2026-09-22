"""任务状态机、单任务锁与强制停止（任务文档 4.1–4.4）。

职责
----
* **单任务锁**：同一时间只允许一个扒谱任务（4.1）。重复提交返回明确提示。
* **状态机**：``idle → queued → running → stopping → done / failed / cancelled``（4.2）。
* **子进程托管**：worker 跑在独立进程里，记录 PID，停止时 ``taskkill /T /F``（4.3/4.4）。
* **看门狗**：超过 ``WATCHDOG_IDLE_SECONDS`` 没有进度则判定卡死并终止（4.3）。
* **清理**：任务结束清理临时目录，``atexit`` 兜底（4.3）。

进度来源
--------
worker 在 stdout 上逐行输出 ``@@P``/``@@R``/``@@E`` JSON（见 ``app.worker``）。
本模块只做透传与聚合，**不生成任何进度**——保证文档「进度必须来自后端真实状态」。
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from app import config
from app.worker import PREFIX_ERROR, PREFIX_PROGRESS, PREFIX_RESULT

__all__ = ["Task", "TaskManager", "TaskBusyError", "manager", "STATE_LABELS"]

#: 状态 → 中文文案（文档 4.2 映射表）
STATE_LABELS = {
    "idle": "就绪",
    "queued": "排队中",
    "running": "运行中",
    "stopping": "停止中",
    "done": "已完成",
    "failed": "失败",
    "cancelled": "已停止",
}

#: 只有这些状态允许新任务（4.2）
_IDLE_STATES = {"idle", "done", "failed", "cancelled"}
MAX_LOG_LINES = 2000


class TaskBusyError(RuntimeError):
    """已有任务在运行时重复提交。"""


class Task:
    """一个扒谱任务的完整状态。"""

    def __init__(
        self,
        task_id: str,
        audio: Path,
        out_dir: Path,
        params: dict[str, Any],
        *,
        kind: str = "transcribe",
        parent_id: str | None = None,
    ) -> None:
        self.id = task_id
        self.kind = kind                       # transcribe | lyrics
        self.parent_id = parent_id             # 歌词任务的父扒谱任务
        self.audio = audio
        self.out_dir = out_dir
        self.params = params
        self.state = "queued"
        self.percent = 0.0
        self.stage = ""
        self.text = ""
        self.detail = ""
        self.elapsed = 0.0
        self.created_at = time.time()
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.pid: int | None = None
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.timeline: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self.warning: str | None = None
        self._lock = threading.Lock()

    # ---------------- 状态变更 ----------------
    def set_state(self, state: str, warning: str | None = None) -> None:
        with self._lock:
            self.state = state
            if warning is not None:
                self.warning = warning
            if state in ("done", "failed", "cancelled"):
                self.ended_at = self.ended_at or time.time()

    def log(self, level: str, message: str) -> dict[str, Any]:
        entry = {"at": round(time.time() - self.created_at, 3), "level": level, "message": message}
        with self._lock:
            self.logs.append(entry)
            if len(self.logs) > MAX_LOG_LINES:
                del self.logs[: len(self.logs) - MAX_LOG_LINES]
        return entry

    def apply_progress(self, payload: dict[str, Any]) -> None:
        with self._lock:
            # 双保险：即使 worker 出问题，也保证对外暴露的 percent 单调不减
            percent = payload.get("percent")
            if isinstance(percent, (int, float)):
                self.percent = max(self.percent, float(percent))
            self.stage = payload.get("stage") or self.stage
            self.text = payload.get("text") or self.text
            self.detail = payload.get("detail") or ""
            self.elapsed = payload.get("elapsed", self.elapsed)
            self.timeline.append(
                {"stage": self.stage, "percent": self.percent, "at": round(time.time() - self.created_at, 3)}
            )
            if len(self.timeline) > 1000:
                del self.timeline[: len(self.timeline) - 1000]

    # ---------------- 序列化 ----------------
    def as_dict(self, *, with_logs: bool = False) -> dict[str, Any]:
        with self._lock:
            data: dict[str, Any] = {
                "task_id": self.id,
                "kind": self.kind,
                "parent_id": self.parent_id,
                "state": self.state,
                "state_label": STATE_LABELS.get(self.state, self.state),
                "audio_name": self.audio.name,
                "audio_original_name": (self.params or {}).get("audio_original_name") or self.audio.name,
                "audio_path": str(self.audio),
                "out_dir": str(self.out_dir),
                "percent": self.percent,
                "stage": self.stage,
                "text": self.text,
                "detail": self.detail,
                "elapsed": self.elapsed,
                "params": dict(self.params),
                "pid": self.pid,
                "error": self.error,
                "warning": self.warning,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "result": self.result,
                "timeline": list(self.timeline),
                "log_lines": len(self.logs),
            }
            if with_logs:
                data["logs"] = list(self.logs)
            return data


class TaskManager:
    """全局任务管理器（单例由 ``manager`` 提供）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: Task | None = None
        self._history: list[Task] = []
        self._subscribers: list[queue.Queue] = []
        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        atexit.register(self.shutdown)

    # ---------------- 订阅（供 SSE） ----------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=5000)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def broadcast_result(self, task: Task) -> None:
        """对外广播某任务的 result 更新。

        用于服务端在「改歌词」「改 ABC」这类**同步操作**之后通知所有已连接页面，
        让其它标签页也能立即看到新产物，而不是等下一次任务事件。
        """
        self._broadcast({"type": "result", "task": task.as_dict()})

    # ---------------- 查询 ----------------
    @property
    def current(self) -> Task | None:
        with self._lock:
            return self._current

    def get(self, task_id: str | None) -> Task | None:
        with self._lock:
            if task_id is None:
                return self._current
            if self._current and self._current.id == task_id:
                return self._current
            for t in reversed(self._history):
                if t.id == task_id:
                    return t
            return None

    def busy(self) -> bool:
        with self._lock:
            t = self._current
            return t is not None and t.state not in _IDLE_STATES

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        """全量状态，供 SSE 重连后先拉一次（文档 3.6.8）。"""
        t = self.get(task_id)
        with self._lock:
            return {
                "busy": self.busy(),
                "task": t.as_dict(with_logs=True) if t else None,
                "history": [h.as_dict() for h in reversed(self._history[-20:])],
            }

    # ---------------- 提交 ----------------
    def submit(self, audio: Path, params: dict[str, Any] | None = None) -> Task:
        """提交新任务；已有任务运行时抛 :class:`TaskBusyError`。"""
        with self._lock:
            if self.busy():
                raise TaskBusyError("已有任务正在运行，请先停止或等待完成")

        params = dict(params or {})
        task_id = uuid.uuid4().hex[:12]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_root = Path(params.pop("output_dir", None) or config.load_settings()["output_dir"])
        safe_stem = _safe_stem(audio.stem)
        out_dir = out_root / f"{stamp}_{safe_stem}"
        out_dir.mkdir(parents=True, exist_ok=True)

        task = Task(task_id, audio, out_dir, params)
        with self._lock:
            self._current = task
            self._history.append(task)
            if len(self._history) > 100:
                del self._history[: len(self._history) - 100]

        task.log("info", f"任务 {task_id} 已排队：{audio.name}")
        self._broadcast({"type": "state", "task": task.as_dict()})
        threading.Thread(target=self._run, args=(task,), daemon=True, name=f"task-{task_id}").start()
        return task

    # ---------------- 执行 ----------------
    def _build_command(self, task: Task) -> list[str]:
        p = task.params
        cmd = [
            sys.executable,
            "-m",
            "app.worker",
            "--audio",
            str(task.audio),
            "--out",
            str(task.out_dir),
            "--preset",
            str(p.get("preset", "default")),
            "--dtype",
            str(p.get("dtype", "bf16")),
            "--device",
            str(p.get("device") or "auto"),
        ]
        if p.get("max_seconds"):
            cmd += ["--max-seconds", str(float(p["max_seconds"]))]
        # 「导出哪些内容」：勾了什么导什么（人声主旋律 / 器乐旋律 / 和弦）
        export_voices = p.get("export_voices")
        if export_voices:
            cmd += ["--export-voices", ",".join(str(x) for x in export_voices)]
        elif not p.get("only_melody", False):
            cmd.append("--no-only-melody")
        if p.get("drop_intro_outro"):
            cmd.append("--drop-intro-outro")
        if p.get("lyrics"):
            cmd += ["--lyrics", str(p["lyrics"])]
        return cmd

    def _run(self, task: Task) -> None:
        task.set_state("running")
        task.started_at = time.time()
        cmd = self._build_command(task)
        task.log("info", "启动推理子进程：" + " ".join(cmd))
        self._broadcast({"type": "state", "task": task.as_dict()})

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(config.ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            task.error = f"无法启动子进程：{exc}"
            task.set_state("failed")
            self._broadcast({"type": "error", "task": task.as_dict(), "message": task.error})
            return

        with self._lock:
            self._proc = proc
        task.pid = proc.pid
        self._start_watchdog()

        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                self._handle_line(task, line)
        finally:
            proc.wait()
            with self._lock:
                self._proc = None
            self._stop_watchdog()

        self._finalize(task, proc.returncode)

    def _handle_line(self, task: Task, line: str) -> None:
        if line.startswith(PREFIX_PROGRESS):
            try:
                payload = json.loads(line[len(PREFIX_PROGRESS):])
            except ValueError:
                return
            task.apply_progress(payload)
            self._broadcast({"type": "progress", "task": task.as_dict()})
        elif line.startswith(PREFIX_RESULT):
            try:
                task.result = json.loads(line[len(PREFIX_RESULT):])
            except ValueError as exc:
                task.error = f"结果解析失败：{exc}"
        elif line.startswith(PREFIX_ERROR):
            try:
                payload = json.loads(line[len(PREFIX_ERROR):])
            except ValueError:
                payload = {"message": line}
            task.error = payload.get("message") or "未知错误"
            entry = task.log("error", task.error)
            self._broadcast({"type": "log", "task": task.as_dict(), "log": entry})
            if payload.get("traceback"):
                tb = task.log("debug", payload["traceback"])
                self._broadcast({"type": "log", "task": task.as_dict(), "log": tb})
        else:
            entry = task.log("info", line)
            self._broadcast({"type": "log", "task": task.as_dict(), "log": entry})

    def _finalize(self, task: Task, returncode: int | None) -> None:
        duration = round(time.time() - (task.started_at or time.time()), 2)
        if task.state == "stopping":
            task.set_state("cancelled")
            task.log("warn", f"任务已停止，用时 {duration}s")
            self._cleanup_temp(task)
            self._broadcast({"type": "state", "task": task.as_dict()})
            return
        if task.error or returncode not in (0, None):
            task.set_state("failed")
            if not task.error:
                task.error = f"子进程异常退出（code={returncode}）"
            task.log("error", task.error)
            self._broadcast({"type": "error", "task": task.as_dict(), "message": task.error})
            return
        task.set_state("done")
        if task.percent < 100.0:
            task.percent = 100.0
        task.log("info", f"任务完成，用时 {duration}s")
        self._broadcast({"type": "result", "task": task.as_dict()})

    # ---------------- 停止 ----------------
    def stop(self) -> Task | None:
        """强制停止当前任务（文档 4.4）。"""
        task = self.current
        if task is None or task.state in _IDLE_STATES:
            return task
        if task.state == "stopping":
            return task
        task.set_state("stopping")
        task.log("warn", "收到停止请求，正在终止子进程…")
        self._broadcast({"type": "state", "task": task.as_dict()})

        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            self._terminate_tree(proc.pid)
            try:
                proc.wait(timeout=config.STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                task.log("warn", "子进程未在宽限期内退出，强制结束")
                self._terminate_tree(proc.pid)
        self._cleanup_temp(task)
        return task

    @staticmethod
    def _terminate_tree(pid: int) -> None:
        """按平台终止整棵进程树（4.3：Windows 用 taskkill /T /F）。"""
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    capture_output=True,
                    timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.killpg(os.getpgid(pid), 9)
        except (OSError, subprocess.SubprocessError):
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    # ---------------- 看门狗 ----------------
    def _start_watchdog(self) -> None:
        self._stop_watchdog()
        self._watchdog_stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watchdog_loop, daemon=True, name="watchdog")
        self._watchdog.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()

    def _watchdog_loop(self) -> None:
        """任务长时间无进度则自动终止（4.3）。"""
        last_seen = (-1.0, time.time())
        while not self._watchdog_stop.wait(5.0):
            task = self.current
            if task is None or task.state in _IDLE_STATES:
                return
            now = time.time()
            if task.percent != last_seen[0]:
                last_seen = (task.percent, now)
                continue
            if now - last_seen[1] > config.WATCHDOG_IDLE_SECONDS:
                task.log("error", f"超过 {config.WATCHDOG_IDLE_SECONDS}s 无进度，判定卡死并终止")
                self.stop()
                return

    # ---------------- 清理 ----------------
    @staticmethod
    def _cleanup_temp(task: Task) -> None:
        """清理任务临时目录（4.3）。输出目录保留，供下载。"""
        try:
            tmp = config.TMP_DIR / task.id
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        except OSError:
            pass

    def shutdown(self) -> None:
        """进程退出兜底：杀掉残留子进程（4.3 atexit）。"""
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            self._terminate_tree(proc.pid)


def _safe_stem(text: str) -> str:
    """把文件名清洗成安全目录名（4.5 防路径穿越）。"""
    keep = []
    for ch in text:
        if ch.isalnum() or ch in "-_":
            keep.append(ch)
        elif ch in " \t":
            keep.append("_")
    name = "".join(keep).strip("_")
    return (name or "audio")[:60]


#: 全局单例
manager = TaskManager()
