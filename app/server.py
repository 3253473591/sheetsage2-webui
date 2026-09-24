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
from typing import Any

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


@app.get("/pianoroll.js")
def pianoroll_js() -> FileResponse:
    """钢琴卷帘组件（独立文件，无构建步骤）。

    单独一条路由而不是把整个 ``web/`` 挂成 StaticFiles：后者会和 ``/``（index.html）
    以及 ``/vendor`` 的现有映射打架，而这里只多一个文件，没必要动那两处。
    """
    path = config.WEB_DIR / "pianoroll.js"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="pianoroll.js 不存在")
    return FileResponse(path, media_type="text/javascript; charset=utf-8")

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
# 钢琴卷帘：保存卷帘的音符表并重建 SVP / MIDI
# --------------------------------------------------------------------------
#: 可下载的导出产物。``abc`` 已随 ③ 的 ABC 编辑器一起删除：卷帘的真相是 roll.json，
#: 不再产出 ABC 文件（ABC 只是模型输出、内部导入用的中间格式）。
_EXPORT_KINDS = {"svp": ".svp", "midi": ".mid"}


def _roll_source_abc(task: Any) -> Path | None:
    """卷帘的**导入来源** ABC。

    ⚠️ 顺序是**先全量、后过滤**，这一点踩过坑：

    * ``score.abc`` —— 模型的全量输出（Vocal + Ins + …）。
    * ``score.melody.abc`` —— 推理时按**当时勾选**过滤过的版本。默认只勾人声主旋律，
      所以这个文件里通常**只有 Vocal**；``export/<歌名>.abc`` 同理（老版本还会被手改）。
    * ``export/<歌名>.abc`` —— 老版本「乐谱编辑」保存的版本，兜底。

    先读 ``score.melody.abc`` 会导致**器乐声部凭空消失**：模型明明给了 ``V.Ins``，
    卷帘里却永远只有 1 条轨，把「人声主旋律」取消勾选就变成"所选内容没有可用音符"。
    声部过滤现在是**卷帘自己的事**（那三个勾选），导入来源必须是全量的。
    """
    root = Path(task.out_dir)
    for p in (
        root / "score.abc",
        root / "score.melody.abc",
        root / "export" / f"{task.audio.stem}.abc",
    ):
        if p.is_file():
            return p
    return None


def _tracks_from_score(score: Any, root: Path) -> list[dict[str, Any]]:
    """把一份已换算到秒的 ``AbcScore`` 拆成卷帘轨道（每个声部一条）。"""
    from app.abcp import to_simple_notes
    from app.rebuild import voice_display, voice_kind

    tracks: list[dict[str, Any]] = []
    for voice in score.voices:
        simple = to_simple_notes(score.notes, voice=voice)
        if not simple:
            continue
        kind = voice_kind(voice) or "ins"
        tracks.append({
            "voice": voice,
            "kind": kind,
            "display": voice_display(voice),
            "is_vocal": kind == "vocal",
            "editable": True,
            "notes": [
                {"start": round(s, 4), "end": round(e, 4), "pitch": int(p), "lyric": ""}
                for s, e, p in simple
            ],
        })
    return tracks


