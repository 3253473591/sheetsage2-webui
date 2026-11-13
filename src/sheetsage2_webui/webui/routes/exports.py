"""下载与导出路由：单个产物、整包 zip、Synthesizer V 工程（SVP）。

缓存都带**产物版本戳**（`artifacts_rev`）：重跑过就一定重算，不会把上一版发出去（G3）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from ..job_files import is_internal, safe_name
from ..job_models import Job
from ...svp import (DEFAULT_PARTS, SVP_PARTS, SVP_VERSION, SvpUnavailable,
                    available_parts, build_svp, cache_key, export_path, load_project)
from ._shared import ApiDeps, require_job


def build_router(deps: ApiDeps) -> APIRouter:
    router = APIRouter()
    manager = deps.manager

    @router.get("/jobs/{job_id}/files/{path:path}")
    async def download_file(job_id: str, path: str) -> FileResponse:
        job = require_job(deps, job_id)
        root = job.out_dir.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(status_code=404, detail="没有这个产物")
        if is_internal(path):                      # job.json / .webui/ 是内部文件，不对外
            raise HTTPException(status_code=404, detail="没有这个产物")
        listed = manager.manifest_files(job)
        if listed is not None and path.replace("\\", "/") not in listed and not job.imported:
            # 清单之外的文件是别的版本留下的：能留在磁盘上，但不能当这一版发出去
            raise HTTPException(status_code=404, detail="这个产物不属于当前这一版结果")
        return FileResponse(target, filename=target.name,
                            headers={"X-Artifact-Rev": str(job.artifacts_rev)})

    @router.get("/jobs/{job_id}/download")
    async def download_bundle(job_id: str, kind: str = "zip",
                              parts: str = "") -> FileResponse:
        """下载：默认整包 zip；`kind=svp` 导 **Synthesizer V Studio 工程**。

        SVP 的内容由下载弹窗勾选（`?parts=vocal,instrumental,chords`），默认只有人声；
        同一组勾选会缓存成一份文件，重复点不重新生成。
        """
        job = require_job(deps, job_id)
        if kind == "svp":
            return await svp_download(job, parts)
        try:
            bundle = await run_in_threadpool(manager.bundle, job)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        name = f"{job.name or 'sheetsage2'}-{job.id}.zip"
        return FileResponse(bundle, filename=name, media_type="application/zip",
                            headers={"X-Artifact-Rev": str(job.artifacts_rev)})

    @router.get("/jobs/{job_id}/exports")
    async def export_options(job_id: str) -> dict[str, Any]:
        """下载弹窗要显示的内容：哪几组能导、各多少个音、会占几轨。"""
        job = require_job(deps, job_id)
        try:
            project = await run_in_threadpool(load_project, job.out_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="还没扒谱，没有工程可导") from exc
        rows = await run_in_threadpool(available_parts, job.out_dir, project)
        return {
            "name": job.name,
            "parts": rows,
            "defaults": [row["key"] for row in rows if row["default"]],
            "svp": {
                "available": any(row["available"] for row in rows),
                "version": SVP_VERSION,
                "hint": "Synthesizer V Studio 工程：带变速与歌词，可直接打开",
            },
        }

    async def svp_download(job: Job, parts: str) -> FileResponse:
        wanted = [item.strip() for item in parts.split(",") if item.strip()] \
            or list(DEFAULT_PARTS)
        unknown = [item for item in wanted if item not in SVP_PARTS]
        if unknown:
            raise HTTPException(status_code=400,
                                detail=f"未知的导出内容 {unknown}；可选 {list(SVP_PARTS)}")
        options = dict(job.options)
        # 缓存键带上**产物版本戳**：重跑过就一定重新生成，不会把上一版发出去（G3）
        target = export_path(job.internal_dir,
                             cache_key(wanted, SVP_VERSION, options,
                                       rev=job.artifacts_rev))
        if not target.is_file():
            try:
                payload, svp_stats = await run_in_threadpool(
                    build_svp, job.out_dir, parts=wanted, options=options,
                    version=SVP_VERSION)
            except SvpUnavailable as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # 先写 .part 再改名：两个请求同时要同一份时不会读到半截文件
            temp = target.with_name(target.name + ".part")
            temp.write_bytes(payload)
            temp.replace(target)
            # 为了让 SVP 同轨不叠音而动过音符的话，日志里说一声（不然"怎么少了一个音"没法查）
            for warning in svp_stats.get("warnings") or []:
                job.emit("log", phase=None, message=f"导出 SVP：{warning}")
        name = f"{safe_name(job.name or 'sheetsage2')}.svp"
        return FileResponse(target, filename=name,
                            media_type="application/octet-stream",
                            headers={"X-Artifact-Rev": str(job.artifacts_rev)})

    return router
