"""下载与导出路由：单个产物、整包 zip、Synthesizer V 工程（SVP）。

缓存都带**产物版本戳**（`artifacts_rev`）：重跑过就一定重算，不会把上一版发出去（G3）。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from ..batch_export import (KINDS, BatchExportError, ensure_svp, export_batch,
                            normalize_parts)
from ..job_files import is_internal, safe_name
from ..job_models import Job
from ...svp import (SVP_VERSION, SvpUnavailable, available_parts, load_project)
from ._shared import ApiDeps, cleanup_dir, require_job


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
        try:
            wanted = normalize_parts(parts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            # 缓存键带产物版本戳（G3）：重跑过就一定重新生成，不会把上一版发出去。
            target, _stats = await run_in_threadpool(ensure_svp, job, wanted)
        except SvpUnavailable as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        name = f"{safe_name(job.name or 'sheetsage2')}.svp"
        return FileResponse(target, filename=name,
                            media_type="application/octet-stream",
                            headers={"X-Artifact-Rev": str(job.artifacts_rev)})

    @router.get("/exports/batch")
    async def batch_download(ids: str = "", kind: str = "svp",
                             parts: str = "") -> FileResponse:
        """**批量下载**：一次把多个工程打成一个 zip。

        `ids` 是逗号分隔的工程 id（顺序即包内顺序）。`kind=svp`（默认）每首一个
        `<歌名>.svp`；`kind=files` 把每首的产物按 `<歌名>/<相对路径>` 摊开。
        包里总有一份 `_manifest.json` 记着"导出了哪些、跳过了哪些（为什么）"。

        单个工程导不出来（还没扒谱 / 没音符）**不算整批失败**，只进清单的 skipped；
        只有一首都导不出来才回 409。id 不存在回 404——"少了三首"必须当场知道。
        """
        chosen: list[str] = []
        for item in ids.split(","):
            job_id = item.strip()
            if job_id and job_id not in chosen:
                chosen.append(job_id)
        if not chosen:
            raise HTTPException(status_code=400, detail="ids 不能为空（逗号分隔多个工程 id）")
        if kind not in KINDS:
            raise HTTPException(status_code=400,
                                detail=f"未知的下载内容 {kind!r}；可选 {list(KINDS)}")
        try:
            wanted = normalize_parts(parts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        missing = [job_id for job_id in chosen if manager.get(job_id) is None]
        if missing:
            raise HTTPException(
                status_code=404,
                detail="没有这些工程：" + "、".join(missing[:5])
                + (f" 等 {len(missing)} 个" if len(missing) > 5 else ""))
        jobs = [manager.get(job_id) for job_id in chosen]

        try:
            target, manifest = await run_in_threadpool(
                export_batch, jobs, kind=kind, parts=wanted)
        except BatchExportError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SvpUnavailable as exc:                 # 没装 libresvip
            raise HTTPException(status_code=501, detail=str(exc)) from exc

        label = "SVP" if kind == "svp" else "产物"
        name = f"批量-{label}-{time.strftime('%Y%m%d-%H%M')}.zip"
        return FileResponse(
            target, filename=name, media_type="application/zip",
            headers={"X-Batch-Exported": str(len(manifest["exported"])),
                     "X-Batch-Skipped": str(len(manifest["skipped"]))},
            background=BackgroundTask(cleanup_dir, target))

    return router
