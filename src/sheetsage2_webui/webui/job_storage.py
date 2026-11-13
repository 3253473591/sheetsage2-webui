"""工程存储：启动恢复 / 导入只读产物、`job.json`、产物清单、阶段产物隔离、打包与清理。

**只碰磁盘，不碰队列**：它不认识线程、调度器和 HTTP，`Job` 的事件流也不由它驱动。
写盘统一走 :func:`..fsutil.write_text_atomic`（先写 `.part` 再 `os.replace`），
所以进程被杀也不会留下半截 JSON。
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping

from ..fsutil import move_into, write_text_atomic
from .job_files import INTERNAL_DIRNAME, safe_name, scan_files
from .job_models import DRAFT_STATE, PHASE_LABELS, PHASES, Job
from .job_text import bytes_text as _bytes_text


class JobStore:
    """一个数据根（默认 `output/`）下的工程读写。

    `data_root` 下**一个子目录就是一个工程**：有 `job.json` 的是 WebUI 建的工程，
    只有 `project.json` / `report.json` 的是命令行或别的工具跑出来的产物（只读）。
    """

    def __init__(self, root: str | Path, *,
                 option_defaults: Mapping[str, Any] | None = None,
                 eta_provider: Callable[[Job, str], Mapping[str, Any] | None] | None = None
                 ) -> None:
        self.root = Path(root)
        self.option_defaults: dict[str, Any] = dict(option_defaults or {})
        self.eta_provider = eta_provider
        #: 打包含一把独立的锁：它跑在请求线程里，不能占用 `job.lock`
        #: （那把锁还要给工作线程汇报进度，几百 MB 的产物目录会把进度卡住）。
        self._bundle_lock = threading.Lock()

    # ---------------------------------------------------------------- 启动恢复

    def load_existing(self) -> list[Job]:
        """扫描数据根，把已有的工程读回来（新的在前）。

        - 有 `job.json` → WebUI 建的工程，按元数据恢复（上次没跑完的阶段标成中断）；
        - 没有 `job.json` 但有 `project.json`/`report.json` → 别处跑出来的产物，
          作为**只读**工程收进列表（能看卷帘 / 下载 / 导 SVP）；
        - 其它目录（比如空的、放别的东西的）跳过，不动它。
        """
        if not self.root.is_dir():
            return []
        restored: list[Job] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            job = self.restore_dir(entry)
            if job is not None:
                restored.append(job)
        restored.sort(key=lambda item: item.created_at, reverse=True)
        return restored


    # ---------------------------------------------------------------- 耗时预估

    def _attach_eta(self, job: Job) -> Job:
        """给工程挂上预估提供者（`Job.snapshot()` 里按阶段调它）。"""
        job.eta_provider = self.eta_provider
        return job

    # ---------------------------------------------------------------- 恢复 / 导入

    def restore_dir(self, root: Path) -> Job | None:
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

    # ---------------------------------------------------------------- 产物清单

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

        **检查与写入在同一把 `job.lock` 里**：删除（`JobManager.delete`）会拿这把锁立
        `removed` 墓碑再动磁盘。检查放在锁外的话，就会出现"检查通过 → 目录被删 → 才落盘"，
        而原子写会 `mkdir(parents=True)`，把删掉的目录重新建出来（重启后成了幽灵工程）。
        """
        try:
            with job.lock:
                if job.imported or job.removed:
                    return
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

    # ---------------------------------------------------------------- 元数据

    def write_metadata(self, job: Job) -> None:
        # 只读工程（output/ 里本来就有的产物）不写元数据：那会把别人跑出来的目录
        # 变成"WebUI 工程"，下次重启就按 WebUI 工程恢复了；已删除的工程同理——
        # 原子写会 `mkdir(parents=True)`，把刚删掉的目录又建回来。
        # 检查和写入必须在同一把锁里，理由见 `write_manifest`。
        try:
            with job.lock:
                if job.imported or job.removed:
                    return
                payload = job.metadata()
                write_text_atomic(job.root / "job.json",
                                  json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError:
            # 元数据只是诊断用，写不进去不该影响任务本身
            pass

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
            # 建目录也认墓碑：工程已经删掉时不能再把 `output/<歌名>/` 建回来
            with job.lock:
                if job.removed:
                    raise FileNotFoundError("工程已删除")
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

    # ---------------------------------------------------------------- 清理可重算的缓存

    def cleanup(self, job: Job) -> dict[str, Any]:
        """清掉一个工程的可重算缓存（整包 zip / SVP / 波形 / 旧版本 prev），返回释放了多少。

        「清理」页（G11）用：这些都是能从产物再算出来的东西，删了不影响结果，只影响下次
        下载/画波形时要重算。
        """
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

    # ---------------------------------------------------------------- 产物版本
    @staticmethod

    def _derived_patterns() -> tuple[str, ...]:
        return ("bundle.zip", "bundle.meta.json", "peaks-*.json")

    def clean_derived(self, job: Job) -> None:
        """阶段开始前清掉上一版的派生缓存（它们都能重算，留着只会发旧货）。"""
        internal = job.internal_dir
        for pattern in self._derived_patterns():
            for path in internal.glob(pattern):
                path.unlink(missing_ok=True)
        export = internal / "export"
        if export.is_dir():
            for path in export.glob("*.svp*"):
                path.unlink(missing_ok=True)

    # ---------------------------------------------------------------- 阶段产物隔离

    def quarantine(self, job: Job, phase: str) -> None:
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

    def flush_prev(job: Job) -> None:
        """阶段成功后清掉 `prev/`（旧版本已经没有用了）。"""
        prev = job.internal_dir / "prev"
        if prev.is_dir():
            shutil.rmtree(prev, ignore_errors=True)
