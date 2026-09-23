"""P-A provider: seeded CPU tracking (design 12 §7.2, D-E7).

Seeds (a person's clicks, or the DEMO ``synthetic_fixture``) are anchors. The seeded points
themselves are often not trackable surface texture (a TCP between two fingers is air, fingertips sit
on silhouettes), so the tracker follows the rigid gripper around them instead:

1. at anchor ``a`` it detects corner features in discs around the visible seeds and tracks them to the
   next anchor ``b`` with pyramidal Lucas-Kanade, each step checked by a forward-backward round trip;
2. members are the features that moved rigidly with the seeds: a RANSAC similarity over feature pairs
   must carry the seeds at ``a`` onto the seeds at ``b``; the background, the table or a slipping object
   drop out - the anchors, never the declared projection, decide membership;
3. per frame a similarity fitted to the members (RANSAC) carries the seeds from ``a``; the known error
   at ``b`` is removed linearly (drift correction; too large an error leaves the segment untrusted);
   the same runs backward from ``b`` and the two estimates are blended where they agree;
4. every anchor re-seeds everything (periodic relocalisation). Disagreement, too few members or a
   poor fit leave the point ``uncertain``; nothing is interpolated into an observation
   (§7.2 "失跟后禁止用插值冒充观测"). After the last anchor the tracker runs forward only, briefly.

The provider sees frames, point ids and seeds only (``observations.provider_inputs``).
"""
from __future__ import annotations

import dataclasses
from typing import Iterable

import cv2
import numpy as np

from .observations import (OCCLUDED, OUT_OF_FRAME, UNCERTAIN, VISIBLE, ObservationBatch, PointTargets,
                           ProviderContext)
from .video import DecodedFrame

METHOD = "optical_flow"
MODEL_VERSION = "pa-seeded-rigid-lk/1.0"


@dataclasses.dataclass(frozen=True)
class TrackerConfig:
    win: int = 21
    levels: int = 3
    step_fb_px: float = 1.0          # forward-backward round trip allowed per LK step
    disc_px: float = 60.0            # feature search radius around each visible seed
    max_features: int = 150
    seed_tol_px: float = 10.0        # a rigid model must carry the seeds onto the far anchor this closely,
    seed_tol_spread: float = 0.15    # ... or within this fraction of the seed spread,
    seed_tol_motion: float = 0.12    # ... or of the seed motion between the anchors
    ransac_iters: int = 300
    min_members: int = 6
    fit_px: float = 2.0              # RANSAC threshold of the per-frame similarity
    max_fit_rmse_px: float = 2.5
    agree_px: float = 4.0            # forward vs backward estimate
    single_max_frames: int = 3       # a one-sided estimate is accepted this close to its anchor
    tail_max_frames: int = 15        # forward-only frames after the last anchor
    edge_px: int = 4


def similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray | None:
    """Least-squares 2D similarity (Umeyama) mapping src -> dst as a 2x3 matrix (>= 2 points)."""
    if len(src) < 2:
        return None
    ms, md = src.mean(0), dst.mean(0)
    a, b = src - ms, dst - md
    var = (a * a).sum()
    if var < 1e-9:
        return None
    cov = b.T @ a / len(src)
    U, S, Vt = np.linalg.svd(cov)
    d = np.sign(np.linalg.det(U @ Vt)) or 1.0
    D = np.diag([1.0, d])
    R = U @ D @ Vt
    s = np.trace(np.diag(S) @ D) / (var / len(src))
    t = md - s * R @ ms
    return np.c_[s * R, t]


def apply(M: np.ndarray, pts: np.ndarray) -> np.ndarray:
    return pts @ M[:, :2].T + M[:, 2]


