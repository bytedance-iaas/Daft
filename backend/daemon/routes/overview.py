"""``{base}/api/v1/overview`` - the overview page in one call (D36; see :mod:`daemon.overview`)."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import overview
from .common import principal, runtime

router = APIRouter()


@router.get("/overview")
def get_overview(request: Request):
    rt, owner = runtime(request), principal(request).owner_id
    return overview.build(rt.repo, owner=owner, now=rt.clock(),
                          tz_offset_minutes=rt.settings.tz_offset_minutes)
