"""L0 numeric trajectory: real Δt, duplicate collapse, jitter band, spikes, q / -q."""
from __future__ import annotations

import copy
import json

import numpy as np

from curation.extensions.eef_consistency import load, motion

from . import synth


def _sample(entry):
    r = load.load_bundle(json.dumps(synth.make_bundle([entry])).encode(), check_media=False)
    assert r.ok, [i.as_dict() for i in r.errors]
    return r.samples[entry["episode_index"]]


def _entry(n=150, **kw):
    pos, quat, grip = synth.eef_trajectory(n, **kw)
    return synth.make_entry(0, n, pos=pos, quat=quat, grip=grip)


def test_smooth_motion_has_low_high_frequency_energy():
    m = motion.l0_metrics(motion.state_series(_sample(_entry())))["summary"]
    assert m["time_source"] == "robot_clock" and m["n_states"] == 150 and m["spike_count"] == 0
    assert m["hf_pos_rms_max_mm"] < 0.5 and m["hf_rot_rms_max_deg"] < 0.1
    assert abs(m["state_rate_hz"] - 15.0) < 0.5 and abs(m["nyquist_hz"] - m["state_rate_hz"] / 2) < 1e-9


def test_five_hz_jitter_shows_up_below_nyquist():
    m = motion.l0_metrics(motion.state_series(_sample(_entry(jitter_m=0.006))))["summary"]
    assert m["hf_pos_rms_max_mm"] > 3.0
    assert m["resolvable_band_hz"][1] >= 5.0


def test_resampling_duplicates_are_collapsed_not_read_as_jitter():
    e = _entry(120)
    dup = copy.deepcopy(e)
    # repeat every 7th state on the next frame, like dataset1's 15 Hz regridding
    for i in range(7, 120, 7):
        dup["frames"][i]["eef"] = copy.deepcopy(dup["frames"][i - 1]["eef"])
        dup["frames"][i]["source_state_index"] = dup["frames"][i - 1]["source_state_index"]
        dup["frames"][i]["source_timing"] = copy.deepcopy(dup["frames"][i - 1]["source_timing"])
    s = motion.state_series(_sample(dup))
    assert s.duplicates_removed == len(range(7, 120, 7))
    m = motion.l0_metrics(s)["summary"]
    assert m["hf_pos_rms_max_mm"] < 1.0


def test_isolated_spike_is_detected():
    e = _entry(120)
    e["frames"][60]["eef"]["position_m"][2] += 0.03
    m = motion.l0_metrics(motion.state_series(_sample(e)))["summary"]
    assert m["spike_count"] >= 1 and 59 <= m["spike_frames"][0] <= 61


def test_quaternion_sign_flip_is_not_a_rotation_jump():
    e = _entry(90)
    for f in e["frames"][40:]:
        f["eef"]["quaternion_xyzw"] = [-x for x in f["eef"]["quaternion_xyzw"]]
    m = motion.l0_metrics(motion.state_series(_sample(e)))["summary"]
    assert m["ang_speed_max_deg_s"] < 30 and m["hf_rot_rms_max_deg"] < 0.1 and m["spike_count"] == 0


def test_no_pose_no_series():
    e = _entry(30)
    for f in e["frames"]:
        f["eef"] = None
    assert motion.state_series(_sample(e)) is None


def test_main_timeline_fallback_without_robot_clock():
    e = _entry(60)
    for f in e["frames"]:
        f["source_timing"] = []
    s = motion.state_series(_sample(e))
    assert s.time_source == "main_timeline"
    np.testing.assert_allclose(np.diff(s.t), 1 / synth.FPS, atol=1e-9)
