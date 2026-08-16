"""单用户 HTTP Basic 鉴权中间件(2026-07-29 U4;默认关,为 APIG 公网入口就位)。

写法照抄同事在运的 lerobot-agent-console(`server.py::auth_middleware`),三条要点
一条不改:

  · **覆盖全部路由,含 WebSocket** —— 所以写成裸 ASGI 中间件而不是
    `BaseHTTPMiddleware`(后者只经手 `scope["type"] == "http"`,WS 会**绕过**鉴权;
    我们的 `/ws/term` 是个真 shell,漏了就等于门没锁)。
  · **`/healthz` 豁免** —— LB / k8s 探针不该被 401(readinessProbe 一红 pod 就下线)。
  · **常数时间比较**(`hmac.compare_digest`)—— 逐字节短路比较会泄漏密码前缀。

启用条件:`CURATION_UI_USER` 与 `CURATION_UI_PASSWORD` **同时**非空。只配一个 = 不启用
(半配的鉴权是最坏情况:自以为锁了其实没锁)。

浏览器在 WS 握手上会自动带上同源已缓存的 Basic 凭证,所以 UI 页面过了鉴权之后
`/ws/term` 也能连上——参考实现在生产里就是这么跑的。
"""
from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os

log = logging.getLogger("curation.ui.auth")

USER_ENV = "CURATION_UI_USER"
PASSWORD_ENV = "CURATION_UI_PASSWORD"

#: 不需要鉴权的路径(LB / k8s 探针)。
EXEMPT_PATHS = frozenset({"/healthz"})

_REALM = 'Basic realm="Robot Data Curation"'


def credentials_from_env() -> tuple[str, str]:
    """→ (user, password);任一为空即视为「没配」。"""
    return os.environ.get(USER_ENV, ""), os.environ.get(PASSWORD_ENV, "")


class BasicAuthMiddleware:
    """裸 ASGI 中间件:http 与 websocket 两种 scope 都拦。"""

    def __init__(self, app, user: str, password: str,
                 exempt: frozenset = EXEMPT_PATHS) -> None:
        self.app = app
        self.user = user
        self.password = password
        self.exempt = exempt

    def _authorized(self, scope) -> bool:
        for key, value in scope.get("headers") or ():
            if key.lower() != b"authorization":
                continue
            if not value.lower().startswith(b"basic "):
                return False
            try:
                raw = base64.b64decode(value[6:]).decode("utf-8", "replace")
            except (binascii.Error, ValueError):
                return False
            user, _, password = raw.partition(":")
            # 两个 compare_digest 都要跑(别 and 短路),否则用户名对不对会有时间差
            ok_user = hmac.compare_digest(user, self.user)
            ok_pass = hmac.compare_digest(password, self.password)
            return ok_user and ok_pass
        return False

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket") \
                or scope.get("path") in self.exempt \
                or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await _deny_websocket(scope, send)
        else:
            await _deny_http(send)


async def _deny_http(send) -> None:
    body = "Authentication required".encode()
    await send({"type": "http.response.start", "status": 401,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()),
                            (b"www-authenticate", _REALM.encode())]})
    await send({"type": "http.response.body", "body": body})


async def _deny_websocket(scope, send) -> None:
    """WS 握手拒绝。

    能发真 401 就发真 401(ASGI 的 "websocket.http.response" 扩展):浏览器收到 401
    才会弹框/补上缓存的凭证再试;拿不到该扩展的服务器只能退回 `websocket.close`。
    """
    if "websocket.http.response" in (scope.get("extensions") or {}):
        body = b"Authentication required"
        await send({"type": "websocket.http.response.start", "status": 401,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode()),
                                (b"www-authenticate", _REALM.encode())]})
        await send({"type": "websocket.http.response.body", "body": body})
    else:
        await send({"type": "websocket.close", "code": 1008})   # 1008 = policy violation


def apply(app, *, terminal_enabled: bool, extra_exempt: tuple = ()):
    """配了凭证就给 `app` 套上鉴权;没配则只打日志。返回 app 本身,便于链式调用。

    extra_exempt:EXEMPT_PATHS 之外再豁免的路径。带挂载前缀部署时传 `{root}/healthz`
    —— 网关那侧的健康检查只能带着前缀进来,探针直连容器端口仍走 `/healthz`。
    """
    user, password = credentials_from_env()
    if user and password:
        app.add_middleware(BasicAuthMiddleware, user=user, password=password,
                           exempt=EXEMPT_PATHS | frozenset(extra_exempt))
        log.info("鉴权:单用户 HTTP Basic 已启用(user=%s)", user)
    elif terminal_enabled:
        # 参考实现的做法:终端是真 shell,没鉴权就得在日志里喊一嗓子
        log.warning("鉴权:未启用 —— 「终端」页签是一个真 shell,公网暴露前务必设置 "
                    "%s + %s(并在网关上再加一层)", USER_ENV, PASSWORD_ENV)
    else:
        log.info("鉴权:未启用(设置 %s + %s 可开启)", USER_ENV, PASSWORD_ENV)
    return app
