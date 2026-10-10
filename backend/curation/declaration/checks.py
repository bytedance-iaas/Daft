"""Checking a declaration when it is confirmed (design doc 25 §3.4).

Two kinds of finding, each located at a field of the declaration:

- **errors** stop the confirmation: what the Schema says, a column or topic the dataset does not have, a width that
  does not fit the layout, a joint column with another number of joints than the robot, a transform that is not
  rigid, distortion coefficients the model does not take, a handheld calibration that does not check;
- **suspects** never stop it and are kept on the declaration (``suspects``): intrinsics that stretch a picture
  unevenly (fx / fy apart by more than 2 % on the video), a principal point off the picture, an opening outside
  0 .. 0.2 m, a TCP offset beyond 0.4 m. Reports and overlays carry them as 可疑.

What needs the data itself (how many poses pair with a frame; three frames drawn for a person to look at) is the
visualizer's dataset-level overlay (design doc 25 §5.1), not this check.
"""
from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np

from . import FIXED_EXTERNAL, WRIST, mapping_of, normalize, schema_errors

#: values per pose row by layout (position + rotation)
LAYOUT_WIDTH = {"xyz_rpy_xyz_extrinsic": 6, "xyz_quat_xyzw": 7, "xyz_quat_wxyz": 7, "xyz_rotmat": 12, "xyz_rot6d": 9}
COEFFICIENTS = {"pinhole": (0,), "opencv_brown": (4, 5, 8), "opencv_fisheye": (4,)}
ASPECT_TOLERANCE = 0.02
OPENING_RANGE_M = (0.0, 0.2)
TCP_FAR_M = 0.4


@dataclasses.dataclass
class Facts:
    """What the check knows of the dataset: its format, its columns (LeRobot / Lance ``info.json`` features: key ->
    {dtype, shape, names}) or its topics (mcap: topic -> schema), and its cameras with their video size [w, h]."""
    kind: str
    columns: dict[str, dict] | None = None
    topics: dict[str, Any] | None = None
    cameras: dict[str, list[int] | None] = dataclasses.field(default_factory=dict)
    robot_joints: dict[str, int] = dataclasses.field(default_factory=dict)

    @classmethod
    def lerobot(cls, info: dict, kind: str = "lerobot") -> "Facts":
        from ..viz.lerobot_info import camera_info_of

        feats = info.get("features") if isinstance(info.get("features"), dict) else {}
        cams = {c["key"]: ([c["width"], c["height"]] if c.get("width") and c.get("height") else None)
                for c in camera_info_of(info)}
        for key, f in feats.items():                    # image features (no video) are cameras too
            if isinstance(f, dict) and f.get("dtype") == "image" and key not in cams:
                cams[key] = None
        return cls(kind=kind, columns={k: v for k, v in feats.items() if isinstance(v, dict)}, cameras=cams)

    @classmethod
    def mcap(cls, topics: dict[str, Any], sizes: dict[str, list[int] | None] | None = None,
             cameras: list[str] | None = None) -> "Facts":
        cams = {t: (sizes or {}).get(t) for t in (cameras if cameras is not None else (sizes or {}))}
        return cls(kind="mcap", topics=dict(topics), cameras=cams)


def _width(feature: dict | None) -> int | None:
    shape = (feature or {}).get("shape")
    if isinstance(shape, list) and shape and all(isinstance(x, int) for x in shape):
        return int(np.prod(shape))
    return None


def _rigid(T: Any) -> str | None:
    """Why ``T`` (4x4) is not a rigid transform, or None."""
    try:
        M = np.asarray(T, float)
    except (TypeError, ValueError):
        return "不是数值矩阵"
    if M.shape != (4, 4) or not np.isfinite(M).all():
        return "要 4×4 的有限数"
    R = M[:3, :3]
    if not np.allclose(M[3], [0, 0, 0, 1], atol=1e-6):
        return "最后一行要是 0 0 0 1"
    if not np.allclose(R.T @ R, np.eye(3), atol=1e-3) or abs(np.linalg.det(R) - 1) > 1e-3:
        return "旋转部分不是正交、行列式为 1 的矩阵"
    return None


def _K(intr: dict) -> np.ndarray | None:
    if intr.get("K") is not None:
        return np.asarray(intr["K"], float)
    f = intr.get("fx_cx_fy_cy")
    if f is not None:
        return np.array([[f[0], 0, f[1]], [0, f[2], f[3]], [0, 0, 1]], float)
    return None


