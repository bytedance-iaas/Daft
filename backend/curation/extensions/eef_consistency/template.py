"""Gripper appearance template (``gripper-template/1.0``): automatic anchors for the P-A tracker.

Design doc 12 §7.2 (F5.8). A template is a small library of gripper patches cut from frames of the
same camera, each with the physical points marked in patch coordinates. At run time ``Redetector``
matches ORB features of the current frame against the entries of that camera, fits a 2D similarity
with RANSAC and carries the entry's points into the frame: that is an anchor, exactly like a person's
click, so the tracker between anchors is unchanged (both ends pinned, forward-backward agreement,
no interpolation). The re-detector sees frames and the template only, never the declared projection.

``make_entry`` / ``make_template`` build a template from a few marked frames; ``check`` measures the
re-detector alone over one clip.
"""
from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import hashlib
import json
import os
import pathlib
from typing import Iterable

import cv2
import numpy as np

from curation.contracts import schemas

SCHEMA_VERSION = "gripper-template/1.0"
SCHEMA_REF = "eef/gripper_template.schema.json"
SEED_METHOD = "gripper_template"             # ``seed_method`` of observation batches anchored by a template
MODEL_VERSION = "template-orb-similarity/1.0"
PROVENANCE_OF_ROW_METHOD = {"human": "human_click", "synthetic_fixture": "synthetic_fixture"}


class TemplateError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class MatchConfig:
    entry_features: int = 800
    frame_features: int = 3000
    ratio: float = 0.8
    min_inliers: int = 20
    min_inlier_ratio: float = 0.05           # of the entry's features
    rng_seed: int = 7                         # RANSAC is seeded per call: same frame, same answer
    ransac_px: float = 4.0
    scale_range: tuple[float, float] = (0.4, 2.5)
    agree_px: float = 8.0
    every_frames: int = 15


@dataclasses.dataclass(frozen=True)
class TemplateEntry:
    entry_id: str
    camera_id: str | None
    closed_fraction: float | None
    roi_xywh: tuple[int, int, int, int]
    patch: np.ndarray                          # gray uint8 (h, w)
    mask: np.ndarray | None                    # uint8 (h, w), features only where > 0
    points: dict[str, np.ndarray]              # point id -> (2,) patch pixels
    keypoints: np.ndarray                      # (M, 2) float32 patch pixels
    descriptors: np.ndarray                    # (M, 32) uint8
    method: str                                # provenance.method


@dataclasses.dataclass(frozen=True)
class GripperTemplate:
    tool: dict
    point_ids: tuple[str, ...]
    matching: MatchConfig
    entries: tuple[TemplateEntry, ...]
    sha256: str
    notes: tuple[str, ...] = ()

    @property
    def methods(self) -> tuple[str, ...]:
        return tuple(sorted({e.method for e in self.entries}))

    @property
    def method(self) -> str:
        return "+".join(self.methods)

    def entries_for(self, camera_id: str) -> list[TemplateEntry]:
        return [e for e in self.entries if e.camera_id in (None, camera_id)]

    def observable_points(self, camera_ids: Iterable[str]) -> dict[str, set[str]]:
        """camera id -> point ids the re-detector can anchor there (cameras without entries are absent)."""
        out: dict[str, set[str]] = {}
        for cid in camera_ids:
            pts = {pid for e in self.entries_for(cid) for pid in e.points}
            if pts:
                out[cid] = pts
        return out


def _orb(n: int):
    return cv2.ORB_create(nfeatures=int(n), scaleFactor=1.2, nlevels=8, edgeThreshold=12, patchSize=31,
                          fastThreshold=8)


def _features(orb, gray: np.ndarray, mask: np.ndarray | None = None):
    kp, desc = orb.detectAndCompute(gray, mask)
    if desc is None or not len(kp):
        return np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8)
    return np.float32([k.pt for k in kp]), desc


def _strict_json(data: bytes):
    return json.loads(data.decode("utf-8"),
                      parse_constant=lambda c: (_ for _ in ()).throw(ValueError(f"non-finite JSON: {c}")))


