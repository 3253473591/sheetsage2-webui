"""整库备份与恢复：把所有工程打成一个 zip，或从一个 zip 里把工程收进来。

归档格式（清单、zip-slip 校验、`include_audio` / `include_cache` 的取舍）在
:mod:`sheetsage2_webui.webui.backup` 里；这里只做**协调**：跟管理器要工程列表、
把每个工程目录塞进包里、恢复时逐个走"重启恢复"那条路并登记回来。

为了不反向依赖 `jobs.py`（门面），管理器把四件事以回调传进来：
`list_jobs` / `audio_seconds` / `restore_dir` / `register`。
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from ..fsutil import move_into
from . import backup as backup_mod
from .job_files import claim_project_dir, project_folder_name, safe_name
from .job_models import Job


class BackupService:
    """整库备份 / 恢复。恢复**绝不覆盖**已有工程（重名自动 `-2`）。"""

    def __init__(self, root: str | Path, *,
                 list_jobs: Callable[[], list[Job]],
                 audio_seconds: Callable[[Job], float | None],
                 restore_dir: Callable[[Path], Job | None],
                 register: Callable[[Job], None]) -> None:
        self.root = Path(root)
        self._list_jobs = list_jobs
        self._audio_seconds = audio_seconds
        self._restore_dir = restore_dir
        self._register = register

    # ---------------------------------------------------------------- 导出
    def write(self, *, include_audio: bool = True,
              include_cache: bool = False) -> tuple[Path, dict[str, Any]]:
        """把所有工程打成一个备份 zip，返回 `(临时文件路径, 摘要)`。

        落在系统临时目录而不是数据根里：数据根是用户的东西，别在旁边拉一坨几百 MB 的包
        （而且它会被磁盘预检算进去）。调用方（API）负责把临时目录清掉。
        """
        projects: list[dict[str, Any]] = []
        pairs: list[tuple[str, Path]] = []
        used: set[str] = set()
        for job in self._list_jobs():
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
                "duration_seconds": self._audio_seconds(job),
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

    # ---------------------------------------------------------------- 导入
    def restore(self, archive: str | Path) -> dict[str, Any]:
        """把备份包里的工程收进来（**不覆盖**已有的，重名自动 `-2`）。

        包里每个工程都会经过和"重启恢复"完全相同的那条路（`restore_dir`）：带 `job.json`
        的按元数据恢复（原音频绝对路径对不上就按布局重找），只有产物的当只读工程收编。
        """
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
                job = self._restore_dir(target)
                if job is None:
                    shutil.rmtree(target, ignore_errors=True)
                    skipped.append({"name": name, "reason": "包里没有可识别的工程"})
                    continue
                self._register(job)
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
