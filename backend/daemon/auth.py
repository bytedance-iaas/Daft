"""HTTP Basic authentication behind an ``AuthProvider`` (design doc 08, section 5).

Ported from v1 ``curation/ui/auth.py``; the three rules are unchanged:

* a **bare ASGI middleware** covering every route (``BaseHTTPMiddleware`` only
  sees ``http`` scopes, a future WebSocket would slip past it);
* the probes ``/healthz`` and ``/readyz`` - at the root and under the mount
  prefix - are **exempt** (a 401 on a probe takes the pod out of service);
* **constant-time comparison** (``hmac.compare_digest`` / bcrypt), and an
  unknown user still costs one hash of the same kind, so timing does not reveal
  which account names exist.

Modes, in v1's order of precedence:

1. htpasswd, several users (``CURATOR_HTPASSWD_FILE``, old name
   ``CURATION_UI_HTPASSWD_FILE``): bcrypt ``$2a$/$2b$/$2y$`` and apr1, the same
   table the rerun viewer's nginx uses. Configured but unreadable or without a
   usable account = **every request is refused** (fail closed).
2. single user from the environment (``CURATOR_AUTH_USER`` / ``CURATOR_AUTH_PASSWORD``,
   old names ``CURATION_UI_USER`` / ``CURATION_UI_PASSWORD``): only when **both**
   are set; one of them alone counts as not configured.
3. nothing configured: no authentication (local development), logged loudly.

``CURATOR_AUTH_MODE`` (``htpasswd`` | ``basic`` | ``none``) pins the mode for
deployments: a pinned mode whose settings are missing refuses every request
instead of falling back to "no authentication". The realm stays
``Robot Data Curation`` so a login on the rerun viewer carries over.
"""
from __future__ import annotations

import base64
import binascii
import collections
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, NamedTuple, Protocol

import anyio

log = logging.getLogger("daemon.auth")

REALM = "Robot Data Curation"
MODE_ENV = "CURATOR_AUTH_MODE"
HTPASSWD_ENVS = ("CURATOR_HTPASSWD_FILE", "CURATION_UI_HTPASSWD_FILE")
USER_ENVS = ("CURATOR_AUTH_USER", "CURATION_UI_USER")
PASSWORD_ENVS = ("CURATOR_AUTH_PASSWORD", "CURATION_UI_PASSWORD")
MODES = ("htpasswd", "basic", "none")

#: Where the authenticated principal is stored on the ASGI scope.
SCOPE_KEY = "curator.principal"

UNAUTHORIZED_MESSAGE = "需要登录：请输入质检平台的账号和密码"


class Principal(NamedTuple):
    owner_id: str
    display_name: str
    roles: frozenset = frozenset({"owner"})


#: Who probes and unauthenticated deployments act as. Single tenant: owner is always default (D3).
ANONYMOUS = Principal("default", "anonymous")


class AuthProvider(Protocol):
    def authenticate(self, scope) -> Principal | None: ...

    def challenge(self) -> tuple[int, list[tuple[bytes, bytes]], bytes]: ...


