"""路由包：按职责分组，应用工厂把它们挂到 `/api` 下。

| 模块 | 管什么 |
|---|---|
| `jobs` | 环境信息、工程增删改、阶段（重）跑、参数、音源、回收站、SSE |
| `media` | 卷帘数据、歌词、波形、原音频 / 干声（Range） |
| `exports` | 单个产物、整包 zip、SVP 工程 |
| `system` | 诊断包、整库备份与恢复 |

所有 router 都靠 `ApiDeps` 拿依赖（见 `_shared`），**导入时不建线程、不加载模型**；
挂载顺序由 `create_app` 决定：API 在前，`/api/*` 的兜底 404 与静态资源在最后。
"""

from __future__ import annotations

from fastapi import APIRouter

from ._shared import ApiDeps
from . import exports, jobs, media, system

#: 挂载顺序即注册顺序（`/api/jobs/{id}/files/{path}` 这种带 path 转换的路由要排在后面）。
BUILDERS = (jobs.build_router, media.build_router, exports.build_router, system.build_router)


def build_routers(deps: ApiDeps) -> list[APIRouter]:
    """按固定顺序造好四组路由。"""
    return [build(deps) for build in BUILDERS]


__all__ = ["ApiDeps", "build_routers", "exports", "jobs", "media", "system"]
