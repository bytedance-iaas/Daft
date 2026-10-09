"""UMI absolute hand trajectories in the current camera, never incremental deltas.

A hand's poses and its wrist camera's poses may be missing on some frames (a VIO stream slower than the
video, a dropped message): nothing is drawn there and trails break. :func:`fill_gaps` bridges short gaps
for whoever reads the sample (design doc 22 §5.2); the bundle itself is never filled. Two handheld
grippers may each be in their own VIO world (``umi.world_frames: per_hand``): each camera only ever
draws its own hand, in the world its calibration names.
"""
from __future__ import annotations

import numpy as np

from . import geometry as G, history

PROMPT_VERSION = "umi-action-prompt/9"  # 9: the MARKED video only; 8: orientation only
COLORS = ((255, 160, 0), (210, 60, 255))  # BGR, stable hand order
#: the checks bridge gaps of up to this many sample intervals (30 fps: 100 ms); the player offers this range
GAP_STEPS = 3
GAP_RANGE_STEPS = (2, 5)
AXIS_M = 0.30  # the drawn approach line, metres ahead of the TCP (long enough to read at the model's 448 px)


def load_hands(sample, rows) -> None:
    """Validate the additive UMI input and retain numeric arrays, including missing samples."""
    cfg = sample.sample.get("umi")
    if cfg is None:
        if any("hands" in row for row in rows):
            raise ValueError("hands require sample.umi")
        return
    if sample.t is None or sample.n_frames < 2 or np.any(np.diff(sample.t) <= 0):
        raise ValueError("UMI requires at least two strictly increasing timestamps")
    owners = cfg["camera_hands"]
    if set(owners) != set(sample.cameras):
        raise ValueError("umi.camera_hands must name every camera exactly once")
    ids = set(owners.values())
    if not 1 <= len(ids) <= 2:
        raise ValueError("UMI supports one or two hands")
    for hand, r in (cfg.get("gripper_range") or {}).items():
        if hand not in ids:
            raise ValueError(f"umi.gripper_range names {hand}, which no camera belongs to")
        if not (np.isfinite([r["min_width_m"], r["max_width_m"]]).all() and 0 <= r["min_width_m"] < r["max_width_m"]):
            raise ValueError(f"umi.gripper_range.{hand}: min_width_m must be below max_width_m")
    per_hand = cfg.get("world_frames") == "per_hand"
    world: dict[str, str] = {}                    # hand -> the reference frame its poses are in
    for cid, cam in sample.cameras.items():
        cal = cam.calibration(sample)
        if cam.mount != "wrist" or cal is None:
            raise ValueError(f"{cid}: UMI requires a calibrated wrist camera")
        hand = owners[cid]
        frame = cal["reference_frame"] if per_hand else sample.reference_frame
        if world.setdefault(hand, frame) != frame:
            raise ValueError(f"{cid}: the cameras of {hand} name different worlds")
    for hand in sorted(ids):
        poses = np.full((sample.n_frames, 4, 4), np.nan)
        widths = np.full(sample.n_frames, np.nan)
        for i, row in enumerate(rows):
            if set(row.get("hands", {})) != ids:
                raise ValueError(f"frame {i}: hands must match umi.camera_hands")
            data = row["hands"][hand]
            if data is None:
                continue
            pose = data["pose"]
            if (pose["pose_type"] != "absolute" or pose["relative_to"] is not None
                    or pose["reference_frame"] != world[hand] or pose["frame_id"] != hand):
                raise ValueError(f"frame {i}/{hand}: expected absolute pose in {world[hand]}")
            q = np.asarray(pose["quaternion_xyzw"])
            if not np.isfinite(q).all() or abs(np.linalg.norm(q) - 1) > 1e-5:
                raise ValueError(f"frame {i}/{hand}: quaternion must be finite and unit length")
            T = G.pose_matrix(pose["position_m"], q)
            if not G.is_rigid(T):
                raise ValueError(f"frame {i}/{hand}: pose must be finite and rigid")
            poses[i] = T
            if data["opening_m"] is not None:
                widths[i] = data["opening_m"]
        sample.hand_poses[hand] = poses
        sample.hand_openings[hand] = widths


def default_gap_s(sample) -> float | None:
    """The gap the checks bridge: GAP_STEPS sample intervals (None without a timeline)."""
    step = history._step(sample.t) if sample.t is not None else np.inf
    return round(GAP_STEPS * step, 6) if np.isfinite(step) else None