def _roll_all_tracks(task: Any) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    """取出**全部**声部的卷帘轨道（不受三个勾选影响）。

    返回 ``(tracks, source_abc_name, header)``。

    为什么不按勾选裁剪：勾选只决定"看哪些/导哪些"。若这里就裁掉，用户
    「取消勾选器乐 → 保存 → 再勾上」就会把器乐轨的音符丢掉（``save_roll`` 的合并
    逻辑能兜住这一点，但源头就别少给更省事）。
    勾选在 ``get_roll`` 返回前、``save_roll`` 重建导出前各自过滤一次。

    **存档自愈**：``roll.json`` 优先，但全量乐谱里有、存档里没有的声部会被补回来。
    卷帘没有"删除整个声部"的功能，缺声部只可能来自旧版本读**过滤版**乐谱的那个 bug
    （见 :func:`_roll_source_abc`）——不补的话，用户在那个版本下保存过一次就永远只有人声。
    """
    from app.abcp import notes_to_seconds, parse_abc
    from app.rebuild import load_measures, load_roll

    root = Path(task.out_dir)

    # 先读全量乐谱（既用于"没有存档"时的导入，也用于给存档补声部）
    abc_tracks: list[dict[str, Any]] = []
    abc_header: dict[str, Any] = {}
    abc_source: str | None = None
    abc_file = _roll_source_abc(task)
    if abc_file is not None:
        try:
            text = abc_file.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text:
            score = parse_abc(text)
            notes_to_seconds(score.notes, bpm=score.bpm or 120.0, measures=load_measures(root))
            abc_tracks = _tracks_from_score(score, root)
            abc_header = dict(score.header)
            abc_source = abc_file.name

    stored = load_roll(root)
    if not (stored and stored.get("tracks")):
        return abc_tracks, abc_source, abc_header

    header = dict(stored.get("header") or {})
    # 存档里的 meter_tuple 经 JSON 变成了数组，还原成元组给 AbcScore 用
    if isinstance(header.get("meter_tuple"), list):
        header["meter_tuple"] = tuple(header["meter_tuple"])
    if not header:
        header = abc_header

    stored_tracks: list[dict[str, Any]] = []
    for t in stored["tracks"]:
        if not isinstance(t, dict):
            continue
        t = dict(t)
        t.setdefault("editable", True)
        t.setdefault("is_vocal", str(t.get("kind")) == "vocal")
        stored_tracks.append(t)

    have = {str(t.get("voice")) for t in stored_tracks}
    restored = [t for t in abc_tracks if str(t.get("voice")) not in have]
    order = {str(t.get("voice")): i for i, t in enumerate(abc_tracks)}
    merged = sorted(stored_tracks + restored,
                    key=lambda t: order.get(str(t.get("voice")), len(order)))
    return merged, (stored.get("source_abc") or abc_source), header


