"""TOS calls made with a decrypted access key (design doc 08, sections 4 and 6).

Four things the Daemon does against TOS itself, each with one small helper here:

* **identity check** when a key is saved or re-verified - one signed ``ListBuckets``; a key
  that may not list buckets is checked with ``HeadBucket`` on the user's test bucket.
  Nothing is read from or written to any bucket (08 §4: identity and permissions are
  checked separately, permissions only against a concrete task);
* **read check** before a task starts - a real ``GET`` of ``meta/info.json`` under the input;
* **write probe** - put a zero-byte object under the delivery directory and delete it (the
  only reliable answer to "can this key write here", v1 ``tos_store.probe_writable``);
* **presigned GET URLs** for the browser, always on the public endpoint (08 §6.3).

Errors from the SDK become a :class:`TosFailure` (kind + scrubbed provider text), which the
callers turn into one Chinese sentence. The SDK client is built by a factory so tests run
offline with a fake; the real one is :func:`sdk_client` (``tos`` is imported lazily).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from curation import tos_store

from .scrub import Scrubber, clip

DEFAULT_REGION = tos_store.DEFAULT_REGION

#: The probe object under a delivery directory; a fixed name, so a failed delete leaves at
#: most one stray object per directory (v1 ``tos_store.PROBE_NAME`` idea, v2 name).
PROBE_OBJECT = ".curator-write-probe"

#: Error codes that mean "TOS did not accept this key", as opposed to "not allowed".
AUTH_CODES = frozenset({
    "InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidSecurityToken", "ExpiredToken",
    "TokenExpired", "InvalidToken", "AccountDisable", "AccountDisabled", "AccessKeyDisabled",
    "InvalidAccessKey", "InvalidAuthorization",
})

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")

#: How long the Daemon waits on TOS for one of its own small calls.
CONNECT_TIMEOUT_S = 5
SOCKET_TIMEOUT_S = 15


@dataclass(frozen=True)
class TosKey:
    """A decrypted TOS access key with its non-secret settings; ``repr`` shows no secret."""

    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)
    credential_id: str | None = None
    name: str | None = None
    region: str | None = None
    endpoint: str | None = None            # the user's endpoint, if any (meta.endpoint)
    test_bucket: str | None = None

    def secrets(self) -> tuple[str, ...]:
        return tuple(s for s in (self.access_key_id, self.secret_access_key, self.session_token)
                     if s)

    def scrubber(self) -> Scrubber:
        return Scrubber(self.secrets())

    @property
    def label(self) -> str:
        return f"访问密钥「{self.name}」" if self.name else "访问密钥"


@dataclass(frozen=True)
class Endpoints:
    region: str
    server: str        # the Daemon's own calls: internal when the deployment says so
    browser: str       # presigned URLs for the browser: always public (08 §6.3)


def normalize_endpoint(raw: str | None) -> str | None:
    """``tos-cn-beijing.volces.com`` -> ``https://tos-cn-beijing.volces.com``; host only.

    Raises ``ValueError`` (Chinese message) for anything with credentials, a path or a query.
    """
    s = str(raw or "").strip()
    if not s:
        return None
    if "://" not in s:
        s = "https://" + s
    parts = urlsplit(s)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("endpoint 的写法不对，形如 https://tos-cn-beijing.volces.com")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValueError("endpoint 里不能带账号或密码")
    if parts.path.strip("/") or parts.query or parts.fragment:
        raise ValueError("endpoint 只写域名（和端口），不要带路径或参数")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def is_internal(endpoint: str | None) -> bool:
    return ".ivolces.com" in str(endpoint or "").lower()


def public_endpoint(region: str) -> str:
    return f"https://tos-{region}.volces.com"


def endpoints(region: str | None, custom: str | None, deployment: str | None) -> Endpoints:
    """Where to send the Daemon's calls and what to sign browser URLs with.

    ``custom`` is the key's own endpoint (used as given); otherwise v1's rule
    (``tos_store.endpoint_for_region``): the internal endpoint when the deployment's
    ``TOS_ENDPOINT`` is internal and in the same region, the public one otherwise.
    """
    want = str(region or "").strip() or DEFAULT_REGION
    server = custom or tos_store.endpoint_for_region(want, deployment)
    browser = custom if custom and not is_internal(custom) else public_endpoint(want)
    return Endpoints(region=want, server=server, browser=browser)


def _quiet_sdk_logger() -> None:
    """The SDK logs every failed attempt at INFO with the raw exception (response headers,
    request URL, retry bodies). The Daemon reports failures itself, scrubbed, so the SDK's
    logger is raised to WARNING - unless an operator configured it on purpose."""
    sdk = logging.getLogger("tos")
    if sdk.level == logging.NOTSET:
        sdk.setLevel(logging.WARNING)


def sdk_client(endpoint: str, region: str, key: TosKey | None):
    """The real TOS SDK client; ``key=None`` is an anonymous (unsigned) client.

    ``dns_cache_time=0``: with its DNS cache on, the SDK patches urllib3's
    ``create_connection`` for the whole process (every ``requests`` call, the VLM calls
    included) and starts a global refresh thread that ``close()`` shuts down for everyone.
    The Daemon makes a handful of short calls; it does not need that.
    """
    import tos  # lazy: tests inject a fake factory and never import the SDK

    _quiet_sdk_logger()
    kwargs: dict[str, Any] = dict(max_retry_count=1, connection_time=CONNECT_TIMEOUT_S,
                                  socket_timeout=SOCKET_TIMEOUT_S, dns_cache_time=0)
    if key is None:
        return tos.TosClientV2("", "", endpoint, region, **kwargs)
    return tos.TosClientV2(key.access_key_id, key.secret_access_key, endpoint, region,
                           security_token=key.session_token or None, **kwargs)


#: ``factory(endpoint, region, key_or_None) -> client`` with the TosClientV2 method names.
ClientFactory = Callable[[str, str, "TosKey | None"], Any]


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TosFailure:
    kind: str           # auth | forbidden | no_bucket | no_object | not_found | unreachable | server | other
    status: int | None
    code: str
    detail: str         # the provider's words, scrubbed, one line

    @property
    def tag(self) -> str:
        """``InvalidAccessKeyId`` / ``HTTP 403`` - the short technical hint in parentheses."""
        if self.code:
            return self.code
        return f"HTTP {self.status}" if self.status else ""


def _status(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _is_transport_error(exc: BaseException) -> bool:
    if getattr(exc, "cause", None) is not None or isinstance(exc, (OSError, TimeoutError)):
        return True
    return type(exc).__module__.split(".", 1)[0] in ("requests", "urllib3")


def classify(exc: BaseException, scrub: Scrubber, *, bucket_op: bool = False) -> TosFailure:
    """An SDK exception -> :class:`TosFailure`. ``bucket_op``: a 404 means the bucket."""
    status = _status(exc)
    code = str(getattr(exc, "code", "") or "")
    message = getattr(exc, "message", None)
    if not isinstance(message, str) or not message.strip():
        message = str(exc) or type(exc).__name__
    detail = clip(scrub(f"{code}: {message}" if code and code not in message else message))
    code = clip(scrub(code), 64)
    if status is None:
        # client side: DNS, connect, TLS, timeout (TosClientError carries the cause)
        if _is_transport_error(exc):
            return TosFailure("unreachable", None, code, detail)
        return TosFailure("other", None, code, detail)
    if code in AUTH_CODES or status == 401:
        return TosFailure("auth", status, code, detail)
    if status == 403:
        return TosFailure("forbidden", status, code, detail)
    if code in ("NoSuchBucket", "PermanentRedirect") or status in (301, 307) or \
            (status == 404 and bucket_op and not code):
        return TosFailure("no_bucket", status, code, detail)
    if status == 404:
        return TosFailure("no_object" if code in ("NoSuchKey", "") else "not_found",
                          status, code, detail)
    if status >= 500:
        return TosFailure("server", status, code, detail)
    return TosFailure("other", status, code, detail)


def reason(failure: TosFailure, *, action: str, uri: str, key: TosKey | None, region: str,
           endpoint: str) -> str:
    """One Chinese sentence for a failed call; ``action`` is 读取 / 写入 / 验证."""
    who = key.label if key is not None else "匿名访问"
    tag = f"（{failure.tag}）" if failure.tag else ""
    if failure.kind == "auth":
        return f"{who}的 AK/SK 不对，或者已经失效{tag}"
    if failure.kind == "forbidden":
        return f"{who}没有{action} {uri} 的权限{tag}"
    if failure.kind == "no_bucket":
        bucket = uri[len("tos://"):].split("/", 1)[0] if uri.startswith("tos://") else uri
        return f"存储桶 {bucket} 不存在，或者不在地域 {region}{tag}"
    if failure.kind == "unreachable":
        return f"连不上 TOS（{endpoint}）：检查地域、endpoint 和网络"
    if failure.kind == "server":
        return f"TOS 服务端出错{tag}，请稍后重试"
    return f"TOS 返回错误{tag}：{failure.detail}"


# ---------------------------------------------------------------------------
# the calls
# ---------------------------------------------------------------------------

def split_uri(uri: str) -> tuple[str, str]:
    """``tos://bucket/a/b`` -> (``bucket``, ``a/b``); already normalized by the caller."""
    bucket, prefix = tos_store.parse_tos_url(uri)
    return bucket, prefix.strip("/")