def _bridge(t: np.ndarray, T: np.ndarray, limit: float) -> list[int]:
    """Fill the NaN poses between two known ones at most ``limit`` seconds apart (position linear, rotation
    slerp), in place; the frames filled."""
    from scipy.spatial.transform import Rotation, Slerp

    known = np.flatnonzero(np.isfinite(T[:, 0, 0]) & np.isfinite(t))
    filled: list[int] = []
    for a, b in zip(known[:-1], known[1:]):
        if b - a < 2 or t[b] - t[a] > limit:
            continue
        inner = np.arange(a + 1, b)
        alpha = (t[inner] - t[a]) / (t[b] - t[a])
        rot = Slerp([0.0, 1.0], Rotation.from_matrix(np.stack([T[a, :3, :3], T[b, :3, :3]])))(alpha).as_matrix()
        T[inner] = G.se3(rot, (1 - alpha)[:, None] * T[a, :3, 3] + alpha[:, None] * T[b, :3, 3])
        filled += inner.tolist()
    return filled


def fill_gaps(sample, max_gap_s: float | None = None) -> dict:
    """Bridge short stretches of missing poses (design doc 22 §5.2): a hand's pose, its cameras' poses and
    its opening on the frames between two known ones at most ``max_gap_s`` apart (default
    :func:`default_gap_s`), in place. Counted in whole sample intervals: half an interval of the clock's
    jitter is allowed (3 intervals of 30 fps are 100 ms, give or take). The checks bridge with the default
    and say so in the record; the player with the viewer's choice. ``{"max_gap_s", "frames": {hand: frames
    filled}}``."""
    if not sample.hand_poses or sample.t is None:
        return {"max_gap_s": None, "frames": {}}
    gap = default_gap_s(sample) if max_gap_s is None else float(max_gap_s)
    step = history._step(sample.t)
    limit = gap + (0.5 * step if np.isfinite(step) else 1e-9)
    owners = sample.sample["umi"]["camera_hands"]
    out = {}
    for hand, poses in sample.hand_poses.items():
        filled = set(_bridge(sample.t, poses, limit))
        for cid, owner in owners.items():
            if owner == hand:
                filled |= set(_bridge(sample.t, sample.cameras[cid].T_reference_camera, limit))
        widths = sample.hand_openings[hand]
        known = np.flatnonzero(np.isfinite(widths))
        for a, b in zip(known[:-1], known[1:]):
            if b - a > 1 and sample.t[b] - sample.t[a] <= limit:
                inner = np.arange(a + 1, b)
                widths[inner] = np.interp(sample.t[inner], [sample.t[a], sample.t[b]], [widths[a], widths[b]])
                filled |= set(inner.tolist())
        out[hand] = len(filled)
    return {"max_gap_s": gap, "frames": out}


def project(sample, camera_id: str, frame: int, hand: str, offsets=None, indices=None):
    """Project target-time tool points through the *current* camera and its media transform."""
    cam = sample.cameras[camera_id]
    cal = cam.calibration(sample)
    ids = np.asarray([frame] if indices is None else indices, dtype=int)
    off = np.zeros((len(ids), 3)) if offsets is None else np.broadcast_to(offsets, (len(ids), 3))
    return G.project_chain(sample.hand_poses[hand][ids], off, cam.T_reference_camera[frame],
                           cal["K"], cal["model"], cal["distortion_coefficients"], cam.H[frame])


def history_indices(sample, frame: int) -> np.ndarray:
    """The contiguous lookback window, in time order and ending at the current frame."""
    return history.indices(sample.t, frame, sample.sample["umi"]["horizon_s"])