@app.get("/api/tasks/{task_id}/roll")
def get_roll(
    task_id: str,
    export_voices: str | None = None,
    only_melody: int | None = None,
) -> JSONResponse:
    """返回**钢琴卷帘**所需的可编辑谱面（③ 的唯一数据源）。

    返回结构::

        {
          "tracks": [ {voice, kind, display, is_vocal, editable, notes: [...]}, ... ],
          "measures": [...], "tempo_map": [...],
          "bpm" / "meter" / "duration" / "export_voices", "warnings": [...]
        }

    几处刻意的设计：
    * ``measures`` 必须给前端 —— 网格线与小节号要走模型真实的小节划分（变速曲里
      一小节的秒数不是常数），而且音符本来就是按这张表换算成秒的，前端用同一张表
      做网格与吸附才和音符**同源**。``tempo_map`` 一并给出以便诊断。
    * 和弦轨 ``editable=False`` —— 它来自 ``chords.mid``，**不是 ABC 声部**。允许编辑就
      必须把它回写成新的声部，会改变导出语义（模型侧它是 ``chord.lab`` 区间推出来的）。
    * 空歌词一律给 ``""``：卷帘上没有词就是没有，导出时落回占位词 ``la``。
      旧的 ④ 歌词填充已按需求删除，歌词的唯一来源是卷帘上的逐音编辑与 Ctrl+L 填词。
    """
    from app.rebuild import (
        CHORD_TRACK_DISPLAY,
        load_chord_notes,
        load_measures,
        select_roll_tracks,
    )
    from app.tempo import derive_tempo_map

    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 勾选优先用请求显式带来的（页面刚切勾选、尚未落盘），否则用任务的既有意图。
    if export_voices is not None or only_melody is not None:
        sel = normalize_export_voices(export_voices, None if export_voices is not None else bool(only_melody))
    else:
        sel = _task_export_voices(task)

    all_tracks, source_abc, header = _roll_all_tracks(task)
    root = Path(task.out_dir)
    measures = load_measures(root)

    warnings: list[str] = []
    tracks: list[dict[str, Any]] = []
    for tr in select_roll_tracks(all_tracks, sel):
        tracks.append({
            "id": len(tracks),
            "voice": tr.get("voice"),
            "kind": tr.get("kind") or "ins",
            "display": tr.get("display") or tr.get("voice"),
            "is_vocal": bool(tr.get("is_vocal")),
            "editable": True,
            "notes": [
                {
                    "start": round(float(n["start"]), 4),
                    "end": round(float(n["end"]), 4),
                    "pitch": int(n["pitch"]),
                    "lyric": "" if n.get("lyric") is None else str(n.get("lyric")),
                }
                for n in (tr.get("notes") or [])
            ],
        })

    chord_notes = load_chord_notes(root) if "chords" in sel else []
    if chord_notes:
        tracks.append({
            "id": len(tracks),
            "voice": "chords",
            "kind": "chords",
            "display": CHORD_TRACK_DISPLAY,
            "is_vocal": False,
            # 只读：见 docstring —— 和弦轨不是 ABC 声部，改了无法无损回写。
            "editable": False,
            "notes": [
                {
                    "start": round(float(n["start"]), 4),
                    "end": round(float(n["end"]), 4),
                    "pitch": int(n["pitch"]),
                    "lyric": "",
                }
                for n in chord_notes
            ],
        })

    # 下面这些「为什么少了一条轨」的说明必须在**提前返回之前**算出来。
    # 否则最需要解释的那次（所选内容一个音符都没有 → available=false）恰好不带原因，
    # 页面只能显示一句干巴巴的「所选内容没有可用音符」。
    in_score = sorted({str(t.get("voice")) for t in all_tracks if t.get("voice")})
    missing_abc = [t for t in sel if t != "chords" and not any(x["kind"] == t for x in tracks)]
    if missing_abc:
        warnings.append("所选声部 " + "/".join(missing_abc) + " 在该片段内没有音符，未生成对应轨道")
    if "chords" in sel and not any(x["kind"] == "chords" for x in tracks):
        warnings.append("未找到和弦轨（chords.mid / transcription.mid 里没有和弦轨），已跳过")
    if source_abc is None and not all_tracks:
        warnings.append("没有找到乐谱文件（score.abc / score.melody.abc），请先完成扒谱")
    # ④ 删掉之后 lyrics.json 不再被消费；文件还在时明确告知，别让用户以为词还在。
    if (root / "lyrics.json").is_file():
        warnings.append("检测到旧的 lyrics.json（④ 歌词填充的产物）已不再参与导出，请在卷帘上用 Ctrl+L 重新填词")

    if not tracks:
        # 把"这首歌里到底有哪些声部"直接写进原因里：用户看到"没有可用音符"时
        # 最需要知道的就是"那我该勾哪个"。实测踩过：过滤后的乐谱里没有 Ins，
        # 用户取消勾选人声后只得到一句无从下手的提示。
        if in_score:
            reason = ("所选内容没有可用音符：这首歌的乐谱里只有声部 " + "、".join(in_score)
                      + "。请勾选对应的「导出内容」")
        else:
            reason = "所选内容没有可用音符：还没有找到乐谱文件，请先完成扒谱"
        return JSONResponse({
            "available": False,
            "tracks": [],
            "reason": reason,
            "voices_in_score": in_score,
            "export_voices": sel,
            "warnings": warnings,
        })

    tempo_map = derive_tempo_map(measures) or [{"t": 0.0, "bpm": float(header.get("q") or 120.0)}]
    duration = max(
        [float(n["end"]) for tr in tracks for n in tr["notes"]] + [0.0]
    )
    meter_tuple = header.get("meter_tuple") or (4, 4)

    return JSONResponse({
        "available": True,
        "source_abc": source_abc,
        "voices_in_score": in_score,
        "bpm": header.get("q"),
        "meter": header.get("meter") or "4/4",
        "beats_per_bar": int(meter_tuple[0]),
        "beat_unit": int(meter_tuple[1]),
        "unit_whole": header.get("unit_whole"),
        "duration": round(duration, 4),
        "tempo_map": tempo_map,
        "measures": measures,
        "export_voices": sel,
        "tracks": tracks,
        "warnings": warnings,
    })