def join_key(prefix: str, name: str) -> str:
    prefix = prefix.strip("/")
    return f"{prefix}/{name}" if prefix else name


@dataclass(frozen=True)
class Verification:
    state: str                  # ok | failed | unverified
    error: str | None           # Chinese, scrubbed; None when ok
    at: int = 0


def verify_identity(client, key: TosKey, *, region: str, endpoint: str,
                    now: int) -> Verification:
    """Identity only (08 §4): ``ListBuckets``; when listing is not allowed, ``HeadBucket`` on
    the key's test bucket. Never reads or writes an object."""
    scrub = key.scrubber()
    try:
        client.list_buckets()
        return Verification("ok", None, now)
    except Exception as exc:  # noqa: BLE001 - the SDK's exception family is wide
        listing = classify(exc, scrub)
    if listing.kind in ("auth", "unreachable"):
        return Verification("failed", reason(listing, action="验证", uri="", key=key,
                                             region=region, endpoint=endpoint), now)
    if key.test_bucket:
        uri = f"tos://{key.test_bucket}"
        try:
            client.head_bucket(key.test_bucket)
            return Verification("ok", None, now)
        except Exception as exc:  # noqa: BLE001
            head = classify(exc, scrub, bucket_op=True)
        if head.kind == "forbidden":
            text = (f"{key.label}不能列出存储桶，对测试用存储桶 {key.test_bucket} 也没有访问权限"
                    f"（{head.tag}），无法确认身份；也可能是 AK/SK 不对")
        else:
            text = reason(head, action="访问", uri=uri, key=key, region=region, endpoint=endpoint)
        return Verification("failed", text, now)
    if listing.kind == "forbidden":
        # TOS took the signature and refused the action: the key exists, it just may not
        # list buckets. Say how to finish the check instead of calling it a failure.
        return Verification(
            "unverified",
            f"{key.label}没有列出存储桶的权限（{listing.tag}），身份还没有确认："
            "填一个它能访问的「测试用存储桶」后重新验证", now)
    return Verification("failed", reason(listing, action="验证", uri="", key=key, region=region,
                                         endpoint=endpoint), now)


