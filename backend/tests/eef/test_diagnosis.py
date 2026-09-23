"""F5.3 diagnosis on synthetic geometry (no video): each fit recovers the injected quantity, and a
hypothesis whose fit does not reach the noise floor stays unsupported."""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from curation.extensions.eef_consistency import contracts as C
from curation.extensions.eef_consistency import diagnosis as DG
from curation.extensions.eef_consistency import load
from curation.extensions.eef_consistency import metrics as MX
from curation.extensions.eef_consistency import profile
from curation.extensions.eef_consistency import runner as R

from . import synth

N = 90
PROF = profile.load("demo")
RNG = np.random.default_rng(7)


def _sample(entry):
    r = load.load_bundle(json.dumps(synth.make_bundle([entry])).encode(), check_media=False)
    assert r.ok, [i.as_dict() for i in r.errors[:3]]
    return r.samples[0]


def _measure(sample, observed: dict[str, np.ndarray]) -> R.CameraMeasure:
    t = sample.t
    cm = R.CameraMeasure("cam0", {}, t, synth.FPS)
    for pid, uv in observed.items():
        cm.obs[pid] = uv
        cm.obs_vis[pid] = np.where(np.isfinite(uv).all(1), "visible", "uncertain")
        cm.positions[pid] = MX.position(sample, "cam0", pid, uv)
        cm.hf[pid] = MX.hf_share(cm.positions[pid]["e_vec"], t, synth.FPS)
    return cm


def _truth(pos, quat, grip, T_bc=None, noise=0.4):
    px = synth.truth_pixels(pos, quat, grip, synth.camera_pose() if T_bc is None else T_bc)
    return {pid: uv + RNG.normal(0, noise, uv.shape) for pid, (uv, _) in px.items()
            if pid in ("tcp", "finger_plus_y", "finger_minus_y", "eef_origin")}


def test_pnp_recovers_a_wrong_declared_extrinsic():
    pos, quat, grip = synth.eef_trajectory(N)
    T_true = synth.camera_pose()
    wrong = T_true @ np.block([[Rotation.from_euler("y", 2, degrees=True).as_matrix(), np.array([[0.03], [0], [0]])],
                               [np.zeros((1, 3)), np.ones((1, 1))]])
    s = _sample(synth.make_entry(0, N, pos=pos, quat=quat, grip=grip, declared_T=wrong))
    cm = _measure(s, _truth(pos, quat, grip, T_true))
    h = DG.extrinsics(s, cm, PROF)
    assert h["supported"] and h["plausible"]
    assert h["fitted"]["delta_translation_mm"] == pytest.approx(30, abs=4)
    assert h["fitted"]["delta_rotation_deg"] == pytest.approx(2, abs=0.4)
    assert h["residual_after_px"] < 1.0 < h["residual_before_px"]


def test_pnp_does_not_explain_a_pose_drift():
    pos, quat, grip = synth.eef_trajectory(N)
    t = np.arange(N) / synth.FPS
    drifted = pos + np.stack([0.04 * np.sin(0.7 * t), 0.03 * np.sin(1.1 * t + 1), 0.02 * np.cos(0.5 * t)], 1)
    s = _sample(synth.make_entry(0, N, pos=drifted, quat=quat, grip=grip))
    cm = _measure(s, _truth(pos, quat, grip))
    h = DG.extrinsics(s, cm, PROF)
    assert not h["supported"]


def test_constant_body_rotation_is_recovered_in_3d():
    pos, quat, grip = synth.eef_trajectory(N)
    bad = (Rotation.from_quat(quat) * Rotation.from_euler("z", 25, degrees=True)).as_quat()
    grip_open = np.zeros(N)                                             # fingers apart: spin about z observable
    s = _sample(synth.make_entry(0, N, pos=pos, quat=bad, grip=grip_open))
    cm = _measure(s, _truth(pos, quat, grip_open))
    cm.orientations["finger_line"] = MX.orientation(s, "cam0", "finger_line", cm.obs, min_len_px=5.0)
    orient = {"status": C.SUSPECT, "axes": {"finger_line": {"status": C.SUSPECT,
                                                            "metrics": {"signed_median_deg": 10.0}}}}
    pos_cell = {"points": {"tcp": {"metrics": {"median_px": 0.5}}}}
    (h,) = DG.constant_orientation(s, cm, orient, pos_cell, [], PROF)
    assert h["supported"]
    assert h["fitted"]["delta_rotation_deg"] == pytest.approx(25, abs=1.5)
    assert abs(h["fitted"]["rotation_axis_eef"][2]) > 0.95


def test_axial_offset_along_the_approach_axis():
    pos, quat, grip = synth.eef_trajectory(N)
    e = synth.make_entry(0, N, pos=pos, quat=quat, grip=grip)
    obs = _truth(pos, quat, grip)
    e = copy.deepcopy(e)
    e["sample"]["point_definitions"]["tcp"]["position_eef_m"] = [0, 0, 0.12]    # declared 4 cm short
    e["sample"]["axis_definitions"]["z"]["length_m"] = None
    s = _sample(e)
    s.cameras["cam0"].provided.pop("tcp")                                        # recomputed from the definition
    cm = _measure(s, {"tcp": obs["tcp"]})
    cell = {"points": {"tcp": {"status": C.SUSPECT}}}
    (h,) = DG.tcp_axial(s, cm, cell, PROF)
    assert h["supported"] and h["fitted"]["offset_along_approach_mm"] == pytest.approx(40, abs=3)


def test_no_profile_no_hypotheses():
    assert DG.hypotheses(None, {}, {}, {}, None) == []