@app.post("/api/tasks/{task_id}/roll")
def save_roll(task_id: str, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """保存卷帘的音符表，并据此重建 ``export/`` 下的 SVP 与 MIDI。

    流程：
      1. 校验音符（**不信前端**：坏音符会一路走到 SVP/MIDI 写出，报错却对不上源头）；
      2. 与**当前全量轨**合并后落盘 ``roll.json``（按声部名，见
         :func:`merge_roll_tracks`）—— 合并起点必须是"全量"而不是空档：
         卷帘只上传被勾选的声部，从空档起步会在**首次保存**时把没勾的声部整条丢掉；
      3. 取**被勾选的**声部 + 和弦轨，造一份"时间已是秒"的乐谱，
         交给同一条导出管线（和弦轨、复音拆轨、八度、速度表全不变）。
    """
    from app.rebuild import (
        merge_roll_tracks,
        rebuild_exports,
        sanitize_roll_tracks,
        save_roll as _save_roll_file,
        score_from_roll,
        select_roll_tracks,
    )

    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if manager.busy():
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改乐谱")

    export_voices = normalize_export_voices(payload.get("export_voices"), payload.get("only_melody"))
    root = Path(task.out_dir)

    try:
        incoming = sanitize_roll_tracks(payload.get("tracks"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"卷帘数据有问题：{e}")

    # 表头（拍号/调号/默认音符长度）沿用导入来源的 ABC：
    # 卷帘改的是音高与时值，不该顺手把调号拍号也改掉。
    existing, source_abc, header = _roll_all_tracks(task)
    bpm = payload.get("bpm") or header.get("q") or (task.result or {}).get("bpm") or 120.0

    merged = merge_roll_tracks(existing, incoming)
    _save_roll_file(root, merged, source_abc=source_abc, bpm=float(bpm), header=header)

    selected = select_roll_tracks(merged, export_voices)
    score = score_from_roll(selected, bpm=float(bpm), header=header)
    result = rebuild_exports(
        root,
        "",                       # 卷帘路径没有 ABC 文本
        score=score,
        bpm=float(bpm),
        export_voices=export_voices,
        project_name=task.audio.stem,
        # ④ 已删除：不再读 lyrics.json，歌词一律取自卷帘的逐音歌词
        use_stored_lyrics=False,
    )
    if not result.get("ok"):
        return JSONResponse(status_code=422, content=result)

    # 记回任务，便于刷新后仍能拿到「导出过哪些声部」；歌词来源固定是逐音
    task.params = {**(task.params or {}), "export_voices": export_voices,
                   "only_melody": export_voices == ["vocal"]}
    task.result = {
        **(task.result or {}),
        "export": result,
        "roll_edited": True,
        "export_voices": export_voices,
        "only_melody": export_voices == ["vocal"],
    }
    return JSONResponse(result)


@app.post("/api/tasks/{task_id}/harmony")
def make_harmony(task_id: str, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """由主人声轨算出一条**平行和声**轨（按调性）。

    为什么换算放在后端而不是前端：**音程只有放在调性里才有意义** —— C 上方的三度是 E，
    D 上方的三度是 F，半音数并不相同（固定移半音会立刻出调）。而调性来自乐谱表头。
    所以音乐理论只留 `app/harmony.py` 一份实现，前端只负责把算好的音符插进卷帘。

    音符由前端发上来（可能还没保存），这里**只算不落盘**：生成的轨要不要留下由用户在
    卷帘上确认，之后走既有的「保存并重新生成导出」那条链路。
    """
    from app.abcp import scale_pitch_classes
    from app.harmony import MAX_DEGREE, build_harmony_notes, chromatic_semitones
    from app.overview import key_to_chinese
    from app.rebuild import harmony_display, harmony_voice_name

    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    try:
        degree = int(payload.get("degree"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="和声度数必须是整数")
    if not 1 <= abs(degree) <= MAX_DEGREE:
        raise HTTPException(status_code=400, detail=f"和声度数必须在 ±1..±{MAX_DEGREE} 度之间")

    notes = payload.get("notes") or []
    if not notes:
        raise HTTPException(status_code=400, detail="没有可作和声来源的音符")

    _tracks, _source, header = _roll_all_tracks(task)
    key = str(header.get("key") or "")
    scale = scale_pitch_classes(key)
    try:
        voice = harmony_voice_name(str(payload.get("voice") or "Vocal"), degree)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    out, info = build_harmony_notes(
        notes, scale=scale, degree=degree, semitones=chromatic_semitones(degree)
    )
    if not out:
        raise HTTPException(status_code=400, detail="换算后没有可用音符（时值非法）")
    return JSONResponse({
        "ok": True,
        "voice": voice,
        "display": harmony_display(degree),
        "kind": "vocal",
        "is_vocal": True,
        "notes": out,
        "key": key,
        "key_cn": key_to_chinese(key) if key else "",
        **info,
    })


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

    为什么不复用 ``task.result``：那份记账会在事后被卷帘保存（``POST /roll``）改写，
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