def draw(img, sample, camera_id: str, frame: int, scale: float):
    import cv2

    out = img.copy()
    h, w = out.shape[:2]
    ids = history_indices(sample, frame)
    owner = sample.sample["umi"]["camera_hands"][camera_id]

    def pixel(uv):
        return tuple(np.clip(np.rint(uv * scale), -1000000, 1000000).astype(int))

    def line(a, b, color, thickness=2):
        if not np.isfinite([a, b]).all():
            return
        ok, p, q = cv2.clipLine((0, 0, w, h), pixel(a), pixel(b))
        if ok:
            cv2.line(out, p, q, color, thickness, cv2.LINE_AA)

    for j, hand in enumerate(sorted(sample.hand_poses)):
        if hand != owner:
            continue
        color = COLORS[j]
        uv, _ = project(sample, camera_id, frame, hand, indices=ids)
        for a, b in zip(uv[:-1], uv[1:]):
            line(a, b, color)
        center = uv[-1]
        if np.isfinite(center).all():
            p = pixel(center)
            if 0 <= p[0] < w and 0 <= p[1] < h:
                cv2.circle(out, p, 5, color, 2, cv2.LINE_AA)
                cv2.putText(out, hand, (p[0] + 7, p[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, .42, color, 1, cv2.LINE_AA)
            axis, _ = project(sample, camera_id, frame, hand, offsets=[0, 0, AXIS_M])
            line(center, axis[0], color, 5)
            opening = sample.hand_openings[hand][frame]
            if np.isfinite(opening):
                # UMI fingers open along camera/TCP x; the optical and TCP axes agree.
                fingers, _ = project(sample, camera_id, frame, hand,
                                     offsets=[[-opening / 2, 0, 0], [opening / 2, 0, 0]],
                                     indices=[frame, frame])
                line(fingers[0], fingers[1], color, 3)
        label = (f"{hand}: opening {sample.hand_openings[hand][frame]:.3f} m"
                 f" | past {sample.sample['umi']['horizon_s']:g}s")
        cv2.putText(out, label, (8, h - 12), cv2.FONT_HERSHEY_SIMPLEX, .45, color, 1, cv2.LINE_AA)
    return out


def build_prompt(sample, camera_id: str, lo: int, hi: int) -> str:
    cfg = sample.sample["umi"]
    owner = cfg['camera_hands'][camera_id]
    color = ('BLUE', 'MAGENTA')[sorted(sample.hand_poses).index(owner)]
    task = (sample.sample['source']['instruction'] or 'Assess the visible manipulation').strip().rstrip('.')
    return "\n".join([
        "Check a UMI recording: a person operates a handheld gripper with a camera on it. There is no robot and "
        "no robot command; check whether the recorded gripper orientation agrees with what the video shows. "
        "You get one continuous MARKED video of the window: the camera view with the own hand's marks drawn on it.",
        f"Task: {task}. Camera {camera_id} is mounted on {owner}'s gripper. Printed frames {lo} to {hi}.",
        f"Identify {owner} first: because the camera is mounted on it, {owner}'s own fingers stay at about the same "
        "place in every frame. A gripper or hand that moves around the frame is a DIFFERENT hand, even if a person "
        f"holds it; never assess it as {owner}.",
        f"Only the camera's own hand {owner} is annotated and assessed. Other hands in the video are scene context; "
        "their trajectories, geometry and opening values are intentionally not overlaid. "
        "Do not report their missing annotations as discrepancies.",
        f"Legend: {owner} is {color}. A circle is the current TCP (tool center point, between the fingertips), a "
        "long thick line is its approach axis, and a crossbar is the recorded finger opening. The text at the bottom "
        "gives the recorded opening in meters. Colors identify hands, not correctness.",
        history.prompt(cfg["horizon_s"], "TCP"),
        "The own-hand current overlay is approximately fixed because the camera moves with the gripper. "
        "This is expected and cannot independently validate its world trajectory. Poses come from SLAM, "
        "not independent motion-capture ground truth. Projections do not account for occlusion or mirror reflections.",
        "Assess ORIENTATION only: whether the approach line follows the direction the own fingers point. The camera "
        "is rigidly mounted on the gripper, so this line stays at the SAME place and direction in every frame of the "
        "video; it does not move when the scene, the object or the arm move. Judge it once for the whole video: "
        "either it follows the fingers throughout, or it is wrong throughout. If it is wrong, return ONE segment "
        "covering the whole printed range; never report a part of the video. Do not assess "
        "the circle's position, the past trajectory, the finger opening, the printed opening value, or grasp and "
        "release timing; the crossbar and the printed opening are shown for context only and are never a "
        "discrepancy. Distinguish a visible discrepancy from occlusion, motion blur, missing calibration evidence "
        "or an ambiguous view of the fingers. Abstain when not observable.",
        "Return ONE JSON object only: "
        '{"gripper_visible": true, "segments": [{"start_frame": 1, "end_frame": 2, '
        '"aspect": "orientation", "confidence": 0.0, "evidence_frames": [1], '
        '"observation": "具体可见证据，中文"}], "summary": "中文总结朝向是否一致，包括无法判断的部分"}.',
        f"Use printed frame numbers {lo}..{hi}; evidence_frames must be inside their segment (1 to 5 frames). "
        "confidence is confidence in a DISCREPANCY, not action success. If no discrepancy is supported, return "
        "segments: []. If no gripper is visible, set gripper_visible to false and do not invent discrepancies.",
    ])
