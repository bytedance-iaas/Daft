"""UMI absolute hand trajectories in the current camera, never incremental deltas."""
from __future__ import annotations

import numpy as np

from . import geometry as G, history

PROMPT_VERSION = "umi-action-prompt/7"  # 7: the hand's opening calibration, saturation reported as such
COLORS = ((255, 160, 0), (210, 60, 255))  # BGR, stable hand order


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
    for cid, cam in sample.cameras.items():
        if cam.mount != "wrist" or cam.calibration(sample) is None:
            raise ValueError(f"{cid}: UMI requires a calibrated wrist camera")
        if not np.isfinite(cam.T_reference_camera).all():
            raise ValueError(f"{cid}: UMI requires camera poses on the sample timeline")
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
                    or pose["reference_frame"] != sample.reference_frame or pose["frame_id"] != hand):
                raise ValueError(f"frame {i}/{hand}: expected absolute pose in {sample.reference_frame}")
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
            axis, _ = project(sample, camera_id, frame, hand, offsets=[0, 0, .06])
            line(center, axis[0], color, 3)
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


def _calibration(r: dict | None) -> list[str]:
    """What the hand's recorded opening means, from its gripper calibration; nothing when the input has none."""
    if not r:
        return []
    lo, top = r["min_width_m"], r["max_width_m"] - r["min_width_m"]
    return [
        f"Opening calibration of this hand: recorded opening = finger-tag distance - {lo:.4f} m, clipped to "
        f"0..{top:.4f} m (from its gripper calibration video). Fully closed fingers with nothing between them read 0, "
        f"and fully open fingers read at or near {top:.4f}; both are normal. The reading is SATURATED only when it "
        f"stays exactly at a bound (0, or {top:.4f}) while the fingers visibly differ from that state: held apart by "
        "an object or still moving while the reading stays at 0, or visibly opening further while it stays at the "
        "top. A reading close to but below the top while the fingers are fully open is normal, not saturation. "
        "Report each saturated stretch as aspect action with an observation starting with '开口标定饱和：', in "
        "every clip where it is visible. It is a calibration problem, not a timing error: do not also report "
        "grasp/release timing because of it; judge timing only by when the reading changes."]


def build_prompt(sample, camera_id: str, lo: int, hi: int) -> str:
    cfg = sample.sample["umi"]
    owner = cfg['camera_hands'][camera_id]
    color = ('BLUE', 'MAGENTA')[sorted(sample.hand_poses).index(owner)]
    task = (sample.sample['source']['instruction'] or 'Assess the visible manipulation').strip().rstrip('.')
    return "\n".join([
        "Check a UMI recording: a person operates a handheld gripper with a camera on it. There is no robot and "
        "no robot command; check whether the recorded gripper motion agrees with what the video shows. "
        "You get continuous RAW and MARKED videos of the same window.",
        f"Task: {task}. Camera {camera_id} is mounted on {owner}'s gripper. Printed frames {lo} to {hi}.",
        f"Identify {owner} first: because the camera is mounted on it, {owner}'s own fingers stay at about the same "
        "place in every frame. A gripper or hand that moves around the frame is a DIFFERENT hand, even if a person "
        f"holds it; never assess it as {owner}.",
        f"Only the camera's own hand {owner} is annotated and assessed. Other hands in RAW are scene context; "
        "their trajectories, geometry and opening values are intentionally not overlaid. "
        "Do not report their missing annotations as discrepancies.",
        f"Legend: {owner} is {color}. A circle is the current TCP (tool center point, between the fingertips), a "
        "short line is its approach axis, and a crossbar is the recorded finger opening. The text at the bottom "
        "gives the recorded opening in meters. Colors identify hands, not correctness.",
        history.prompt(cfg["horizon_s"], "TCP"),
        "The own-hand current overlay is approximately fixed because the camera moves with the gripper. "
        "This is expected and cannot independently validate its world trajectory. Poses come from SLAM, "
        "not independent motion-capture ground truth. Projections do not account for occlusion or mirror reflections.",
        "First watch RAW for the objects and the actual grasp, transfer and release events. Then compare MARKED. "
        "position: the circle is not at the visible point between the fingertips. orientation: the approach line "
        "does not follow the direction the fingers point. action: the recent motion or the opening/closing timing "
        "disagrees with the visible events. Do not infer physical collisions from a 2D crossing alone. Distinguish "
        "a visible discrepancy from occlusion, missing calibration evidence or an ambiguous target. Abstain when "
        "not observable.",
        "Opening: fingers holding an object stay apart by the object's contact width, so a stable opening "
        "while an object is held, carried or manipulated is normal. The printed opening is a metric record; do not "
        "compare it with a size guessed from the image. Judge grasp and release by the object actually between the "
        "fingers, not by other objects it carries or that move with it. It is released when it stops moving with "
        "the camera (it stays behind as the gripper moves away); while it is still between the fingers it is held. "
        "Opening widening at or just before the release and staying open afterwards is a normal release. Report "
        "opening or timing only when a visible finger opening/closing, or the held object leaving the fingers, "
        "happens clearly before or after the recorded opening change.",
        *_calibration(cfg.get("gripper_range", {}).get(owner)),
        "Return ONE JSON object only: "
        '{"gripper_visible": true, "segments": [{"start_frame": 1, "end_frame": 2, '
        '"aspect": "position|orientation|both|action", "confidence": 0.0, "evidence_frames": [1], '
        '"observation": "具体可见证据，中文"}], "summary": "中文总结，包括无法判断的部分"}.',
        f"Use printed frame numbers {lo}..{hi}; evidence_frames must be inside their segment (1 to 5 frames). "
        "confidence is confidence in a DISCREPANCY, not action success. If no discrepancy is supported, return "
        "segments: []. If no gripper is visible, set gripper_visible to false and do not invent discrepancies.",
    ])
