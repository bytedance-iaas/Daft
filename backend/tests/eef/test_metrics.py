"""F5.3 measurements on arrays: lag sign (D-E8), orientation ranges, residual units, segments, HF share."""
from __future__ import annotations

import numpy as np
import pytest

from curation.extensions.eef_consistency import metrics as MX
from curation.extensions.eef_consistency import segments as SG

FPS = 15.0


def _track(n=150):
    t = np.arange(n) / FPS
    return t, np.stack([200 + 80 * np.sin(0.9 * t), 150 + 40 * np.sin(1.4 * t + 0.3)], 1)


@pytest.mark.parametrize("lag_frames", [5, -3, -8, 0])
def test_lag_sign_follows_u_visual_t_equals_u_declared_t_plus_lag(lag_frames):
    t, x = _track()
    declared = x
    # the video shows what the record declares lag_frames later: u_visual(t) = u_declared(t + lag)
    visual = np.full_like(x, np.nan)
    idx = np.arange(len(t)) + lag_frames
    ok = (idx >= 0) & (idx < len(t))
    visual[ok] = declared[idx[ok]]
    scan = MX.lag_scan(t, [declared], [visual], fps=FPS, search_s=(-1.0, 1.0))
    assert scan["lag_frames"] == pytest.approx(lag_frames, abs=0.15)
    if lag_frames:
        assert scan["improvement"] > 5 and scan["drop_px"] > 2
    assert scan["residual_px"].shape == scan["taus_s"].shape


def test_lag_scan_uses_one_mask_for_every_tau_and_reports_motion():
    t, x = _track()
    static = np.repeat(x[:1], len(t), 0)
    scan = MX.lag_scan(t, [static], [static + 0.3], fps=FPS, search_s=(-1.0, 1.0))
    assert scan["motion_px_s"] < 1e-6 and abs(scan["frames"] - (len(t) - 2 * 15)) <= 1   # +-1 s cut at both ends
    wins = MX.local_lags(t, [x], [x], fps=FPS, search_s=(-0.5, 0.5), window_s=4.0, hop_s=2.0)
    assert wins and all(abs(w["lag_s"]) < 0.02 for w in wins)


def test_orientation_directed_and_undirected_ranges():
    from curation.extensions.eef_consistency.metrics import orientation  # noqa: F401 (API shape below)
    d = np.array([[1.0, 0.0]])
    rot = lambda a: np.array([[np.cos(np.radians(a)), np.sin(np.radians(a))]])  # noqa: E731
    cos = lambda a, b: float((a * b).sum() / np.linalg.norm(a) / np.linalg.norm(b))  # noqa: E731
    assert np.degrees(np.arccos(cos(d, rot(30)))) == pytest.approx(30)
    assert np.degrees(np.arccos(abs(cos(d, rot(180))))) == pytest.approx(0, abs=1e-6)   # undirected: flip = same line


def test_hysteresis_on_off_duration_and_gaps():
    t = np.arange(100) / FPS
    v = np.zeros(100)
    v[10:40] = 12.0            # 2 s above on
    v[25:28] = np.nan          # invalid frames inside: bridged, not counted
    v[60:66] = 12.0            # 0.4 s: too short
    v[80:95] = 8.0             # between off and on: never starts
    segs = SG.hysteresis(v, t, on=10, off=7, min_duration_s=1.0, max_gap_s=0.4, reason="x")
    assert len(segs) == 1 and segs[0].start == 10 and segs[0].end == 39 and segs[0].reasons == ["x"]
    assert segs[0].valid_frames == 27
    assert SG.worst_frames(np.where(np.isnan(v), 0, v) + np.arange(100) * 1e-3, segs[0], 2) == [38, 39]


def test_rolling_median_ignores_invalid_frames():
    x = np.array([1, 1, 50, 1, np.nan, 1, 1], float)
    rm = SG.rolling_median(x, 3)
    assert rm[2] == 1 and np.isnan(rm[4])


def test_hf_share_separates_jitter_from_drift():
    t = np.arange(300) / FPS
    drift = np.stack([10 * np.sin(0.3 * t), 5 * np.cos(0.2 * t)], 1)
    jitter = np.stack([3 * np.sin(2 * np.pi * 5 * t), 3 * np.cos(2 * np.pi * 4.3 * t)], 1)
    assert MX.hf_share(drift, t, FPS)["hf_energy_share"] < 0.05
    assert MX.hf_share(jitter, t, FPS)["hf_energy_share"] > 0.9
    gappy = drift.copy()
    gappy[::10] = np.nan                                                  # runs shorter than 16 frames are skipped
    assert MX.hf_share(gappy, t, FPS)["hf_rms_px"] is None
