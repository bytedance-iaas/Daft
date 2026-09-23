"""Adapters: three-file sample <-> bundle entry; customer World_Policy samples -> form C / plain images."""
from __future__ import annotations

import json

import pytest

from curation.extensions.eef_consistency import capability as CAP
from curation.extensions.eef_consistency import contracts as C
from curation.extensions.eef_consistency import load
from curation.extensions.eef_consistency.adapters import unified_sample, world_policy

from . import demo_data, synth


def test_explode_then_pack_is_lossless(tmp_path):
    media_root = tmp_path / "lerobot"
    (media_root / "videos/cam").mkdir(parents=True)
    (media_root / "videos/cam/episode_000000.mp4").write_bytes(b"\0")
    entry = synth.make_entry(0, 20)
    folder = unified_sample.explode(entry, tmp_path / "samples", media_root=media_root)
    assert json.loads((folder / "sample.json").read_text())["annotations_path"] == "frames.jsonl"
    assert (folder / "frames.jsonl").read_text().count("\n") == 20
    back = unified_sample.pack(folder, 0, media_root=media_root)
    assert back == entry
    b = unified_sample.bundle([back], dataset_id="synthetic", codebase_version="v2.1", fps=15, episode_count=1)
    r = load.load_bundle(json.dumps(b).encode(), lerobot_root=media_root)
    assert r.ok


@pytest.mark.parametrize("family", ["droid", "LVP"])
def test_world_policy_reference_samples_are_accepted_but_unsupported(tmp_path, family):
    root = demo_data.ROOT / "reference" / family
    if not root.is_dir():
        pytest.skip("customer reference samples not present")
    dirs = sorted(p for p in root.iterdir() if (p / "sample.json").is_file())
    assert len(dirs) == 10
    entries = [world_policy.convert(d, tmp_path, family=family, episode_index=i) for i, d in enumerate(dirs)]
    b = unified_sample.bundle(entries, dataset_id=f"reference/{family}", codebase_version="n/a", fps=16,
                              episode_count=len(entries), media_uri_base="bundle_file")
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(b))
    r = load.load_bundle(path)                                    # media resolve next to the bundle
    assert r.ok, [i.as_dict() for i in r.errors[:5]]
    for ep, s in r.samples.items():
        assert s.timebase == "index_only" and not s.eef_mask.any()
        cap = CAP.sample_capability(s, observable={})
        assert cap["availability"] == C.UNSUPPORTED, cap["subitems"]
        views = {v["kind"] for v in s.sample["views"]}
        if family == "droid":
            assert views == {"camera", "pose_visualization"} and s.sample["raw_pose_sequence"]["semantics"] == "unresolved"
            assert s.n_frames == 65 and "source_view_2" in s.cameras and s.cameras["source_view_2"].mount == "wrist"
            assert cap["subitems"][C.STATE_MOTION]["reason_code"] == C.POSE_SEMANTICS_UNKNOWN
        else:
            assert views == {"image"} and not s.cameras and s.n_frames == 1
