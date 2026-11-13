"""系统路由：诊断包、整库备份与恢复。

这三件事都"碰全局"（全部工程 + 数据根 + 日志），所以单独一组，不进工程路由。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from ..backup import BackupRejected
from ._shared import ApiDeps, copy_upload, require_job


def cleanup_temp_dir(path: Path) -> None:
    """下载完把备份的临时目录删掉（包里可能有几百 MB，不能一直留着）。"""
    shutil.rmtree(path.parent, ignore_errors=True)


def build_router(deps: ApiDeps) -> APIRouter:
    router = APIRouter()
    manager = deps.manager

    # ------------------------------------------------------------------ 诊断（G12）
    @router.get("/diagnose")
    async def diagnose_all() -> Response:
        """一键打包诊断信息（全部工程 + env + 日志），下载成 zip。"""
        payload = await run_in_threadpool(manager.diagnose_bundle)
        return Response(content=payload, media_type="application/zip",
                        headers={"Content-Disposition":
                                 f'attachment; filename="diagnose-{int(time.time())}.zip"'})

    @router.get("/jobs/{job_id}/diagnose")
    async def diagnose_job(job_id: str) -> Response:
        """单个工程的诊断包。"""
        require_job(deps, job_id)
        payload = await run_in_threadpool(manager.diagnose_bundle, job_id)
        return Response(content=payload, media_type="application/zip",
                        headers={"Content-Disposition":
                                 f'attachment; filename="diagnose-{job_id}.zip"'})

    # ------------------------------------------------------------------ 备份 / 迁移（G22）
    @router.get("/backup")
    async def backup(include_audio: bool = True, include_cache: bool = False) -> FileResponse:
        """把所有工程打成一个 zip（换电脑 / 重装系统前点一下）。

        默认带原音频（不带就没法重跑）与产物、`job.json`；`include_cache=true` 才带
        `bundle.zip` / 波形 / SVP 缓存——那些都能从产物重算，带着只是让包翻倍。
        """
        target, summary = await run_in_threadpool(
            manager.backup_file, include_audio=include_audio, include_cache=include_cache)
        return FileResponse(
            target, filename=target.name, media_type="application/zip",
            headers={"X-Backup-Projects": str(summary.get("projects") or 0)},
            background=BackgroundTask(cleanup_temp_dir, target))

    @router.post("/backup/restore", status_code=201)
    async def restore_backup(
        file: Annotated[UploadFile, File(...)],
    ) -> dict[str, Any]:
        """导入备份包：把里面的工程收进当前数据目录（重名自动 `-2`，绝不覆盖）。"""
        limit = int(deps.max_backup_mb * 1024 * 1024)
        handle, path = tempfile.mkstemp(prefix="sheetsage2-restore-", suffix=".zip")
        os.close(handle)
        try:
            written = await run_in_threadpool(
                copy_upload, file.file, Path(path), limit, deps.max_backup_mb)
            if written == 0:
                raise HTTPException(status_code=400, detail="上传的是空文件")
            try:
                return await run_in_threadpool(manager.restore_backup, path)
            except BackupRejected as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            Path(path).unlink(missing_ok=True)

    return router