def load_template(source) -> GripperTemplate:
    """Read and validate a template (path, bytes or dict); decode the patches, compute their ORB features."""
    if isinstance(source, dict):
        payload = source
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    else:
        raw = bytes(source) if isinstance(source, (bytes, bytearray)) \
            else pathlib.Path(os.path.expanduser(str(source))).read_bytes()
        try:
            payload = _strict_json(raw)
        except ValueError as exc:
            raise TemplateError(f"gripper template is not valid JSON: {exc}") from None
    errs = schemas.errors(SCHEMA_REF, payload)
    if errs:
        raise TemplateError(f"gripper template does not follow {SCHEMA_VERSION}: {errs[0]}")
    m, f = payload.get("matching", {}), payload.get("feature", {})
    cfg = MatchConfig(entry_features=int(f.get("entry_features", 800)), frame_features=int(f.get("frame_features", 3000)),
                      ratio=float(m.get("ratio", 0.8)), min_inliers=int(m.get("min_inliers", 20)),
                      min_inlier_ratio=float(m.get("min_inlier_ratio", 0.05)), rng_seed=int(m.get("rng_seed", 7)),
                      ransac_px=float(m.get("ransac_px", 4.0)), scale_range=tuple(m.get("scale_range", (0.4, 2.5))),
                      agree_px=float(m.get("agree_px", 8.0)), every_frames=int(m.get("every_frames", 15)))
    point_ids = tuple(payload["tool"]["point_ids"])
    orb = _orb(cfg.entry_features)
    entries: list[TemplateEntry] = []
    ids: set[str] = set()
    for k, e in enumerate(payload["entries"]):
        where = f"entries[{k}] ({e['entry_id']})"
        if e["entry_id"] in ids:
            raise TemplateError(f"{where}: duplicate entry_id")
        ids.add(e["entry_id"])
        try:
            buf = np.frombuffer(base64.b64decode(e["patch_png_base64"], validate=True), np.uint8)
        except Exception as exc:  # noqa: BLE001
            raise TemplateError(f"{where}: patch is not base64: {exc}") from None
        patch = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
        if patch is None or patch.size == 0:
            raise TemplateError(f"{where}: patch is not a decodable image")
        h, w = patch.shape
        mask = None
        if e.get("mask_png_base64"):
            try:
                mask = cv2.imdecode(np.frombuffer(base64.b64decode(e["mask_png_base64"], validate=True), np.uint8),
                                    cv2.IMREAD_GRAYSCALE)
            except Exception as exc:  # noqa: BLE001
                raise TemplateError(f"{where}: mask is not base64: {exc}") from None
            if mask is None or mask.shape != patch.shape:
                raise TemplateError(f"{where}: mask must be a decodable image of the patch's size")
            mask = (mask > 127).astype(np.uint8) * 255
        pts: dict[str, np.ndarray] = {}
        for pid, uv in e["points_patch_px"].items():
            if pid not in point_ids:
                raise TemplateError(f"{where}: point {pid!r} is not in tool.point_ids")
            if not (0 <= uv[0] < w and 0 <= uv[1] < h):
                raise TemplateError(f"{where}: point {pid!r} lies outside the patch")
            pts[pid] = np.asarray(uv, float)
        kp, desc = _features(orb, patch, mask)
        entries.append(TemplateEntry(e["entry_id"], e["camera_id"], e["closed_fraction"],
                                     tuple(int(v) for v in e["roi_xywh"]), patch, mask, pts, kp, desc,
                                     e["provenance"]["method"]))
    if not any(len(e.descriptors) >= cfg.min_inliers for e in entries):
        raise TemplateError(f"no entry has at least {cfg.min_inliers} features: patches too small or textureless")
    return GripperTemplate(dict(payload["tool"]), point_ids, cfg, tuple(entries), hashlib.sha256(raw).hexdigest(),
                           tuple(payload.get("notes", [])))


@dataclasses.dataclass(frozen=True)
class Detection:
    entry_id: str
    inliers: int
    scale: float
    points: dict[str, np.ndarray]              # point id -> (2,) frame pixels
    confidence: float
    uncertainty_px: float


def _apply(M: np.ndarray, pts: np.ndarray) -> np.ndarray:
    return pts @ M[:, :2].T + M[:, 2]


