"""AWS Signature V4 query-string presigning for S3-compatible endpoints (design doc 15 §2.4; D55).

The ReRun web viewer reads TOS through its S3-compatible endpoint (listings come back as S3
XML, the same code also reads AWS S3 and MinIO), so the URLs the Daemon signs for it are S3
URLs, not the TOS-native ones the ``tos`` SDK makes for ``/media/sign``. The SDK cannot sign
those and the image has no boto3, so this is the small standard-library version:

* virtual-host style: ``https://<bucket>.<endpoint host>/<key>``; ``key=""`` is the bucket
  itself (a listing);
* only ``host`` is signed and the payload is ``UNSIGNED-PAYLOAD``, so a ``Range`` header does
  not change the signature: one URL serves every byte range of its object;
* a session token, when the key has one, goes into the signed query as
  ``X-Amz-Security-Token``.

A pure function: no I/O, no clock (the caller passes the time, so tests pin it). The URL
carries the access key id and the signature, never the secret key.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Mapping
from urllib.parse import quote, urlsplit

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
#: SigV4 refuses presigned URLs valid for longer than seven days.
MAX_TTL_S = 7 * 24 * 3600


def _enc(text: str, safe: str = "") -> str:
    """RFC 3986 encoding as SigV4 wants it: unreserved characters stay, everything else
    becomes ``%XX`` of its UTF-8 bytes (``quote`` keeps ``-_.~`` already; said once more)."""
    return quote(text, safe=safe + "-_.~")


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def presign(method: str, endpoint: str, bucket: str, key: str, query: Mapping[str, str], *,
            access_key_id: str, secret_access_key: str, region: str, ttl_s: int, at_ms: int,
            session_token: str | None = None) -> str:
    """A presigned URL for ``method`` on ``key`` of ``bucket`` (``key=""``: the bucket).

    ``endpoint`` is the service endpoint without the bucket (``https://tos-s3-cn-beijing.volces.com``);
    ``query`` is the request's own parameters (``list-type``, ``prefix``, ...), unencoded;
    ``at_ms`` is the signing time in epoch milliseconds (the Daemon's clock).
    """
    method = str(method).upper()
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path.strip("/"):
        raise ValueError(f"not a bare endpoint: {endpoint!r}")
    if not bucket or "/" in bucket:
        raise ValueError(f"not a bucket name: {bucket!r}")
    ttl_s = int(ttl_s)
    if not 1 <= ttl_s <= MAX_TTL_S:
        raise ValueError(f"ttl_s must be 1-{MAX_TTL_S}, got {ttl_s}")
    if not access_key_id or not secret_access_key:
        raise ValueError("an access key id and a secret key are both needed")
    for name in query:
        if name.lower().startswith("x-amz-"):
            raise ValueError(f"{name} is set by the signer, not the caller")

    host = f"{bucket}.{parts.netloc.lower()}"
    amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(int(at_ms) // 1000))
    date = amz_date[:8]
    scope = f"{date}/{region}/{SERVICE}/aws4_request"
    params = {str(k): str(v) for k, v in query.items()}
    params.update({
        "X-Amz-Algorithm": ALGORITHM,
        "X-Amz-Credential": f"{access_key_id}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(ttl_s),
        "X-Amz-SignedHeaders": "host",
    })
    if session_token:
        params["X-Amz-Security-Token"] = session_token
    # sorted by the encoded name, as SigV4 says; the URL keeps the same order
    canonical_query = "&".join(f"{n}={v}" for n, v in
                               sorted((_enc(k), _enc(v)) for k, v in params.items()))
    path = "/" + _enc(key, safe="/")          # S3 encodes the path once and keeps '/'
    canonical_request = "\n".join(
        [method, path, canonical_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    string_to_sign = "\n".join(
        [ALGORITHM, amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    signing_key = ("AWS4" + secret_access_key).encode("utf-8")
    for part in (date, region, SERVICE, "aws4_request"):
        signing_key = _hmac(signing_key, part)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{parts.scheme}://{host}{path}?{canonical_query}&X-Amz-Signature={signature}"
