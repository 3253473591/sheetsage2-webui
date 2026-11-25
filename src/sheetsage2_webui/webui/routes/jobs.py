"""工程与阶段路由：环境信息、工程增删改、阶段（重）跑、参数、音源、回收站、SSE。

全部挂在 `/api` 下。**导入时没有任何副作用**：依赖由 :class:`ApiDeps` 传进来。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from ... import __version__
from ...asr import language_choices
from ...bootstrap import find_ffmpeg
from ...meta import write_meta
from ..job_errors import ImportedProject, StageNotReady, StaleRevision, UploadRejected
from ..batch_scan import MAX_BATCH
from ..job_models import DEFAULT_PLAN, PHASE_LABELS, PHASES
from ..job_options import param_choices
from ..preflight import preflight
from ._shared import ApiDeps, event_stream, require_job

#: 阶段卡上显示的引擎名。
PHASE_ENGINES = {"transcribe": "SheetSage2", "lyrics": "Qwen3-ASR"}


def build_router(deps: ApiDeps) -> APIRouter:
    router = APIRouter()
    manager = deps.manager

    # ------------------------------------------------------------------ 基础
    @router.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @router.get("/env")
    async def env() -> dict[str, Any]:
        info = manager.env()
        engine = deps.session.describe() if deps.session is not None else {}
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
            "static_ready": deps.static_ready,
            "max_upload_mb": manager.max_upload_mb,
            # 单批上限：批量导入弹层用它画"一次最多 N 首"，拖拽上传也据此提前拦
            "batch_max": MAX_BATCH,
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
    @router.get("/jobs")
    async def list_jobs(view: str = "") -> dict[str, Any]:
        """工程列表。`view=rows` 是**轻量投影**（侧栏 / 批量表用：不带 `options` 里的 LRC
        全文、不带产物清单），全量快照仍按 `GET /api/jobs/{id}` 取。"""
        if view == "rows":
            return {"jobs": [job.row() for job in manager.list()]}
        return {"jobs": [job.snapshot() for job in manager.list()]}

    @router.post("/jobs", status_code=201)
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

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        job = require_job(deps, job_id)
        # 手动精修只改/加 auto.svp / manual.svp；再打开工程时顺手刷新 meta，
        # 不需要用户重启或跑脚本。写失败不影响快照。
        try:
            await run_in_threadpool(write_meta, job.out_dir)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return job.snapshot()

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        job = manager.cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="没有这个工程")
        return job.snapshot()

    @router.delete("/jobs/{job_id}")
    async def delete_job(job_id: str, permanent: bool = False) -> dict[str, Any]:
        """删除工程。默认进**回收站**（能恢复）；`?permanent=true` 才是真删。"""
        require_job(deps, job_id)
        try:
            manager.delete(job_id, permanent=permanent)
        except ImportedProject as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"deleted": job_id, "permanent": bool(permanent)}

    # ------------------------------------------------------------------ 回收站
    @router.get("/trash")
    async def list_trash() -> dict[str, Any]:
        """回收站：被删掉的工程（`.trash/` 下的目录）。"""
        return {"entries": await run_in_threadpool(manager.trash)}

    @router.post("/trash/{entry_id}/restore")
    async def restore_from_trash(entry_id: str) -> dict[str, Any]:
        job = await run_in_threadpool(manager.restore, entry_id)
        if job is None:
            raise HTTPException(status_code=404, detail="回收站里没有这一项")
        return job.snapshot()

    @router.delete("/trash/{entry_id}")
    async def purge_from_trash(entry_id: str) -> dict[str, Any]:
        if not manager.purge_from_trash(entry_id):
            raise HTTPException(status_code=404, detail="回收站里没有这一项")
        return {"purged": entry_id}

    @router.post("/jobs/{job_id}/cleanup")
    async def cleanup_job(job_id: str) -> dict[str, Any]:
        """清掉一个工程的可重算缓存（zip / SVP / 波形 / prev），释放磁盘（G11）。"""
        require_job(deps, job_id)
        return await run_in_threadpool(manager.cleanup, job_id)

    # ------------------------------------------------------------------ 阶段
    @router.post("/jobs/{job_id}/stages/{phase}")
    async def run_stage(job_id: str, phase: str) -> dict[str, Any]:
        """单独（重）跑一个阶段。

        歌词识别的前置条件是**扒谱已完成**（工作流是 扒谱 → 歌词），没满足就 409 +
        "请先完成扒谱"；界面把这句直接显示出来。要在扒谱还没跑完时排歌词，用 `/chain`。
        """
        require_job(deps, job_id)
        try:
            job = manager.run_stage(job_id, phase)
        except (StageNotReady, ImportedProject) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job.snapshot()                       # type: ignore[union-attr]

    @router.post("/jobs/{job_id}/chain")
    async def run_chain(job_id: str) -> dict[str, Any]:
        """「⏵ 自动提取」：扒谱 → 歌词 一条龙（故意在扒谱没跑完时就排歌词）。"""
        require_job(deps, job_id)
        try:
            job = manager.run_chain(job_id)
        except ImportedProject as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job.snapshot()                       # type: ignore[union-attr]

    @router.patch("/jobs/{job_id}/options")
    async def set_options(job_id: str, body: dict[str, Any],
                          request: Request) -> dict[str, Any]:
        """改工程级可调参数（参数面板：精度 / 设备 / 范围 / 量化 / 模型 / 音源 / 语言）。

        校验在 `job_options.validate_options`（和 `/api/env` 发出去的取值表同源）。
        改了参数 = 换了口径，对应阶段的旧结果作废；阶段正在跑改不了（409）。

        **乐观锁**（G8）：body 里带 `expected_rev`（或 `If-Match: <rev>` 头）时，
        服务端按工程当前的 `rev` 校验——不一致回 409 + 当前快照，前端据此提示"这页是旧的"。
        """
        job = require_job(deps, job_id)
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
    @router.post("/jobs/{job_id}/stems/{kind}", status_code=201)
    async def upload_stem(job_id: str, kind: str,
                          file: Annotated[UploadFile, File(...)]) -> dict[str, Any]:
        """给工程补一条音源（`vocal` = 干声）。**不校验内容**：传混音原曲也允许。"""
        require_job(deps, job_id)
        try:
            job = await run_in_threadpool(manager.attach_stem, job_id, kind,
                                          file.filename or "stem", file.file)
        except UploadRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409 if isinstance(exc, RuntimeError) else 400,
                                detail=str(exc)) from exc
        return job.snapshot()                       # type: ignore[union-attr]

    @router.delete("/jobs/{job_id}/stems/{kind}")
    async def delete_stem(job_id: str, kind: str) -> dict[str, Any]:
        require_job(deps, job_id)
        try:
            job = await run_in_threadpool(manager.detach_stem, job_id, kind)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409 if isinstance(exc, RuntimeError) else 400,
                                detail=str(exc)) from exc
        return job.snapshot()                       # type: ignore[union-attr]

    # ------------------------------------------------------------------ 进度
    @router.get("/jobs/{job_id}/events")
    async def events(job_id: str, request: Request, since: int = 0):
        job = require_job(deps, job_id)
        return event_stream(job, manager, since, request)

    return router
