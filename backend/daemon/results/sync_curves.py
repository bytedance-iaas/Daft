"""One episode's video-action sync curves, for the Episode tab (design doc 03 §6, F6.2).

C4 ``getEpisodeSyncCurves`` (``GET /tasks/{id}/episodes/{index}/sync-curves``): per
camera, how much the picture moves (optical-flow energy) and how much the arm moves
(joint speed) over time, the cross-correlation of the two over the lag, and where the
check's reading sits on it - v1's sync plot as data, drawn by the page.

The frame stage keeps the curves in ``checks/video_action_sync/curves/ep<NNNNNN>.json``
(``pipeline.sync_plots``: by default only for episodes worth a look - not aligned, or
with flagged or unreadable cameras). The readings (lag, peak, code, trusted) are the
record the revision saw; the cross-correlation curve is computed with the very
preprocessing of the check (``export.sync_plots._xcorr_curve``: static head and tail
trimmed), so the reading sits on its curve. Each series has at most 600 points.

404 ``not_found`` with ``details.reason``: ``module_not_run`` (the task did not select
the sync check), ``no_record`` (the episode never reached it, or it failed there),
``no_curves`` (nothing was kept for it).
"""
from __future__ import annotations

import math

from ..errors import ApiError
from .episode import skipped_episodes
from .files import cached_json, json_safe
from .revision import Revision

MODULE = "video_action_sync"
CURVES_DIR = f"checks/{MODULE}/curves"
MAX_POINTS = 600
#: the lags drawn: the scan window of the check (``global_lag`` max_lag_s)
WINDOW_S = 2.0


def _missing(rev: Revision, episode: int, reason: str, message: str) -> ApiError:
    return ApiError("not_found", message, details={"episode_index": episode,
                                                   "revision": rev.number, "reason": reason})


def _cameras(doc: dict) -> dict[str, dict]:
    """``{camera: {t, flow, speed, ...}}``: the multi-camera format (2026-08-07 on) or the
    single-camera one before it (``export.sync_plots._normalize_curves``)."""
    if isinstance(doc.get("cameras"), dict):
        return {str(k): v for k, v in doc["cameras"].items() if isinstance(v, dict)}
    if "t" in doc:
        det = doc.get("detail") if isinstance(doc.get("detail"), dict) else {}
        return {"camera": {"t": doc.get("t"), "flow": doc.get("flow"), "speed": doc.get("speed"),
                           "lag_s": det.get("lag_s"), "corr_peak": det.get("corr_peak"),
                           "code": det.get("code")}}
    return {}


def _every(n: int) -> int:
    return max(1, math.ceil(n / MAX_POINTS))


def _rounded(values, nd: int) -> list:
    out = []
    for v in values:
        f = float(v)
        out.append(round(f, nd) if math.isfinite(f) else None)
    return out


def _camera(name: str, cv: dict, reading: dict) -> dict | None:
    import numpy as np

    from curation.export.sync_plots import _xcorr_curve

    try:
        t = np.asarray(cv.get("t") or [], dtype=float)
        flow = np.asarray(cv.get("flow") or [], dtype=float)
        speed = np.asarray(cv.get("speed") or [], dtype=float)
    except (TypeError, ValueError):
        return None
    n = min(len(t), len(flow), len(speed))
    if n < 2:
        return None
    t, flow, speed = t[:n], flow[:n], speed[:n]
    lag_s = reading.get("lag_s", cv.get("lag_s"))
    out = {"camera": name, "lag_s": lag_s,
           "corr_peak": reading.get("corr_peak", cv.get("corr_peak")),
           "code": reading.get("code", cv.get("code")), "trusted": reading.get("trusted"),
           "lags": [], "xcorr": [], "peak": None}
    step = _every(n)
    out.update(t=_rounded(t[::step], 3), flow=_rounded(flow[::step], 5),
               speed=_rounded(speed[::step], 6))
    try:
        lags, xc = _xcorr_curve(t, flow, speed)
    except Exception:  # noqa: BLE001 - a curve that cannot be correlated is still drawn
        return out
    win = np.abs(lags) <= WINDOW_S
    lags, xc = lags[win], xc[win]
    if len(lags):
        step = _every(len(lags))
        out.update(lags=_rounded(lags[::step], 3), xcorr=_rounded(xc[::step], 4))
        x = None if lag_s is None or isinstance(lag_s, bool) else float(lag_s)
        # the reading on the drawn curve: its x is the check's, its y read off the curve;
        # a reading outside the drawn window gets no marker (v1's _peak_marker)
        if x is not None and math.isfinite(x) and lags.min() <= x <= lags.max():
            out["peak"] = {"lag_s": round(x, 3), "corr": round(float(np.interp(x, lags, xc)), 4)}
    return out


def sync_curves(rev: Revision, episode: int) -> dict:
    ep = int(episode)
    name = f"ep{ep:06d}"
    if rev.entries().get(ep) is None:
        missing = skipped_episodes(rev).get(ep)
        why = "它的源文件缺失，没有参与质检" if missing is not None else "不在这个任务的质检范围里"
        raise _missing(rev, ep, "source_missing" if missing is not None else "episode_missing",
                       f"结果版本 r{rev.number} 里没有 {name}（{why}）")
    if MODULE not in rev.modules():
        raise _missing(rev, ep, "module_not_run", "这个任务没有勾选「视频-动作同步」，没有同步曲线")
    rec = rev.record(MODULE, ep)
    if rec is None:
        raise _missing(rev, ep, "no_record", f"{name} 没有走到视频-动作同步这一档（前面已被判废），没有同步曲线")
    if rec.get("verdict") == "error":
        raise _missing(rev, ep, "no_record", f"视频-动作同步在 {name} 上执行出错，没有同步曲线；补跑成功后才有")
    details = rec.get("details") if isinstance(rec.get("details"), dict) else {}
    try:
        doc = cached_json(rev.store.docs, rev.run_dir / CURVES_DIR / f"{name}.json")
    except FileNotFoundError:
        if details.get("verdict") == "aligned" and not details.get("flagged_cameras") \
                and not details.get("abstained_cameras"):
            msg = f"{name} 同步正常：默认只为值得留意的条目（没对齐、有相机被标注或测不准）保存同步曲线"
        else:
            msg = f"{name} 的同步曲线不在本地工作目录里（这一档没有保存，或测量时没有可用的相机）"
        raise _missing(rev, ep, "no_curves", msg) from None
    except ValueError:
        raise _missing(rev, ep, "no_curves", f"{name} 的同步曲线文件读不了") from None
    doc = doc if isinstance(doc, dict) else {}
    readings = details.get("per_camera") if isinstance(details.get("per_camera"), dict) else {}
    cameras = []
    for cam, cv in sorted(_cameras(doc).items()):
        reading = readings.get(cam) if isinstance(readings.get(cam), dict) else {}
        drawn = _camera(cam, cv, reading)
        if drawn is not None:
            cameras.append(drawn)
    if not cameras:
        raise _missing(rev, ep, "no_curves", f"{name} 的同步曲线是空的（每路相机的样本都太少）")
    tol = doc.get("lag_tol_s")
    return json_safe({"episode_index": ep, "revision": rev.number,
                      "verdict": details.get("verdict") or doc.get("verdict"),
                      "consensus_lag_s": details.get("consensus_lag_s", doc.get("consensus_lag_s")),
                      "lag_tol_s": float(tol) if isinstance(tol, (int, float)) and not isinstance(tol, bool)
                      else 0.25,
                      "window_s": WINDOW_S, "cameras": cameras})
