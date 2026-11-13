"""媒体路由：卷帘数据、歌词、波形包络、原音频 / 干声（都支持 Range）。

这些接口共同的特点是"**读产物但不改任务**"：只读磁盘、带缓存，没准备好就 404。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ..lanes import build_lanes
from ..media import clamp_buckets, peaks_for
from ._shared import ApiDeps, audio_response, read_artifact_json, require_job


def build_router(deps: ApiDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/jobs/{job_id}/project")
    async def project(job_id: str) -> dict[str, Any]:
        return await read_artifact_json(require_job(deps, job_id), "project.json")

    @router.get("/jobs/{job_id}/lyrics")
    async def lyrics(job_id: str) -> dict[str, Any]:
        return await read_artifact_json(require_job(deps, job_id), "lyrics.json")

    @router.get("/jobs/{job_id}/lanes")
    async def lanes(job_id: str) -> dict[str, Any]:
        """卷帘要画的东西（秒域）：小节表 / 段落 / 和弦 / 人声音符 / 歌词行。"""
        job = require_job(deps, job_id)
        try:
            return await run_in_threadpool(build_lanes, job.out_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/jobs/{job_id}/peaks")
    async def peaks(job_id: str, buckets: int | None = None,
                    source: str = "mix") -> dict[str, Any]:
        """波形包络。`source=stem` 取上传的干声，默认取混音原曲。

        **没有音频就是 404，不是 500**：`output/` 里载入的只读工程本来就没有原音频，
        按文件名去解码会一路炸到 `media.compute_peaks`（以前就是这么 500 的）。
        """
        job = require_job(deps, job_id)
        if source == "stem":
            stem = job.stems.get("vocal")
            if not stem:
                raise HTTPException(status_code=404, detail="没有上传干声")
            audio = Path(str(stem["path"]))
            cache_key = "stem"
        else:
            audio = job.upload
            cache_key = "mix"
        if not audio.is_file():
            raise HTTPException(
                status_code=404,
                detail="这个工程没有原音频" if source == "mix" else "这条音源的文件不见了")
        size = clamp_buckets(buckets)
        try:
            return await run_in_threadpool(peaks_for, job.internal_dir, audio, size, cache_key)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/jobs/{job_id}/audio")
    async def audio(job_id: str, request: Request) -> Response:
        """上传的音频本体。**支持 Range**——不然浏览器拖动进度条就废了。"""
        job = require_job(deps, job_id)
        if not job.upload.is_file():
            raise HTTPException(status_code=404,
                                detail="这个工程是从 output/ 载入的已有产物，没有原音频")
        return audio_response(job.upload, request)

    @router.get("/jobs/{job_id}/stems/{kind}/audio")
    async def stem_audio(job_id: str, kind: str, request: Request) -> Response:
        """干声本体，同样支持 Range（能在页面上直接试听）。"""
        job = require_job(deps, job_id)
        stem = job.stems.get(kind)
        if not stem:
            raise HTTPException(status_code=404, detail="没有上传这条音源")
        return audio_response(Path(str(stem["path"])), request)

    return router
