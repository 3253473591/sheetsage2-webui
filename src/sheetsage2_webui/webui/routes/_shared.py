"""路由共用的依赖对象与小工具。

**为什么要有这个文件**：路由模块在导入时**不能**建线程、加载模型或读用户目录
（那会让 `import` 变成有副作用的动作）。所以依赖集中在一个 :class:`ApiDeps` 里，
由应用工厂造好后传给各 router；router 只定义路径与状态码。

这里也放几个跨路由重复用的小东西：`require_job`（404 的措辞）、SSE 帧与终态事件、
带 Range 的音频响应、读产物 JSON（产物可能正被写一半）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, BinaryIO

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from ..job_models import Job
from ..jobs import JobManager
from ..media import media_type, parse_range

#: SSE 的轮询间隔与心跳间隔：事件是另一个线程追加的，只能轮询；静默太久会被掐连接。
POLL_SECONDS = 0.2
HEARTBEAT_SECONDS = 15.0


@dataclass(frozen=True)
class ApiDeps:
    """一个应用实例的全部外部依赖（router 只认它，不认 `create_app` 的闭包）。"""

    manager: JobManager
    #: 真实的模型会话；测试里是 None（只给 `runners` 假函数）。
    session: Any = None
    #: 备份包上限（MB）：备份要装得下几十首歌的产物与原音频。
    max_backup_mb: float = 8192.0
    #: 前端构建产物是否就绪（`/api/env` 用它提示"还没 npm run build"）。
    static_ready: bool = False


def require_job(deps: ApiDeps, job_id: str) -> Job:
    """按 id 取工程；没有就是 404（所有路由共用同一句措辞）。"""
    job = deps.manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="没有这个工程")
    return job


def sse_frame(payload: dict[str, Any]) -> bytes:
    """一个 SSE 帧。`id:` 让客户端知道续传点（我们更依赖 `?since=`）。"""
    body = json.dumps(payload, ensure_ascii=False)
    return f"id: {payload['seq']}\ndata: {body}\n\n".encode("utf-8")


def terminal_event(job: Job, seq: int) -> dict[str, Any]:
    """补一个终态事件：客户端连上时任务已经结束了，也要让它干净地收尾。"""
    state = job.state
    type_ = {"done": "done", "failed": "error", "cancelled": "cancelled"}.get(state, "done")
    message = job.error or ("全部完成" if state == "done" else "任务已结束")
    return {"seq": seq, "type": type_, "time": job.created_at,
            "data": {"message": message, "state": state, "phase": job.stage}}


def event_stream(job: Job, manager: JobManager, since: int,
                 request: Request) -> StreamingResponse:
    """SSE：从 `since` 之后补发，然后跟着跑；客户端断了或任务被删就收尾。"""

    async def stream() -> AsyncIterator[bytes]:
        seq = max(0, since)
        if job.terminal and not job.events_since(seq):
            yield sse_frame(terminal_event(job, seq + 1))
            return
        yield b": connected\n\n"
        silence = 0.0
        while True:
            pending = job.events_since(seq)
            for event in pending:
                seq = event.seq
                yield sse_frame(event.as_dict())
            if pending:
                silence = 0.0
            if job.terminal:
                # 终态事件已经在上面发过了；再确认一次没有漏的就可以收尾
                for event in job.events_since(seq):
                    seq = event.seq
                    yield sse_frame(event.as_dict())
                return
            if manager.get(job.id) is None:
                # 任务在流没关之前被删了：直接收尾，别在这儿空转
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(POLL_SECONDS)
            silence += POLL_SECONDS
            if silence >= HEARTBEAT_SECONDS:
                silence = 0.0
                yield b": ping\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


def audio_response(path: Path, request: Request) -> Response:
    """带 Range 的音频响应。混音原曲和干声都走这里。"""
    if not path.is_file():
        raise HTTPException(status_code=404, detail="音频不见了")
    size = path.stat().st_size
    kind = media_type(path)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    try:
        window = parse_range(request.headers.get("range"), size)
    except ValueError as exc:
        return Response(status_code=416,
                        headers={**headers, "Content-Range": f"bytes */{size}"},
                        content=str(exc).encode("utf-8"))
    if window is None:
        return FileResponse(path, media_type=kind, headers=headers)
    start, end = window
    length = end - start + 1

    def chunks() -> AsyncIterator[bytes]:
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                block = handle.read(min(64 * 1024, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    return StreamingResponse(
        chunks(), media_type=kind, status_code=206,
        headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}",
                 "Content-Length": str(length)})


async def read_artifact_json(job: Job, name: str) -> dict[str, Any]:
    """读产物 JSON。

    产物是**工作线程正在写**的：界面在扒谱刚结束时就可能来拿 `project.json`，此刻文件
    可能只有半截。所以解析失败不当 500，而是等一下再试一次，仍然不行就回 404
    （"还没准备好"比"服务器错误"更贴近事实）。
    """
    path = job.out_dir / name
    for attempt in range(2):
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                if attempt == 0:
                    await asyncio.sleep(0.2)
                    continue
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"读取 {name} 失败：{exc}") from exc
        raise HTTPException(status_code=404, detail=f"还没有 {name}")
    raise HTTPException(status_code=404, detail=f"{name} 正在写入，请稍后重试")


def copy_upload(stream: BinaryIO, target: Path, limit: int, limit_mb: float) -> int:
    """把上传流落到 `target`，超过 `limit` 立刻拒绝（备份包可能是几 GB，别读进内存）。"""
    written = 0
    with target.open("wb") as handle:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise HTTPException(
                    status_code=400,
                    detail=f"备份包超过 {limit_mb:g} MB 上限，请分批导入")
            handle.write(chunk)
    return written
