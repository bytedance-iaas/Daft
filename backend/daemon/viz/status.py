"""Whether the visualizer can open a registered dataset (C4 2.4.0 ``VizStatus``, design doc 18 §5.0).

LeRobot v2 / v3 and mcap have a reader in this phase; an mcap dataset also needs its field mapping
confirmed (D62, design doc 18 §6.4 step 7) - until then the visualize page greys it out and says why.
Lance waits for its reader (phase two, design doc 18 §10).
"""
from __future__ import annotations

from ..repo import protocol as P
from ..repo.extras import dataset_format

#: the C4 ``DatasetFormat`` values a reader serves, and which reader
READERS = {"lerobot_v2": "lerobot", "lerobot_v3": "lerobot", "mcap": "mcap"}


def viz_status(ds: P.Dataset) -> dict:
    """C4 ``VizStatus`` of a registration."""
    fmt = dataset_format(ds.preflight if isinstance(ds.preflight, dict) else {})
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
    if dataset_format(ds.preflight if isinstance(ds.preflight, dict) else {}) != "mcap":
        return None
    mapping = ds.viz_mapping if isinstance(ds.viz_mapping, dict) else None
    if mapping is None:
        return {"state": "none", "version": int(ds.viz_mapping_version or 0), "updated_at": None,
                "name": None}
    name = mapping.get("name")
    return {"state": "confirmed", "version": int(ds.viz_mapping_version or 0),
            "updated_at": ds.viz_mapping_updated_at, "name": name if isinstance(name, str) else None}
