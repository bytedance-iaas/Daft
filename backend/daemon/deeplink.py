"""v1 deep links (design doc 07, section 2.1; 09 §2.4), ported from v1 ``ui/runner.py``.

The rerun viewer's "质检" button opens ``{base}/?dataset=tos://...&region=...``.
v2's new-task page lives at ``{base}/tasks/new``; the old entry answers with a
302 there, query string untouched, and the page (W10) does the prefilling with
the same rules. The parsing functions are kept byte-for-byte in behaviour so the
server and v1's tests agree on what a link means:

* dataset keys ``dataset`` / ``dataset_url`` / ``url``: all three read, values merged
  in key order, de-duplicated, commas split again - one link can carry several datasets;
* ``region``: the first value matching ``^[a-z0-9][a-z0-9-]{1,31}$``, only for preselection;
* ``endpoint`` / ``tos_endpoint``: sanitized to a bare host name, only ever used
  for a hint, never to read anything (XSS / SSRF surface);
* ``source=public``: the HuggingFace cache bucket.

A key that is present must get a visible answer on the page even when its
value is empty or invalid, so the redirect happens whenever any of the keys is
present, whatever the values.
"""
from __future__ import annotations

import re
from typing import Mapping
from urllib.parse import unquote, urlsplit

DEEPLINK_KEYS = ("dataset", "dataset_url", "url")
REGION_KEY = "region"
ENDPOINT_KEYS = ("endpoint", "tos_endpoint")
SOURCE_KEY = "source"
#: Any of these in the query of ``{base}/`` makes it an old deep link.
ENTRY_KEYS = DEEPLINK_KEYS + (REGION_KEY,) + ENDPOINT_KEYS + (SOURCE_KEY,)

_TOS_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
_ENDPOINT_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
_REGION_IN_HOST_RE = re.compile(r"(?<![a-z0-9])((?:cn|ap|us|eu)-[a-z]+(?:-\d+)?)(?![a-z0-9])")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def _values(qp, key: str) -> list:
    """Starlette ``QueryParams`` (``getlist``) or a plain dict (tests)."""
    if hasattr(qp, "getlist"):
        return list(qp.getlist(key))
    if key not in qp:
        return []
    value = qp[key]
    return list(value) if isinstance(value, (list, tuple)) else [value]


def deeplink_values(qp) -> tuple[list[str], bool]:
    """-> (dataset references to preselect, whether any dataset key was present)."""
    out: list[str] = []
    present = False
    for key in DEEPLINK_KEYS:
        vals = _values(qp, key)
        if vals:
            present = True
        for raw in vals:
            for s in str(raw or "").split(","):
                s = s.strip()
                if s and s not in out:
                    out.append(s)
    return out, present


def deeplink_region(qp) -> tuple[str | None, bool]:
    """-> (the first valid region or None, whether the key was present)."""
    vals = _values(qp, REGION_KEY)
    for raw in vals:
        s = str(raw or "").strip().lower()
        if s and _TOS_REGION_RE.match(s):
            return s, True
    return None, bool(vals)


def sanitize_endpoint(raw) -> str | None:
    """Untrusted endpoint -> bare lower-case host name, or None when it cannot be cleaned."""
    s = str(raw or "").strip()
    if not s or len(s) > 1024:
        return None
    try:
        host = urlsplit(s if "://" in s else "//" + s).hostname
    except ValueError:
        return None
    host = str(host or "").strip().lower()
    if not host or len(host) > 253 or not _ENDPOINT_HOST_RE.fullmatch(host):
        return None
    return host


def endpoint_region(host) -> str | None:
    """``tos-cn-beijing.ivolces.com`` -> ``cn-beijing``; None when no region is recognizable."""
    m = _REGION_IN_HOST_RE.search(str(host or "").lower())
    return m.group(1) if m else None


def deeplink_endpoint(qp) -> tuple[str | None, bool]:
    """-> (the first endpoint that sanitizes, whether any endpoint key was present)."""
    present = False
    for key in ENDPOINT_KEYS:
        vals = _values(qp, key)
        if vals:
            present = True
        for raw in vals:
            host = sanitize_endpoint(raw)
            if host:
                return host, True
    return None, present


def _safe_name(name: str) -> None:
    s = str(name or "").strip()
    if not s:
        raise ValueError("名字不能为空")
    if "/" in s or "\\" in s:
        raise ValueError(f"名字里不能带路径分隔符：{s!r}")
    if s in (".", "..") or s.startswith("."):
        raise ValueError(f"名字不能以点开头：{s!r}")
    if not _NAME_RE.match(s):
        raise ValueError(f"名字只能用字母、数字、点、下划线、连字符，且以字母或数字开头，最长 80 个字符：{s!r}")


def parse_dataset_ref(raw: str) -> dict:
    """One dataset reference -> ``{"bucket", "prefix", "dataset"}`` or ``{"error": ...}``.

    A bare name keeps bucket and prefix ``None``; ``tos://bucket/prefix/name`` gives
    the bucket lower-cased, the prefix without surrounding slashes (``""`` = bucket
    root) and the last segment as the dataset name. Anything odd is an error,
    never a guess.
    """
    s = str(raw or "").strip()
    if "://" not in s:
        return {"bucket": None, "prefix": None, "dataset": s}
    if not s.lower().startswith("tos://"):
        return {"error": f"只认识 tos:// 开头的数据集链接，这个解析不了：{s}"}
    try:
        parts = urlsplit(s)
    except ValueError:
        return {"error": f"数据集链接解析不了：{s}"}
    bucket = unquote(parts.netloc or "").strip().lower()
    if not bucket:
        return {"error": f"链接里没有存储桶名，解析不了：{s}"}
    segs = [unquote(x).strip() for x in (parts.path or "").split("/") if x.strip()]
    if not segs:
        return {"error": f"链接只有存储桶名、没有数据集段，不知道要选哪个数据集：{s}"}
    name = segs[-1]
    try:
        _safe_name(name)
    except ValueError as e:
        return {"error": f"链接里的数据集名不合法（{e}），这个链接不处理：{s}"}
    return {"bucket": bucket, "prefix": "/".join(segs[:-1]), "dataset": name}


def is_entry(qp: Mapping) -> bool:
    """Is a request for ``{base}/`` an old deep link (any of the v1 keys present)?"""
    return any(_values(qp, key) for key in ENTRY_KEYS)