class Redetector:
    """Find one camera's gripper in a frame from the template alone: whole frame, no prior."""

    def __init__(self, template: GripperTemplate, camera_id: str):
        self.cfg = template.matching
        self.entries = [e for e in template.entries_for(camera_id) if len(e.descriptors) >= self.cfg.min_inliers]
        self.orb = _orb(self.cfg.frame_features)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)

    @property
    def usable(self) -> bool:
        return bool(self.entries)

    def detect(self, gray: np.ndarray) -> Detection | None:
        cfg = self.cfg
        cv2.setRNGSeed(cfg.rng_seed)
        fpts, fdesc = _features(self.orb, gray)
        if len(fdesc) < cfg.min_inliers:
            return None
        cands = []
        for e in self.entries:
            pairs = self.matcher.knnMatch(e.descriptors, fdesc, k=2)
            good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < cfg.ratio * p[1].distance]
            if len(good) < cfg.min_inliers:
                continue
            src = e.keypoints[[g.queryIdx for g in good]]
            dst = fpts[[g.trainIdx for g in good]]
            M, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=cfg.ransac_px,
                                                 maxIters=2000, confidence=0.995)
            need = max(cfg.min_inliers, int(np.ceil(cfg.min_inlier_ratio * len(e.descriptors))))
            if M is None or inl is None or int(inl.sum()) < need:
                continue
            scale = float(np.sqrt(abs(np.linalg.det(M[:, :2]))))
            if not (cfg.scale_range[0] <= scale <= cfg.scale_range[1]):
                continue
            cands.append((int(inl.sum()), scale, e.entry_id, {pid: _apply(M, p[None])[0] for pid, p in e.points.items()}))
        if not cands:
            return None
        cands.sort(key=lambda c: -c[0])
        n, scale, eid, pts = cands[0]
        for m, _, _, other in cands[1:]:          # a rival entry placing the gripper elsewhere: ambiguous, abstain
            common = set(other) & set(pts)
            if common and m >= 0.8 * n and max(float(np.linalg.norm(other[p] - pts[p])) for p in common) > cfg.agree_px:
                return None
        return Detection(eid, n, scale, pts, float(min(1.0, n / (3.0 * cfg.min_inliers))), max(1.0, cfg.ransac_px / 2))


# --- building ------------------------------------------------------------------------------------

def rigid_member_mask(frames: list[np.ndarray], pts_a: dict, pts_b: dict, *, tracker=None, radius_px: int = 14,
                      min_members: int = 6, min_shift_px: float = 20.0) -> np.ndarray | None:
    """Where the gripper is in ``frames[0]``: the features that the tracker carries from ``frames[0]`` to
    ``frames[-1]`` rigidly with the marked points (``pts_a`` -> ``pts_b``), as a disc mask. Static background,
    the table and a slipping object drop out - the tracker's own membership rule (``_members``). The points
    must move at least ``min_shift_px`` between the two frames, otherwise nothing separates the gripper from a
    static background and no mask is returned."""
    from .tracking import SeededLKProvider, TrackerConfig

    common = [pid for pid in pts_a if pid in pts_b and pts_a[pid] is not None and pts_b[pid] is not None]
    if len(common) < 2 or len(frames) < 2:
        return None
    start = np.asarray([pts_a[p] for p in common], float)
    far = np.asarray([pts_b[p] for p in common], float)
    if float(np.linalg.norm(far.mean(0) - start.mean(0))) < min_shift_px:
        return None            # a still gripper cannot be told from the still background by motion
    _, info = SeededLKProvider(tracker or TrackerConfig())._carry(list(frames), start, far)
    feats = info.get("member_feats")
    if feats is None or len(feats) < min_members:
        return None
    mask = np.zeros(frames[0].shape, np.uint8)
    for u, v in np.vstack([feats, start]):
        cv2.circle(mask, (int(round(u)), int(round(v))), int(radius_px), 255, -1)
    return mask


