"""``{base}/api/v1/overview`` - the overview page in one call (D36; see :mod:`daemon.overview`).

``?days=7|30|90|365`` picks the period of ``recent`` (C4 1.10.0); anything else is 400.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from .. import overview
from ..errors import ApiError
from .common import principal, runtime

router = APIRouter()


@router.get("/overview")
def get_overview(request: Request, days: int = Query(overview.DEFAULT_DAYS)):
    if days not in overview.RANGES:
        raise ApiError("validation_failed",
                       "时间范围只能是近 7 天、近 1 月、近 3 月或近 1 年（days 取 7、30、90 或 365）",
                       details={"errors": [{"field": "days", "problem": "not one of 7, 30, 90, 365"}]})
    rt, owner = runtime(request), principal(request).owner_id
    return overview.build(rt.repo, owner=owner, now=rt.clock(),
                          tz_offset_minutes=rt.settings.tz_offset_minutes, days=days)
