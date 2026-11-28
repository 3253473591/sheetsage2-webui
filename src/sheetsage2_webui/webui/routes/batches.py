"""批量路由：扫描预览、一次建一批工程、批次的列表与明细。

**批次不是实体**：没有 `.batches/` 目录、没有第二套状态机——批次 id 与标题写在每个工程的
`job.json` 里（`Job.batch`），`GET /api/batches` 从工程列表**派生**（见 `job_models.batches_of`）。
所以重启之后分组自动还在，删掉某个工程也只是那一行少一首。

三条约束（和 `docs/模块结构.md` 一致）：
- 路由只做参数整形与状态码，扫描/配对/建工程都在 `batch_scan` 与 `JobManager` 里；
- 扫目录、复制音频都是磁盘活 → 一律 `run_in_threadpool`；
- 建工程走 `JobManager.create_from_path`（不是再写一遍上传逻辑）。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from ...asr import read_lrc
from .. import batch_scan
from ..batch_scan import MAX_BATCH, scan_folder
from ..job_errors import ImportedProject, StageNotReady, UploadRejected
from ..job_models import TERMINAL_STATES, Job, batches_of
from ..job_options import validate_options
from ..preflight import ALIGNER_MODEL_DIR
from ._shared import ApiDeps

#: 批量默认跑「扒谱 → 歌词」一条龙（串行，一个跑完自动接下一个）。
BATCH_PLAN: tuple[str, ...] = ("transcribe", "lyrics")


def build_router(deps: ApiDeps) -> APIRouter:
    router = APIRouter()
    manager = deps.manager

    # ------------------------------------------------------------------ 扫描
    @router.post("/batches/browse")
    async def browse_folder() -> dict[str, Any]:
        """弹**服务进程**的原生目录选择器（本机工具才有这个便利）。

        页面拿不到服务器目录，所以"浏览…"只能由服务端弹窗；没有 tkinter / 没有桌面时
        回 501 + 一句人话，界面据此提示"手动粘贴路径"。
        """
        try:
            # 走模块属性（而不是导入时绑定的名字）：测试要能替换掉它，别真弹窗
            folder = await run_in_threadpool(batch_scan.pick_folder)
        except batch_scan.FolderPickUnavailable as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        return {"dir": folder}

    @router.post("/batches/scan")
    async def scan_batch(body: dict[str, Any]) -> dict[str, Any]:
        """**预览**（不落盘）：这个文件夹里有哪些歌、哪些配到了词、哪些有问题。"""
        folder = _folder_of(body)
        try:
            result = await run_in_threadpool(
                scan_folder, folder,
                data_root=manager.root,
                max_items=_max_items(body),
                probe=_probe_duration,
                aligner_available=_aligner_available(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result.as_dict()

    # ------------------------------------------------------------------ 建批次
    @router.post("/batches", status_code=201)
    async def create_batch(body: dict[str, Any]) -> dict[str, Any]:
        """按文件夹建一批工程：每首一个工程（`output/<歌名>/`），一起排进串行队列。

        - 参数一次设定跟着整批（`options`），其中 **歌词源按扫描结果逐首覆盖**——
          批量里不能让用户手填一首的 LRC，那就不叫批量了；
        - LRC 以**文本**写进 `options.lyrics_lrc`（和歌词抽屉一个口径），
          所以源目录以后被挪走/删掉也不影响重跑；
        - 某首建不起来只记进 `problems`（不中断整批）；一首都没建起来才 400。
        """
        folder = _folder_of(body)
        plan = _plan_of(body)
        link = bool(body.get("link", True))
        raw_options = body.get("options") or {}
        if not isinstance(raw_options, dict):
            raise HTTPException(status_code=400, detail="options 必须是对象")
        try:
            base_options = validate_options(raw_options)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            scanned = await run_in_threadpool(
                scan_folder, folder,
                data_root=manager.root,
                max_items=_max_items(body),
                probe=_probe_duration,
                aligner_available=_aligner_available(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        batch_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        title = _batch_title(Path(scanned.folder).name or "批量", manager.list())
        batch = {"id": batch_id, "title": title, "folder": str(scanned.folder)}

        jobs: list[dict[str, Any]] = []
        problems = list(scanned.problems)
        for index, item in enumerate(scanned.items):
            options = dict(base_options)
            if item.lrc is not None:
                try:
                    options["lyrics_lrc"] = read_lrc(item.lrc)
                except OSError as exc:
                    problems.append(f"{item.audio.name}：读歌词失败（{exc}）")
                    continue
            # `index` = 文件夹里的顺序：批次内的排序**不靠时钟**（Windows 的时钟粒度
            # 约 15ms，一批工程几毫秒就建完了，时间戳并列时顺序会乱）。
            entry = dict(batch, index=index)
            try:
                job = manager.create_from_path(
                    item.audio, plan=plan, options=options, batch=entry, link=link)
            except UploadRejected as exc:
                problems.append(f"{item.audio.name}：{exc}")
                continue
            jobs.append(job.row())

        if not jobs:
            detail = "一首都没建起来" + ("：" + "；".join(problems[:3]) if problems else "")
            raise HTTPException(status_code=400, detail=detail)
        return {"batch": batch, "jobs": jobs, "problems": problems}

    # ------------------------------------------------------------------ 上传式批量
    @router.post("/batches/upload", status_code=201)
    async def upload_into_batch(
        file: Annotated[UploadFile, File(...)],
        lyrics: Annotated[str | None, Form()] = None,
        batch_id: Annotated[str | None, Form()] = None,
        title: Annotated[str | None, Form()] = None,
        options: Annotated[str | None, Form()] = None,
        plan: Annotated[str | None, Form()] = None,
        expected_total: Annotated[int | None, Form()] = None,
    ) -> dict[str, Any]:
        """**拖文件夹**这条路的建工程接口：一次一个文件，带同一个 `batch_id` 就归一批。

        为什么不像扫描那样一次建完：浏览器拖进来的文件只能一个个上传（几百 MB 的音频
        不能攒在内存里）。所以客户端先传第一个（服务端铸一个批次 id 给它），后面每个
        都带上这个 id；`index` 按已有成员数递增，批次里的顺序 = 上传顺序。
        """
        raw: dict[str, Any] = {}
        if options:
            try:
                parsed = json.loads(options)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="options 不是合法的 JSON") from exc
            if not isinstance(parsed, dict):
                raise HTTPException(status_code=400, detail="options 必须是对象")
            raw.update(parsed)
        if lyrics:
            raw["lyrics_lrc"] = lyrics
        try:
            values = validate_options(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        batch = _ensure_batch(manager.list(), batch_id, title, expected_total)
        # 单批上限：**已经有多少首**比 `expected_total` 更可信（客户端可能撒谎/漏传）
        members = [job for job in manager.list()
                   if (job.batch or {}).get("id") == batch["id"]]
        if len(members) >= MAX_BATCH:
            raise HTTPException(
                status_code=400,
                detail=f"这一批已经到 {MAX_BATCH} 首上限了：剩下的请分到另一个文件夹，"
                       f"再拖一次就是新的一批")
        # `index` = 批次里的序号（上传顺序）；`job.batch` 带着它，排序就不靠时钟
        entry = dict(batch, index=len(members))
        wanted = _plan_of({"plan": plan})
        try:
            job = await run_in_threadpool(
                manager.create, file.filename or "audio", file.file, wanted,
                options=values, batch=entry)
        except UploadRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"batch": job.batch, "jobs": [job.row()], "problems": []}

    # ------------------------------------------------------------------ 暂停 / 继续
    @router.post("/batches/pause")
    async def pause() -> dict[str, Any]:
        """不再派发新的工程（**正在跑的那一首不打断**）。"""
        manager.pause()
        return {"paused": manager.paused}

    @router.post("/batches/resume")
    async def resume() -> dict[str, Any]:
        manager.resume()
        return {"paused": manager.paused}

    # ------------------------------------------------------------------ 查询
    @router.get("/batches")
    async def list_batches() -> dict[str, Any]:
        return {"batches": batches_of(manager.list())}

    @router.get("/batches/{batch_id}")
    async def get_batch(batch_id: str) -> dict[str, Any]:
        """批次明细：成员行（轻量投影）+ 统计。行点进去就是普通工程页。"""
        members = _members(manager.list(), batch_id)
        if not members:
            raise HTTPException(status_code=404, detail="没有这个批次")
        return {
            "batch": members[0].batch,
            "rows": [job.row() for job in members],
            "stats": _stats(members),
            # 闸门可能是"用户点的暂停"，也可能是**磁盘满自动暂停**——界面要能分辨
            "paused": manager.paused,
        }

    @router.delete("/batches/{batch_id}")
    async def delete_batch(batch_id: str, permanent: bool = False) -> dict[str, Any]:
        """**整批删除**：成员全进回收站（`permanent=true` 才是真删）。

        为什么不在跑的时候删：取消是协作式的，正在写的推理还在往工程目录里落文件；
        等它停下来再删，才不会删到一半又长回来。所以这里回 409 让人先点「停止」。
        """
        members = _members(manager.list(), batch_id)
        if not members:
            raise HTTPException(status_code=404, detail="没有这个批次")
        busy = [job for job in members if job.state not in TERMINAL_STATES]
        if busy:
            raise HTTPException(
                status_code=409,
                detail=f"这一批还有 {len(busy)} 首在跑或排队：先点「停止」，等它停下来再删")
        deleted: list[str] = []
        skipped: list[dict[str, Any]] = []
        for job in members:
            try:
                if manager.delete(job.id, permanent=permanent):
                    deleted.append(job.id)
            except (ImportedProject, RuntimeError) as exc:
                skipped.append({"id": job.id, "name": job.name, "reason": str(exc)})
        if not deleted:
            reason = skipped[0]["reason"] if skipped else "一首都没删掉"
            raise HTTPException(status_code=409, detail=reason)
        return {"deleted": len(deleted), "job_ids": deleted, "skipped": skipped}

    @router.post("/batches/{batch_id}/resume")
    async def resume_remaining(batch_id: str) -> dict[str, Any]:
        """**继续剩余**：把这一批里没跑完的成员按顺序重新排队（已完成的绝不动）。

        这是"崩溃/断电之后回来怎么办"的答案：重启后成员的阶段被标成 idle、工程退回 draft，
        队列是空的——点这里一次就能接着跑，而不是手动点 28 次。

        顺手解除派发闸门（磁盘满自动暂停之后，用户清理完点这个就能继续）。
        """
        members = _members(manager.list(), batch_id)
        if not members:
            raise HTTPException(status_code=404, detail="没有这个批次")
        manager.resume()
        resumed: list[str] = []
        skipped: list[dict[str, Any]] = []
        for job in members:
            if job.imported:
                skipped.append({"id": job.id, "name": job.name, "reason": "只读产物，不能重跑"})
                continue
            with job.lock:
                transcribe_done = job.stages["transcribe"].state == "done"
                lyrics_done = job.stages["lyrics"].state == "done"
            if transcribe_done and lyrics_done:
                continue
            try:
                if transcribe_done:
                    manager.run_stage(job.id, "lyrics", force=True)
                else:
                    manager.run_chain(job.id, force=True)
            except (ImportedProject, StageNotReady, RuntimeError, ValueError) as exc:
                skipped.append({"id": job.id, "name": job.name, "reason": str(exc)})
                continue
            resumed.append(job.id)
        return {"resumed": len(resumed), "job_ids": resumed, "skipped": skipped}

    return router


# --------------------------------------------------------------------- 内部
def _folder_of(body: dict[str, Any]) -> str:
    folder = str(body.get("dir") or "").strip()
    if not folder:
        raise HTTPException(status_code=400, detail="要扫哪个文件夹？（dir）")
    return folder


def _max_items(body: dict[str, Any]) -> int:
    try:
        value = int(body.get("max_items") or MAX_BATCH)
    except (TypeError, ValueError):
        value = MAX_BATCH
    return max(1, min(value, MAX_BATCH))


def _plan_of(body: dict[str, Any]) -> list[str]:
    raw = body.get("plan")
    items = ([str(part).strip() for part in str(raw).split(",")]
             if raw else list(BATCH_PLAN))
    wanted = [phase for phase in items if phase]
    unknown = [phase for phase in wanted if phase not in BATCH_PLAN]
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"未知阶段 {unknown}；可选 {list(BATCH_PLAN)}")
    return wanted or list(BATCH_PLAN)


def _probe_duration(path: Path) -> dict[str, Any] | None:
    """批量预览里的时长：ffmpeg 探一下，探不到就当没有（不阻断扫描）。"""
    try:
        from ..media import probe_audio

        return probe_audio(path)
    except Exception:            # noqa: BLE001 - 体检坏了不该让整个文件夹扫不了
        return None


def _aligner_available() -> bool | None:
    """日/韩 LRC 要 Qwen 对齐器；没装就在预览里提前说。"""
    return (ALIGNER_MODEL_DIR / "config.json").is_file()


def _members(jobs: list[Job], batch_id: str) -> list[Job]:
    members = [job for job in jobs if (job.batch or {}).get("id") == batch_id]
    # 有 `index`（= 文件夹里的顺序）就按它；老批次没有就退回创建时间。
    members.sort(key=lambda job: ((job.batch or {}).get("index", 1 << 30), job.created_at))
    return members


def _batch_title(base: str, jobs: list[Job]) -> str:
    """批次标题：`名字 · 月-日 时:分`。

    - **用来源的名字**（扫描 = 文件夹名；拖拽 = 浏览器给的文件夹名），不要"拖入 N 首"——
      那种名字过一夜就分不清是哪批；
    - 带时分（不只日期）：同一个文件夹一天可能跑两批；
    - 同一分钟又同名 → 加 `(2)`、`(3)`：侧栏里两个一模一样的组没法认。
    """
    name = (base or "").strip() or "未命名批次"
    stamp = time.strftime("%m-%d %H:%M")
    existing = {str((job.batch or {}).get("title") or "") for job in jobs}
    title = f"{name} · {stamp}"
    index = 2
    while title in existing:
        title = f"{name} · {stamp} ({index})"
        index += 1
    return title


def _ensure_batch(jobs: list[Job], batch_id: str | None, title: str | None,
                  expected_total: int | None = None) -> dict[str, Any]:
    """上传式批量的归属：给了 `batch_id` 就复用（标题不覆盖），否则铸一个新的。

    `expected_total` 只在**缺的时候**补上（客户端知道拖了几首）：传了一半断掉之后，
    界面就靠它说"还差 N 首"。
    """
    wanted = str(batch_id or "").strip()
    existing = next((job.batch for job in jobs
                     if wanted and (job.batch or {}).get("id") == wanted), None)
    if existing is not None:
        batch = dict(existing)
        batch.pop("index", None)                 # index 是每个工程的，不进批次头
        if expected_total and not batch.get("expected_total"):
            batch["expected_total"] = int(expected_total)
        return batch
    new_id = wanted or f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    batch = {
        "id": new_id,
        "title": _batch_title(str(title).strip() if title and str(title).strip()
                              else "拖入的文件", jobs),
        "folder": None,
    }
    if expected_total:
        batch["expected_total"] = int(expected_total)
    return batch


def _stats(members: list[Job]) -> dict[str, Any]:
    states = [job.state for job in members]
    return {
        "total": len(members),
        "done": states.count("done"),
        "failed": states.count("failed"),
        "running": states.count("running"),
        "pending": sum(1 for state in states if state in ("queued", "draft", "cancelled")),
        "created_at": min(job.created_at for job in members),
        "finished_at": max((job.stages[phase].finished_at or 0.0
                            for job in members for phase in job.stages), default=0.0),
    }
