"""A handheld gripper's trajectory from its own recording (design doc 22 §5.4, F5.20).

A task that gives no trajectory.json on a handheld-gripper mcap (the built-in UMI layout) gets each episode's
trajectory from the recording itself, the way ``export-umi-mcap`` makes it, with the gripper calibration the task
gives or the built-in DAS DEMO one (every value assumed, no camera's intrinsics). One episode at a time, when the
opinion comes to it; the file is read in place or streamed from TOS. The single-episode bundle and its export
report go into the module's output directory (``trajectory/episode_<N>.json`` and ``.report.json`` beside it),
where the overlay reads them; the run directory's delivery carries them.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import threading
from typing import Mapping

DERIVE_VERSION = "derive-umi-mcap/1"
DIR = "trajectory"
#: why an episode has no derived trajectory
EPISODE_MISSING, NOT_DERIVED, UNREADABLE, INVALID = ("episode_missing", "trajectory_not_derived", "recording_unreadable",
                                                     "trajectory_invalid")


def bundle_path(out_dir, ep: int) -> pathlib.Path:
    """Where the episode's derived bundle is (``out_dir``: the module's output directory)."""
    return pathlib.Path(out_dir) / DIR / f"episode_{int(ep):06d}.json"


def report_path(out_dir, ep: int) -> pathlib.Path:
    return pathlib.Path(out_dir) / DIR / f"episode_{int(ep):06d}.report.json"


def _write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _join(root: str, name: str) -> str:
    if "://" in str(root):
        from ...streams import objects as SO

        return SO.join(str(root), name)
    return os.path.join(str(root), name)


def handheld(root: str, name: str) -> bool:
    """Whether the recording ``name`` under ``root`` is a handheld gripper's: a hand's VIO pose topic (the built-in
    UMI layout). A file that cannot be read is not."""
    from .adapters import umi_mcap as X

    try:
        topics = X._topics_of(_join(root, name))
    except (OSError, ValueError, KeyError):
        return False
    return any(X.HAND_RE.match(t) and t.endswith("/vio/eef_pose") for t in topics)


class Derived:
    """The task's trajectory, derived episode by episode (each once, cached; thread-safe).

    ``root``: the dataset (a local directory or the ``tos://`` URI of a streamed one); ``numbering``: episode ->
    file name; ``calibration``: the uploaded ``umi-calibration/2`` file, None for the built-in one; ``out_dir``: the
    module's output directory. ``sha256`` names what the derivation depends on besides the recording (the
    calibration's bytes and this code's version), for ``--resume``."""

    def __init__(self, *, root: str, numbering: Mapping[int, str], calibration: str | None, out_dir: str,
                 dataset_id: str, horizon_s: float = 1.0):
        from .adapters import umi_mcap as X

        path = pathlib.Path(calibration) if calibration else X.BUILTIN_CALIBRATION
        raw = path.read_bytes()
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise X.ExportError(f"the gripper calibration is not JSON: {exc}") from None
        self.cfg = X.check_calibration(doc, "the gripper calibration" if calibration else "the built-in calibration")
        sha = hashlib.sha256(raw).hexdigest()
        self.calibration = {"gripper": self.cfg["gripper"], "builtin": calibration is None, "sha256": sha,
                            "assumed": X.assumed(self.cfg),
                            "intrinsics_fallback": sorted((self.cfg.get("intrinsics_fallback") or {}))}
        self.sha256 = hashlib.sha256(json.dumps({"calibration": sha, "version": DERIVE_VERSION, "generator": X.GENERATOR},
                                                sort_keys=True).encode()).hexdigest()
        self.root, self.numbering, self.out_dir = str(root), dict(numbering), out_dir
        self.dataset_id, self.horizon_s = dataset_id, float(horizon_s)
        self._guard = threading.Lock()
        self._locks: dict[int, threading.Lock] = {}
        self._done: dict[int, tuple] = {}

    @property
    def episodes(self) -> list[int]:
        return sorted(self.numbering)

    def sample(self, ep: int):
        """(the episode's ``EefSample`` or None, its ``trajectory_source``): None when the episode cannot be derived,
        the source then says why."""
        ep = int(ep)
        with self._guard:
            if ep in self._done:
                return self._done[ep]
            lock = self._locks.setdefault(ep, threading.Lock())
        with lock:
            with self._guard:
                if ep in self._done:
                    return self._done[ep]
            got = self._derive(ep)
            with self._guard:
                self._done[ep] = got
            return got

    def _source(self, report: dict | None = None, **why) -> dict:
        out = {"kind": "derived", "calibration": self.calibration}
        if report is not None:
            out.update(status=report.get("status"), rows=report.get("rows"), anchor_topic=report.get("anchor_topic"),
                       dropped_before_anchor=report.get("dropped_before_anchor"),
                       cameras={h: {k: c.get(k) for k in ("status", "reason", "pairing_rate", "intrinsics", "first_decodable",
                                                          "video_wh", "fx_fy")}
                                for h, c in (report.get("cameras") or {}).items()},
                       suspects=list(report.get("suspects") or []))
        out.update(why)
        return out

    def _derive(self, ep: int):
        from . import load
        from .adapters import umi_mcap as X

        name = self.numbering.get(ep)
        if name is None:
            return None, self._source(status="unsupported", reason=EPISODE_MISSING)
        try:
            bundle, report = X.episode_bundle(_join(self.root, name), ep, self.cfg, uri=name, dataset_id=self.dataset_id,
                                              horizon_s=self.horizon_s)
        except X.ExportError as exc:            # not a handheld gripper's recording, or nothing after the anchor
            return None, self._source(status="unsupported", reason=NOT_DERIVED, message=str(exc)[:300])
        except (OSError, ValueError, KeyError) as exc:
            return None, self._source(status="unsupported", reason=UNREADABLE, message=f"{type(exc).__name__}: {exc}"[:300])
        _write(report_path(self.out_dir, ep), json.dumps(report, ensure_ascii=False, indent=1).encode())
        if bundle is None:                      # every camera unsupported (no camera_info and no fallback, ...)
            return None, self._source(report, reason=NOT_DERIVED)
        data = json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode()
        res = load.load_bundle(bundle, check_media=False, episodes=[ep])
        if not res.ok or ep not in res.samples:
            message = res.errors[0].message if res.errors else "no sample"
            return None, self._source(report, status="unsupported", reason=INVALID, message=message[:300])
        _write(bundle_path(self.out_dir, ep), data)
        return res.samples[ep], self._source(report)