class SeededLKProvider:
    method = METHOD
    model_version = MODEL_VERSION

    def __init__(self, config: TrackerConfig | None = None):
        self.cfg = config or TrackerConfig()
        self._lk = dict(winSize=(self.cfg.win, self.cfg.win), maxLevel=self.cfg.levels,
                        criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 30, 0.01))

    # -- features -----------------------------------------------------------------------------
    def _features(self, img: np.ndarray, seeds: np.ndarray) -> np.ndarray:
        ok = np.isfinite(seeds).all(1)
        if not ok.any():
            return np.zeros((0, 2))
        mask = np.zeros(img.shape, np.uint8)
        for u, v in seeds[ok]:
            cv2.circle(mask, (int(round(u)), int(round(v))), int(self.cfg.disc_px), 255, -1)
        pts = cv2.goodFeaturesToTrack(img, self.cfg.max_features, 0.01, 4, mask=mask, blockSize=5)
        return np.zeros((0, 2)) if pts is None else pts.reshape(-1, 2).astype(float)

    def _step(self, a: np.ndarray, b: np.ndarray, pts: np.ndarray) -> np.ndarray:
        out = np.full_like(pts, np.nan)
        ok = np.isfinite(pts).all(1)
        if not ok.any():
            return out
        p0 = pts[ok].astype(np.float32).reshape(-1, 1, 2)
        p1, st1, _ = cv2.calcOpticalFlowPyrLK(a, b, p0, None, **self._lk)
        p0r, st2, _ = cv2.calcOpticalFlowPyrLK(b, a, p1, None, **self._lk)
        fb = np.linalg.norm((p0r - p0).reshape(-1, 2), axis=1)
        q = p1.reshape(-1, 2)
        h, w = b.shape
        e = self.cfg.edge_px
        good = (st1.ravel() == 1) & (st2.ravel() == 1) & (fb < self.cfg.step_fb_px) \
            & (q[:, 0] >= e) & (q[:, 0] < w - e) & (q[:, 1] >= e) & (q[:, 1] < h - e)
        idx = np.flatnonzero(ok)
        out[idx[good]] = q[good]
        return out

    def _run(self, frames: list[np.ndarray], feats: np.ndarray) -> np.ndarray:
        tracks = np.full((len(frames), len(feats), 2), np.nan)
        tracks[0] = feats
        for j in range(1, len(frames)):
            tracks[j] = self._step(frames[j - 1], frames[j], tracks[j - 1])
            if not np.isfinite(tracks[j]).any():
                break
        return tracks

    def _seed_tol(self, start, far, common) -> float:
        spread = float(np.linalg.norm(start[common].max(0) - start[common].min(0)))
        shift = float(np.linalg.norm(far[common].mean(0) - start[common].mean(0)))
        return max(self.cfg.seed_tol_px, self.cfg.seed_tol_spread * spread, self.cfg.seed_tol_motion * shift)

    def _members(self, feats, end, alive, start, far, vis) -> np.ndarray:
        """Features that moved rigidly with the seeds between the two anchors.

        RANSAC over pairs of tracked features: a candidate similarity must carry the seeds from the
        near anchor onto the far anchor (so a static background cannot win while the gripper moves,
        and large in-plane rotations are fine); the model with most feature inliers defines the
        members. The seeds alone would be ill-posed (a closed gripper makes them coincide).
        """
        cfg = self.cfg
        common = vis & np.isfinite(far).all(1)
        idx = np.flatnonzero(alive)
        none = np.zeros(len(feats), bool)
        if not common.any() or len(idx) < cfg.min_members:
            return none
        X, Y = feats[idx], end[idx]
        S0, S1 = start[common], far[common]
        tol = self._seed_tol(start, far, common)
        rng = np.random.default_rng(len(idx))
        best, best_n = None, 0
        for _ in range(cfg.ransac_iters):
            i, j = rng.choice(len(idx), 2, replace=False)
            M = similarity(X[[i, j]], Y[[i, j]])
            if M is None or np.linalg.norm(apply(M, S0) - S1, axis=1).max() > tol:
                continue
            inl = np.linalg.norm(apply(M, X) - Y, axis=1) < cfg.fit_px * 1.5
            if inl.sum() > best_n:
                best, best_n = inl, int(inl.sum())
        if best is None or best_n < cfg.min_members:
            return none
        M = similarity(X[best], Y[best])
        if M is None or np.linalg.norm(apply(M, S0) - S1, axis=1).max() > tol:
            return none
        out = none.copy()
        out[idx[np.linalg.norm(apply(M, X) - Y, axis=1) < cfg.fit_px * 1.5]] = True
        return out if out.sum() >= cfg.min_members else none

    def _carry(self, frames: list[np.ndarray], start: np.ndarray, far: np.ndarray | None,
               known_members: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
        """Seeds carried from frames[0] through the clip: (len, k, 2) estimates (NaN where not trusted).

        With a far anchor the known end error is removed linearly over the segment (drift correction):
        the motion shape comes from tracking, the anchors pin both ends. An end error beyond the seed
        tolerance means the rigid model did not hold, and the whole segment is left untrusted.
        """
        cfg = self.cfg
        L = len(frames) - 1
        est = np.full((L + 1, len(start), 2), np.nan)
        info = {"members": 0, "fits": 0, "rejected_fits": 0}
        vis = np.isfinite(start).all(1)
        if not vis.any():
            return est, info
        feats = self._features(frames[0], start) if known_members is None else known_members
        if len(feats) < cfg.min_members:
            return est, info
        tracks = self._run(frames, feats)
        member = np.isfinite(tracks[L]).all(1)
        if far is not None:
            member = self._members(feats, tracks[L], member, start, far, vis)
        elif known_members is None:
            member[:] = False                          # unverified features could be background
        else:
            member = np.isfinite(tracks).all(2).any(0)
        info["members"] = int(member.sum())
        info["member_feats"] = feats[member]
        est[0, vis] = start[vis]
        for j in range(1, L + 1):
            m = member & np.isfinite(tracks[j]).all(1)
            if m.sum() < cfg.min_members:
                continue
            M, inl = cv2.estimateAffinePartial2D(feats[m].astype(np.float32), tracks[j, m].astype(np.float32),
                                                 method=cv2.RANSAC, ransacReprojThreshold=cfg.fit_px)
            if M is None or inl is None or inl.sum() < cfg.min_members:
                info["rejected_fits"] += 1
                continue
            inl = inl.ravel().astype(bool)
            r = np.linalg.norm(apply(M, feats[m][inl]) - tracks[j, m][inl], axis=1)
            if float(np.sqrt((r ** 2).mean())) > cfg.max_fit_rmse_px:
                info["rejected_fits"] += 1
                continue
            info["fits"] += 1
            est[j, vis] = apply(M, start[vis])
        if far is not None:
            common = vis & np.isfinite(far).all(1)
            end_err = far - est[L]
            ok_end = common & np.isfinite(end_err).all(1)
            if not ok_end.any() or np.linalg.norm(end_err[ok_end], axis=1).max() > self._seed_tol(start, far, common):
                est[1:] = np.nan
                info["end_error_px"] = None
                return est, info
            info["end_error_px"] = float(np.linalg.norm(end_err[ok_end], axis=1).max())
            w = (np.arange(L + 1) / L)[:, None]
            for k in np.flatnonzero(ok_end):
                est[:, k] += w * end_err[k]
            est[1:, vis & ~ok_end] = np.nan            # a point the far anchor does not see stays one-sided
        return est, info

    # -- segments -----------------------------------------------------------------------------
    def _segment(self, frames, a_pts, b_pts, batch, pids, first, tail_members=None):
        cfg = self.cfg
        L = len(frames) - 1
        fwd, i1 = self._carry(frames, a_pts, b_pts, known_members=tail_members if b_pts is None else None)
        bwd = None
        if b_pts is not None:
            bwd, i2 = self._carry(frames[::-1], b_pts, a_pts)
            bwd = bwd[::-1]
            batch.stats["members"].append(min(i1["members"], i2["members"]))
            # gripper features confirmed at anchor b (backward pass) carry a following tail
            self._tail = i2.get("member_feats")
        last = L if b_pts is None else L - 1
        h, w = frames[0].shape
        for j in range(1, last + 1):
            fi = first + j
            for n, pid in enumerate(pids):
                f = fwd[j, n]
                b = bwd[j, n] if bwd is not None else np.array([np.nan, np.nan])
                f_ok, b_ok = bool(np.isfinite(f).all()), bool(np.isfinite(b).all())
                if f_ok and b_ok:
                    d = float(np.linalg.norm(f - b))
                    if d > cfg.agree_px:
                        batch.visibility[pid][fi] = UNCERTAIN
                        batch.stats["disagreements"] += 1
                        continue
                    wgt = j / L
                    uv, conf, unc = (1 - wgt) * f + wgt * b, 1.0 - 0.5 * d / cfg.agree_px, 0.5 * d + 0.5
                elif f_ok and (j <= cfg.single_max_frames or (bwd is None and j <= cfg.tail_max_frames)):
                    uv, conf, unc = f, max(0.2, 0.7 - 0.03 * j), 1.0 + 0.2 * j
                elif b_ok and (L - j) <= cfg.single_max_frames:
                    uv, conf, unc = b, max(0.2, 0.7 - 0.03 * (L - j)), 1.0 + 0.2 * (L - j)
                else:
                    batch.visibility[pid][fi] = UNCERTAIN
                    batch.stats["lost"] += 1
                    continue
                if not (0 <= uv[0] < w and 0 <= uv[1] < h):
                    batch.visibility[pid][fi] = OUT_OF_FRAME
                    continue
                batch.uv[pid][fi] = uv
                batch.visibility[pid][fi] = VISIBLE
                batch.confidence[pid][fi] = conf
                batch.uncertainty[pid][fi] = unc

    def locate(self, frames: Iterable[DecodedFrame], targets: PointTargets, ctx: ProviderContext) -> ObservationBatch:
        n = ctx.media_frame_count
        pids = list(targets.point_ids)
        seeds = targets.seeds
        seed_method = seeds.method if seeds else None
        batch = ObservationBatch.empty(ctx.camera_id, METHOD,
                                       f"{MODEL_VERSION}; seeds={seed_method}:{seeds.model_version if seeds else '-'}",
                                       n, pids)
        batch.stats = {"seed_method": seed_method, "anchors": 0, "disagreements": 0, "lost": 0, "members": [],
                       "seed_hash_matches": 0, "seed_hash_checked": 0, "frames_decoded": 0}
        anchors = sorted(f for f in (seeds.by_media_frame if seeds else {}) if 0 <= f < n)
        batch.stats["anchors"] = len(anchors)
        anchor_set = set(anchors)

        def anchor_pts(f):
            pts = np.full((len(pids), 2), np.nan)
            for k, pid in enumerate(pids):
                sp = seeds.by_media_frame[f].get(pid)
                if sp is None:
                    batch.visibility[pid][f] = UNCERTAIN
                elif sp.visibility == VISIBLE and sp.uv is not None:
                    pts[k] = sp.uv
                    batch.uv[pid][f] = sp.uv
                    batch.visibility[pid][f] = VISIBLE
                    batch.confidence[pid][f] = sp.confidence
                    batch.uncertainty[pid][f] = 0.5
                else:
                    batch.visibility[pid][f] = sp.visibility if sp.visibility in (OCCLUDED, OUT_OF_FRAME) else UNCERTAIN
            return pts

        def anchor_at(fr: DecodedFrame):
            if fr.index not in anchor_set:
                return None
            batch.stats["seed_hash_checked"] += 1
            batch.stats["seed_hash_matches"] += int(seeds.image_sha256.get(fr.index) == batch.image_sha256[fr.index])
            return anchor_pts(fr.index)

        return self._track(frames, n, pids, batch, anchor_at)

    def _track(self, frames: Iterable[DecodedFrame], n: int, pids: list[str], batch: ObservationBatch,
               anchor_at) -> ObservationBatch:
        """The anchor-to-anchor loop. ``anchor_at(frame)`` returns the anchor points (k, 2; NaN = not
        placed) when the frame is an anchor, else None; seeds, or a re-detection, decide that."""
        buf: list[np.ndarray] = []
        buf_first = None
        prev = None
        self._tail = None
        for fr in frames:
            if fr.index >= n:
                break
            batch.stats["frames_decoded"] += 1
            batch.image_sha256[fr.index] = fr.sha256()
            pts = anchor_at(fr)
            if pts is not None:
                self._tail = None
                if buf and prev is not None:
                    self._segment(buf + [fr.gray], prev, pts, batch, pids, buf_first)
                buf, buf_first, prev = [fr.gray], fr.index, pts
            elif buf:
                buf.append(fr.gray)
                if len(buf) > 4 * self.cfg.tail_max_frames + 1:      # anchors too far apart: stop, bound memory
                    buf, prev = [], None
        if len(buf) > 1 and prev is not None:
            self._segment(buf[: self.cfg.tail_max_frames + 1], prev, None, batch, pids, buf_first,
                          tail_members=self._tail)
        for pid in pids:                                 # decoded frames never reached by a track
            vis = batch.visibility[pid]
            for f in batch.image_sha256:
                if vis[f] == "none":
                    vis[f] = UNCERTAIN
        m = batch.stats.pop("members")
        batch.stats["members_median"] = float(np.median(m)) if m else 0.0
        return batch


TEMPLATE_MODEL_VERSION = "pa-template-rigid-lk/1.0"


class TemplateLKProvider(SeededLKProvider):
    """P-A with automatic anchors (design 12 §7.2, F5.8): a gripper template re-detects the gripper
    every ``every_frames`` frames (and on every frame until the first hit); each hit is an anchor for
    the same segment tracking as the seeded provider. No seeds, no projection, no pose."""

    model_version = TEMPLATE_MODEL_VERSION

    def __init__(self, template, config: TrackerConfig | None = None):
        super().__init__(config)
        self.template = template

    def locate(self, frames: Iterable[DecodedFrame], targets: PointTargets, ctx: ProviderContext) -> ObservationBatch:
        from .template import SEED_METHOD, Redetector

        n = ctx.media_frame_count
        pids = list(targets.point_ids)
        red = Redetector(self.template, ctx.camera_id)
        batch = ObservationBatch.empty(ctx.camera_id, METHOD,
                                       f"{TEMPLATE_MODEL_VERSION}; template={self.template.method}:{self.template.sha256[:12]}",
                                       n, pids)
        batch.stats = {"seed_method": SEED_METHOD, "template_methods": list(self.template.methods),
                       "template_sha256": self.template.sha256, "template_entries": len(red.entries),
                       "anchors": 0, "redetections_tried": 0, "redetections_ok": 0, "entries_used": {},
                       "disagreements": 0, "lost": 0, "members": [], "seed_hash_matches": 0, "seed_hash_checked": 0,
                       "frames_decoded": 0}
        every = self.template.matching.every_frames
        state = {"last": None}

        def anchor_at(fr: DecodedFrame):
            if not red.usable:
                return None
            if state["last"] is not None and fr.index - state["last"] < every:
                return None
            batch.stats["redetections_tried"] += 1
            det = red.detect(fr.gray)
            if det is None:                          # try again on the next frame
                return None
            batch.stats["redetections_ok"] += 1
            batch.stats["anchors"] += 1
            batch.stats["entries_used"][det.entry_id] = batch.stats["entries_used"].get(det.entry_id, 0) + 1
            state["last"] = fr.index
            h, w = fr.gray.shape
            pts = np.full((len(pids), 2), np.nan)
            for k, pid in enumerate(pids):
                uv = det.points.get(pid)
                if uv is None:
                    batch.visibility[pid][fr.index] = UNCERTAIN
                elif not (0 <= uv[0] < w and 0 <= uv[1] < h):
                    batch.visibility[pid][fr.index] = OUT_OF_FRAME
                else:
                    pts[k] = uv
                    batch.uv[pid][fr.index] = uv
                    batch.visibility[pid][fr.index] = VISIBLE
                    batch.confidence[pid][fr.index] = det.confidence
                    batch.uncertainty[pid][fr.index] = det.uncertainty_px
            return pts

        return self._track(frames, n, pids, batch, anchor_at)
