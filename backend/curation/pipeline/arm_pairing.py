"""Which arm each wrist camera rides, measured on the CPU rather than looked at (MV-2).

A wrist camera is bolted to a hand: when that hand moves the whole picture swings, when the other
hand moves it does not. The dataset records both hands, so "which arm does this camera ride" is a
measurement, not a judgement - and a far better one than the picture evidence a model has to work
from, which on a handheld rig is the other gripper showing as some twenty pixels at the rim of a
fisheye circle (what a model answered on 24 FastUMI episodes: 1 crossing found, 20 abstentions).

The measurement, per episode:

1. **the picture's own motion**, per camera and per sample: how far the whole frame shifted from the
   one before it, by phase correlation over a small grey copy - no features, no objects, a few
   milliseconds a frame, and no notion of what is in the picture;
2. **each arm's speed**, per sample, from the dataset's own recorded channels (``left_x …`` /
   ``right_x …``, which :mod:`curation.pipeline.dataset_claim` split into columns);
3. **the moments that carry the answer**: those where ONE arm moves and the others are still. A
   camera's reading for an arm is how far its picture travelled per metre that arm travelled in
   those moments, so the two readings are comparable even when one hand is the busy one. Moments
   where both hands move together say nothing and are left out.
4. **one pairing for the whole episode**, not a choice per camera: two cameras and two arms admit
   two pairings (as named, or crossed), each scored by the readings of the arm it gives each camera.
   The better pairing wins, and only by a margin. Deciding per camera instead let both cameras pick
   the same arm - a contradiction that reads as a crossing, and was behind 3 of the 4 wrong answers
   the per-camera version gave on the same 24 episodes.

A camera is called crossed when the arm the episode's best pairing gives it is not the arm the
dataset puts it on. Anything short of a clear margin answers ``unknown``: a wrong "consistent" hides
a broken dataset, and an unknown costs nothing but a line in the report.

Measured on ``anchor-arms-fastumi`` (14 episodes whose camera files are crossed, confirmed by the
pixel rule, and 10 the sample set found no crossing in): 12 of the 14 crossings found and 2
unknown, 8 of the 10 clean episodes called consistent with 1 unknown and 1 called crossed; 11
seconds for all 48 videos, and not one model request. The model, asked the same question about the
same episodes, found 1 crossing and abstained on 20. No episode was called consistent that was
crossed, which is the error that matters: it would hide a broken dataset.
"""
from __future__ import annotations

import itertools
import math

#: samples a second of both series; the videos are 20-60 fps, the recordings 20-50 Hz
HZ = 10.0
#: width the grey copy is shrunk to before the frame-to-frame shift is estimated
SMALL_W = 160
#: an arm's own moments: it is in its top quarter of speed while every other arm is in its bottom half
BUSY_Q, QUIET_Q = 0.75, 0.50
#: and is clearly slower at that very moment - two hands moving together are nobody's solo moment,
#: however the quantiles fall
SOLO_RATIO = 0.5
#: how many such moments each arm needs for its column of readings to count (about a second of
#: solo motion; a mean over fewer samples than this was where the one false "consistent" came from)
MIN_MOMENTS = 8
#: how far the best pairing must beat the next one, in summed shares (a share is 0..1 per camera)
MIN_MARGIN = 0.10
#: pairings are enumerated, so refuse the rare rig with too many of either to enumerate
MAX_SIDES = 4


def _open(path: str):
    """The video, opened the way the rest of the kernel opens one: a remote path through the
    episode's shared blocks when the stage opened them, else a presigned source."""
    import av

    from ..adapters.decode import REMOTE_OPEN_OPTIONS
    from ..ingest import dsfs
    from ..streams import blockcache

    remote = dsfs.is_remote(path) or path.startswith(("https://", "http://"))
    handle = blockcache.open_remote(path) if remote else None
    if handle is not None:
        return av.open(handle)
    if remote:
        return av.open(dsfs.media_source(path), options=REMOTE_OPEN_OPTIONS)
    return av.open(dsfs.media_source(path))


