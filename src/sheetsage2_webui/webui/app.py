"""FastAPI 应用工厂：安全中间件 + 生命周期 + 路由注册 + 前端静态资源。

路由按职责拆在 `routes/`（工程 / 媒体 / 下载导出 / 系统），这里只负责：

1. 造依赖（`JobManager`、`RunnerSession`、`ApiDeps`）并挂路由；
2. 安全中间件（G13：非回环监听要 token，回环监听要同源）；
3. 生命周期（关停时 `manager.shutdown()` + `session.release()`）；
4. 前端产物（`web/` 构建到 `static/`）+ `/api/*` 的兜底 404。

路由一览（全部挂在 `/api` 下，其余路径归前端 SPA）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/env` | 服务端信息（两个模型 / 设备 / **参数取值表** / 是否锁定 / 队列） |
| GET | `/api/jobs` | 工程列表（新的在前） |
| POST | `/api/jobs` | 上传音频并建工程（multipart，字段 `file`，可选 `plan`）；**默认不排任何阶段** |
| GET | `/api/jobs/{id}` | 工程快照（两个阶段的状态 + 参数 + 产物清单 + 摘要） |
| PATCH | `/api/jobs/{id}/options` | 改参数面板上的参数（范围见 `job_options.PARAM_SPECS`） |
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

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..bootstrap import PROJECT_ROOT
from .job_options import asr_model_label, default_options
from .job_trash import TRASH_MAX_BYTES, TRASH_MAX_DAYS, TRASH_PURGE_INTERVAL
from .jobs import JobManager
from .runner import RunnerSession
from .security import (TOKEN_COOKIE, extract_token, generate_pair_token,
                       host_is_loopback, origin_is_loopback, token_matches)
from .routes import ApiDeps, build_routers

#: 前端构建产物（`web/vite.config.ts` 的 `build.outDir` 指向这里）。
STATIC_DIR = Path(__file__).resolve().parent / "static"
#: 工程数据根 = **项目里的 `output/`**：一个子目录就是一个工程，和命令行跑出来的一致。
#: 这样重启不会丢东西，命令行跑过的歌也能在侧栏里看到（只读）。
DEFAULT_DATA_ROOT = PROJECT_ROOT / "output"

#: 导入备份包的大小上限（MB）：备份要装得下几十首歌的产物与原音频。
MAX_BACKUP_MB = 8192.0

BUILD_HINT = (
    "SheetSage2 Studio 服务已启动，但还没有前端产物。\n\n"
    "在 web/ 目录里执行：\n    npm install\n    npm run build\n\n"
    "然后刷新本页。开发时也可以用 `npm run dev`（Vite 会把 /api 代理到本服务）。\n"
)


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
    meta_watch: bool = False,
    meta_watch_dirs: Iterable[str | Path] = (),
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
                             probe_upload=probe_upload,
                             meta_watch=meta_watch,
                             meta_watch_dirs=meta_watch_dirs)
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

    # ------------------------------------------------------------------ 路由
    deps = ApiDeps(manager=manager, session=session, max_backup_mb=MAX_BACKUP_MB,
                   static_ready=static_ready)
    for router in build_routers(deps):
        app.include_router(router, prefix="/api")

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
