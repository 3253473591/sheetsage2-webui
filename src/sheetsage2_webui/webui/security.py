"""CSRF / Origin / Token（G13）：挡住"打开任意网页就能往本服务发请求"的本地裸奔。

**威胁**：服务默认 `127.0.0.1`、无鉴权、无 Origin 校验。用户访问的**任意网页**都能向
`http://127.0.0.1:8000` 发简单请求（`POST /cancel`、`POST /stages/...` 不需要预检），
于是"打开网页 → 后台悄悄开始扒谱 / 取消任务 / 删工程"；DNS rebinding 还能绕过同源限制
读到 `/api/jobs` 的响应。

**两层防线**（都做成开关，默认关，产品入口 `__main__.py` 打开）：

| 开关 | 干什么 | 谁开它 |
|---|---|---|
| `loopback_guard` | 非 GET 校验 `Origin`/`Referer` 必须是回环；`/api/*` 校验 `Host` 头（防 DNS rebinding） | 回环监听时 |
| `require_token` | 所有 `/api/*` 必须带 `pair_token`（头/查询/cookie） | `--host 0.0.0.0` 时强制 |

`pair_token` 对本地用户**无感**：服务端把它写进 HttpOnly cookie，浏览器自动带；非回环时
启动日志里再给一个 `/?token=...` 的地址。
"""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

#: 携带 token 的请求头 / cookie 名。
TOKEN_HEADER = "x-pair-token"
TOKEN_COOKIE = "sheetsage_token"

#: 回环主机名（`Host` 头去端口之后要比对这些）。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def generate_pair_token() -> str:
    """一次性配对 token：16 字节随机（32 个 hex）。每次启动都换。"""
    return secrets.token_hex(16)


def host_is_loopback(host_header: str) -> bool:
    """`Host: 127.0.0.1:8000` 这种头去掉端口后是不是回环。"""
    host = (host_header or "").strip().lower()
    # 去掉端口（IPv6 带方括号：`[::1]:8000`）
    if host.startswith("["):
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.split(":", 1)[0]
    host = host.strip("[]")
    return host in LOOPBACK_HOSTS


def origin_is_loopback(origin: str) -> bool:
    """`Origin: http://evil.com` 这种头的主机部分是不是回环。"""
    if not origin or origin.lower() == "null":
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return host_is_loopback(parsed.hostname or "")


def extract_token(request) -> str | None:
    """从请求里抠 token：头 → Bearer → 查询参数 → cookie（任意一处有就行）。"""
    header = request.headers.get(TOKEN_HEADER)
    if header:
        return header.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    token = request.query_params.get("token")
    if token:
        return str(token).strip()
    return request.cookies.get(TOKEN_COOKIE)


def token_matches(provided: str | None, expected: str) -> bool:
    """常量时间比较，避免时序侧信道（对本地服务有点讲究过头，但零成本）。"""
    return secrets.compare_digest(provided or "", expected)