def read_object(client, bucket: str, key: str, *, limit: int = 1 << 20) -> bytes:
    """A small object's bytes (``meta/info.json``); at most ``limit`` bytes are read."""
    out = client.get_object(bucket, key)
    try:
        data = out.read(limit) if hasattr(out, "read") else bytes(out)
    finally:
        close = getattr(out, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - closing a finished body must not fail the check
                pass
    return data


@dataclass(frozen=True)
class ProbeOutcome:
    ok: bool
    kind: str                   # ok | leftover | <TosFailure.kind>
    reason: str                 # Chinese; empty when ok
    failure: TosFailure | None = None


def write_probe(client, uri: str, *, key: TosKey, region: str, endpoint: str) -> ProbeOutcome:
    """Put a zero-byte object under ``uri`` and delete it again (a real write)."""
    bucket, prefix = split_uri(uri)
    name = join_key(prefix, PROBE_OBJECT)
    scrub = key.scrubber()
    try:
        client.put_object(bucket, name, content=b"")
    except Exception as exc:  # noqa: BLE001
        failure = classify(exc, scrub)
        return ProbeOutcome(False, failure.kind, reason(failure, action="写入", uri=uri, key=key,
                                                        region=region, endpoint=endpoint), failure)
    try:
        client.delete_object(bucket, name)
    except Exception as exc:  # noqa: BLE001
        failure = classify(exc, scrub)
        return ProbeOutcome(True, "leftover",
                            f"写入成功，但探针对象 {PROBE_OBJECT} 没能删掉（{failure.tag}），"
                            "可以手动删除", failure)
    return ProbeOutcome(True, "ok", "")


def presign_get(client, bucket: str, key: str, ttl_s: int) -> str:
    try:
        import tos

        method = tos.HttpMethodType.Http_Method_Get
    except ImportError:          # tests without the SDK: the fake ignores the method
        method = "GET"
    return client.pre_signed_url(method, bucket, key, expires=int(ttl_s)).signed_url


def anonymous_url(bucket: str, key: str, region: str) -> str:
    """The public bucket is read anonymously: no signature, the plain public URL (08 §6.3)."""
    host = public_endpoint(region).split("://", 1)[1]
    return f"https://{bucket}.{host}/{quote(key.lstrip('/'))}"


def valid_bucket(name: str) -> bool:
    return bool(_BUCKET_RE.match(str(name or "")))
