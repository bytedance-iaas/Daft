"""The frontend under ``{base}/`` (design doc 07, section 2.3; 09 §2.4).

* Files of the built frontend (``frontend/dist``, ``/app/web`` in the image) are
  served as they are; ``assets/*`` (content-hashed names) are cached for a year,
  everything else is revalidated.
* Any other path under ``{base}/`` gets ``index.html`` (SPA fallback: a refresh
  on ``/curation/tasks/123/report`` must not 404), with the mount prefix injected
  as ``window.__CURATOR_BASE__`` and ``<base href="{base}/">`` so the relative
  asset URLs of a ``base: './'`` build resolve from any route depth.
* A missing file that is clearly an asset (``assets/...`` or a static file
  extension) is a 404, not ``index.html``.
* v1's entry ``{base}/?dataset=...`` (any v1 deep-link key) answers ``302`` to
  ``{base}/tasks/new?<the same query string>``; ``{base}`` without the slash goes
  to ``{base}/``.
"""
from __future__ import annotations

import json
import mimetypes
import os
import pathlib
import re
import threading

from fastapi import FastAPI, Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .. import deeplink
from ..errors import ApiError

_ASSET_EXT = frozenset({".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                        ".ico", ".webp", ".avif", ".woff", ".woff2", ".ttf", ".otf", ".eot",
                        ".json", ".txt", ".wasm", ".webmanifest", ".mp4", ".webm"})
_HEAD_RE = re.compile(rb"<head(\s[^>]*)?>", re.IGNORECASE)
IMMUTABLE = "public, max-age=31536000, immutable"


class Frontend:
    def __init__(self, static_dir: pathlib.Path | None, base_path: str):
        self.dir = pathlib.Path(static_dir).resolve() if static_dir else None
        self.base_path = base_path
        self._index: tuple[float, bytes] | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.dir is not None and (self.dir / "index.html").is_file()

    def _missing(self) -> ApiError:
        where = str(self.dir) if self.dir else "CURATOR_STATIC_DIR"
        return ApiError("not_found", f"前端资源没有打包进来：找不到 {where}/index.html")

    def index_html(self) -> bytes:
        path = self.dir / "index.html"
        mtime = path.stat().st_mtime
        with self._lock:
            if self._index is None or self._index[0] != mtime:
                self._index = (mtime, self._inject(path.read_bytes()))
            return self._index[1]

    def _inject(self, html: bytes) -> bytes:
        base = json.dumps(self.base_path).replace("<", "\\u003c").encode()
        snippet = b"<script>window.__CURATOR_BASE__=" + base + b";</script>"
        if b"<base " not in html.lower():
            href = (self.base_path + "/").replace('"', "%22").encode()
            snippet = b'<base href="' + href + b'">' + snippet
        m = _HEAD_RE.search(html)
        if m is None:
            return snippet + html
        return html[: m.end()] + snippet + html[m.end():]

    def index_response(self) -> Response:
        return HTMLResponse(self.index_html(), headers={"Cache-Control": "no-cache"})

    def serve(self, rel: str) -> Response:
        if not self.available:
            raise self._missing()
        rel = rel.lstrip("/")
        if rel in ("", "index.html"):
            return self.index_response()
        candidate = (self.dir / rel).resolve()
        try:
            candidate.relative_to(self.dir)
        except ValueError:
            raise ApiError("not_found", "这个地址不存在") from None
        if candidate.is_file():
            cache = IMMUTABLE if rel.startswith("assets/") else "no-cache"
            media = mimetypes.guess_type(candidate.name)[0]
            return FileResponse(candidate, media_type=media, headers={"Cache-Control": cache})
        if rel.startswith("assets/") or os.path.splitext(rel)[1].lower() in _ASSET_EXT:
            raise ApiError("not_found", "这个静态文件不存在")
        return self.index_response()


def install(app: FastAPI, static_dir: pathlib.Path | None, base_path: str) -> Frontend:
    front = Frontend(static_dir, base_path)
    new_task = f"{base_path}/tasks/new"

    def entry(request: Request):
        if deeplink.is_entry(request.query_params):
            query = request.url.query
            return RedirectResponse(new_task + (f"?{query}" if query else ""), status_code=302)
        return None

    def root(request: Request):
        redirect = entry(request)
        if redirect is not None:
            return redirect
        if base_path and request.url.path == base_path:
            query = request.url.query
            return RedirectResponse(base_path + "/" + (f"?{query}" if query else ""),
                                    status_code=302)
        return front.serve("")

    def any_path(request: Request, path: str):
        if path.split("/", 1)[0] in ("api", "events"):
            raise ApiError("not_found", "这个接口不存在（或还没有实现）")
        return front.serve(path)

    for route in dict.fromkeys((base_path, base_path + "/")):
        if route:
            app.add_api_route(route, root, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route(base_path + "/{path:path}", any_path, methods=["GET", "HEAD"],
                      include_in_schema=False)
    return front
