"""本地 Web 服务：FastAPI + SSE（任务文档 3.1 / 3.6 / 5）。

职责边界
--------
本模块**只做传输层**：静态页面、上传、提交、进度推送、停止、产物下载、
环境检测与设置。所有推理与产物生成都在 ``app.worker`` 子进程里完成，
进度一律来自 worker 的真实事件，服务端不做任何假推进。

仅监听 ``127.0.0.1``（文档 3.1：不暴露公网）。
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import sys
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Mapping, Sequence

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import config, envcheck
from app.rebuild import normalize_export_voices
from app.tasks import TaskBusyError, manager

# --------------------------------------------------------------------------
# 约定
# --------------------------------------------------------------------------
SSE_HEARTBEAT_SECONDS = 15.0  # 文档 3.1：带心跳，防止前端卡死
SSE_POLL_SECONDS = 0.15

app = FastAPI(title="SheetSage2 最小化扒谱 Web 工具", docs_url=None, redoc_url=None)

# 静态资源本地化提供（abcjs 谱面渲染库），保证离线可用。
# 不挂载的话页面请求 /vendor/abcjs/abcjs-basic-min.js 会 404，乐谱就渲染不出来。
_VENDOR_DIR = config.WEB_DIR / "vendor"
if _VENDOR_DIR.is_dir():
    app.mount("/vendor", StaticFiles(directory=str(_VENDOR_DIR)), name="vendor")

# 本项目**自己的**前端脚本（钢琴卷帘等）。刻意不放 /vendor —— 那里是第三方库，
# 混进去会让"哪些是我们写的、哪些是别人的、许可怎么算"变得含混。
# 不挂载的话页面请求 /assets/pianoroll.js 会 404，③ 的卷帘整个挂不上。
if config.WEB_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(config.WEB_DIR)), name="assets")

#: 已上传文件登记表：token → {"path": Path, "name": 原始文件名}。
#: 进程重启会丢，但磁盘上的 ``<token>.name`` 边车文件能让我们重建它。
_uploads: dict[str, dict[str, Any]] = {}
#: 环境全量检测缓存（全量约 2.3s，不宜每次调用都跑）
_env_cache: dict[str, Any] = {"at": 0.0, "data": None}
ENV_CACHE_SECONDS = 30.0

#: 本服务实际监听的端口。端口检测不能把它自己算成「被占用」，
#: 否则服务会把自己判为环境不通过、永远拒绝开始任务。
_self_port: int | None = None


def _sse(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _uploads_dir() -> Path:
    d = config.TMP_DIR / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _upload_record(token: str) -> dict[str, Any] | None:
    """按 token 取上传登记；内存里没有时**从磁盘重建**。

    上传文件落盘为 ``<token>.<ext>``，原始文件名写在 ``<token>.name`` 边车里。
    这样服务重启后仍然能恢复「已上传的音频」，用户刷新页面后可以直接重跑。
    """
    rec = _uploads.get(token)
    if rec and Path(rec["path"]).is_file():
        return rec
    d = _uploads_dir()
    for path in d.glob(f"{token}.*"):
        if path.suffix == ".name" or not path.is_file():
            continue
        side = d / f"{token}.name"
        try:
            original = side.read_text(encoding="utf-8").strip() if side.is_file() else path.name
        except OSError:
            original = path.name
        rec = {"path": path, "name": original or path.name}
        _uploads[token] = rec
        return rec
    return None


def _cleanup_old_uploads(max_age_hours: float = 24.0) -> int:
    """清理过期的上传临时文件（文档 4.3：任务结束后清理临时目录）。

    上传的音频会被登记在内存里供本进程使用，进程重启后登记表就失效了，
    磁盘上的副本却会一直留着——启动时扫一遍删掉，避免越积越多。
    """
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    try:
        for p in _uploads_dir().iterdir():
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                removed += 1
    except OSError:
        pass
    return removed


# --------------------------------------------------------------------------
# 缓存清理（清空 output/ 与 tmp/ 的内容）
# --------------------------------------------------------------------------
#: 允许清理的目录**白名单** —— 只有这两个，永远不要往里加别的。
#: 用「名字 → 路径」而不是直接收前端传路径，是为了让接口**永远无法删到别处**
#: （前端只发 `targets: ["output","tmp"]`，认不出的名字直接 400）。
_CACHE_TARGETS = {"output": config.OUTPUT_DIR, "tmp": config.TMP_DIR}


def _tree_stat(path: Path) -> dict[str, int]:
    """统计一棵子树的大小 / 文件数 / 子目录数。占用、权限问题一律忽略。"""
    stat = {"bytes": 0, "files": 0, "dirs": 0}
    if not path.is_dir():
        return stat
    for p in path.rglob("*"):
        try:
            if p.is_symlink():
                continue
            if p.is_file():
                stat["files"] += 1
                stat["bytes"] += p.stat().st_size
            elif p.is_dir():
                stat["dirs"] += 1
        except OSError:
            continue
    return stat


def _entry_stat(path: Path) -> dict[str, int]:
    """统计**一个条目**：目录走 ``_tree_stat``，普通文件按它自己算。

    ⚠️ 别用 ``_tree_stat`` 直接量普通文件 —— 它的第一行就是
    `if not path.is_dir(): return 0`，散在 ``tmp/`` 根下的文件（开发期的
    ``duet_*.wav``、worker 的中间产物）会被统计成 0 字节 / 0 个文件，
    于是"清理了但报告说没释放"。沙盒测试实测踩到过。
    """
    if path.is_dir() and not path.is_symlink():
        st = _tree_stat(path)
        # `_tree_stat` 用 rglob 只数**后代**，根目录自己不算 —— 但清理时这个
        # 子目录是**真的被删掉了**，必须计入，否则会和 `/api/cache` 的口径对不上
        # （显示"output 有 2 个目录"，清完却报"删了 0 个目录"）。
        st["dirs"] += 1
        return st
    try:
        return {"bytes": path.stat().st_size, "files": 1, "dirs": 0}
    except OSError:
        return {"bytes": 0, "files": 0, "dirs": 0}


def _clear_dir_contents(path: Path) -> dict[str, int]:
    """删掉目录**里面**的东西，**目录本身保留**（启动/上传/出任务都要用它）。

    ⚠️ 这是**永久删除**，不走回收站 —— 所以调用方必须先让用户二次确认。
    Windows 上正被占用的文件（比如浏览器还在读上传的音频）会删不掉，
    这里**不抛异常**，只计数进 ``failed``，由接口如实回报给用户。
    """
    stat = {"bytes": 0, "files": 0, "dirs": 0, "failed": 0}
    if not path.is_dir():
        return stat
    for child in list(path.iterdir()):
        sub = _entry_stat(child)
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError:
            stat["failed"] += 1
            continue
        stat["bytes"] += sub["bytes"]
        stat["files"] += sub["files"]
        stat["dirs"] += sub["dirs"]
    return stat


# --------------------------------------------------------------------------
# 静态页面
# --------------------------------------------------------------------------
#: 本地小工具的所有文件都**随时会被重新生成**（导出、歌词、页面），必须禁止浏览器缓存。
#:
#: 踩过的坑：导出接口用**同一个 URL** 返回重新生成后的文件，响应头只有
#: `etag` / `last-modified` 而没有 `Cache-Control`，浏览器按启发式规则缓存了第一次的
#: 响应 —— 于是「取消 ② → 重新生成 → 再点导出」下到的还是那份**单轨旧文件**，
#: 表现成"导出怎么都是单轨"。（实测下载目录里两份文件的字节数一模一样。）
_NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


def _file_response(path: str | Path, *, filename: str | None = None) -> FileResponse:
    """统一的文件响应：一律 `no-store`，保证拿到的永远是磁盘上现在这一份。"""
    kwargs: dict[str, Any] = {"headers": dict(_NO_STORE)}
    if filename:
        kwargs["filename"] = filename
    return FileResponse(path, **kwargs)


@app.get("/")
def index() -> FileResponse:
    page = config.WEB_DIR / "index.html"
    if not page.is_file():
        raise HTTPException(status_code=503, detail="前端页面缺失：web/index.html")
    return _file_response(page)


@app.get("/api/audio/{token}/info")
def audio_info(token: str) -> JSONResponse:
    """轻量存在性检查：前端刷新后用文件名里的 token 还原已上传音频。

    单独做一个 JSON 接口而不是 HEAD，是因为 FastAPI 的 ``@app.get`` **不会**自动
    应答 HEAD（实测返回 405）；用 GET 直接问又会把整个音频拉一遍（十几 MB）。
    """
    rec = _upload_record(token)
    if rec is None:
        return JSONResponse({"available": False, "token": token})
    path = Path(rec["path"])
    return JSONResponse(
        {
            "available": True,
            "token": token,
            "name": rec["name"],
            "size": path.stat().st_size,
        }
    )

@app.get("/api/audio/{token}")
def stream_audio(token: str) -> FileResponse:
    """把已上传的音频回喂给浏览器，供 ① 的迷你播放器与波形使用。

    只认登记表里的 token，不接受任意路径（文档 4.5 防路径穿越）。
    """
    rec = _upload_record(token)
    if rec is None:
        raise HTTPException(status_code=404, detail="上传文件已失效，请重新选择音频")
    return _file_response(Path(rec["path"]), filename=rec["name"])


@app.post("/api/tasks/{task_id}/open-folder")
def open_folder(task_id: str) -> JSONResponse:
    """在系统文件管理器里打开该任务的输出目录。"""
    import subprocess

    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    target = Path(task.out_dir)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="输出目录不存在")
    try:
        if os.name == "nt":
            # explorer 对含空格/中文的路径需要显式加引号参数
            subprocess.Popen(["explorer", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"无法打开目录：{exc}") from exc
    return JSONResponse({"opened": True, "dir": str(target)})


# --------------------------------------------------------------------------
# 上传（文档 4.5：限制扩展名、限制大小、清洗文件名）
# --------------------------------------------------------------------------
@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> JSONResponse:
    name = Path(file.filename or "audio").name  # 去掉任何目录成分
    ext = Path(name).suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        allowed = " / ".join(sorted(e.lstrip(".") for e in config.ALLOWED_EXTENSIONS))
        raise HTTPException(status_code=400, detail=f"仅支持 {allowed} 格式")

    settings = config.load_settings()
    limit_mb = int(settings.get("max_upload_mb") or config.MAX_UPLOAD_MB)
    limit = limit_mb * 1024 * 1024

    token = uuid.uuid4().hex
    target = _uploads_dir() / f"{token}{ext}"

    written = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过大小上限（{limit_mb} MB），请压缩或裁剪后再试",
                    )
                fh.write(chunk)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"写入上传文件失败：{exc}") from exc
    finally:
        await file.close()

    if written == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="上传内容为空")

    _uploads[token] = {"path": target, "name": name}
    # 边车文件：即使服务重启、内存登记表丢失，也能还原原始文件名与文件本身
    try:
        (target.parent / f"{token}.name").write_text(name, encoding="utf-8")
    except OSError:
        pass
    return JSONResponse(
        {
            "token": token,
            "name": name,
            "size": written,
            "ext": ext,
            "duration": _probe_duration(target),
        }
    )


def _probe_duration(path: Path) -> float | None:
    """用 ffprobe 读取时长（迷你播放器显示用）；失败不阻断流程。"""
    exe = config.ffprobe_path()
    if not exe:
        return None
    import subprocess

    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return float((out.stdout or "").strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# --------------------------------------------------------------------------
# 任务
# --------------------------------------------------------------------------
@app.post("/api/tasks")
def create_task(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    token = str(payload.get("token") or "")
    rec = _upload_record(token)
    if rec is None:
        raise HTTPException(status_code=400, detail="上传文件已失效，请重新选择音频")
    audio = Path(rec["path"])

    # 关键条件不满足时禁止开跑（文档 5.1）
    blocking = [c for c in _env(force=False)["checks"] if c["blocking"] and c["status"] == "fail"]
    if blocking:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "环境检测未通过，无法开始扒谱",
                "blocking": [{"label": c["label"], "detail": c["detail"], "fix": c["fix"]} for c in blocking],
            },
        )

    _settings = config.load_settings()
    params = {
        "preset": payload.get("preset", "default"),
        "dtype": payload.get("dtype", _settings.get("default_dtype", "bf16")),
        # 设备：请求里给了就用请求的，否则跟 settings.json 的 default_device（默认 auto）
        "device": payload.get("device") or _settings.get("default_device") or "auto",
        # 「导出哪些内容」：勾了什么导什么（人声主旋律 / 器乐旋律 / 和弦）
        "export_voices": normalize_export_voices(payload.get("export_voices"), payload.get("only_melody")),
        "drop_intro_outro": bool(payload.get("drop_intro_outro", False)),
        "max_seconds": payload.get("max_seconds"),
        "lyrics": payload.get("lyrics", "la"),
        # 保留原始文件名（磁盘上是 <token>.mp3，直接展示给用户会很难看）
        "audio_original_name": rec["name"],
    }
    if payload.get("output_dir"):
        params["output_dir"] = payload["output_dir"]

    try:
        task = manager.submit(audio, params)
    except TaskBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"task_id": task.id, "state": task.state, "out_dir": str(task.out_dir)})


@app.get("/api/state")
def get_state(task_id: str | None = None) -> JSONResponse:
    return JSONResponse(manager.snapshot(task_id))


@app.post("/api/stop")
def stop_task() -> JSONResponse:
    task = manager.stop()
    if task is None:
        return JSONResponse({"stopped": False, "detail": "当前没有可停止的任务"})
    return JSONResponse({"stopped": True, "task": task.as_dict()})


@app.get("/api/events")
async def events(request: Request, task_id: str | None = None) -> StreamingResponse:
    """SSE 进度流。

    先发一条全量 ``snapshot``，这样刷新页面 / 断线重连后能立刻恢复状态
    （文档 3.6.8），随后续发增量事件，并每 15s 发一次心跳。
    """

    async def stream():
        q = manager.subscribe()
        try:
            yield _sse({"type": "snapshot", **manager.snapshot(task_id)})
            last_beat = time.time()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = q.get_nowait()
                except queue.Empty:
                    if time.time() - last_beat >= SSE_HEARTBEAT_SECONDS:
                        yield ": heartbeat\n\n"
                        last_beat = time.time()
                    await asyncio.sleep(SSE_POLL_SECONDS)
                    continue
                yield _sse(event)
                last_beat = time.time()
        finally:
            manager.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# --------------------------------------------------------------------------
# 产物
# --------------------------------------------------------------------------
def _resolve_artifact(task_id: str, rel: str) -> Path:
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    root = Path(task.out_dir).resolve()
    target = (root / rel).resolve()
    # 防路径穿越（文档 4.5）
    if not str(target).startswith(str(root)) or not target.is_file():
        raise HTTPException(status_code=404, detail="产物不存在")
    return target


@app.get("/api/artifacts/{task_id}")
def list_artifacts(task_id: str) -> JSONResponse:
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    root = Path(task.out_dir)
    files = []
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if p.is_file():
                files.append(
                    {
                        "rel": str(p.relative_to(root)).replace("\\", "/"),
                        "name": p.name,
                        "size": p.stat().st_size,
                    }
                )
    return JSONResponse({"task_id": task_id, "out_dir": str(root), "files": files})


@app.get("/api/artifacts/{task_id}/download")
def download_artifact(task_id: str, rel: str) -> FileResponse:
    path = _resolve_artifact(task_id, rel)
    return _file_response(path, filename=path.name)


# --------------------------------------------------------------------------
# 乐谱编辑：保存改后的 ABC 并重建 SVP / MIDI
# --------------------------------------------------------------------------
_EXPORT_KINDS = {"svp": ".svp", "midi": ".mid", "abc": ".abc"}


@app.post("/api/tasks/{task_id}/abc")
def update_abc(task_id: str, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """保存用户编辑后的 ABC，并据此重建 ``export/`` 下的 SVP 与 MIDI。

    对应已定裁决：**编辑后导出也用编辑结果**。
    """
    from app.rebuild import rebuild_exports

    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if manager.busy():
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改乐谱")

    abc_text = str(payload.get("abc") or "")
    if not abc_text.strip():
        raise HTTPException(status_code=400, detail="ABC 内容为空")

    # 「导出哪些内容」以**页面当前勾选状态**为准（前端随请求带上），
    # 否则用户改完 ABC 保存时会被默认值悄悄改掉导出内容。
    export_voices = normalize_export_voices(payload.get("export_voices"), payload.get("only_melody"))

    result = rebuild_exports(
        task.out_dir,
        abc_text,
        bpm=payload.get("bpm"),
        export_voices=export_voices,
        lyrics=str(payload.get("lyrics") or "la"),
        project_name=task.audio.stem,
    )
    if not result.get("ok"):
        return JSONResponse(status_code=422, content=result)

    # 记回任务，便于刷新后仍能拿到编辑后的 ABC；同时把意图写进 params，
    # 后续「歌词填充 / 清空」等重建导出走的是同一份 export_voices，不再各说各话。
    task.params = {**(task.params or {}), "export_voices": export_voices,
                   "only_melody": export_voices == ["vocal"]}
    task.result = {
        **(task.result or {}),
        "abc_filtered": abc_text,
        "export": result,
        "abc_edited": True,
        "export_voices": export_voices,
        "only_melody": export_voices == ["vocal"],
    }
    return JSONResponse(result)


@app.get("/api/tasks/{task_id}/export/{kind}")
def download_export(task_id: str, kind: str) -> FileResponse:
    """下载导出产物（svp / midi / abc），文件名对用户友好。"""
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    ext = _EXPORT_KINDS.get(kind.lower())
    if ext is None:
        raise HTTPException(status_code=400, detail=f"不支持的导出类型：{kind}")

    export_dir = Path(task.out_dir) / "export"
    target = export_dir / f"{task.audio.stem}{ext}"
    if not target.is_file():
        # 回退：取 export/ 下第一个该扩展名的文件
        candidates = sorted(export_dir.glob(f"*{ext}")) if export_dir.is_dir() else []
        if not candidates:
            raise HTTPException(status_code=404, detail="导出产物尚未生成")
        target = candidates[0]
    return _file_response(target, filename=target.name)


def _disk_export_info(task: Any) -> list[dict[str, Any]]:
    """列出 ``export/`` 目录里**真实存在**的产物及其轨道名。

    为什么不复用 ``task.result``：那份记账会在事后被 ``update_abc`` / 歌词填充改写，
    页面也可能拿着旧结果 —— 于是出现「界面说 3 轨、下载到 1 轨」这种自相矛盾。
    要回答"点导出会得到什么"，唯一可信的就是磁盘上这个文件本身。
    """
    import mido

    export_dir = Path(task.out_dir) / "export"
    files: list[dict[str, Any]] = []
    if not export_dir.is_dir():
        return files
    for p in sorted(export_dir.iterdir()):
        if not p.is_file():
            continue
        item: dict[str, Any] = {
            "name": p.name,
            "size": p.stat().st_size,
            "mtime": p.stat().st_mtime,
            "tracks": [],
        }
        try:
            if p.suffix == ".svp":
                data = json.loads(p.read_text(encoding="utf-8"))
                item["tracks"] = [t.get("name") for t in data.get("tracks") or []]
            elif p.suffix == ".mid":
                mid = mido.MidiFile(p)
                item["tracks"] = [m.name for t in mid.tracks for m in t if m.type == "track_name"]
        except Exception as exc:  # noqa: BLE001 - 读不出来不算致命，如实标注
            item["tracks"] = None
            item["error"] = f"{type(exc).__name__}: {exc}"[:200]
        files.append(item)
    return files


@app.get("/api/tasks/{task_id}/export-info")
def export_info(task_id: str) -> JSONResponse:
    """磁盘上的导出产物实况（轨道名 / 大小 / 生成时间）。"""
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return JSONResponse({"task_id": task_id, "out_dir": str(task.out_dir),
                         "files": _disk_export_info(task)})


# --------------------------------------------------------------------------
# 歌词识别（独立任务；对应页面「⑤ 歌词识别」）
# --------------------------------------------------------------------------
def _chord_track_notes(root: Path) -> list[dict[str, Any]]:
    """和弦音符（秒），供「取消仅主旋律乐谱」时的试听与导出。

    实现见 :func:`app.rebuild.load_chord_notes`——试听与导出**共用同一处**，
    避免「试听有和弦、导出的 SVP/MIDI 没有」这类分歧。
    """
    from app.rebuild import load_chord_notes

    return load_chord_notes(root)


@app.get("/api/tasks/{task_id}/notes")
def get_notes(task_id: str, only_melody: int = 1, voices: str | None = None) -> JSONResponse:
    """返回**当前乐谱**（含用户编辑过的版本）解析出的音符，供卷帘、试听与歌词预览。

    之所以不直接用模型原始的 ``playback.json``：用户在卷帘上改过音符之后，
    试听必须跟着改后的谱面走，否则听到的和看到的不是一回事。

    优先级：``export/<歌名>.abc``（最近一次生成导出的版本）→ ``score.edited.abc``
    → ``score.melody.abc`` → ``score.abc``。

    **要哪些内容**（二选一，``voices`` 优先）：

    * ``voices=vocal,ins`` —— 前端 ③ 的三个勾选。**推荐**：能精确表达
      "只要器乐旋律"这种既不是"只人声"也不是"全都要"的组合。
    * ``only_melody=1|0`` —— 老的布尔口径，保留兼容。``0`` 等于"全都要"。

    ⚠️ 只用 ``only_melody`` 会出错：它只有两个值，而勾选有三种独立开关。
    实测只勾「器乐旋律」时它=``0``，于是**和弦轨也一起被放进来**（预览里多出和弦、
    卷帘上多出灰色和弦块），而导出侧其实是对的 —— 表现为"勾了器乐却带出和弦"。
    """
    from app.abcp import notes_to_seconds, parse_abc, to_simple_notes
    from app.rebuild import (display_name, load_lyrics_words, load_measures,
                             load_note_lyrics, normalize_export_voices, voice_kind,
                             voice_sort_key)
    from app.lyrics import assign_lyrics
    from app.tempo import derive_tempo_map

    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    root = Path(task.out_dir)
    if not getattr(task, "audio", None):
        raise HTTPException(status_code=400, detail="任务缺少音频信息")

    # ---- 要哪些内容：显式 voices 优先，否则退回 only_melody 布尔 ----
    # 必须优先用显式的 voices：布尔只有两个值，而 ③ 的三个勾选是**三个独立开关**
    # （只勾器乐、只勾和弦…）。只用布尔就会把"只勾器乐"当成"全都要"，
    # 于是和弦被一并放出来 —— 实测的"勾了器乐却带出和弦"就是这个。
    if voices is not None:
        # 显式给了就**完全照办**（空字符串 = 什么都不勾，尊重用户）
        want = set(normalize_export_voices(str(voices).split(","))) if str(voices).strip() else set()
    else:
        want = set(normalize_export_voices(None, bool(only_melody)))

    # 选一个能满足 want 的谱面：缺声部时回落到模型原始谱，**绝不拿别的声部顶替**
    abc_file, score, fell_back = _score_for(root, task, want)
    if abc_file is None or score is None:
        return JSONResponse({"available": False, "notes": [], "reason": "尚无乐谱"})

    abc_text = abc_file.read_text(encoding="utf-8")
    measures = load_measures(root)
    notes_to_seconds(score.notes, bpm=score.bpm or 120.0, measures=measures)

    # 主旋律声部优先（歌词按它分配）
    all_voices = list(score.voices) or sorted({n.voice for n in score.notes})
    melody_voices = [v for v in all_voices if voice_kind(v) == "vocal"] or all_voices
    voice = melody_voices[0] if melody_voices else None
    melody_simple = to_simple_notes(score.notes, voice=voice) if voice else to_simple_notes(score.notes)

    # 按勾选挑声部（保持 ABC 里的声明顺序）
    selected: list[str] = [v for v in all_voices if voice_kind(v) in want]
    # 勾了内容但 ABC 侧一个声部都没匹配上（典型情况：**只勾了和弦**）时退回全部，
    # 免得页面空白 —— 和弦音符由下面的 chord_notes 单独供给。
    # want 为空（三个勾选全取消）时不退回：那时就该什么都不显示。
    if not selected and want:
        selected = list(all_voices)
    # 排成**规范顺序**（人声 → 器乐 → 和弦），与导出侧 pick_voices() 同一套排序：
    # 否则卷帘上的曲目顺序与导出的轨道顺序会不一致，而它们本该一一对应。
    selected.sort(key=voice_sort_key)

    simple: list[tuple[float, float, int]] = []
    for v in selected:
        simple.extend(to_simple_notes(score.notes, voice=v))
    if not simple:
        simple = melody_simple

    # 歌词只在主旋律上分配，其余声部给占位，避免器乐把歌词吃掉。
    words = load_lyrics_words(root)
    lyric_of: dict[tuple[float, float, int], str] = {}
    if words and melody_simple:
        mel_lyrics, _ = assign_lyrics(
            words, [(s, e) for s, e, _ in melody_simple], fallback="la"
        )
        lyric_of = {
            (round(s, 4), round(e, 4), p): text
            for (s, e, p), text in zip(melody_simple, mel_lyrics)
        }

    # 高亮：只在单声部时有意义（多声部时序号对不上）。前端卷帘已按音符自己的
    # [start,end) 判高亮，不再依赖这份；保留它是为了兼容旧前端。
    highlight: list[dict[str, float]] = []
    if len(selected) <= 1:
        hi_score = parse_abc(abc_text, merge_ties=False)
        notes_to_seconds(hi_score.notes, bpm=hi_score.bpm or 120.0, measures=measures)
        hi_simple = (
            to_simple_notes(hi_score.notes, voice=voice)
            if voice
            else to_simple_notes(hi_score.notes)
        )
        highlight = [
            {"start": round(s, 4), "end": round(e, 4), "pitch": p} for s, e, p in hi_simple
        ]

    # 和弦轨来自 chords.mid、**不是 ABC 声部**，所以单独判断：只在勾了「和弦」时给。
    chord_notes = _chord_track_notes(root) if "chords" in want else []

    # ---- 分轨结构：给**钢琴卷帘**用 ----
    # 上面那份扁平的 ``notes`` 是给试听/高亮用的，把各声部拼在一起、**丢掉了声部归属**，
    # 而卷帘必须按声部编辑（人声主旋律 / 器乐旋律各自一条），所以另给一份带声部的。
    manual_lyrics = load_note_lyrics(root)
    tracks: list[dict[str, Any]] = []
    for v in selected:
        sn = to_simple_notes(score.notes, voice=v)
        lines = list(manual_lyrics.get(v) or [])
        if len(lines) != len(sn):
            # 没有手动歌词（或长度对不上，说明那是上一版谱面的）→ 回落到词表分配结果
            lines = [lyric_of.get((round(s, 4), round(e, 4), p), "la") for s, e, p in sn]
        tracks.append(
            {
                "voice": v,
                "display": display_name(v),
                # 用 voice_kind 而不是硬比字符串：拆出来的 Vocal2 也要算人声
                "is_vocal": voice_kind(v) == "vocal",
                "notes": [
                    {"start": round(s, 4), "end": round(e, 4), "pitch": p, "lyric": ly}
                    for (s, e, p), ly in zip(sn, lines)
                ],
            }
        )

    # 速度表：卷帘的网格与 Ctrl+G 吸附必须按**blick**走，否则变速曲上网格会越走越偏
    # （§16/§23 的同一类坑）。这里直接把后端派生好的表交给前端，避免两套实现漂移。
    # 只留 t/bpm 两个字段：派生过程里的 seconds/quarters/measures 是中间量，别塞给前端。
    tempo_map = [
        {"t": float(x["t"]), "bpm": float(x["bpm"])}
        for x in (derive_tempo_map(measures) or [])
        if x.get("bpm")
    ] or [{"t": 0.0, "bpm": float(score.bpm or 120.0)}]

    # 「人声主旋律有没有填歌词」——导出前的居中确认框据此决定要不要拦。
    # 判据是**字面歌词里有没有非 "la" 的内容**，而不是"有没有 lyrics.json"：
    # 用户可以在卷帘上逐音删空，那时也该提醒。
    vocal_lines = [ly for t in tracks if t["is_vocal"] for ly in (n["lyric"] for n in t["notes"])]
    has_vocal_lyrics = any(str(ly).strip() and str(ly).strip() != "la" for ly in vocal_lines)

    return JSONResponse(
        {
            "available": True,
            "abc_file": abc_file.name,
            # 勾的声部在当前编辑稿里没有、于是回落到了模型原始谱 —— 前端据此提示用户
            # （"这次推理只导出了人声，器乐旋律取自模型原始谱"），不要静默。
            "fell_back_to_model": fell_back,
            "voice": voice,
            "voices": selected,
            # 兼容字段：只有"确实只要人声主旋律"时才是 true
            "only_melody": want == {"vocal"},
            "want": sorted(want),
            "bpm": score.bpm,
            "meter": score.header.get("meter"),
            "tempo_map": tempo_map,
            "tracks": tracks,
            "has_vocal_lyrics": has_vocal_lyrics,
            "duration": max(
                [e for _, e, _ in simple]
                + [n["end"] for n in chord_notes]
                + [0.0]
            ),
            "lyrics_source": "filled" if words else "fallback",
            "notes": [
                {
                    "start": round(s, 4),
                    "end": round(e, 4),
                    "pitch": p,
                    "lyrics": lyric_of.get((round(s, 4), round(e, 4), p), "la"),
                }
                for (s, e, p) in simple
            ],
            # 和弦音符（秒）。页面试听会把它叠到钢琴上，等价于整合包的 mix 渲染。
            "chord_notes": chord_notes,
            # 与页面上 .abcjs-note 元素按序号一一对应
            "highlight_notes": highlight,
        }
    )


def _task_export_voices(task: Any) -> list[str]:
    """任务的「导出哪些内容」意图（人声主旋律 / 器乐旋律 / 和弦）。

    优先用 ``params``（用户可在提交后通过保存 ABC / 导出来改主意），
    其次用 worker 写在 ``result`` 里的快照，最后回落到默认（只要人声主旋律）。
    """
    params = getattr(task, "params", None) or {}
    if "export_voices" in params:
        return normalize_export_voices(params["export_voices"])
    result = getattr(task, "result", None) or {}
    if "export_voices" in result:
        return normalize_export_voices(result["export_voices"])
    if "only_melody" in params:
        return normalize_export_voices(None, bool(params["only_melody"]))
    return normalize_export_voices(None, result.get("only_melody") if "only_melody" in result else None)


def _abc_candidates(root: Path, task: Any) -> list[Path]:
    """当前谱面的候选文件，**按优先级**排列。

    优先 ``export/<歌名>.abc``（最近一次生成导出的版本，也就是卷帘保存后的产物），
    依次回落到编辑稿、单声部稿、模型原始稿。
    """
    return [
        root / "export" / f"{task.audio.stem}.abc",
        root / "score.edited.abc",
        root / "score.melody.abc",
        root / "score.abc",
    ]


def _current_abc_file(root: Path, task: Any) -> Path | None:
    """当前谱面的 ABC 文件（最高优先级的那个存在的候选）。"""
    return next((p for p in _abc_candidates(root, task) if p.is_file()), None)


def _score_for(root: Path, task: Any, want: set[str]):
    """挑一个**能满足 want** 的谱面，返回 ``(路径, AbcScore, 是否发生了回落)``。

    为什么不能无脑用"当前谱面"：任务可能是用「只勾人声主旋律」跑的，那么
    ``export/<歌名>.abc`` 与 ``score.melody.abc`` **只有 Vocal**。用户后来勾上
    「器乐旋律」时，如果只看当前谱面，就会一个器乐声部都找不到 —— 而**绝不能**
    拿人声顶替（那会让用户以为拿到的是器乐）。正确做法是回落到模型原始的
    ``score.abc``，那里两条旋律都在。

    返回值第三个元素 ``fell_back`` 表示**没有用最高优先级的那个文件**
    （即真的回落了），调用方据此提示用户，而不是静默换数据。
    """
    from app.abcp import parse_abc
    from app.rebuild import voice_kind

    kinds_wanted = {k for k in want if k != "chords"}
    first = None
    for p in _abc_candidates(root, task):
        if not p.is_file():
            continue
        try:
            sc = parse_abc(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏文件不该让整个接口 500
            continue
        if first is None:
            first = (p, sc)
        have = {voice_kind(v) for v in (sc.voices or [])}
        if not kinds_wanted or kinds_wanted <= have:
            # 命中的文件不是最高优先级的那个 → 才算"回落过"
            return p, sc, (first[0] != p)
    if first is None:
        return None, None, False
    return first[0], first[1], True


def _has_vocal_lyrics(note_lyrics: Mapping[str, Sequence[str]], voices: Sequence[str]) -> bool:
    """人声主旋律有没有**非占位**的歌词。导出前的确认框据此决定要不要拦。"""
    from app.rebuild import voice_kind

    for v in voices:
        if voice_kind(v) != "vocal":
            continue
        for raw in note_lyrics.get(v) or []:
            t = str(raw or "").strip()
            if t and t != "la":
                return True
    return False


@app.post("/api/tasks/{task_id}/roll")
def save_roll(task_id: str, payload: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    """保存**钢琴卷帘**的编辑结果，并重新生成导出。

    卷帘是 ③ 唯一的编辑器（ABC 文本框与五线谱已删除），所以这里是"用户改了谱面"
    的唯一入口。前端把编辑后的音符（**单位秒**）连同逐音歌词一起发来，后端：

    1. :func:`app.abcp.seconds_to_score` 把秒换回记谱位置 —— 变速曲必须走小节课表，
       不能拿单一 BPM 硬除（§16/§23 的同一类坑）；
    2. 同一轨内有重叠就拆成 ``Vocal`` / ``Vocal2`` …：ABC 的一条 ``V:`` 天生单声部，
       塞不进重叠。拆法复用 :func:`app.rebuild.monophonic_groups`，与导出侧同一套逻辑；
    3. :func:`app.abcs.score_to_abc` 序列化成 ABC，写 ``score.edited.abc``；
    4. 逐音歌词存 ``lyrics_notes.json`` —— ``-`` / ``+`` 是记号，原样保留；
    5. :func:`app.rebuild.rebuild_exports` 重生成 SVP / MIDI / ABC。

    **写盘前会做一次 ABC 往返自检**：序列化出来的文本必须能解析回同一批音符，
    差一点点就整笔放弃并报错。宁可让用户重试，也不要把坏谱面写进产物。

    请求体::

        {"tracks": [{"voice": "Vocal", "notes": [{"start","end","pitch","lyric"}]}],
         "export_voices": ["vocal"], "bpm": 120}
    """
    from app.abcp import parse_abc, quantize_notation, seconds_to_score
    from app.abcs import header_from_abc, score_to_abc
    from app.rebuild import load_measures, monophonic_groups, normalize_export_voices, rebuild_exports, save_note_lyrics

    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if manager.busy():
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改谱面")

    payload = dict(payload or {})
    root = Path(task.out_dir)
    measures = load_measures(root)

    src_abc = _current_abc_file(root, task)
    if src_abc is None:
        raise HTTPException(status_code=400, detail="还没有可编辑的乐谱")
    # 表头（调号/拍号/标题…）沿用当前 ABC 的：编辑音符不该把这些弄丢。
    # 必须用 header_from_abc 而不是 parse_abc(...).header —— 后者丢 X:/T:，
    # 而且会把 `V: Vocal clef=treble` 里的 clef/name 当成音符（实测多出 35 个幽灵音符）。
    header = header_from_abc(src_abc.read_text(encoding="utf-8"))

    raw_tracks = payload.get("tracks")
    if not isinstance(raw_tracks, list):
        raise HTTPException(status_code=400, detail="tracks 必须是数组")

    bpm = float(payload.get("bpm") or (task.result or {}).get("bpm") or 120.0)
    warnings: list[str] = []
    voices: list[tuple[str, list[tuple[float, float, int]]]] = []
    note_lyrics: dict[str, list[str]] = {}
    total = 0

    for tr in raw_tracks:
        if not isinstance(tr, dict):
            continue
        name = str(tr.get("voice") or "").strip()
        notes = tr.get("notes") or []
        if not name or not isinstance(notes, list) or not notes:
            continue

        rows: list[tuple[float, float, int, str]] = []
        for n in notes:
            if not isinstance(n, dict):
                continue
            try:
                start, end = float(n["start"]), float(n["end"])
                pitch = int(round(float(n["pitch"])))
            except (KeyError, TypeError, ValueError):
                continue
            if end <= start or not (0 <= pitch <= 127):
                continue
            lyric = n.get("lyric")
            rows.append((start, end, pitch, str(lyric) if lyric is not None else "la"))
        if not rows:
            continue
        # 按时间（同刻按音高）排序；歌词必须跟着一起排，否则会串行错位
        rows.sort(key=lambda r: (r[0], r[2]))
        spans = [(r[0], r[1], r[2]) for r in rows]
        lyrics = [r[3] for r in rows]

        # 秒 → 记谱位置，再**吸附到记谱网格**：不吸附的话浮点噪声会让相邻音
        # 看起来重叠 1e-16，还会把序列化器需要的基本单位抬到 1/8192 而拒绝生成
        # （实测 22 个真实样本里 21 个会被拒）。见 app.abcp.quantize_notation。
        notated = quantize_notation(seconds_to_score(spans, bpm=bpm, measures=measures))
        groups = monophonic_groups([(o, o + d, p) for o, d, p in notated])
        if len(groups) > 1:
            warnings.append(
                f"{name} 同一轨内有音符重叠，已拆成 {len(groups)} 条（{name}1/{name}2…）"
                "—— Synthesizer V 一条轨只能有一个音同时响"
            )
        for gi, idxs in enumerate(groups):
            vname = name if len(groups) == 1 else f"{name}{gi + 1}"
            voices.append((vname, [notated[i] for i in idxs]))
            note_lyrics[vname] = [lyrics[i] for i in idxs]
        total += len(spans)

    if not voices or total == 0:
        raise HTTPException(status_code=400, detail="卷帘里没有任何可用音符，未保存")

    try:
        abc_text = score_to_abc(header, voices)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"谱面无法写入 ABC：{exc}") from exc

    # ---- 往返自检：写盘前必须证明"序列化 → 解析"是无损的 ----
    back = parse_abc(abc_text, merge_ties=False)
    back_notes = [
        (n.voice, round(n.onset, 9), round(n.duration, 9), n.pitch)
        for n in back.notes
        if n.pitch is not None
    ]
    want_notes = [
        (v, round(o, 9), round(d, 9), p) for v, ns in voices for o, d, p in ns
    ]
    ok = len(back_notes) == len(want_notes)
    worst = 0.0
    if ok:
        for a, b in zip(sorted(back_notes), sorted(want_notes)):
            if a[0] != b[0] or a[3] != b[3]:
                ok = False
                break
            worst = max(worst, abs(a[1] - b[1]), abs(a[2] - b[2]))
        if ok and worst > 1e-6:
            ok = False
    if not ok:
        log_msg = (
            f"ABC 往返自检失败：写 {len(want_notes)} 个音、读回 {len(back_notes)} 个，"
            f"最大误差 {worst:.3e}"
        )
        raise HTTPException(status_code=500, detail=f"{log_msg}；已放弃保存，原始谱面未改动")

    (root / "score.edited.abc").write_text(abc_text, encoding="utf-8")
    save_note_lyrics(root, note_lyrics)

    export_voices = normalize_export_voices(payload.get("export_voices"), payload.get("only_melody"))
    info = rebuild_exports(
        root,
        abc_text,
        bpm=bpm,
        export_voices=export_voices,
        project_name=task.audio.stem,
        note_lyrics=note_lyrics,
        use_stored_note_lyrics=False,   # 直接用刚收到的歌词，别去读盘上那份
    )

    task.params = {
        **(task.params or {}),
        "export_voices": export_voices,
        "only_melody": export_voices == ["vocal"],
    }
    task.result = {
        **(task.result or {}),
        "export": info,
        "export_voices": export_voices,
        "only_melody": export_voices == ["vocal"],
    }

    # ⚠️ **刻意不 broadcast_result**。
    #
    # 广播会经 SSE 走到前端的 applyTask() → onResult() → reloadRoll() → roll.load()
    # → fit()，于是**每次自动保存都把用户的横向缩放重置回"整首适宽"**
    # （用户实测反馈："每次都需要重新放大…改完一个音符后失焦，又回到 100% 了"），
    # 而且还会把正在编辑、尚未保存的改动冲掉。
    #
    # 这里不需要广播：**发起请求的那个客户端已经从响应体里拿到了 exports**，
    # 自己更新界面即可（`saveRoll()` 会更新 #exportNote 并刷新试听）。
    # 代价：另开一个标签页不会自动看到这次改动 —— 本工具是单机单人用，可接受。
    #
    # 另外把"缩放不该被重载重置"也补在了 `pianoroll.js::load()` 里（防御性：
    # `/lyrics`、切换勾选等其它路径也会 reload，那些地方同样不该丢缩放）。

    return JSONResponse(
        {
            "ok": bool(info.get("ok")),
            "error": info.get("error"),
            "exports": info.get("exports") or [],
            "warnings": warnings + list(info.get("warnings") or []),
            "voices": [v for v, _ in voices],
            "notes": total,
            "has_vocal_lyrics": _has_vocal_lyrics(note_lyrics, [v for v, _ in voices]),
            "abc": abc_text,
        }
    )


@app.post("/api/tasks/{task_id}/lyrics")
def fill_lyrics_endpoint(task_id: str, payload: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    """歌词填充：把用户提供的 **LRC 或纯文本**分配到音符，并重新生成导出。

    本接口**不做歌词识别**（Qwen3-ASR 已移除）。歌词来源是用户：

    * 拖入 ``.lrc`` 文件 → 有行级时间戳，按时间窗把字分配到音符（最准）；
    * 粘贴 LRC 文本 → 同上；
    * 粘贴纯文本 → 没有时间信息，按顺序**一字一音**铺开。

    结果存到 ``lyrics.json``（词是带时间戳的），导出时再对**当前音符**做分配，
    这样用户在乐谱编辑里改过 ABC 之后歌词也不会错位。
    """
    from app.lyrics import LyricWord, build_lrc, fill_lyrics, SECTION_LABELS
    from app.rebuild import LYRICS_FILENAME, NOTE_LYRICS_FILENAME, rebuild_exports

    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if manager.busy():
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改歌词")

    payload = dict(payload or {})
    text = str(payload.get("text") or "")
    fallback = str(payload.get("fallback") or "la")
    continuation = str(payload.get("continuation") or "auto")
    # 分词模式：不勾「按空格切分」= auto（中文逐字、英文按词）；
    # 勾上 = space（"wo ai ni" → wo/ai/ni）。char 是强制逐字符，界面暂不暴露。
    split = str(payload.get("split") or "auto")
    root = Path(task.out_dir)

    # 清空歌词：删掉 lyrics.json / lyrics.lrc，并把导出重建回统一占位
    if payload.get("clear"):
        (root / LYRICS_FILENAME).unlink(missing_ok=True)
        (root / NOTE_LYRICS_FILENAME).unlink(missing_ok=True)
        (root / "lyrics.lrc").unlink(missing_ok=True)
        abc_clear = root / "score.melody.abc"
        if not abc_clear.is_file():
            abc_clear = root / "score.abc"
        info: dict[str, Any] = {"ok": False, "error": "未找到 ABC"}
        if abc_clear.is_file():
            info = rebuild_exports(
                root,
                abc_clear.read_text(encoding="utf-8"),
                bpm=(task.result or {}).get("bpm"),
                export_voices=_task_export_voices(task),
                lyrics=fallback,
                use_stored_lyrics=False,
                project_name=task.audio.stem,
            )
        task.result = {**(task.result or {}), "export": info, "lyrics_filled": False}
        manager.broadcast_result(task)
        return JSONResponse({"ok": True, "cleared": True, "export": info, "lrc": ""})

    if not text.strip():
        raise HTTPException(status_code=400, detail="歌词内容为空")

    # 取当前乐谱的音符（含用户编辑过的版本）
    notes_data = json.loads(bytes(get_notes(task_id).body).decode("utf-8"))
    if not notes_data.get("available") or not notes_data.get("notes"):
        raise HTTPException(status_code=400, detail="还没有可用的乐谱，无法填充歌词")
    spans = [(n["start"], n["end"]) for n in notes_data["notes"]]

    out = fill_lyrics(
        text,
        spans,
        fmt=str(payload.get("format") or "auto"),
        fallback=fallback,
        continuation=continuation,
        split=split,
    )
    # 批量导入是"重新铺一遍"，必须把之前卷帘上手动编辑的逐音歌词清掉 ——
    # 否则 rebuild_exports 会优先用手动那份，表现为"导入了但没生效"。
    (root / NOTE_LYRICS_FILENAME).unlink(missing_ok=True)

    words = [LyricWord(w["text"], w["start"], w["end"], line=int(w.get("line", -1))) for w in out["words"]]
    joined = "".join(w.text for w in words)
    record = {
        "source": "user",
        "format": out["format"],
        "text": joined,
        "words_text": joined,
        "words": [w.as_dict() for w in words],
        "items": [[w.text, round(w.start, 3), round(w.end, 3)] for w in words],
        "fallback": fallback,
        "continuation": continuation,
        "split": out.get("split", split),
    }
    (root / LYRICS_FILENAME).write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # 顺带导出 LRC（按原曲段落插标签）
    sections: list[dict[str, Any]] = []
    summary_file = root / "summary.json"
    if summary_file.is_file():
        try:
            sections = json.loads(summary_file.read_text(encoding="utf-8")).get("sections") or []
        except (OSError, ValueError):
            sections = []
    lrc = build_lrc(words, sections=sections, section_labels=SECTION_LABELS)
    if lrc:
        (root / "lyrics.lrc").write_text(lrc, encoding="utf-8")

    # 重新生成导出，让 SVP 立刻带上新歌词
    abc_file = root / "score.melody.abc"
    if not abc_file.is_file():
        abc_file = root / "score.abc"
    export_info: dict[str, Any] = {"ok": False, "error": "未找到 ABC"}
    if abc_file.is_file():
        export_info = rebuild_exports(
            root,
            abc_file.read_text(encoding="utf-8"),
            bpm=(task.result or {}).get("bpm"),
            export_voices=_task_export_voices(task),
            lyrics=fallback,
            lyrics_words=words,
            continuation=continuation,
            project_name=task.audio.stem,
        )

    task.result = {**(task.result or {}), "export": export_info, "lyrics_filled": True}
    manager.broadcast_result(task)

    return JSONResponse(
        {
            "ok": out["ok"],
            "format": out["format"],
            "lyrics_text": joined,
            "lyrics": out["lyrics"],
            "stats": out["stats"],
            "warnings": out["warnings"],
            "export": export_info,
            "lrc": lrc,
        }
    )


@app.get("/api/tasks/{task_id}/lyrics")
def get_lyrics(task_id: str) -> JSONResponse:
    """读取当前已填充的歌词。"""
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    path = Path(task.out_dir) / "lyrics.json"
    if not path.is_file():
        return JSONResponse({"available": False})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"歌词文件读取失败：{exc}") from exc
    return JSONResponse({"available": True, **data})


@app.get("/api/tasks/{task_id}/lrc")
def download_lrc(task_id: str) -> FileResponse:
    """下载已填充歌词导出的 LRC。"""
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    target = Path(task.out_dir) / "lyrics.lrc"
    if not target.is_file():
        raise HTTPException(status_code=404, detail="尚未生成 LRC")
    return _file_response(target, filename=target.name)
# --------------------------------------------------------------------------
# 环境 / 显存 / 设置
# --------------------------------------------------------------------------
def _is_our_server(port: int) -> bool:
    """探测该端口上是不是本服务（防止有人绕过 main() 直接跑 uvicorn）。"""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{config.HOST}:{port}/api/health", timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def _own_port() -> int | None:
    if _self_port is not None:
        return _self_port
    port = int(config.load_settings().get("port") or config.DEFAULT_PORT)
    return port if _is_our_server(port) else None


def _env(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force and _env_cache["data"] and now - _env_cache["at"] < ENV_CACHE_SECONDS:
        return _env_cache["data"]
    data = envcheck.run_checks(own_port=_own_port())
    _env_cache.update({"at": now, "data": data})
    return data


@app.get("/api/env")
def get_env(full: bool = True) -> JSONResponse:
    if full:
        return JSONResponse(_env(force=False))
    return JSONResponse({"gpu": envcheck.gpu_snapshot()})


@app.get("/api/gpu")
def get_gpu() -> JSONResponse:
    """廉价显存快照，供显存条高频轮询（组件 C21）。"""
    return JSONResponse(envcheck.gpu_snapshot())


@app.get("/api/settings")
def get_settings() -> JSONResponse:
    return JSONResponse(config.load_settings())


@app.post("/api/settings")
def update_settings(patch: dict[str, Any] = Body(...)) -> JSONResponse:
    return JSONResponse(config.save_settings(patch))


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "busy": manager.busy()})


@app.get("/api/cache")
def cache_info() -> JSONResponse:
    """当前 ``output/`` 与 ``tmp/`` 里各有多少东西（供「清理缓存」按钮先算账）。"""
    items: dict[str, Any] = {}
    total = 0
    for name, path in _CACHE_TARGETS.items():
        st = _tree_stat(path)
        items[name] = {**st, "path": str(path)}
        total += st["bytes"]
    return JSONResponse({"ok": True, "busy": manager.busy(), "total_bytes": total, "items": items})


@app.post("/api/cache/clear")
def cache_clear(payload: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    """清空 ``output/`` 与 ``tmp/`` 的内容（**永久删除，不进回收站**）。

    * 任务正在跑时**拒绝**（worker 还在往这两个目录里写，边写边删只会把任务搞坏）；
    * 只认白名单里的名字，前端发不了任意路径；
    * 清 ``tmp/`` 时会一并清掉内存里的「上传登记表」—— 不然前端还能拿着一个
      已经不存在文件的 token 去提交任务。
    """
    if manager.busy():
        raise HTTPException(status_code=409,
                            detail="正在扒谱，先停止或等它跑完，再清理缓存")

    targets = (payload or {}).get("targets") or ["output", "tmp"]
    unknown = [t for t in targets if t not in _CACHE_TARGETS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"不认识的清理目标：{unknown}")

    current = manager.current
    current_dir = Path(current.out_dir) if current is not None else None

    items: dict[str, Any] = {}
    freed = 0
    for name in targets:
        st = _clear_dir_contents(_CACHE_TARGETS[name])
        items[name] = st
        freed += st["bytes"]

    if "tmp" in targets:
        _uploads.clear()

    # 当前任务的产物是不是正好被删掉了？是的话前端要收起导出按钮，别让它点出 404。
    current_task_cleared = False
    if current_dir is not None:
        for name in targets:
            try:
                current_dir.relative_to(_CACHE_TARGETS[name])
                current_task_cleared = True
            except ValueError:
                pass

    return JSONResponse({
        "ok": True,
        "targets": targets,
        "items": items,
        "freed_bytes": freed,
        "current_task_cleared": current_task_cleared,
    })


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="SheetSage2 最小化扒谱 Web 工具")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    config.ensure_dirs()
    removed = _cleanup_old_uploads()
    if removed:
        print(f"[启动] 已清理 {removed} 个过期上传临时文件")
    settings = config.load_settings()
    port = args.port or int(settings.get("port") or config.DEFAULT_PORT)

    global _self_port
    _self_port = port

    url = f"http://{args.host}:{port}/"

    # 端口被占用时给出明确提示，而不是抛栈（文档 5.1）
    if envcheck.port_in_use(port, args.host):
        # 如果是**本工具自己的另一个实例**占着（最常见的原因：双击了两次
        # start.bat），那就直接把浏览器指过去，而不是报错退出——报错只会让人困惑。
        if _is_our_server(port):
            print("=" * 62)
            print(f"  SheetSage2 扒谱服务已经在运行：{url}")
            print("  已为你打开浏览器；若要重启，请先关闭原来那个窗口。")
            print("=" * 62)
            if not args.no_browser and settings.get("open_browser", True):
                webbrowser.open(url)
            return 0
        print(f"[错误] 端口 {port} 已被**其它程序**占用。")
        print("       请在 settings.json 里改 port，或关掉占用该端口的程序。")
        print(f"       排查命令：netstat -ano | findstr :{port}")
        return 2

    print("=" * 62)
    print("  SheetSage2 扒谱（整合包）")
    print(f"  地址：{url}")
    print("  仅监听本机回环地址，不对外暴露。按 Ctrl+C 退出。")
    # 这几行是给测试者看的：出问题时把这块连同下面的报错一起发出来，就能定位环境问题。
    print("-" * 62)
    print(f"  解释器  ：{sys.executable}")
    print(f"  ffmpeg  ：{config.ffmpeg_path() or '未找到（提交任务会失败）'}")
    default_device = str(settings.get("default_device") or "auto")
    snap = envcheck.gpu_snapshot()
    if default_device == "cpu":
        print("  推理设备：CPU（settings.json 里指定了 cpu）")
    elif snap.get("available"):
        print(f"  推理设备：GPU（{snap['name']}，当前空闲 {snap['free_mib']:.0f} MiB）")
    else:
        print("  推理设备：CPU（没检测到可用的 NVIDIA 显卡）")
        print("            速度会慢很多 —— 5 分钟的歌大概要十几到几十分钟，跑起来别急。")
    print("=" * 62)

    if not args.no_browser and settings.get("open_browser", True):
        import threading

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=args.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