def check(doc: Any, facts: Facts | None = None) -> dict:
    """``{"errors": [{field, problem}], "suspects": [{code, field, message, args?}]}`` of a declaration; ``facts``
    None checks the document alone (the Schema and what needs no dataset)."""
    errors = list(schema_errors(doc))
    suspects: list[dict] = []
    if errors or not isinstance(doc, dict):
        return {"errors": errors, "suspects": suspects}
    doc = normalize(doc)

    def err(field: str, problem: str) -> None:
        errors.append({"field": field, "problem": problem})

    def sus(code: str, field: str, message: str, **args) -> None:
        suspects.append({"code": code, "field": field, "message": message, **({"args": args} if args else {})})

    # layer 1 (an mcap dataset's mapping): the C7 rules and the dataset's topics
    mapping = mapping_of(doc)
    if mapping is not None:
        from ..viz import mcap_mapping as MM

        topics = set(facts.topics) if facts is not None and facts.topics is not None else None
        errors += [{"field": p["field"], "problem": p["problem"]} for p in MM.validate(mapping, topics)
                   if not p["problem"].startswith("schema_version")]

    def source(path: str, spec: dict, width_need: int | None = None) -> int | None:
        """Check a record's source against the dataset; its width (after the slice) when known."""
        if facts is None:
            return None
        if spec.get("key") is not None:
            if facts.columns is None:
                err(f"{path}.key", "这个数据集没有列，记录要用 topic 指明")
                return None
            feat = facts.columns.get(spec["key"])
            if feat is None:
                err(f"{path}.key", f"数据集里没有列 {spec['key']}")
                return None
            width = _width(feat)
        else:
            if facts.topics is None:
                err(f"{path}.topic", "这个数据集没有 topic，记录要用列名指明")
                return None
            if spec["topic"] not in facts.topics:
                err(f"{path}.topic", f"数据集里没有 topic {spec['topic']}")
                return None
            width = None
        sl = spec.get("slice")
        if sl is not None:
            if sl[1] <= sl[0]:
                err(f"{path}.slice", "切片的终点要大于起点")
                return None
            if width is not None and sl[1] > width:
                err(f"{path}.slice", f"切片超出了列的宽度 {width}")
                return None
            width = sl[1] - sl[0]
        if width is not None and width_need is not None and width != width_need:
            err(path, f"这一来源每行 {width} 个数，按所选布局要 {width_need} 个")
        return width

    sem = doc.get("semantics") or {}
    pose = sem.get("pose")
    if isinstance(pose, dict):
        need = LAYOUT_WIDTH[pose["layout"]]
        if pose.get("quaternion_key"):
            if not pose["layout"].startswith("xyz_quat") or pose.get("key") is None:
                err("semantics.pose.quaternion_key", "另取四元数列只用于 xyz_quat_* 布局、位置来自列的位姿")
            else:
                need = 3
                if facts is not None and facts.columns is not None:
                    q = facts.columns.get(pose["quaternion_key"])
                    if q is None:
                        err("semantics.pose.quaternion_key", f"数据集里没有列 {pose['quaternion_key']}")
                    elif _width(q) not in (None, 4):
                        err("semantics.pose.quaternion_key", "四元数列每行要 4 个数")
        source("semantics.pose", pose, need)
    joints = sem.get("joints")
    if isinstance(joints, dict):
        from ..extensions.eef_consistency import robots as RB

        n = RB.get(joints["robot"]).joints
        source("semantics.joints", joints, n)
    grip = sem.get("gripper")
    if isinstance(grip, dict):
        width = source("semantics.gripper", grip)
        if width is not None and int(grip.get("index") or 0) >= width:
            err("semantics.gripper.index", f"下标超出了列的宽度 {width}")
        cf = grip.get("closed_fraction")
        if isinstance(cf, dict) and float(cf["max"]) == float(cf["min"]):
            err("semantics.gripper.closed_fraction", "min 与 max 不能相等")

    cal = doc.get("calibration") or {}
    for src, c in sorted((cal.get("cameras") or {}).items()):
        path = f"calibration.cameras.{src}"
        if facts is not None and facts.cameras and src not in facts.cameras:
            err(path, f"数据集里没有相机 {src}")
            continue
        intr = c.get("intrinsics")
        size = (facts.cameras.get(src) if facts is not None else None) or None
        if isinstance(intr, dict):
            K = _K(intr)
            if K is None or K[0, 0] <= 0 or K[1, 1] <= 0:
                err(f"{path}.intrinsics", "fx、fy 要是正数")
            else:
                nc = len(intr.get("coefficients") or [])
                if nc not in COEFFICIENTS[intr["model"]] and not (intr["model"] == "pinhole" and nc == 0):
                    err(f"{path}.intrinsics.coefficients",
                        f"{intr['model']} 要 {' 或 '.join(map(str, COEFFICIENTS[intr['model']]))} 个畸变系数，给了 {nc} 个")
                cal_wh = intr.get("image_size_wh") or size
                sx = sy = 1.0
                if size and cal_wh:
                    sx, sy = size[0] / cal_wh[0], size[1] / cal_wh[1]
                    if abs(sx / sy - 1) > ASPECT_TOLERANCE:
                        sus("intrinsics_scaling", f"{path}.intrinsics",
                            f"标定图 {cal_wh[0]}×{cal_wh[1]} 到视频 {size[0]}×{size[1]} 横竖缩放不一致（{sx:.3f} 对 {sy:.3f}）",
                            calibration_wh=list(cal_wh), video_wh=list(size))
                fx, fy = K[0, 0] * sx, K[1, 1] * sy
                if abs(fx / fy - 1) > ASPECT_TOLERANCE:
                    sus("intrinsics_aspect", f"{path}.intrinsics",
                        f"换到视频尺寸后 fx 与 fy 差 {abs(fx / fy - 1) * 100:.1f}%（超过 2%）", fx=round(fx, 3), fy=round(fy, 3))
                if cal_wh:
                    cx, cy = K[0, 2], K[1, 2]
                    if not (0 <= cx <= cal_wh[0] and 0 <= cy <= cal_wh[1]):
                        sus("intrinsics_center", f"{path}.intrinsics", f"主点 ({cx:.1f}, {cy:.1f}) 在画面之外",
                            cx=round(float(cx), 3), cy=round(float(cy), 3))
        ext = c.get("extrinsics")
        if isinstance(ext, dict):
            if ext["mode"] == "static" and ext.get("T_reference_camera") is not None:
                why = _rigid(ext["T_reference_camera"])
                if why:
                    err(f"{path}.extrinsics.T_reference_camera", why)
            elif ext["mode"] == "camera_tcp":
                why = _rigid(ext["T_camera_tcp"])
                if why:
                    err(f"{path}.extrinsics.T_camera_tcp", why)
                if c.get("mount") != WRIST:
                    err(f"{path}.extrinsics", "相机到工具的固定变换只用于腕部相机")
            elif ext["mode"] == "column":
                if c.get("mount") != FIXED_EXTERNAL:
                    err(f"{path}.extrinsics", "逐行的相机位姿列只用于第三视角相机")
                if facts is not None and facts.columns is not None:
                    feat = facts.columns.get(ext["key"])
                    if feat is None:
                        err(f"{path}.extrinsics.key", f"数据集里没有列 {ext['key']}")
                    elif _width(feat) not in (None, 6):
                        err(f"{path}.extrinsics.key", "外参列每行要 6 个数（x y z roll pitch yaw）")
                elif facts is not None:
                    err(f"{path}.extrinsics.key", "这个数据集没有列")
        H = c.get("media_transform")
        if isinstance(H, list):
            M = np.asarray(H, float)
            if not np.isfinite(M).all() or abs(np.linalg.det(M)) < 1e-12:
                err(f"{path}.media_transform", "要可逆的 3×3 矩阵")

    tool = cal.get("tool")
    if isinstance(tool, dict):
        op = tool.get("max_opening_m")
        if op is not None and not (OPENING_RANGE_M[0] < float(op) <= OPENING_RANGE_M[1]):
            sus("opening_range", "calibration.tool.max_opening_m", f"最大开口 {op} m 不在 0–0.2 m 之间", opening_m=op)
        tcp = tool.get("tcp_offset_m")
        if tcp is not None and float(np.linalg.norm(tcp)) > TCP_FAR_M:
            sus("tcp_offset_far", "calibration.tool.tcp_offset_m", f"TCP 偏移 {np.linalg.norm(tcp):.3f} m，超过 0.4 m")
        if (tool.get("axes") or {}).get("from") == "tcp" and tcp is None:
            err("calibration.tool.axes.from", "坐标轴从 TCP 画起，但没有给 TCP 偏移")
        if bool(tool.get("finger_axis")) != bool(tool.get("max_opening_m")) and (tool.get("finger_axis") or op):
            err("calibration.tool", "手指方向与最大开口要一起给")
    hh = cal.get("handheld")
    if isinstance(hh, dict):
        from ..extensions.eef_consistency.adapters import umi_mcap as X

        try:
            X.check_calibration(hh["calibration"], "the handheld calibration")
        except X.ExportError as exc:
            err("calibration.handheld.calibration", str(exc)[:300])
        else:
            rng = hh["calibration"].get("gripper_range") or {}
            for hand, r in sorted(rng.items()):
                if isinstance(r, dict) and r.get("max_width_m") is not None and \
                        not (OPENING_RANGE_M[0] < float(r["max_width_m"]) <= OPENING_RANGE_M[1]):
                    sus("opening_range", f"calibration.handheld.calibration.gripper_range.{hand}",
                        f"{hand} 的最大开口 {r['max_width_m']} m 不在 0–0.2 m 之间")

    timing = doc.get("timing")
    if isinstance(timing, dict) and facts is not None and facts.columns is not None:
        for i, clock in enumerate(timing.get("source_clocks") or []):
            if clock["key"] not in facts.columns:
                err(f"timing.source_clocks.{i}.key", f"数据集里没有列 {clock['key']}")
        k = timing.get("source_state_index_key")
        if k and k not in facts.columns:
            err("timing.source_state_index_key", f"数据集里没有列 {k}")
    return {"errors": errors, "suspects": suspects}