def _first(env: Mapping[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = str(env.get(name, "") or "")
        if value.strip():
            return value
    return ""


@dataclass(frozen=True)
class AuthConfig:
    mode: str = ""                  # "" = decide from what is configured (v1 behaviour)
    htpasswd_file: str = ""
    user: str = ""
    password: str = ""

    def __repr__(self) -> str:      # the password never reaches a log line
        return (f"AuthConfig(mode={self.mode!r}, htpasswd_file={self.htpasswd_file!r}, "
                f"user={self.user!r}, password={'***' if self.password else ''!r})")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AuthConfig":
        env = os.environ if env is None else env
        mode = str(env.get(MODE_ENV, "") or "").strip().lower()
        if mode and mode not in MODES:
            from .settings import ConfigError
            raise ConfigError(f"{MODE_ENV} 只能是 {' / '.join(MODES)}，现在是 {mode!r}")
        return cls(mode=mode, htpasswd_file=_first(env, HTPASSWD_ENVS).strip(),
                   user=_first(env, USER_ENVS).strip(), password=_first(env, PASSWORD_ENVS))


# ---------------------------------------------------------------------------
# password hashes
# ---------------------------------------------------------------------------

_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")
_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def apr1(password: str, salt: str) -> str:
    """Apache ``$apr1$`` MD5 crypt (``openssl passwd -apr1``), standard library only."""
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


def verify_hash(password: str, hashed: str) -> bool:
    if hashed.startswith(_BCRYPT_PREFIXES):
        import bcrypt  # runtime dependency (backend/requirements.txt)
        try:
            return bcrypt.checkpw(password.encode(), hashed.encode())
        except ValueError:
            return False
    parts = hashed.split("$")
    if len(parts) != 4 or parts[1] != "apr1":
        return False
    return hmac.compare_digest(apr1(password, parts[2]), hashed)


def load_htpasswd(path: str) -> dict[str, str]:
    """``user:hash`` lines -> dict. Bad lines are skipped with a warning, not fatal."""
    users: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            user, sep, hashed = line.partition(":")
            if not sep or not user or not hashed:
                log.warning("htpasswd line %d is not user:hash; skipped", lineno)
                continue
            if not hashed.startswith(_BCRYPT_PREFIXES + ("$apr1$",)):
                log.warning("htpasswd line %d (user=%s): only bcrypt ($2y$/$2b$/$2a$) and apr1 "
                            "hashes are supported; skipped", lineno, user)
                continue
            users[user] = hashed
    return users


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------

def _basic_credentials(scope) -> tuple[str, str] | None:
    """(user, password) from the first Authorization header, or None."""
    for key, value in scope.get("headers") or ():
        if key.lower() != b"authorization":
            continue
        if not value[:6].lower() == b"basic ":
            return None
        try:
            raw = base64.b64decode(value[6:].strip(), validate=True).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return None
        user, sep, password = raw.partition(":")
        return (user, password) if sep else None
    return None


def _challenge() -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    body = json.dumps({"error": {"code": "unauthorized", "message": UNAUTHORIZED_MESSAGE}},
                      ensure_ascii=False).encode()
    return 401, [(b"content-type", b"application/json"),
                 (b"www-authenticate", f'Basic realm="{REALM}"'.encode())], body


class _VerifiedCache:
    """Remember successful checks for a few minutes so bcrypt does not run on every request.

    Keys are HMACs of ``user:password`` under a per-process random key: the cache
    never holds a password, and only credentials that already verified are in it.
    """

    def __init__(self, ttl_s: float = 300.0, size: int = 1024):
        self._ttl, self._size = ttl_s, size
        self._secret = os.urandom(32)
        self._entries: collections.OrderedDict[bytes, float] = collections.OrderedDict()
        self._lock = threading.Lock()

    def _key(self, user: str, password: str) -> bytes:
        return hmac.new(self._secret, f"{user}\0{password}".encode(), "sha256").digest()

    def hit(self, user: str, password: str) -> bool:
        key = self._key(user, password)
        with self._lock:
            expiry = self._entries.get(key)
            if expiry is None:
                return False
            if expiry < time.monotonic():
                del self._entries[key]
                return False
            return True

    def add(self, user: str, password: str) -> None:
        with self._lock:
            self._entries[self._key(user, password)] = time.monotonic() + self._ttl
            while len(self._entries) > self._size:
                self._entries.popitem(last=False)


class BasicAuthProvider:
    """Checks HTTP Basic credentials with ``check(user, password)``."""

    def __init__(self, check: Callable[[str, str], bool], *, description: str):
        self._check = check
        self._cache = _VerifiedCache()
        self.description = description

    def authenticate(self, scope) -> Principal | None:
        creds = _basic_credentials(scope)
        if creds is None:
            return None
        user, password = creds
        if not self._cache.hit(user, password):
            if not self._check(user, password):
                return None
            self._cache.add(user, password)
        return Principal("default", user)

    def challenge(self):
        return _challenge()


class DenyAllProvider:
    """Authentication was asked for but cannot work: refuse everyone (fail closed)."""

    description = "deny-all (misconfigured)"

    def authenticate(self, scope) -> Principal | None:
        return None

    def challenge(self):
        return _challenge()


class NoAuthProvider:
    description = "disabled"

    def authenticate(self, scope) -> Principal | None:
        return ANONYMOUS

    def challenge(self):
        return _challenge()


def htpasswd_check(users: dict[str, str]) -> Callable[[str, str], bool]:
    """``check(user, password)``; unknown users pay for a hash of the same kind and cost."""
    bcrypt_hash = next((h for h in users.values() if h.startswith(_BCRYPT_PREFIXES)), None)
    if bcrypt_hash is not None:
        import bcrypt
        cost = int(bcrypt_hash.split("$")[2])
        dummy = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(rounds=cost)).decode()
    else:
        dummy = apr1("not-a-real-password", "xxxxxxxx")

    def check(user: str, password: str) -> bool:
        hashed = users.get(user)
        if hashed is None:
            verify_hash(password, dummy)
            return False
        return verify_hash(password, hashed)

    return check


def single_user_check(user: str, password: str) -> Callable[[str, str], bool]:
    def check(u: str, p: str) -> bool:
        # both comparisons always run (no short circuit): no timing hint about the user name
        ok_user = hmac.compare_digest(u.encode(), user.encode())
        ok_pass = hmac.compare_digest(p.encode(), password.encode())
        return ok_user and ok_pass
    return check


def build_provider(config: AuthConfig) -> AuthProvider:
    mode = config.mode
    if mode == "none":
        log.warning("authentication disabled explicitly (%s=none)", MODE_ENV)
        return NoAuthProvider()
    if mode == "htpasswd" or (not mode and config.htpasswd_file):
        if not config.htpasswd_file:
            log.error("%s=htpasswd but no htpasswd file is configured: refusing every request",
                      MODE_ENV)
            return DenyAllProvider()
        try:
            users = load_htpasswd(config.htpasswd_file)
        except OSError as err:
            log.error("htpasswd file unreadable (%s): %s", config.htpasswd_file, err)
            users = {}
        if not users:
            log.error("htpasswd configured but has no usable account: refusing every request "
                      "(probes excepted); fix the file and restart")
            return DenyAllProvider()
        log.info("authentication: htpasswd, %d account(s)", len(users))
        return BasicAuthProvider(htpasswd_check(users),
                                 description=f"htpasswd ({len(users)} accounts)")
    if mode == "basic" or (not mode and config.user and config.password):
        if not (config.user and config.password):
            log.error("%s=basic needs both a user and a password: refusing every request", MODE_ENV)
            return DenyAllProvider()
        log.info("authentication: single user %s", config.user)
        return BasicAuthProvider(single_user_check(config.user, config.password),
                                 description="single user")
    log.warning("authentication NOT enabled: set %s (recommended) or %s + %s before exposing "
                "the Daemon", HTPASSWD_ENVS[0], USER_ENVS[0], PASSWORD_ENVS[0])
    return NoAuthProvider()


# ---------------------------------------------------------------------------
# middleware
# ---------------------------------------------------------------------------

class AuthMiddleware:
    """Bare ASGI middleware: http and websocket scopes both pass through the provider."""

    def __init__(self, app, provider: AuthProvider, exempt: frozenset[str]):
        self.app = app
        self.provider = provider
        self.exempt = exempt

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope.get("path") in self.exempt:
            scope[SCOPE_KEY] = ANONYMOUS
            await self.app(scope, receive, send)
            return
        principal = await anyio.to_thread.run_sync(self.provider.authenticate, scope)
        if principal is not None:
            scope[SCOPE_KEY] = principal
            await self.app(scope, receive, send)
            return
        status, headers, body = self.provider.challenge()
        if scope["type"] == "websocket":
            await _deny_websocket(scope, send, status, headers, body)
            return
        await send({"type": "http.response.start", "status": status,
                    "headers": headers + [(b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


async def _deny_websocket(scope, send, status, headers, body) -> None:
    """A real 401 when the server supports the denial extension, else close with 1008."""
    if "websocket.http.response" in (scope.get("extensions") or {}):
        await send({"type": "websocket.http.response.start", "status": status,
                    "headers": headers + [(b"content-length", str(len(body)).encode())]})
        await send({"type": "websocket.http.response.body", "body": body})
    else:
        await send({"type": "websocket.close", "code": 1008})


def principal_of(scope) -> Principal:
    return scope.get(SCOPE_KEY) or ANONYMOUS
