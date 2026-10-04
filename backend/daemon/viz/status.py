"""Whether the visualizer can open a registered dataset (C4 2.4.0 ``VizStatus``, design doc 18 §5.0).

LeRobot v2 / v3 and mcap have a reader in this phase; an mcap dataset also needs its field mapping
confirmed (D62, design doc 18 §6.4 step 7) - until then the visualize page greys it out and says why.
Lance waits for its reader (phase two, design doc 18 §10).

An mcap dataset is one whose preflight found mcap episode files, whether or not the check reader could
read them with the site's default topics (``format.supported`` false, "找不到必需的动作 topic"): the
mapping is exactly what makes such a dataset readable, so the visualizer takes it as mcap. Likewise a
LeRobot dataset the check reader refuses (Galaxea's action split into ``action.left_arm`` … with no
``action`` column) is LeRobot to the visualizer: its reader needs info.json, the data and the videos,
not the columns the checks want (F13.8, design doc 18 §9.6).
"""
from __future__ import annotations

from ..repo import protocol as P
from ..repo.extras import dataset_format

#: the C4 ``DatasetFormat`` values a reader serves, and which reader
READERS = {"lerobot_v2": "lerobot", "lerobot_v3": "lerobot", "mcap": "mcap"}


def is_mcap(preflight: dict | None) -> bool:
    """The preflight found mcap episode files (readable by the checks with the defaults or not)."""
    fmt = preflight.get("format") if isinstance(preflight, dict) else None
    return isinstance(fmt, dict) and fmt.get("kind") == "mcap"


def lerobot_version(preflight: dict | None) -> str | None:
    """The LeRobot layout (v2 / v3) the preflight found, readable by the checks or not."""
    fmt = preflight.get("format") if isinstance(preflight, dict) else None
    if isinstance(fmt, dict) and fmt.get("kind") == "lerobot" and fmt.get("version") in ("v2", "v3"):
        return fmt["version"]
    return None


def viz_format(preflight: dict | None) -> str:
    """``dataset_format`` as the visualizer sees it: mcap and LeRobot whether the checks read them or not."""
    if is_mcap(preflight):
        return "mcap"
    version = lerobot_version(preflight)
    if version is not None:
        return f"lerobot_{version}"
    return dataset_format(preflight if isinstance(preflight, dict) else {})


def viz_status(ds: P.Dataset) -> dict:
    """C4 ``VizStatus`` of a registration."""
    fmt = viz_format(ds.preflight if isinstance(ds.preflight, dict) else {})
    if fmt not in READERS:
        reason = ("Lance 数据集的可视化读取器在第二期" if fmt == "lance"
                  else "这个数据集的格式不支持可视化")
        return {"state": "unsupported", "reason": reason}
    if fmt == "mcap" and not ds.viz_mapping:
        return {"state": "mapping_pending",
                "reason": "mcap 数据集要先确认字段映射：到数据集详情的「mcap 配置」确认"}
    return {"state": "ready", "reason": None}


def mapping_info(ds: P.Dataset) -> dict | None:
    """C4 ``DatasetMappingInfo``; None for a dataset that is not mcap."""
    if not is_mcap(ds.preflight if isinstance(ds.preflight, dict) else None):
        return None
    mapping = ds.viz_mapping if isinstance(ds.viz_mapping, dict) else None
    if mapping is None:
        return {"state": "none", "version": int(ds.viz_mapping_version or 0), "updated_at": None,
                "name": None}
    name = mapping.get("name")
    return {"state": "confirmed", "version": int(ds.viz_mapping_version or 0),
            "updated_at": ds.viz_mapping_updated_at, "name": name if isinstance(name, str) else None}
