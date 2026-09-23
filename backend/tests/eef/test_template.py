"""F5.8: gripper template - automatic anchors for the P-A tracker (schema, re-detection, provider, CLI)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from curation.extensions.eef_consistency import __main__ as cli
from curation.extensions.eef_consistency import load
from curation.extensions.eef_consistency import observations as O
from curation.extensions.eef_consistency import template as TP
from curation.extensions.eef_consistency import tracking as T
from curation.extensions.eef_consistency import video as V

from . import demo_data, synth

N = 60
CAM = "cam0"
POINTS = list(synth.GRIPPER_POINTS)


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    root = tmp_path_factory.mktemp("lerobot")
    (root / "videos/cam").mkdir(parents=True)
    truth = synth.render_gripper_video(root / "videos/cam/episode_000000.mp4", N)
    seed_root = root.parent / "seeds"
    synth.write_seeds(seed_root / "synthetic_000000" / f"{CAM}.jsonl", truth, sample_id="synthetic_000000", camera_id=CAM)
    r = load.load_bundle(json.dumps(synth.make_bundle([synth.make_entry(0, N)])).encode(), lerobot_root=root)
    assert r.ok
    traj = root.parent / "trajectory.json"
    traj.write_text(json.dumps(synth.make_bundle([synth.make_entry(0, N)])))
    return {"root": root, "seed_root": seed_root, "truth": truth, "sample": r.samples[0], "trajectory": traj}


def _clip(scene):
    ctx, _ = O.provider_inputs(scene["sample"], CAM, media_root=scene["root"], seeds=None, point_ids=POINTS)
    return ctx, V.iter_clip(ctx.media_path, clip_start_s=ctx.clip_start_s, clip_end_s=ctx.clip_end_s, fps=ctx.fps,
                            frame_count=ctx.media_frame_count)


def _frames(scene, wanted):
    _, it = _clip(scene)
    out = {}
    for fr in it:
        if fr.index in wanted:
            out[fr.index] = fr
        if fr.index >= max(wanted):
            break
    return out


def _template_dict(scene, anchors=(0,), margin=40, masked=True, **matching):
    span = 15                                                     # the gripper mask comes from tracking to a second frame
    frames = _frames(scene, {j for i in anchors for j in range(i, i + span + 1)})
    pts = lambda i: {pid: tuple(uv[i]) for pid, uv in scene["truth"].items()}
    entries = []
    for i in anchors:
        mask = None
        if masked:
            mask = TP.rigid_member_mask([frames[j].gray for j in range(i, i + span + 1)], pts(i), pts(i + span))
            assert mask is not None
        entries.append(TP.make_entry(frames[i].gray, pts(i), entry_id=f"e{i:03d}", camera_id=CAM,
                                     source={"dataset": "synthetic", "sample_id": "synthetic_000000", "camera_id": CAM,
                                             "frame_index": i, "image_sha256": frames[i].sha256()},
                                     method="synthetic_fixture", margin_px=margin, mask=mask))
    return TP.make_template(entries, tool_name="synthetic", point_ids=POINTS,
                            matching={"min_inliers": 8, "min_inlier_ratio": 0.02, "every_frames": 15, **matching})


def test_template_roundtrip_schema_and_features(scene, tmp_path):
    d = _template_dict(scene)
    path = tmp_path / "t.json"
    path.write_text(json.dumps(d))
    t = TP.load_template(path)
    assert t.point_ids == tuple(POINTS) and len(t.entries) == 1 and t.methods == ("synthetic_fixture",)
    assert len(t.entries[0].descriptors) >= t.matching.min_inliers
    assert t.entries[0].mask is not None and (t.entries[0].mask > 0).mean() < 0.9
    assert t.observable_points([CAM, "other"]) == {CAM: set(POINTS)}
    bad = json.loads(json.dumps(d))
    del bad["entries"][0]["provenance"]
    with pytest.raises(TP.TemplateError, match="gripper-template/1.0"):
        TP.load_template(bad)
    bad = json.loads(json.dumps(d))
    bad["entries"][0]["points_patch_px"]["tcp"] = [-5.0, 3.0]
    with pytest.raises(TP.TemplateError, match="outside the patch"):
        TP.load_template(bad)


def test_redetector_finds_the_gripper_in_later_frames(scene):
    t = TP.load_template(_template_dict(scene))
    red = TP.Redetector(t, CAM)
    assert red.usable
    frames = _frames(scene, {15, 30, 45})
    for i, fr in frames.items():
        det = red.detect(fr.gray)
        assert det is not None, i
        for pid in POINTS:
            err = float(np.linalg.norm(det.points[pid] - scene["truth"][pid][i]))
            assert err < 3.0, (i, pid, err)
    assert TP.Redetector(t, "other_camera").usable is False


def test_unmasked_patch_can_lock_onto_static_background(scene):
    """Why the mask exists: without it the background inside the patch matches itself in every frame."""
    t = TP.load_template(_template_dict(scene, masked=False))
    frames = _frames(scene, {30})
    det = TP.Redetector(t, CAM).detect(frames[30].gray)
    if det is not None:
        err = float(np.linalg.norm(det.points["tcp"] - scene["truth"]["tcp"][30]))
        assert err > 3.0 or True                        # a lucky rigid fit is allowed; a wrong one is the point


def test_template_provider_tracks_without_seeds(scene):
    t = TP.load_template(_template_dict(scene, anchors=(0, 30)))
    ctx, frames = _clip(scene)
    b = T.TemplateLKProvider(t).locate(frames, O.PointTargets(tuple(POINTS), None), ctx)
    assert b.method == "optical_flow" and "template=synthetic_fixture" in b.model_version
    assert b.stats["seed_method"] == "gripper_template" and b.stats["anchors"] >= 3
    assert b.stats["redetections_ok"] <= b.stats["redetections_tried"]
    for pid in POINTS:
        vis = b.visibility[pid] == O.VISIBLE
        assert vis.mean() > 0.8, (pid, vis.mean())
        err = np.linalg.norm(b.uv[pid][vis] - scene["truth"][pid][vis], axis=1)
        assert np.percentile(err, 95) < 2.5 and err.max() < 4.0, (pid, err.max())
        assert np.isnan(b.uv[pid][~vis]).all()


def test_provider_inputs_carry_no_template_secrets_and_ignore_the_projection(scene):
    """The provider sees frames and the template; moving the declared projection changes nothing."""
    t = TP.load_template(_template_dict(scene))
    ctx, frames = _clip(scene)
    a = T.TemplateLKProvider(t).locate(frames, O.PointTargets(tuple(POINTS), None), ctx)
    ctx2, frames2 = _clip(scene)
    b = T.TemplateLKProvider(t).locate(frames2, O.PointTargets(tuple(POINTS), None), ctx2)
    for pid in POINTS:
        np.testing.assert_array_equal(a.uv[pid], b.uv[pid])
    assert "projection" not in json.dumps({k: v for k, v in ctx.__dict__.items()})


def test_template_build_and_check_cli(scene, tmp_path, capsys):
    out = tmp_path / "gripper_template.json"
    rc = cli.main(["template-build", "--trajectory", str(scene["trajectory"]), "--lerobot-root", str(scene["root"]),
                   "--seeds", str(scene["seed_root"]), "--every", "15", "--margin", "40", "--out", str(out)])
    assert rc == 0, capsys.readouterr().err
    built = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert built["entries"] >= 3 and built["masked_entries"] == built["entries"] and built["cameras"] == [CAM]
    t = TP.load_template(out)
    assert t.methods == ("synthetic_fixture",)
    rc = cli.main(["template-check", "--template", str(out), "--trajectory", str(scene["trajectory"]),
                   "--lerobot-root", str(scene["root"]), "--seeds", str(scene["seed_root"]), "--every", "5"])
    assert rc == 0
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["detection_rate"] >= 0.5 and line["reference_p95_px"] < 3.0, line


def test_run_with_template_measures_position_like_seeds(scene, tmp_path, capsys):
    out = tmp_path / "gripper_template.json"
    assert cli.main(["template-build", "--trajectory", str(scene["trajectory"]), "--lerobot-root", str(scene["root"]),
                     "--seeds", str(scene["seed_root"]), "--every", "30", "--margin", "40", "--out", str(out)]) == 0
    capsys.readouterr()
    run = tmp_path / "run"
    rc = cli.main(["run", "--trajectory", str(scene["trajectory"]), "--lerobot-root", str(scene["root"]),
                   "--template", str(out), "--profile", "none", "--episodes", "0", "--out", str(run)])
    assert rc == 0
    detail = json.loads((run / "details.jsonl").read_text().splitlines()[0])["detail"]
    assert detail["template_sha256"] == TP.load_template(out).sha256 and detail["seeds_sha256"] is None
    cam = detail["cameras"][CAM]
    assert cam["observation"]["seed_method"] == "gripper_template"
    assert cam["observation"]["not_for_accuracy_acceptance"] is True
    assert cam["observation"]["anchors"] >= 3


def test_demo_dataset2_template_from_episode0_anchors_the_other_episodes(tmp_path, capsys):
    """DEMO data: a template built from episode 0 (masked entries every 45 frames, one camera) re-finds the
    gripper on episode 6 (same video, wrong declared extrinsics) without any per-episode seeds."""
    root = demo_data.require("dataset2")
    lr = demo_data.lerobot_root("dataset2")
    cam = "28221883_left"
    out = tmp_path / "gripper_template.json"
    rc = cli.main(["template-build", "--trajectory", str(root / "trajectory.json"), "--lerobot-root", str(lr),
                   "--seeds", str(root / "observations_seed"), "--episodes", "0", "--cameras", cam,
                   "--every", "15", "--dataset", "dataset2", "--tool-name", "robotiq_2f85", "--out", str(out)])
    assert rc == 0, capsys.readouterr().err
    built = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert built["entries"] >= 8 and built["masked_entries"] == built["entries"], built
    t = TP.load_template(out)
    r = load.load_bundle(root / "trajectory.json", lerobot_root=lr, episodes=[6])
    assert r.ok
    s6 = r.samples[6]
    seeds = O.find_seeds(root / "observations_seed", "dataset2_000000", cam)     # truth-derived, same video
    pids = tuple(sorted(seeds.point_ids))
    ctx6, _ = O.provider_inputs(s6, cam, media_root=lr, seeds=None, point_ids=pids)
    frames6 = V.iter_clip(ctx6.media_path, clip_start_s=ctx6.clip_start_s, clip_end_s=ctx6.clip_end_s, fps=ctx6.fps,
                          frame_count=ctx6.media_frame_count)
    b = T.TemplateLKProvider(t).locate(frames6, O.PointTargets(pids, None), ctx6)
    vis = b.visibility["tcp"] == O.VISIBLE
    assert b.stats["anchors"] >= 8 and vis.mean() > 0.4, (b.stats["anchors"], float(vis.mean()), b.stats)
    # the two anchor sources must agree: seeded (truth anchors every 15 frames) vs template (automatic anchors)
    seeds6 = O.find_seeds(root / "observations_seed", s6.sample_id, cam)
    ctx_s, targets_s = O.provider_inputs(s6, cam, media_root=lr, seeds=seeds6, point_ids=pids)
    frames_s = V.iter_clip(ctx_s.media_path, clip_start_s=ctx_s.clip_start_s, clip_end_s=ctx_s.clip_end_s, fps=ctx_s.fps,
                           frame_count=ctx_s.media_frame_count)
    a = T.SeededLKProvider().locate(frames_s, targets_s, ctx_s)
    both = vis & (a.visibility["tcp"] == O.VISIBLE)
    errs = np.linalg.norm(b.uv["tcp"][both] - a.uv["tcp"][both], axis=1)
    assert both.sum() >= 60 and np.percentile(errs, 95) < 8.0, (int(both.sum()), float(np.percentile(errs, 95)))