def picture_motion(path: str, *, from_ts: float = 0.0, to_ts: float | None = None,
                   hz: float = HZ, width: int = SMALL_W) -> list[float]:
    """How far the whole picture shifted between consecutive samples of this camera's episode
    window, in pixels of the shrunken copy, about ``hz`` samples a second."""
    import numpy as np

    small = []
    with _open(path) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        rate = float(stream.average_rate or hz) or hz
        every = max(1, int(round(rate / hz)))
        tb = float(stream.time_base or 0) or (1.0 / rate)
        kept = 0
        for frame in container.decode(stream):
            when = float(frame.pts) * tb if frame.pts is not None else kept / rate
            if when < from_ts:
                continue
            if to_ts is not None and when > to_ts:
                break
            kept += 1
            if (kept - 1) % every:
                continue
            grey = frame.to_ndarray(format="gray").astype("float32")
            step = max(1, grey.shape[1] // width)
            small.append(grey[::step, ::step])
    out: list[float] = []
    for a, b in zip(small, small[1:]):
        if a.shape != b.shape:
            break
        fa, fb = np.fft.rfft2(a), np.fft.rfft2(b)
        cross = fa * np.conj(fb)
        mag = np.abs(cross)
        with np.errstate(invalid="ignore", divide="ignore"):
            cross = np.where(mag > 1e-9, cross / np.where(mag > 1e-9, mag, 1.0), 0)
        corr = np.fft.irfft2(cross, s=a.shape)
        peak = np.unravel_index(int(np.argmax(corr)), corr.shape)
        dy = peak[0] if peak[0] <= a.shape[0] // 2 else peak[0] - a.shape[0]
        dx = peak[1] if peak[1] <= a.shape[1] // 2 else peak[1] - a.shape[1]
        out.append(math.hypot(float(dx), float(dy)))
    return out


def arm_speeds(action, channels: dict[str, list[int]], *, fps: float = 20.0, hz: float = HZ,
               from_ts: float = 0.0) -> dict[str, list[float]]:
    """Each arm's speed at the same instants :func:`picture_motion` samples: how far its recorded
    end point moved in one recorded step there.

    One step at the sample instant, not the path summed over the whole interval between samples:
    both series are then "how fast is this moving right now", and the recording's own jitter
    between samples stays out of it.
    """
    import numpy as np

    a = np.asarray(action, dtype="float64")
    if a.ndim != 2 or len(a) < 3:
        return {}
    fps = float(fps) if fps and math.isfinite(float(fps)) and float(fps) > 0 else 20.0
    step = max(1, int(round(fps / hz)))
    start = max(0, int(round(float(from_ts) * fps)))
    out: dict[str, list[float]] = {}
    for arm, cols in channels.items():
        cols = [c for c in cols if isinstance(c, int) and 0 <= c < a.shape[1]]
        if len(cols) < 2:
            continue
        travel = np.linalg.norm(np.diff(a[start:, cols], axis=0), axis=1)
        if len(travel) < step * 2:
            continue
        out[arm] = [float(x) for x in travel[::step]]
    return out


def moments(speeds: dict[str, list[float]], n: int) -> dict[str, list[int]]:
    """Per arm, the samples where it alone is moving; empty when fewer than two arms are given."""
    import numpy as np

    arrays = {arm: np.asarray(v[:n]) for arm, v in speeds.items() if len(v) >= n}
    if len(arrays) < 2 or n < MIN_MOMENTS * 2:
        return {}
    out: dict[str, list[int]] = {}
    for arm, v in arrays.items():
        # >= rather than >, so a series that sits at one speed while it moves still has moments
        busy = (v >= np.quantile(v, BUSY_Q)) & (v > 0)
        still = np.ones(n, dtype=bool)
        for other, u in arrays.items():
            if other != arm:
                still &= (u <= np.quantile(u, QUIET_Q)) & (u < SOLO_RATIO * v)
        out[arm] = [int(i) for i in np.flatnonzero(busy & still)]
    return out


def readings(motion: dict[str, list[float]], speeds: dict[str, list[float]]) -> tuple[dict, dict]:
    """``({camera: {arm: picture travel per unit of arm travel}}, {arm: how many moments})``.

    Both are empty when the episode has no moment where one arm alone moves - two hands that always
    move together cannot be told apart by their cameras, and that is an honest ``unknown``.
    """
    import numpy as np

    lengths = [len(v) for v in motion.values()] + [len(v) for v in speeds.values()]
    n = min(lengths) if lengths else 0
    found = moments(speeds, n)
    if not found or any(len(ix) < MIN_MOMENTS for ix in found.values()):
        return {}, {arm: len(ix) for arm, ix in found.items()}
    table: dict[str, dict[str, float]] = {}
    for camera, series in motion.items():
        m = np.asarray(series[:n])
        row = {}
        for arm, ix in found.items():
            travelled = float(np.asarray(speeds[arm][:n])[ix].mean())
            row[arm] = round(float(m[ix].mean()) / travelled, 3) if travelled > 1e-9 else 0.0
        table[camera] = row
    return table, {arm: len(ix) for arm, ix in found.items()}


def match(table: dict[str, dict[str, float]], arms: list[str]) -> tuple[dict[str, str], float]:
    """The best pairing of these cameras onto these arms, and how far it beat the next one.

    Each camera contributes the share of its own readings that falls on the arm the pairing gives
    it, so a camera that cannot tell the arms apart contributes about the same to every pairing and
    the margin comes from the cameras that can.

    Two cameras and two arms give the two pairings one expects. More cameras than arms (two of them
    on the same hand) give every mapping that leaves no arm without a camera, and more arms than
    cameras give every mapping that puts no two cameras on one arm - in both the declared mapping is
    among them, which is what the margin is measured against.
    """
    cameras = sorted(table)
    if len(cameras) < 2 or len(arms) < 2 or len(cameras) > MAX_SIDES or len(arms) > MAX_SIDES:
        return {}, 0.0
    if len(cameras) >= len(arms):
        options = [p for p in itertools.product(arms, repeat=len(cameras)) if set(p) == set(arms)]
    else:
        options = list(itertools.permutations(arms, len(cameras)))
    scored = []
    for pairing in options:
        total = 0.0
        for camera, arm in zip(cameras, pairing):
            row = table[camera]
            whole = sum(max(0.0, v) for v in row.values())
            total += (max(0.0, row.get(arm, 0.0)) / whole) if whole > 1e-12 else 1.0 / len(row or [1])
        scored.append((total, pairing))
    if len(scored) < 2:                     # nothing to compare the best mapping against
        return {}, 0.0
    scored.sort(key=lambda s: -s[0])
    best, runner_up = scored[0], scored[1]
    return dict(zip(cameras, best[1])), round(best[0] - runner_up[0], 3)


def declared_arm(camera: str, said: dict, arms: list[str]) -> str | None:
    """The arm the dataset puts this camera on, or None when it puts it on no arm.

    The declaration (C7) answers first; without one the arm word in the camera's own name answers,
    which is the dataset's claim too and the one MV-2 is about: a file called ``left_camera`` claims
    to be the left hand's. A camera the declaration calls fixed rides no arm and is left out.
    """
    spec = said.get(camera) or {}
    if spec.get("mount") == "fixed_external":
        return None
    owner = spec.get("owner")
    if isinstance(owner, str) and owner.strip().lower() in arms:
        return owner.strip().lower()
    hits = [a for a in arms if a in camera.lower()]
    return hits[0] if len(hits) == 1 else None


def pair(video: dict, action, claim: dict | None, *, fps: float = 20.0) -> dict:
    """``{identity, crossed, note, basis, reason, readings}`` for one episode; never raises.

    ``video`` is ``{camera: {"path", "from_ts", "to_ts"}}`` as the stage holds it, ``action`` the
    episode's recorded action rows, ``claim`` what the dataset says about itself
    (:mod:`curation.pipeline.dataset_claim`). Everything that cannot be measured - one arm, one
    camera, no declared owner, two hands that always move together - answers ``unknown``, with the
    words in ``note`` and the registry's reason code in ``reason`` (``not_applicable`` when the
    dataset gives nothing to measure, ``measurement_inconclusive`` when it was measured and the
    evidence did not settle it), because MV-2 is a comparison and a missing half is not a verdict.
    """
    out: dict = {"identity": "unknown", "crossed": [], "note": "", "basis": "motion_pairing",
                 "reason": "not_applicable", "readings": {}}
    claim = claim or {}
    arms = [a for a in (claim.get("arms") or []) if isinstance(a, str)]
    channels = {a: (claim.get("channels") or {}).get(a) or [] for a in arms}
    if len(arms) < 2:
        out["note"] = "数据集只记了一条臂，没有左右手身份可比"
        return out
    said = claim.get("cameras") or {}
    declared = {str(c): declared_arm(str(c), said, arms) for c in (video or {})}
    mine = {str(c): s for c, s in (video or {}).items() if declared[str(c)]}
    if len(mine) < 2:
        out["note"] = "只有一路相机标明了属于哪条臂，单路看不出身份是否串线"
        return out
    if len({declared[c] for c in mine}) < 2:
        out["note"] = "标明了臂的相机都在同一条臂上，没有可对调的两路"
        return out
    try:
        speeds = arm_speeds(action, channels, fps=fps)
        if len(speeds) < 2:
            out["note"] = "数据集的动作通道分不出两条臂的列"
            return out
        motion = {}
        for camera, spec in sorted(mine.items()):
            spec = spec if isinstance(spec, dict) else {}
            to_ts = spec.get("to_ts")
            motion[camera] = picture_motion(
                str(spec.get("path")), from_ts=float(spec.get("from_ts") or 0.0),
                to_ts=float(to_ts) if to_ts is not None else None)
        table, counts = readings(motion, speeds)
        paired, margin = match(table, arms) if table else ({}, 0.0)
    except Exception as exc:  # noqa: BLE001 - a reading that could not be taken, not the stage's error
        out["note"] = f"读不出画面运动或各臂速度：{type(exc).__name__}: {exc}"[:200]
        out["reason"] = "measurement_inconclusive"
        return out
    out["readings"] = {"hz": HZ, "declared": {c: declared[c] for c in mine}, "moments": counts,
                       "moves_with": table, "margin": margin, "paired": paired}
    out["reason"] = "measurement_inconclusive"
    if not table:
        out["note"] = "没有一刻是只有一条臂在动，两条臂的画面分不开"
        return out
    if not paired or margin < MIN_MARGIN:
        out["note"] = "几种对应关系的证据一样强，判不出哪路相机跟着哪条臂"
        return out
    out["reason"] = ""
    crossed = sorted(c for c, arm in paired.items() if declared.get(c) and arm != declared[c])
    if crossed:
        # the note says which camera rides what; the headline is the report's and the console's
        out.update(identity="crossed", crossed=crossed,
                   note="、".join(f"{c.split('.')[-1]} 的画面跟着 {paired[c]} 动" for c in crossed)
                        + f"（余量 {margin}）")
    else:
        out.update(identity="consistent", note="每路相机的画面运动都跟着数据集给它的那条臂")
    return out
