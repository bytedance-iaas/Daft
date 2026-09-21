"""摄入 schema 校验(借 lerobot-doctor 思路,文档中的 M1 的拒收关卡)。

托管服务收客户数据:必需字段缺失/维度不一致/时间戳乱序 → **拒收并报清晰错误**,
绝不静默吞下(错误消息说清楚缺什么/哪条错,客户能照着修)。
"""
from __future__ import annotations

import os

import numpy as np


class IngestValidationError(ValueError):
    """摄入校验失败:消息里说清楚缺什么/哪条错。"""


_REQUIRED_INFO_KEYS = ("codebase_version", "fps", "features", "data_path", "chunks_size")


def validate_info(info: dict, dataset_dir: str = "<unknown>") -> None:
    """meta/info.json 结构校验。"""
    missing = [k for k in _REQUIRED_INFO_KEYS if k not in info]
    if missing:
        raise IngestValidationError(
            f"{dataset_dir}: info.json 缺少必需字段 {missing}(LeRobot 元数据不完整)")
    if not isinstance(info["fps"], (int, float)) or info["fps"] <= 0:
        raise IngestValidationError(
            f"{dataset_dir}: info.json fps 非法: {info['fps']!r}(应为正数)")
    if "action" not in info["features"]:
        raise IngestValidationError(
            f"{dataset_dir}: features 里没有 'action'(机器人数据必须有动作通道);"
            f"现有 features: {sorted(info['features'])}")


def validate_episode_row(row: dict) -> None:
    """单条 episode 的结构校验(M1 产出的行,列名见 ingest.schema)。"""
    eid = row.get("episode_id", "<no id>")

    action = row.get("action")
    if action is None or len(action) == 0:
        raise IngestValidationError(f"{eid}: action 缺失或为空")
    action = np.asarray(action)
    if action.ndim != 2:
        raise IngestValidationError(
            f"{eid}: action 应为 [T, dim] 二维,got shape={action.shape}")
    if not np.issubdtype(action.dtype, np.floating):
        raise IngestValidationError(f"{eid}: action dtype 应为浮点,got {action.dtype}")
    if not np.isfinite(action).all():
        bad = int(np.count_nonzero(~np.isfinite(action)))
        raise IngestValidationError(f"{eid}: action 含 {bad} 个 NaN/Inf")

    ts = row.get("timestamps")
    if ts is None or len(ts) != len(action):
        raise IngestValidationError(
            f"{eid}: timestamps 长度({None if ts is None else len(ts)})"
            f" != action 帧数({len(action)})")
    dt = np.diff(np.asarray(ts, dtype=np.float64))
    if len(dt) and dt.min() <= 0:
        k = int(np.argmin(dt))
        raise IngestValidationError(
            f"{eid}: 时间戳非严格递增(帧 {k}→{k+1}: {ts[k]:.6f}→{ts[k+1]:.6f})")

    state = row.get("proprio_state")
    if state is not None and len(state) != len(action):
        raise IngestValidationError(
            f"{eid}: proprio_state 帧数({len(state)}) != action 帧数({len(action)})")

    from . import dsfs
    for cam, v in (row.get("video") or {}).items():
        if not dsfs.exists(v["path"]):
            raise IngestValidationError(f"{eid}: 视频文件不存在: {cam} → {v['path']}")
        if not (0.0 <= v["from_ts"] < v["to_ts"]):
            raise IngestValidationError(
                f"{eid}: 视频时间边界非法: {cam} [{v['from_ts']}, {v['to_ts']})")

    fps = row.get("fps")
    if not fps or fps <= 0:
        raise IngestValidationError(f"{eid}: fps 非法: {fps!r}")


def validate_rows(rows: list[dict]) -> None:
    """整数据集行级校验 + 跨行一致性(action 维度全数据集应一致)。"""
    if not rows:
        raise IngestValidationError("数据集为空(0 条 episode)")
    dims = set()
    for row in rows:
        validate_episode_row(row)
        dims.add(np.asarray(row["action"]).shape[1])
    if len(dims) > 1:
        raise IngestValidationError(
            f"action 维度跨 episode 不一致: {sorted(dims)}(同一数据集应同构)")


def stats_prior_warnings(dataset_dir: str, dark_mean: float = 0.05) -> list[str]:
    """零成本先验预警(B4):读 meta/stats.json(v2 全局统计),图像通道均值近黑
    → 疑似占位/死相机。仅提示进报告,不判决(判决走逐条检查)。"""
    from . import dsfs

    p = dsfs.join(dataset_dir, "meta", "stats.json")
    if not dsfs.exists(p):
        return []
    warns = []
    try:
        stats = dsfs.read_json(p)
    except Exception:  # noqa: BLE001
        return [f"meta/stats.json 无法解析"]
    for key, v in stats.items():
        if not key.startswith("observation.images"):
            continue
        try:
            chans = [c[0][0] if isinstance(c, list) else c for c in v["mean"]]
            m = sum(float(x) for x in chans) / len(chans)
            if m < dark_mean:
                warns.append(f"{key}: 全集平均亮度 {m:.3f}(近黑)——疑似占位/死相机通道")
        except Exception:  # noqa: BLE001
            continue
    return warns