def make_entry(gray: np.ndarray, points: dict[str, tuple[float, float]], *, entry_id: str, camera_id: str | None,
               source: dict, method: str, closed_fraction: float | None = None, margin_px: int = 70,
               note: str | None = None, mask: np.ndarray | None = None) -> dict:
    """One entry from a frame and the points a person marked on it (frame pixels): the patch is the
    points' bounding box grown by ``margin_px``; ``mask`` (frame-sized, white = gripper, e.g. from
    ``rigid_member_mask``) restricts the features to the gripper so static background cannot be matched."""
    h, w = gray.shape
    P = np.asarray(list(points.values()), float)
    x0, y0 = np.floor(P.min(0) - margin_px).astype(int)
    x1, y1 = np.ceil(P.max(0) + margin_px).astype(int)
    x0, y0, x1, y1 = max(0, int(x0)), max(0, int(y0)), min(w, int(x1) + 1), min(h, int(y1) + 1)
    if x1 - x0 < 16 or y1 - y0 < 16:
        raise TemplateError(f"{entry_id}: the patch around the points is too small ({x1 - x0}x{y1 - y0})")
    ok, png = cv2.imencode(".png", np.ascontiguousarray(gray[y0:y1, x0:x1]))
    if not ok:
        raise TemplateError(f"{entry_id}: cannot encode the patch")
    mask_b64 = None
    if mask is not None:
        if mask.shape != gray.shape:
            raise TemplateError(f"{entry_id}: mask must have the frame's size")
        ok, mpng = cv2.imencode(".png", np.ascontiguousarray((mask[y0:y1, x0:x1] > 0).astype(np.uint8) * 255))
        if not ok:
            raise TemplateError(f"{entry_id}: cannot encode the mask")
        mask_b64 = base64.b64encode(mpng.tobytes()).decode("ascii")
    return {"entry_id": entry_id, "camera_id": camera_id, "closed_fraction": closed_fraction,
            "roi_xywh": [x0, y0, x1 - x0, y1 - y0],
            "patch_png_base64": base64.b64encode(png.tobytes()).decode("ascii"),
            "mask_png_base64": mask_b64,
            "points_patch_px": {pid: [round(float(u - x0), 2), round(float(v - y0), 2)] for pid, (u, v) in points.items()},
            "source": {"dataset": source.get("dataset"), "sample_id": source.get("sample_id"),
                       "camera_id": source.get("camera_id"), "frame_index": source.get("frame_index"),
                       "image_sha256": source.get("image_sha256")},
            "provenance": {"method": method, "created_at": _dt.date.today().isoformat(), "note": note}}


def make_template(entries: list[dict], *, tool_name: str, point_ids: Iterable[str], matching: dict | None = None,
                  notes: Iterable[str] = ()) -> dict:
    return {"schema_version": SCHEMA_VERSION, "tool": {"name": tool_name, "point_ids": list(point_ids)},
            "feature": {"type": "orb"}, "matching": {"model": "similarity", **(matching or {})},
            "entries": entries, "notes": list(notes)}


def check(template: GripperTemplate, camera_id: str, frames, *, every: int | None = None,
          reference: dict[int, dict[str, np.ndarray]] | None = None) -> dict:
    """The re-detector alone over decoded frames: tries every ``every`` frames, reports the success
    rate, the entries used and, against ``reference`` (frame -> point -> pixels, e.g. seeds), the errors."""
    red = Redetector(template, camera_id)
    step = every or template.matching.every_frames
    tried = ok = 0
    used: dict[str, int] = {}
    errors: list[float] = []
    for fr in frames:
        if fr.index % step:
            continue
        tried += 1
        det = red.detect(fr.gray)
        if det is None:
            continue
        ok += 1
        used[det.entry_id] = used.get(det.entry_id, 0) + 1
        ref = (reference or {}).get(fr.index)
        if ref:
            for pid, uv in det.points.items():
                if pid in ref and ref[pid] is not None:
                    errors.append(float(np.linalg.norm(np.asarray(ref[pid], float) - uv)))
    return {"camera_id": camera_id, "entries": len(red.entries), "tried": tried, "detected": ok,
            "detection_rate": round(ok / tried, 3) if tried else None, "entries_used": used,
            "reference_points": len(errors), "reference_p50_px": round(float(np.median(errors)), 2) if errors else None,
            "reference_p95_px": round(float(np.percentile(errors, 95)), 2) if errors else None}
