"""Browser URLs for objects under a prefix (design doc 08, section 6; 03 §7; D16).

Used by ``GET /media/sign`` and meant for every other place that hands the browser a video
or a frame (the new-task page's episode preview, the report's episode view):

1. the path must stay under the prefix - normalized first, anything that could climb out
   (``..``, backslashes, control characters, percent-encoded dots or slashes) is refused
   before a signature is made, so no request can sign an arbitrary object of the bucket;
2. short-lived: the caller passes the TTL (the API allows 60-3600 s, default 30 minutes);
3. signed on the **public** endpoint - an internal ``*.ivolces.com`` URL is dead in a
   browser. The anonymous public cache bucket is not signed; its plain public URL is given.
"""
from __future__ import annotations

import re
from urllib.parse import unquote

from . import tos as T
from .service import SecretsService

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class BadPath(ValueError):
    """The path would leave the prefix, or is empty; ``message_zh`` says why."""

    def __init__(self, message_zh: str):
        super().__init__(message_zh)
        self.message_zh = message_zh


def relative_key(raw: str) -> str:
    """A caller's path -> a clean relative object key, or :class:`BadPath`."""
    text = str(raw or "")
    if not text.strip():
        raise BadPath("path 不能为空")
    if "\\" in text or _CONTROL.search(text):
        raise BadPath("path 里不能有反斜杠或控制字符")
    segments = []
    for seg in text.split("/"):
        if seg in ("", "."):
            continue
        decoded = unquote(seg)
        if seg == ".." or decoded in (".", "..") or "/" in decoded or "\\" in decoded:
            raise BadPath("path 只能指向这个目录之内的文件，不能有 ..")
        segments.append(seg)
    if not segments:
        raise BadPath("path 没有指向任何文件")
    return "/".join(segments)


def object_under(uri: str, rel: str) -> tuple[str, str]:
    """(bucket, object key) of ``rel`` under ``tos://bucket/prefix``; checked to stay inside."""
    bucket, base = T.split_uri(uri)
    key = T.join_key(base, relative_key(rel))
    if base and not key.startswith(base.rstrip("/") + "/"):
        raise BadPath("path 只能指向这个目录之内的文件")
    return bucket, key


def browser_url(svc: SecretsService, uri: str, rel: str, *, ttl_s: int,
                key: T.TosKey | None, region: str | None = None) -> str:
    """A URL the browser can GET for ``rel`` under ``uri``: presigned with ``key`` on the
    public endpoint, or - ``key=None``, the anonymous public bucket - the plain public URL."""
    bucket, object_key = object_under(uri, rel)
    if key is None:
        return T.anonymous_url(bucket, object_key, region or T.DEFAULT_REGION)
    with svc.tos(key, region, browser=True) as (client, _):
        return T.presign_get(client, bucket, object_key, ttl_s)
