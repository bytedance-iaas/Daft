"""The least of S3 that Lance's object store reads with: a local directory served path-style
(``/<bucket>/<key>``) - ListObjectsV2 (prefix, delimiter), GET with Range, HEAD. No signature is checked.
Lance on TOS goes through TOS's S3-compatible endpoint the same way (design doc 19 §4.3)."""
from __future__ import annotations

import hashlib
import os
import threading
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape


class FakeS3:
    """``with FakeS3(root) as s3:`` serves ``root/<bucket>/...`` at ``s3.endpoint``; ``s3.requests``
    counts what was asked (method, key, range)."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.requests: list[tuple[str, str, str | None]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "FakeS3":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        s3 = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _split(self) -> tuple[str, str, dict]:
                url = urlsplit(self.path)
                parts = unquote(url.path).lstrip("/").split("/", 1)
                return parts[0], (parts[1] if len(parts) > 1 else ""), parse_qs(url.query)

            def _file(self, bucket: str, key: str) -> str | None:
                path = os.path.abspath(os.path.join(s3.root, bucket, key))
                if not path.startswith(s3.root + os.sep) or not os.path.isfile(path):
                    return None
                return path

            def _send(self, code: int, body: bytes = b"", headers: dict | None = None) -> None:
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _meta(self, path: str) -> dict:
                st = os.stat(path)
                return {"Last-Modified": formatdate(st.st_mtime, usegmt=True),
                        "ETag": '"' + hashlib.md5(f"{path}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest() + '"',
                        "Accept-Ranges": "bytes", "Content-Type": "application/octet-stream"}

            def do_HEAD(self) -> None:
                bucket, key, _ = self._split()
                s3.requests.append(("HEAD", key, None))
                path = self._file(bucket, key)
                if path is None:
                    return self._send(404)
                self.send_response(200)
                for k, v in self._meta(path).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(os.path.getsize(path)))
                self.end_headers()

            def do_GET(self) -> None:
                bucket, key, query = self._split()
                if not key and "list-type" in query:
                    return self._list(bucket, query)
                rng = self.headers.get("Range")
                s3.requests.append(("GET", key, rng))
                path = self._file(bucket, key)
                if path is None:
                    return self._send(404, b"<Error><Code>NoSuchKey</Code></Error>", {"Content-Type": "application/xml"})
                size = os.path.getsize(path)
                headers = self._meta(path)
                with open(path, "rb") as fh:
                    if rng and rng.startswith("bytes="):
                        a, _, b = rng[len("bytes="):].partition("-")
                        if a:
                            start, end = int(a), min(int(b), size - 1) if b else size - 1
                        else:
                            start, end = max(0, size - int(b)), size - 1
                        if start >= size:
                            return self._send(416, b"", {"Content-Range": f"bytes */{size}"})
                        fh.seek(start)
                        body = fh.read(end - start + 1)
                        return self._send(206, body, {**headers, "Content-Range": f"bytes {start}-{end}/{size}"})
                    return self._send(200, fh.read(), headers)

            def _list(self, bucket: str, query: dict) -> None:
                prefix = (query.get("prefix") or [""])[0]
                delim = (query.get("delimiter") or [""])[0]
                after = (query.get("start-after") or [""])[0]
                s3.requests.append(("LIST", prefix, None))
                base = os.path.join(s3.root, bucket)
                keys = []
                for d, _, files in os.walk(base):
                    for f in files:
                        k = os.path.relpath(os.path.join(d, f), base).replace(os.sep, "/")
                        if k.startswith(prefix) and k > after:
                            keys.append(k)
                keys.sort()
                contents, prefixes = [], []
                for k in keys:
                    rest = k[len(prefix):]
                    if delim and delim in rest:
                        p = prefix + rest.split(delim, 1)[0] + delim
                        if p not in prefixes:
                            prefixes.append(p)
                        continue
                    meta = self._meta(os.path.join(base, k))
                    contents.append(f"<Contents><Key>{escape(k)}</Key><LastModified>2026-10-04T00:00:00.000Z</LastModified>"
                                    f"<ETag>{escape(meta['ETag'])}</ETag><Size>{os.path.getsize(os.path.join(base, k))}</Size>"
                                    f"<StorageClass>STANDARD</StorageClass></Contents>")
                body = ('<?xml version="1.0" encoding="UTF-8"?>'
                        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                        f"<Name>{escape(bucket)}</Name><Prefix>{escape(prefix)}</Prefix>"
                        f"<KeyCount>{len(contents) + len(prefixes)}</KeyCount><MaxKeys>1000</MaxKeys>"
                        "<IsTruncated>false</IsTruncated>" + "".join(contents)
                        + "".join(f"<CommonPrefixes><Prefix>{escape(p)}</Prefix></CommonPrefixes>" for p in prefixes)
                        + "</ListBucketResult>").encode()
                self._send(200, body, {"Content-Type": "application/xml"})

        return Handler
