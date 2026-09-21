"""HTTP Basic 鉴权中间件(2026-07-29 U4 单用户版;2026-08-17 加 htpasswd 多用户模式)。

写法照抄同事在运的 lerobot-agent-console(`server.py::auth_middleware`),三条要点
一条不改:

  · **覆盖全部路由,含 WebSocket** —— 所以写成裸 ASGI 中间件而不是
    `BaseHTTPMiddleware`(后者只经手 `scope["type"] == "http"`,WS 会**绕过**鉴权;
    我们的 `/ws/term` 是个真 shell,漏了就等于门没锁)。
  · **`/healthz` 豁免** —— LB / k8s 探针不该被 401(readinessProbe 一红 pod 就下线)。
  · **常数时间比较**(`hmac.compare_digest` / bcrypt)—— 短路比较会泄漏密码前缀。

两种配置模式,按优先级:

1. **htpasswd 多用户**(推荐,与 rerun web viewer 的 nginx 共用同一份账号表,
   同域名部署时任意账号登录一次两边通行):
   `CURATION_UI_HTPASSWD_FILE` = htpasswd 文件路径,每行「用户名:密码哈希」。
   支持 bcrypt(`htpasswd -nbB`,$2y$/$2b$/$2a$)和 apr1(`openssl passwd -apr1`)。
   配了此 env 但文件读不到/没有可用账号 = **拒绝所有请求**(fail-closed:
   配置错误必须炸在脸上,不能静默裸奔)。
2. **单用户明文**(旧模式,兼容存量部署):`CURATION_UI_USER` 与
   `CURATION_UI_PASSWORD` **同时**非空。只配一个 = 不启用(半配的鉴权是最坏
   情况:自以为锁了其实没锁)。

浏览器在 WS 握手上会自动带上同源已缓存的 Basic 凭证,所以 UI 页面过了鉴权之后
`/ws/term` 也能连上——参考实现在生产里就是这么跑的。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
from typing import Callable

log = logging.getLogger("curation.ui.auth")

USER_ENV = "CURATION_UI_USER"
PASSWORD_ENV = "CURATION_UI_PASSWORD"
HTPASSWD_ENV = "CURATION_UI_HTPASSWD_FILE"

#: 不需要鉴权的路径(LB / k8s 探针)。
EXEMPT_PATHS = frozenset({"/healthz"})

_REALM = 'Basic realm="Robot Data Curation"'


def credentials_from_env() -> tuple[str, str]:
    """→ (user, password);任一为空即视为「没配」。"""
    return os.environ.get(USER_ENV, ""), os.environ.get(PASSWORD_ENV, "")


# ---------------------------------------------------------------- htpasswd

def load_htpasswd(path: str) -> dict[str, str]:
    """解析 htpasswd 文件 → {用户名: 哈希}。坏行跳过并告警,不让一行拖垮整个文件。"""
    users: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            user, sep, hashed = line.partition(":")
            if not sep or not user or not hashed:
                log.warning("htpasswd 第 %d 行格式不是「用户名:哈希」,跳过", lineno)
                continue
            if not hashed.startswith(("$2a$", "$2b$", "$2y$", "$apr1$")):
                log.warning("htpasswd 第 %d 行(user=%s)哈希格式不支持"
                            "(只认 bcrypt $2y$/$2b$/$2a$ 与 apr1),跳过", lineno, user)
                continue
            users[user] = hashed
    return users


#: apr1(Apache MD5 crypt)的 base64 字母表(crypt 顺序,与标准 base64 不同)。
_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _apr1(password: str, salt: str) -> str:
    """Apache 的 $apr1$ MD5 crypt(`openssl passwd -apr1` 产的那种)。纯标准库实现。"""
    pw = password.encode()
    salt_b = salt.encode()
    m = hashlib.md5(pw + b"$apr1$" + salt_b)
    inner = hashlib.md5(pw + salt_b + pw).digest()
    i = len(pw)
    while i > 0:
        m.update(inner[: min(16, i)])
        i -= 16
    i = len(pw)
    while i:
        m.update(b"\0" if i & 1 else pw[:1])
        i >>= 1
    final = m.digest()
    for i in range(1000):
        m2 = hashlib.md5(pw if i & 1 else final)
        if i % 3:
            m2.update(salt_b)
        if i % 7:
            m2.update(pw)
        m2.update(final if i & 1 else pw)
        final = m2.digest()

    def b64(v: int, n: int) -> str:
        out = ""
        for _ in range(n):
            out += _ITOA64[v & 0x3F]
            v >>= 6
        return out

    digest = "".join(
        b64((final[a] << 16) | (final[b] << 8) | final[c], 4)
        for a, b, c in ((0, 6, 12), (1, 7, 13), (2, 8, 14), (3, 9, 15), (4, 10, 5))
    ) + b64(final[11], 2)
    return f"$apr1${salt}${digest}"


def _verify_hash(password: str, hashed: str) -> bool:
    """密码 vs 单条哈希。load_htpasswd 已过滤掉不认识的格式,这里只剩两种。"""
    if hashed.startswith(("$2a$", "$2b$", "$2y$")):
        import bcrypt  # 生产依赖(requirements.txt);缺了在 import 处炸,好定位
        try:
            return bcrypt.checkpw(password.encode(), hashed.encode())
        except ValueError:
            return False
    # $apr1$salt$digest
    parts = hashed.split("$")
    if len(parts) != 4:
        return False
    return hmac.compare_digest(_apr1(password, parts[2]), hashed)


def make_htpasswd_check(users: dict[str, str]) -> Callable[[str, str], bool]:
    """→ check(user, password)。未知用户也做一次同量级的哈希运算,拉平时间差,
    防止靠响应时间枚举用户名。"""
    def check(user: str, password: str) -> bool:
        hashed = users.get(user)
        if hashed is None:
            _apr1(password, "xxxxxxxx")
            return False
        return _verify_hash(password, hashed)
    return check


def make_single_user_check(user: str, password: str) -> Callable[[str, str], bool]:
    def check(u: str, p: str) -> bool:
        # 两个 compare_digest 都要跑(别 and 短路),否则用户名对不对会有时间差
        ok_user = hmac.compare_digest(u, user)
        ok_pass = hmac.compare_digest(p, password)
        return ok_user and ok_pass
    return check


# ---------------------------------------------------------------- middleware

class BasicAuthMiddleware:
    """裸 ASGI 中间件:http 与 websocket 两种 scope 都拦。凭证核验交给 check 回调。"""

    def __init__(self, app, check: Callable[[str, str], bool],
                 exempt: frozenset = EXEMPT_PATHS) -> None:
        self.app = app
        self.check = check
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
            return self.check(user, password)
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
    """按模块 docstring 的优先级给 `app` 套上鉴权;没配则只打日志。返回 app 本身。

    extra_exempt:EXEMPT_PATHS 之外再豁免的路径。带挂载前缀部署时传 `{root}/healthz`
    —— 网关那侧的健康检查只能带着前缀进来,探针直连容器端口仍走 `/healthz`。
    """
    exempt = EXEMPT_PATHS | frozenset(extra_exempt)
    htpasswd_path = os.environ.get(HTPASSWD_ENV, "")
    if htpasswd_path:
        try:
            users = load_htpasswd(htpasswd_path)
        except OSError as err:
            log.error("鉴权:htpasswd 文件读不到:%s\n文件路径:%s", err, htpasswd_path)
            users = {}
        if users:
            app.add_middleware(BasicAuthMiddleware, check=make_htpasswd_check(users),
                               exempt=exempt)
            log.info("鉴权:htpasswd 多用户 HTTP Basic 已启用(%d 个账号)", len(users))
        else:
            # fail-closed:配置声明了要鉴权,就绝不能因为文件坏了而敞开
            app.add_middleware(BasicAuthMiddleware, check=lambda u, p: False,
                               exempt=exempt)
            log.error("鉴权:%s 已配置但没有可用账号 —— 已锁死全部请求(探针除外),"
                      "修好 htpasswd 文件再重启", HTPASSWD_ENV)
        return app

    user, password = credentials_from_env()
    if user and password:
        app.add_middleware(BasicAuthMiddleware,
                           check=make_single_user_check(user, password),
                           exempt=exempt)
        log.info("鉴权:单用户 HTTP Basic 已启用(user=%s)", user)
    elif terminal_enabled:
        # 参考实现的做法:终端是真 shell,没鉴权就得在日志里喊一嗓子
        log.warning("鉴权:未启用 —— 「终端」页签是一个真 shell,公网暴露前务必设置 "
                    "%s(推荐)或 %s + %s(并在网关上再加一层)",
                    HTPASSWD_ENV, USER_ENV, PASSWORD_ENV)
    else:
        log.info("鉴权:未启用(设置 %s 或 %s + %s 可开启)",
                 HTPASSWD_ENV, USER_ENV, PASSWORD_ENV)
    return app
