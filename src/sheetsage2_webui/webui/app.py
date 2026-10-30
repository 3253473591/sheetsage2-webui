"""FastAPI 应用：工程 + 两个阶段 + 进度流 + 卷帘数据 + 产物下载 + 前端静态资源。

路由一览（全部挂在 `/api` 下，其余路径归前端 SPA）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/env` | 服务端信息（两个模型 / 设备 / **参数取值表** / 是否锁定 / 队列） |
| GET | `/api/jobs` | 工程列表（新的在前） |
| POST | `/api/jobs` | 上传音频并建工程（multipart，字段 `file`，可选 `plan`）；**默认不排任何阶段** |
| GET | `/api/jobs/{id}` | 工程快照（两个阶段的状态 + 参数 + 产物清单 + 摘要） |
| PATCH | `/api/jobs/{id}/options` | 改参数面板上的参数（范围见 `jobs.PARAM_SPECS`） |
| POST | `/api/jobs/{id}/cancel` | 请求取消（下一检查点生效） |
| DELETE | `/api/jobs/{id}` | 删除工程与产物 |
| POST | `/api/jobs/{id}/stages/{phase}` | 单独（重）跑一个阶段：`transcribe` / `lyrics` |
| POST | `/api/jobs/{id}/chain` | 「自动提取」：扒谱 → 歌词 一条龙 |
| GET | `/api/jobs/{id}/events` | SSE 事件流，可带 `?since=<seq>` 续传 |
| GET | `/api/jobs/{id}/project` | tick 域工程（卷帘用：小节表 / 和弦 / 段落 / 音符） |
| GET | `/api/jobs/{id}/lyrics` | 歌词识别结果（text / chars / lines / fill） |
| GET | `/api/jobs/{id}/peaks` | 波形包络（`?buckets=`，带缓存） |
| GET | `/api/jobs/{id}/audio` | 上传的音频，支持 Range（浏览器播放/拖动） |
| GET | `/api/jobs/{id}/files/{path}` | 下载单个产物 |
| GET | `/api/jobs/{id}/download` | 下载：默认整包 zip；`?kind=svp&parts=` 导 SVP 工程 |
| GET | `/api/jobs/{id}/exports` | 导出弹窗要显示的内容（可勾哪几组 / 各多少音 / 占几轨） |

**前端产物缺失时**（没跑过 `npm run build`），根路径会回一段纯文本说明——比 404 好排查。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, BinaryIO, Mapping

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..asr import language_choices
from ..bootstrap import PROJECT_ROOT, find_ffmpeg
from ..svp import (DEFAULT_PARTS, SVP_PARTS, SVP_VERSION, SvpUnavailable,
                   available_parts, build_svp, cache_key, export_path, load_project)
from .backup import BackupRejected
from .jobs import (DEFAULT_PLAN, PHASE_LABELS, PHASES, TRASH_MAX_BYTES, TRASH_MAX_DAYS,
                   TRASH_PURGE_INTERVAL, ImportedProject, Job, JobManager,
                   StageNotReady, StaleRevision, UploadRejected, asr_model_label,
                   default_options, is_internal, param_choices, safe_name, validate_options)
from .lanes import build_lanes
from .media import clamp_buckets, media_type, parse_range, peaks_for
from .preflight import preflight
from .runner import RunnerSession
from .security import (TOKEN_COOKIE, extract_token, generate_pair_token,
                       host_is_loopback, origin_is_loopback, token_matches)

#: 前端构建产物（`web/vite.config.ts` 的 `build.outDir` 指向这里）。
STATIC_DIR = Path(__file__).resolve().parent / "static"
#: 工程数据根 = **项目里的 `output/`**：一个子目录就是一个工程，和命令行跑出来的一致。
#: 这样重启不会丢东西，命令行跑过的歌也能在侧栏里看到（只读）。
DEFAULT_DATA_ROOT = PROJECT_ROOT / "output"

#: 阶段卡上显示的引擎名。
PHASE_ENGINES = {"transcribe": "SheetSage2", "lyrics": "Qwen3-ASR"}

#: SSE 的轮询间隔与心跳间隔：事件是另一个线程追加的，只能轮询；静默太久会被掐连接。
POLL_SECONDS = 0.2
HEARTBEAT_SECONDS = 15.0

#: 导入备份包的大小上限（MB）：备份要装得下几十首歌的产物与原音频。
MAX_BACKUP_MB = 8192.0

BUILD_HINT = (
    "SheetSage2 Studio 服务已启动，但还没有前端产物。\n\n"
    "在 web/ 目录里执行：\n    npm install\n    npm run build\n\n"
    "然后刷新本页。开发时也可以用 `npm run dev`（Vite 会把 /api 代理到本服务）。\n"
)


def _copy_upload(stream: BinaryIO, target: Path, limit: int) -> int:
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
                    detail=f"备份包超过 {MAX_BACKUP_MB:g} MB 上限，请分批导入")
            handle.write(chunk)
    return written


def _cleanup_temp_dir(path: Path) -> None:
    """下载完把备份的临时目录删掉（包里可能有几百 MB，不能一直留着）。"""
    shutil.rmtree(path.parent, ignore_errors=True)


def _sse(payload: dict[str, Any]) -> bytes:
    """一个 SSE 帧。`id:` 让客户端知道续传点（我们更依赖 `?since=`）。"""
    body = json.dumps(payload, ensure_ascii=False)
    return f"id: {payload['seq']}\ndata: {body}\n\n".encode("utf-8")


def _terminal_event(job: Job, seq: int) -> dict[str, Any]:
    """补一个终态事件：客户端连上时任务已经结束了，也要让它干净地收尾。"""
    state = job.state
    type_ = {"done": "done", "failed": "error", "cancelled": "cancelled"}.get(state, "done")
    message = job.error or ("全部完成" if state == "done" else "任务已结束")
    return {"seq": seq, "type": type_, "time": job.created_at,
            "data": {"message": message, "state": state, "phase": job.stage}}


def _audio_response(path: Path, request: Request) -> Response:
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


def create_app(
    *,
    data_root: str | Path | None = None,
    runners: Any = None,
    session: RunnerSession | None = None,
    max_upload_mb: float = 512.0,
    workers: int = 1,
    manager: JobManager | None = None,
    lock_params: bool = False,
    stall_timeout: float | Mapping[str, float] | None = None,
    hard_timeout: float | Mapping[str, float] | None = None,
    watchdog_interval: float = 1.0,
    use_trash: bool = True,
    trash_max_days: float = TRASH_MAX_DAYS,
    trash_max_bytes: float = TRASH_MAX_BYTES,
    trash_purge_interval: float = TRASH_PURGE_INTERVAL,
    probe_upload: bool = True,
    pair_token: str | None = None,
    require_token: bool = False,
    loopback_guard: bool = False,
    allowed_hosts: tuple[str, ...] = (),
    device_hint: str | None = None,
) -> FastAPI:
    """构造应用。

    - 生产：不传 `runners`，内部建一个 `RunnerSession`（两个阶段 + 两个模型持有者）
    - 测试：`runners={"transcribe": 假函数, "lyrics": 假函数}`，不碰模型
    - `lock_params=True`（「4G 特供」）：参数由服务端锁死，`PATCH /options` 一律 409
    - `stall_timeout` / `hard_timeout`：阶段守护的阈值（测试里传个很小的数就能验）
    - `device_hint`：这台机器实际跑在 `cuda` 还是 `cpu`。**只影响耗时预估的系数**（G20），
      不传就按会话/`auto` 猜（有卡按显卡算）。
    """
    if session is None and runners is None:
        session = RunnerSession()
    if runners is None and session is not None:
        runners = session.runners()
    # 参数默认值：服务端用 `--asr-model` 换了识别模型的话，「模型」那一栏的默认值跟着走
    defaults = default_options()
    if session is not None:
        label = asr_model_label(session.describe().get("asr_model"))
        if label:
            defaults["asr_model"] = label
    if manager is None:
        manager = JobManager(data_root or DEFAULT_DATA_ROOT, runners=runners,
                             max_upload_mb=max_upload_mb, workers=workers,
                             lock_params=lock_params, option_defaults=defaults,
                             stall_timeout=stall_timeout, hard_timeout=hard_timeout,
                             watchdog_interval=watchdog_interval, use_trash=use_trash,
                             trash_max_days=trash_max_days,
                             trash_max_bytes=trash_max_bytes,
                             trash_purge_interval=trash_purge_interval,
                             probe_upload=probe_upload)
    static_ready = (STATIC_DIR / "index.html").is_file()

    # 耗时预估（G20）的设备口径：显式传入 > 会话自报 > 不知道（按显卡算）。
    resolved_hint = device_hint
    if resolved_hint is None and session is not None:
        resolved_hint = session.describe().get("device")
    if str(resolved_hint or "").strip().lower() in ("", "auto", "none"):
        resolved_hint = None
    manager.set_device_hint(resolved_hint)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        manager.shutdown()
        if session is not None:
            session.release()

    app = FastAPI(title="SheetSage2 Studio", version=__version__, lifespan=lifespan)
    app.state.manager = manager
    app.state.session = session

    token = pair_token or generate_pair_token()
    app.state.pair_token = token

    # ------------------------------------------------------------------ 安全（G13）
    @app.middleware("http")
    async def security(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            # 非 API（SPA 壳 / 静态资源）：顺手种下 HttpOnly token cookie（无感）
            response = await call_next(request)
            if require_token and not request.cookies.get(TOKEN_COOKIE):
                response.set_cookie(TOKEN_COOKIE, token, httponly=True, samesite="strict")
            return response
        if require_token:
            # 非回环监听：所有 API 都要 token（头 / Bearer / ?token= / cookie 都认）
            if not token_matches(extract_token(request), token):
                return JSONResponse({"detail": "没有访问凭证，拒绝访问"}, status_code=403)
        elif loopback_guard:
            host = request.headers.get("host", "")
            if host and not host_is_loopback(host) and host not in allowed_hosts:
                return JSONResponse({"detail": "非本机请求被拒绝"}, status_code=403)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("origin") or request.headers.get("referer")
                if origin and not origin_is_loopback(origin):
                    return JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403)
        response = await call_next(request)
        if require_token and not request.cookies.get(TOKEN_COOKIE):
            response.set_cookie(TOKEN_COOKIE, token, httponly=True, samesite="strict")
        return response

    # ------------------------------------------------------------------ 基础
    @app.get("/api/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/env")
    async def env() -> dict[str, Any]:
        info = manager.env()
        engine = session.describe() if session is not None else {}
        asr_models = engine.pop("asr_models", None)
        default_asr = engine.pop("asr_model", None)
        asr = {
            "model": default_asr,
            "loaded": engine.pop("asr_loaded", None),
            "device": engine.get("device"),
        }
        return {
            "version": __version__,
            "model": engine.get("model", "自定义 runner"),
            "device": engine.get("device", "自定义 runner"),
            "model_loaded": bool(engine.get("loaded")),
            "asr": asr,
            "ffmpeg": find_ffmpeg(),
            "data_root": str(manager.root),
            "static_ready": static_ready,
            "max_upload_mb": manager.max_upload_mb,
            "queue": info["queue"],
            "disk": info.get("disk", {"free": 0, "total": 0, "used": 0}),
            "preflight": preflight(manager.root),
            "languages": language_choices(),
            # 参数面板的取值真源：前端照着画，PATCH 也照着校验。
            "params": param_choices(asr_models, default_asr),
            "params_locked": bool(info.get("params_locked")),
            "phases": [
                {"key": key, "label": PHASE_LABELS[key], "engine": PHASE_ENGINES[key]}
                for key in PHASES
            ],
        }

    # ------------------------------------------------------------------ 工程
    def _require(job_id: str) -> Job:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="没有这个工程")
        return job

    @app.get("/api/jobs")
    async def list_jobs() -> dict[str, Any]:
        return {"jobs": [job.snapshot() for job in manager.list()]}

    @app.post("/api/jobs", status_code=201)
    async def create_job(
        file: Annotated[UploadFile, File(...)],
        plan: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        """建工程。**默认一个阶段都不排（门禁）**：丢音频只是把工程建出来，
        参数面板这时才露出来，用户点「开始扒谱」/「开始识别歌词」才真的开跑。

        要一条龙就显式传 `plan=transcribe,lyrics`，或建完调 `/chain`。"""
        wanted = PHASES
        if plan:
            chosen = [item.strip() for item in plan.split(",") if item.strip()]
            unknown = [item for item in chosen if item not in PHASES]
            if unknown:
                raise HTTPException(status_code=400, detail=f"未知阶段 {unknown}")
            wanted = tuple(chosen)
        else:
            wanted = DEFAULT_PLAN
        try:
            job = await run_in_threadpool(manager.create, file.filename or "audio",
                                         file.file, wanted)
        except UploadRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job.snapshot()

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        return _require(job_id).snapshot()

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        job = manager.cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="没有这个工程")
        return job.snapshot()

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str, permanent: bool = False) -> dict[str, Any]:
        """删除工程。默认进**回收站**（能恢复）；`?permanent=true` 才是真删。"""
        _require(job_id)
        try:
            manager.delete(job_id, permanent=permanent)
        except ImportedProject as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"deleted": job_id, "permanent": bool(permanent)}

    # ------------------------------------------------------------------ 回收站
    @app.get("/api/trash")
    async def list_trash() -> dict[str, Any]:
        """回收站：被删掉的工程（`.trash/` 下的目录）。"""
        return {"entries": await run_in_threadpool(manager.trash)}

    @app.post("/api/trash/{entry_id}/restore")
    async def restore_from_trash(entry_id: str) -> dict[str, Any]:
        job = await run_in_threadpool(manager.restore, entry_id)
        if job is None:
            raise HTTPException(status_code=404, detail="回收站里没有这一项")
        return job.snapshot()

    @app.delete("/api/trash/{entry_id}")
    async def purge_from_trash(entry_id: str) -> dict[str, Any]:
        if not manager.purge_from_trash(entry_id):
            raise HTTPException(status_code=404, detail="回收站里没有这一项")
        return {"purged": entry_id}

    @app.post("/api/jobs/{job_id}/cleanup")
    async def cleanup_job(job_id: str) -> dict[str, Any]:
        """清掉一个工程的可重算缓存（zip / SVP / 波形 / prev），释放磁盘（G11）。"""
        _require(job_id)
        return await run_in_threadpool(manager.cleanup, job_id)

    # ------------------------------------------------------------------ 诊断（G12）
    @app.get("/api/diagnose")
    async def diagnose_all() -> Response:
        """一键打包诊断信息（全部工程 + env + 日志），下载成 zip。"""
        payload = await run_in_threadpool(manager.diagnose_bundle)
        return Response(content=payload, media_type="application/zip",
                        headers={"Content-Disposition":
                                 f'attachment; filename="diagnose-{int(time.time())}.zip"'})

    @app.get("/api/jobs/{job_id}/diagnose")
    async def diagnose_job(job_id: str) -> Response:
        """单个工程的诊断包。"""
        _require(job_id)
        payload = await run_in_threadpool(manager.diagnose_bundle, job_id)
        return Response(content=payload, media_type="application/zip",
                        headers={"Content-Disposition":
                                 f'attachment; filename="diagnose-{job_id}.zip"'})

    # ------------------------------------------------------------------ 备份 / 迁移（G22）
    @app.get("/api/backup")
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
            background=BackgroundTask(_cleanup_temp_dir, target))

    @app.post("/api/backup/restore", status_code=201)
    async def restore_backup(
        file: Annotated[UploadFile, File(...)],
    ) -> dict[str, Any]:
        """导入备份包：把里面的工程收进当前数据目录（重名自动 `-2`，绝不覆盖）。"""
        limit = int(MAX_BACKUP_MB * 1024 * 1024)
        handle, path = tempfile.mkstemp(prefix="sheetsage2-restore-", suffix=".zip")
        os.close(handle)
        try:
            written = await run_in_threadpool(_copy_upload, file.file, Path(path), limit)
            if written == 0:
                raise HTTPException(status_code=400, detail="上传的是空文件")
            try:
                return await run_in_threadpool(manager.restore_backup, path)
            except BackupRejected as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            Path(path).unlink(missing_ok=True)


    @app.post("/api/jobs/{job_id}/stages/{phase}")
    async def run_stage(job_id: str, phase: str) -> dict[str, Any]:
        """单独（重）跑一个阶段。

        歌词识别的前置条件是**扒谱已完成**（工作流是 扒谱 → 歌词），没满足就 409 +
        "请先完成扒谱"；界面把这句直接显示出来。要在扒谱还没跑完时排歌词，用 `/chain`。
        """
        _require(job_id)
        try:
            job = manager.run_stage(job_id, phase)
        except (StageNotReady, ImportedProject) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job.snapshot()                       # type: ignore[union-attr]

    @app.post("/api/jobs/{job_id}/chain")
    async def run_chain(job_id: str) -> dict[str, Any]:
        """「⏵ 自动提取」：扒谱 → 歌词 一条龙（故意在扒谱没跑完时就排歌词）。"""
        _require(job_id)
        try:
            job = manager.run_chain(job_id)
        except ImportedProject as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job.snapshot()                       # type: ignore[union-attr]

    @app.patch("/api/jobs/{job_id}/options")
    async def set_options(job_id: str, body: dict[str, Any],
                          request: Request) -> dict[str, Any]:
        """改工程级可调参数（参数面板：精度 / 设备 / 范围 / 量化 / 模型 / 音源 / 语言）。

        校验在 `jobs.validate_options`（和 `/api/env` 发出去的取值表同源）。
        改了参数 = 换了口径，对应阶段的旧结果作废；阶段正在跑改不了（409）。

        **乐观锁**（G8）：body 里带 `expected_rev`（或 `If-Match: <rev>` 头）时，
        服务端按工程当前的 `rev` 校验——不一致回 409 + 当前快照，前端据此提示"这页是旧的"。
        """
        job = _require(job_id)
        payload = dict(body)
        raw_rev = payload.pop("expected_rev", None)
        if raw_rev is None:
            raw_rev = request.headers.get("if-match")
        expected_rev: int | None = None
        if raw_rev is not None and str(raw_rev).strip():
            try:
                expected_rev = int(str(raw_rev).strip())
            except ValueError:
                raise HTTPException(status_code=400, detail="expected_rev 必须是整数")
        try:
            updated = await run_in_threadpool(manager.set_options, job_id,
                                              expected_rev, **payload)
        except StaleRevision as exc:
            raise HTTPException(status_code=409,
                                detail={"message": str(exc), "snapshot": job.snapshot()}) from exc
        except ValueError as exc:                    # 值不认识/越界
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:                  # 阶段正在跑 / 4G 特供锁定
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return updated.snapshot()                    # type: ignore[union-attr]

    # ------------------------------------------------------------------ 音源
    @app.post("/api/jobs/{job_id}/stems/{kind}", status_code=201)
    async def upload_stem(job_id: str, kind: str,
                          file: Annotated[UploadFile, File(...)]) -> dict[str, Any]:
        """给工程补一条音源（`vocal` = 干声）。**不校验内容**：传混音原曲也允许。"""
        _require(job_id)
        try:
            job = await run_in_threadpool(manager.attach_stem, job_id, kind,
                                          file.filename or "stem", file.file)
        except UploadRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409 if isinstance(exc, RuntimeError) else 400,
                                detail=str(exc)) from exc
        return job.snapshot()                       # type: ignore[union-attr]

    @app.delete("/api/jobs/{job_id}/stems/{kind}")
    async def delete_stem(job_id: str, kind: str) -> dict[str, Any]:
        _require(job_id)
        try:
            job = await run_in_threadpool(manager.detach_stem, job_id, kind)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409 if isinstance(exc, RuntimeError) else 400,
                                detail=str(exc)) from exc
        return job.snapshot()                       # type: ignore[union-attr]

    @app.get("/api/jobs/{job_id}/stems/{kind}/audio")
    async def stem_audio(job_id: str, kind: str, request: Request) -> Response:
        """干声本体，同样支持 Range（能在页面上直接试听）。"""
        job = _require(job_id)
        stem = job.stems.get(kind)
        if not stem:
            raise HTTPException(status_code=404, detail="没有上传这条音源")
        return _audio_response(Path(str(stem["path"])), request)

    # ------------------------------------------------------------------ 进度
    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str, request: Request, since: int = 0) -> StreamingResponse:
        job = _require(job_id)

        async def stream() -> AsyncIterator[bytes]:
            seq = max(0, since)
            if job.terminal and not job.events_since(seq):
                yield _sse(_terminal_event(job, seq + 1))
                return
            yield b": connected\n\n"
            silence = 0.0
            while True:
                pending = job.events_since(seq)
                for event in pending:
                    seq = event.seq
                    yield _sse(event.as_dict())
                if pending:
                    silence = 0.0
                if job.terminal:
                    # 终态事件已经在上面发过了；再确认一次没有漏的就可以收尾
                    for event in job.events_since(seq):
                        seq = event.seq
                        yield _sse(event.as_dict())
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

    # ------------------------------------------------------------------ 数据
    async def _read_json(job: Job, name: str) -> dict[str, Any]:
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

    @app.get("/api/jobs/{job_id}/project")
    async def project(job_id: str) -> dict[str, Any]:
        return await _read_json(_require(job_id), "project.json")

    @app.get("/api/jobs/{job_id}/lyrics")
    async def lyrics(job_id: str) -> dict[str, Any]:
        return await _read_json(_require(job_id), "lyrics.json")

    @app.get("/api/jobs/{job_id}/lanes")
    async def lanes(job_id: str) -> dict[str, Any]:
        """卷帘要画的东西（秒域）：小节表 / 段落 / 和弦 / 人声音符 / 歌词行。"""
        job = _require(job_id)
        try:
            return await run_in_threadpool(build_lanes, job.out_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/peaks")
    async def peaks(job_id: str, buckets: int | None = None,
                    source: str = "mix") -> dict[str, Any]:
        """波形包络。`source=stem` 取上传的干声，默认取混音原曲。

        **没有音频就是 404，不是 500**：`output/` 里载入的只读工程本来就没有原音频，
        按文件名去解码会一路炸到 `media.compute_peaks`（以前就是这么 500 的）。
        """
        job = _require(job_id)
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

    @app.get("/api/jobs/{job_id}/audio")
    async def audio(job_id: str, request: Request) -> Response:
        """上传的音频本体。**支持 Range**——不然浏览器拖动进度条就废了。"""
        job = _require(job_id)
        if not job.upload.is_file():
            raise HTTPException(status_code=404,
                                detail="这个工程是从 output/ 载入的已有产物，没有原音频")
        return _audio_response(job.upload, request)

    # ------------------------------------------------------------------ 产物
    @app.get("/api/jobs/{job_id}/files/{path:path}")
    async def download_file(job_id: str, path: str) -> FileResponse:
        job = _require(job_id)
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

    @app.get("/api/jobs/{job_id}/download")
    async def download_bundle(job_id: str, kind: str = "zip",
                              parts: str = "") -> FileResponse:
        """下载：默认整包 zip；`kind=svp` 导 **Synthesizer V Studio 工程**。

        SVP 的内容由下载弹窗勾选（`?parts=vocal,instrumental,chords`），默认只有人声；
        同一组勾选会缓存成一份文件，重复点不重新生成。
        """
        job = _require(job_id)
        if kind == "svp":
            return await _svp_download(job, parts)
        try:
            bundle = await run_in_threadpool(manager.bundle, job)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        name = f"{job.name or 'sheetsage2'}-{job.id}.zip"
        return FileResponse(bundle, filename=name, media_type="application/zip",
                            headers={"X-Artifact-Rev": str(job.artifacts_rev)})

    @app.get("/api/jobs/{job_id}/exports")
    async def export_options(job_id: str) -> dict[str, Any]:
        """下载弹窗要显示的内容：哪几组能导、各多少个音、会占几轨。"""
        job = _require(job_id)
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

    async def _svp_download(job: Job, parts: str) -> FileResponse:
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

    # ------------------------------------------------------------------ 前端
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        """浏览器总会来要一次标签页图标。我们没做图标（前端用的是内联 SVG），
        这里回 204 —— 不然日志里天天一行 `404 /favicon.ico`，看着像出错了。"""
        return Response(status_code=204)

    # ------------------------------------------------------------------ 兜底
    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE",
                                                "HEAD", "OPTIONS"],
                   include_in_schema=False)
    async def unknown_api(rest: str, request: Request) -> Response:
        """`/api/*` 下没匹配上的路径，回一个**说得清**的 JSON 404。

        不加这条的话，请求会落到最后挂的 `StaticFiles`——它只接受 GET/HEAD，于是非 GET
        的请求拿到一个光秃秃的 `405 Method Not Allowed`，看不出是"接口不存在"还是
        "方法不对"（前端只会显示 "Method Not Allowed"）。实测踩过：删除工程时路径里
        少了一段，报的就是这个。
        """
        return JSONResponse(status_code=404, content={
            "detail": f"没有这个接口：{request.method} {request.url.path}",
            "method": request.method, "path": request.url.path})

    if static_ready:
        # 必须最后挂：放在 API 路由之后，`/api/*` 才不会被这条兜底吃掉
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    else:
        @app.get("/", response_class=PlainTextResponse)
        async def missing_frontend() -> str:
            return BUILD_HINT

    return app


def data_root_from_env() -> Path:
    """数据目录：`SHEETSAGE_WEBUI_DATA` 优先，默认 `<项目根>/output`（一个子目录 = 一个工程）。"""
    override = os.environ.get("SHEETSAGE_WEBUI_DATA")
    return Path(override) if override else DEFAULT_DATA_ROOT
