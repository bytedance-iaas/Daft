"""F5.1 acceptance on the DEMO data: both trajectory.json files read, form B everywhere, self-consistent."""
from __future__ import annotations

import pytest

from curation.extensions.eef_consistency import capability as CAP
from curation.extensions.eef_consistency import contracts as C
from curation.extensions.eef_consistency import load

from . import demo_data


@pytest.fixture(scope="module", params=sorted(demo_data.DATASETS))
def loaded(request):
    name = request.param
    root = demo_data.require(name)
    return name, load.load_bundle(root / "trajectory.json", lerobot_root=demo_data.lerobot_root(name))


def test_bundle_reads_with_no_errors_or_warnings(loaded):
    name, r = loaded
    spec = demo_data.DATASETS[name]
    assert r.ok, [i.as_dict() for i in r.errors[:5]]
    assert not r.warnings, [i.as_dict() for i in r.warnings[:5]]
    assert sorted(r.samples) == list(range(spec["episodes"]))
    assert all(s.n_frames == spec["frames"] and len(s.cameras) == spec["cameras"] for s in r.samples.values())
    assert r.meta["media_uri_base"] == "lerobot_root"


def test_recomputed_projection_is_within_source_quantisation(loaded):
    _, r = loaded
    assert r.report["max_reprojection_difference_px"] < C.REPROJECTION_TOLERANCE_PX
    for s in r.samples.values():
        assert s.consistency["max_px"] < C.REPROJECTION_TOLERANCE_PX


def test_form_b_capability_is_available_for_every_episode(loaded):
    _, r = loaded
    for ep, s in r.samples.items():
        observable = {cid: set(load.declared_point_ids(s, cid)) for cid in s.cameras}
        cap = CAP.sample_capability(s, observable=observable)
        for k in CAP.CORE_SUBITEMS + (C.INPUT_CONSISTENCY,):
            assert cap["subitems"][k]["availability"] == C.AVAILABLE, (ep, k, cap["subitems"][k])
        assert s.pose_type == "absolute" and s.has_absolute_pose


def test_dataset2_episode6_keeps_the_declared_extrinsic_error():
    """The wrong ext1 extrinsics are the input under test: identical provided/recomputed, never replaced."""
    root = demo_data.require("dataset2")
    r = load.load_bundle(root / "trajectory.json", lerobot_root=demo_data.lerobot_root("dataset2"), episodes=[0, 6])
    s0, s6 = r.samples[0], r.samples[6]
    T0 = s0.cameras["27432424_left"].T_reference_camera[0]
    T6 = s6.cameras["27432424_left"].T_reference_camera[0]
    assert abs(T0[:3, 3] - T6[:3, 3]).max() > 0.02                     # ext1 differs by the declared 3 cm
    assert abs(s0.cameras["28221883_left"].T_reference_camera[0] - s6.cameras["28221883_left"].T_reference_camera[0]
               ).max() < 1e-12                                          # ext2 untouched
