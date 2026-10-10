"""A camera whose calibration is more likely wrong than its episodes (design doc 25 §7.4, D86).

Over a task's records: on one camera the position sub-item's constant offset - the median residual vector of its
points, declared projection minus the gripper in the picture - points the same way and has a similar size on many
episodes. When the CPU found the position off on at least ``MIN_EPISODES`` episodes of a camera, at least
``MIN_SHARE`` of the episodes it measured there, each within ``MAX_ANGLE_DEG`` of their mean direction and with
sizes whose coefficient of variation is below ``MAX_CV``, the task has one dataset-level ``calibration_suspect``
for that camera: more likely the extrinsics, the TCP offset or an assumed value than each episode's own data.
Its readings carry the camera, the episodes, the direction and size, the PnP corrections the diagnosis fitted on
those episodes (design doc 12 §8.6) and the calibration values that were assumptions.

Nothing here changes a record: the per-episode findings stay as the checks wrote them, the dataset-level finding
names the episodes it covers.
"""
from __future__ import annotations

import math
from statistics import median

MIN_EPISODES = 5
MIN_SHARE = 0.6
MAX_ANGLE_DEG = 30.0
MAX_CV = 0.5

#: the image directions in eight sectors, from +u (right) turning towards +v (down)
_DIRECTIONS = ("右", "右下", "下", "左下", "左", "左上", "上", "右上")


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _offset(camera: dict) -> tuple[str, tuple[float, float] | None]:
    """(the position sub-item's CPU status, its constant offset in pixels: the median over the points the CPU
    could assess) of one camera of a record."""
    pos = ((camera.get("subitems") or {}).get("position_2d") or {}) if isinstance(camera, dict) else {}
    status = str(pos.get("status") or "")
    us, vs = [], []
    for point in (pos.get("points") or {}).values():
        if not isinstance(point, dict) or point.get("status") not in ("ok", "suspect"):
            continue
        m = point.get("metrics") or {}
        u, v = _num(m.get("median_u_px")), _num(m.get("median_v_px"))
        if u is not None and v is not None:
            us.append(u)
            vs.append(v)
    return status, ((median(us), median(vs)) if us else None)


def _mean_direction(vectors: list[tuple[float, float]]) -> float:
    s = sum(v / math.hypot(u, v) for u, v in vectors if math.hypot(u, v) > 0)
    c = sum(u / math.hypot(u, v) for u, v in vectors if math.hypot(u, v) > 0)
    return math.degrees(math.atan2(s, c))


def _apart(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def direction_zh(deg: float) -> str:
    """The image direction of an angle from +u towards +v, in words."""
    return _DIRECTIONS[int(((deg % 360.0) + 22.5) // 45.0) % 8]


def _pnp(details: dict, camera: str) -> dict | None:
    for h in details.get("diagnosis") or []:
        if isinstance(h, dict) and h.get("hypothesis") == "extrinsics_error" and h.get("camera_id") == camera:
            fitted = h.get("fitted") or {}
            dt, dr = _num(fitted.get("delta_translation_mm")), _num(fitted.get("delta_rotation_deg"))
            if dt is not None and dr is not None:
                return {"translation_mm": dt, "rotation_deg": dr, "supported": bool(h.get("supported"))}
    return None


def _assumed(details: dict, camera: str) -> list[str]:
    """The calibration values the trajectory took as assumptions for ``camera``: a declaration's items of this
    camera and of the tool (design doc 25 §3.4; every camera's when the record does not say which source is
    which), a handheld gripper's calibration fields (design doc 22 §7)."""
    src = details.get("trajectory_source") or {}
    if not isinstance(src, dict):
        return []
    if src.get("kind") == "derived":
        return [str(a) for a in (src.get("calibration") or {}).get("assumed") or []]
    own = (src.get("camera_sources") or {}).get(camera)
    out = []
    for a in map(str, src.get("assumed") or []):
        if a.startswith("calibration.cameras."):
            if own is None or a == f"calibration.cameras.{own}" or a.startswith(f"calibration.cameras.{own}."):
                out.append(a)
        elif a.startswith(("calibration.", "tool")):
            out.append(a)
    return out


def suspects(records: dict[int, dict]) -> list[dict]:
    """``[{"camera", "message", "readings"}]``: one per camera that meets the module docstring's rule, over the
    task's judged records (``episode -> record``, C2 2.0 with ``details``)."""
    measured: dict[str, dict[int, tuple[str, tuple[float, float] | None]]] = {}
    for ep, rec in records.items():
        details = (rec or {}).get("details") or {}
        for cid, cam in (details.get("cameras") or {}).items():
            status, offset = _offset(cam)
            if status in ("ok", "suspect"):
                measured.setdefault(str(cid), {})[int(ep)] = (status, offset)
    out = []
    for cid, by_ep in sorted(measured.items()):
        off = {ep: o for ep, (st, o) in by_ep.items() if st == "suspect" and o is not None and math.hypot(*o) > 0}
        if len(off) < MIN_EPISODES:
            continue
        mean = _mean_direction(list(off.values()))
        members = [ep for ep, o in off.items() if _apart(math.degrees(math.atan2(o[1], o[0])), mean) < MAX_ANGLE_DEG]
        if members:
            mean = _mean_direction([off[ep] for ep in members])
            members = sorted(ep for ep, o in off.items()
                             if _apart(math.degrees(math.atan2(o[1], o[0])), mean) < MAX_ANGLE_DEG)
        sizes = [math.hypot(*off[ep]) for ep in members]
        if len(members) < MIN_EPISODES or len(members) < MIN_SHARE * len(by_ep):
            continue
        mean_size = sum(sizes) / len(sizes)
        cv = math.sqrt(sum((x - mean_size) ** 2 for x in sizes) / len(sizes)) / mean_size
        if cv >= MAX_CV:
            continue
        spread = median(_apart(math.degrees(math.atan2(off[ep][1], off[ep][0])), mean) for ep in members)
        fits = [p for p in (_pnp(records[ep].get("details") or {}, cid) for ep in members) if p is not None]
        assumed = sorted({a for ep in members for a in _assumed(records[ep].get("details") or {}, cid)})
        readings = {"camera": cid, "episodes": members, "measured_episodes": len(by_ep),
                    "share": round(len(members) / len(by_ep), 3),
                    "direction_deg": round(mean, 1), "direction_spread_deg": round(spread, 1),
                    "size_px": {"median": round(median(sizes), 1), "cv": round(cv, 3)}}
        if fits:
            readings["pnp"] = {"episodes": len(fits), "supported": sum(p["supported"] for p in fits),
                               "translation_mm": round(median(p["translation_mm"] for p in fits), 1),
                               "rotation_deg": round(median(p["rotation_deg"] for p in fits), 2)}
        if assumed:
            readings["assumed"] = assumed
        msg = (f"相机 {cid}：{len(members)} / {len(by_ep)} 条的位置有同向的恒定偏差（声明的投影比画面里的夹爪偏"
               f"{direction_zh(mean)}，中位 {median(sizes):.0f} px）")
        if fits:
            msg += f"，PnP 修正中位 {readings['pnp']['translation_mm']:.0f} mm、{readings['pnp']['rotation_deg']:.1f}°"
        msg += "：多半是外参、TCP 偏移或假设值，不是逐条的数据问题"
        if assumed:
            msg += f"（标定里按假设值的：{'、'.join(assumed)}）"
        out.append({"camera": cid, "message": msg, "readings": readings})
    return out
