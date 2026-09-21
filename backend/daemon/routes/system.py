"""Probes (design doc 09, section 2.2): no authentication, at the root and under ``{base}``.

* ``/healthz`` - the process is alive; no dependency is checked.
* ``/readyz`` - database writable, master key loaded, work and scratch
  directories writable, startup reconciliation done. Any ``false`` -> 503, the
  pod takes no traffic until it recovers.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from .common import runtime

PROBES = ("/healthz", "/readyz")


def healthz():
    return {"status": "ok"}


def readyz(request: Request):
    checks = runtime(request).readiness()
    ok = all(checks.values())
    return JSONResponse({"status": "ok" if ok else "not_ready", "checks": checks},
                        status_code=200 if ok else 503, headers={"Cache-Control": "no-store"})


def probe_paths(base_path: str) -> frozenset[str]:
    paths = set(PROBES)
    if base_path:
        paths |= {base_path + p for p in PROBES}
    return frozenset(paths)


def install(app: FastAPI, base_path: str) -> None:
    for prefix in dict.fromkeys(("", base_path)):
        app.add_api_route(prefix + "/healthz", healthz, methods=["GET"], include_in_schema=False)
        app.add_api_route(prefix + "/readyz", readyz, methods=["GET"], include_in_schema=False)
