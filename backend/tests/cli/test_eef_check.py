"""The EEF-video consistency module on the CLI chain (design doc 12 §11, §13.4; D49 / C.9).

On tools/parity's mini dataset the exterior camera shows a red block that moves with joint 0 over a
static checkerboard. A 2D-only trajectory.json declares the block centre for episodes 0-6 (episode 1
lags the video by 4 frames, episode 2 is offset by 9 px) with P-A seeds from the true centre. Since
D49 the module is a vlm-tier hard gate: it runs on the frame stage's survivors, measures with the CPU,
asks the model (the parity fake here) and passes, rejects or leaves an episode to a person; only its
rejects change the verdict. With seeds every 15 frames the 128x96 clips are too poor for the tracker,
which abstains between the seeds (``coverage_insufficient``); that is a person's question now, never a
fake ok. The verdict branches with scripted answers are in test_eef_review.py.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil

import numpy as np
import pytest

from .pipeline import Chain, codes_of, passed_of, read_jsonl, results, run, verdict_of

URL = "http://fake-vlm.test/v1"
VLM = ("--vlm-endpoint", URL, "--vlm-model", "fake-vlm", "--retry", "0")

MODS = ("timestamp_check,kinematic_limits,motion_quality,visual_quality,video_action_sync,task_success,"
        "dedup")
EEF = "eef_video_consistency"
CAM = "exterior"


def _truth(ep: int) -> np.ndarray:
    from parity import fixtures as F

    src = F.DUPLICATE_OF.get(ep, ep)
    q = F._joint_track(F.EPISODES[src][0], src)
    lo, hi = float(q[:, 0].min()), float(q[:, 0].max())
    pos = (q[:, 0] - lo) / max(hi - lo, 1e-6)
    cx = np.array([int(12 + p * (F.WIDTH - 36)) for p in pos], float)
    return np.stack([cx + 11.5, np.full(len(cx), F.HEIGHT // 2 - 12 + 11.5)], 1)


def _entry(ep: int, *, shift: int = 0, offset: float = 0.0) -> dict:
    from parity import fixtures as F

    uv = _truth(ep)
    n = len(uv)
    sid = f"mini_{ep:06d}"
    prov = {"source": "test", "method": "fixture geometry", "assurance": "synthetic"}
    frames = []
    for i in range(n):
        u, v = uv[min(max(i - shift, 0), n - 1)]
        u += offset
        pt = {"uv_px": [float(u), float(v)], "depth_m": None, "status": "valid", "in_frame": True}
        frames.append({"schema_version": "eef-video/1.0.0", "sample_id": sid, "frame_index": i, "timestamp_s": i / F.FPS,
                       "source_state_index": None, "source_timing": [], "eef": None, "gripper": None,
                       "cameras": {CAM: {"video_frame_index": i, "video_timestamp_s": i / F.FPS,
                                         "image_size_wh": [F.WIDTH, F.HEIGHT], "calibration_id": None,
                                         "T_reference_camera": None, "H_media_from_calibration": np.eye(3).tolist(),
                                         "projection": {"source": "provided", "pixel_space": "media",
                                                        "points": {"block_center": pt}}}}})
    sample = {"schema_version": "eef-video/1.0.0", "sample_id": sid,
              "source": {"dataset": "mini", "episode_id": str(ep), "instruction": None}, "frame_count": n,
              "timebase": "video_pts", "annotations_path": "#frames", "calibration_path": None,
              "eef_frame": None, "reference_frame": None,
              "views": [{"view_id": CAM, "kind": "camera", "camera_id": CAM, "mount": "fixed_external",
                         "media": {"kind": "video",
                                   "uri": f"videos/chunk-000/observation.images.exterior/episode_{ep:06d}.mp4",
                                   "image_size_wh": [F.WIDTH, F.HEIGHT], "frame_count": n, "fps": float(F.FPS),
                                   "clip_start_s": 0.0, "clip_end_s": n / F.FPS}}],
              "point_definitions": {"block_center": {"meaning": "centre of the red block", "frame_id": None,
                                                     "model": "external_2d", "position_eef_m": None,
                                                     "position_open_eef_m": None, "position_closed_eef_m": None,
                                                     "provenance": prov}},
              "axis_definitions": {}, "raw_pose_sequence": None, "notes": ["test fixture"]}
    return {"episode_index": ep, "sample": sample, "calibration": None, "frames": frames}


def _files(tmp_path, *, corrupt: bool = False, seed_every: int = 15) -> str:
    """trajectory.json for episodes 0-6 plus observations_seed/ next to it; returns its path."""
    d = tmp_path / "eef"
    entries = [_entry(ep, shift=4 if ep == 1 else 0, offset=9.0 if ep == 2 else 0.0) for ep in range(7)]
    if corrupt:
        entries[3]["frames"][10]["cameras"][CAM]["projection"]["points"]["block_center"]["truth"] = [1, 2]
    bundle = {"schema_version": "eef-video/1.0.0", "container": "trajectory-bundle/1.0",
              "dataset": {"id": "mini", "lerobot_codebase_version": "v2.1", "fps": 15.0, "episode_count": 8},
              "media_uri_base": "lerobot_root", "samples": entries}
    (d / "observations_seed").mkdir(parents=True)
    (d / "trajectory.json").write_text(json.dumps(bundle))
    for ep in range(7):
        uv = _truth(ep)
        rows = [{"schema_version": "eef-video/1.0.0", "sample_id": f"mini_{ep:06d}", "frame_index": i,
                 "camera_id": CAM, "video_frame_index": i, "pixel_space": "media", "method": "synthetic_fixture",
                 "model_version": "test", "input_image_sha256": "0" * 64, "projection_visible_to_localizer": False,
                 "points": {"block_center": {"uv_px": [float(uv[i, 0]), float(uv[i, 1])], "visibility": "visible",
                                             "confidence": 1.0, "uncertainty_px": None}}}
                for i in range(0, len(uv), seed_every)]
        (d / "observations_seed" / f"mini_{ep:06d}").mkdir()
        (d / "observations_seed" / f"mini_{ep:06d}" / f"{CAM}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
    return str(d / "trajectory.json")


@contextlib.contextmanager
def fake_vlm(tmp_path, answer=None):
    """The parity fake model in-process (the ``cli`` fixture runs the command in this process)."""
    from parity import vlm_tape as T
    from parity.fakevlm import FakeVlm

    from curation.adapters import vlm_client

    fake = FakeVlm("fake-vlm")
    if answer is not None:
        fake.answer = answer
    os.makedirs(tmp_path, exist_ok=True)
    hooks = T.TapeHooks("record", tape_out=os.path.join(str(tmp_path), f"tape-{id(fake)}.jsonl.gz"),
                        transport=fake.transport())
    hooks.install(vlm_client)
    try:
        yield fake
    finally:
        hooks.uninstall()


def test_a_trajectory_that_cannot_be_generated_is_asked_for(mini_dataset, tmp_path):
    """A dataset that looks computable (meta/umi_calibration.json, design doc 24) but whose trajectory cannot be
    generated: the file is asked for, the module is still offered."""
    import shutil

    data = tmp_path / "ds"
    shutil.copytree(mini_dataset, data)
    (data / "meta" / "umi_calibration.json").write_text("{}")
    (entry,) = run("preflight", "--input", str(data), "--modules", EEF).doc["modules"]
    assert (entry["availability"], entry["reason_code"]) == ("needs_input", "trajectory_missing")
    assert entry["input_hint"] == {"field": "trajectory_json"} and "could not be generated" in entry["reason"]


def test_preflight_asks_for_the_file_and_reminds_of_a_model(mini_dataset, tmp_path):
    traj = _files(tmp_path)
    doc = run("preflight", "--input", mini_dataset, "--modules", EEF).doc
    (entry,) = doc["modules"]
    # the file is optional only where the platform can compute the trajectory (design doc 24); not here
    assert entry["availability"] == "needs_input" and entry["reason_code"] == "trajectory_missing"
    assert entry["input_hint"] == {"field": "trajectory_json"}
    (entry,) = run("preflight", "--input", mini_dataset, "--modules", EEF,
                   "--param", f"{EEF}.trajectory_json={traj}").doc["modules"]
    # design doc 25 D84: no model backend is a reminder, not a question
    assert entry["availability"] == "available" and "input_hint" not in entry
    assert any(n.startswith("vlm_backend_missing:") for n in entry["notes"])
    assert [(c["mount"], c["drawable"]) for c in entry["cameras"]] == [("fixed_external", True)]
    assert "observation_seeds" in entry["applicable_params"] and "record_mapping" not in entry["applicable_params"]
    (entry,) = run("preflight", "--input", mini_dataset, "--modules", EEF, "--vlm-backend", "ark",
                   "--param", f"{EEF}.trajectory_json={traj}").doc["modules"]
    assert entry["availability"] == "available"
    # the same file without seeds or a template next to it (D-E15): available, the model's opinion only
    alone = tmp_path / "alone" / "trajectory.json"
    alone.parent.mkdir()
    alone.write_text(open(traj, encoding="utf-8").read())
    (bare,) = run("preflight", "--input", mini_dataset, "--modules", EEF, "--vlm-backend", "ark",
                  "--param", f"{EEF}.trajectory_json={alone}").doc["modules"]
    assert bare["availability"] == "available" and "input_hint" not in bare
    assert any("vlm_opinion" in n for n in bare["notes"])
    assert bare["episode_counts"] == {"available": 7, "unsupported": 1}
    assert entry["episode_counts"] == {"available": 7, "unsupported": 1}           # episode 7 is not declared
    assert entry["subitems"]["position_2d"]["availability"] == "available"
    assert entry["subitems"]["orientation_2d"] == {"availability": "unsupported",
                                                   "reason_code": "axis_mapping_missing"}
    assert entry["subitems"]["state_motion"]["reason_code"] == "pose_semantics_unknown"
    bad = run("preflight", "--input", mini_dataset, "--param", f"{EEF}.no_such=1")
    assert bad.rc != 0 and "no parameter" in bad.doc["error"]["message"]


def test_check_usage_and_whole_module_failures(mini_dataset, tmp_path):
    rd = str(tmp_path / "run")
    missing = run("check", "--modules", EEF, "--input", mini_dataset, "--run-dir", rd, "--episodes", "0-1")
    assert missing.rc != 0 and "no end-effector poses" in missing.doc["error"]["message"]
    mixed = run("check", "--modules", f"visual_quality,{EEF}", "--input", mini_dataset, "--run-dir", rd,
                "--episodes", "0-1", "--param", f"{EEF}.trajectory_json=/nope")
    assert mixed.rc != 0 and "one call runs one stage" in mixed.doc["error"]["message"]
    corrupt = _files(tmp_path, corrupt=True)
    bad = run("check", "--modules", EEF, "--input", mini_dataset, "--run-dir", rd, "--episodes", "0-1",
              "--param", f"{EEF}.trajectory_json={corrupt}")
    assert bad.rc == 4 and "truth" in bad.doc["error"]["message"]
    no_model = run("check", "--modules", EEF, "--input", mini_dataset, "--run-dir", rd, "--episodes", "0-1",
                   "--param", f"{EEF}.trajectory_json={_files(tmp_path / 'ok')}",
                   "--vlm-endpoint", "http://127.0.0.1:9/v1", "--vlm-model", "none")     # nothing listens there
    assert no_model.rc == 4 and "VLM endpoint cannot be used" in no_model.doc["error"]["message"]
    assert not os.path.exists(os.path.join(rd, "checks", EEF, "results.jsonl"))   # probed before any episode


def _check(cli, dataset, rd, traj, *extra):
    res = cli("check", "--modules", EEF, "--input", dataset, "--run-dir", rd, "--episodes", "0-2",
              "--param", f"{EEF}.trajectory_json={traj}", *VLM, *extra)
    assert res.rc == 0, res.doc
    return res.doc["modules"][EEF]


def test_another_file_or_other_seeds_are_another_input(cli, mini_dataset, tmp_path):
    """F5.5 acceptance 2: the file's hash is part of the input; --resume redoes what another file made."""
    rd = str(tmp_path / "run")
    traj = _files(tmp_path / "a")
    with fake_vlm(tmp_path):
        first = _check(cli, mini_dataset, rd, traj)
        same = _check(cli, mini_dataset, rd, traj, "--resume")
        assert same["skipped_existing"] == 3 and same["input_digest"] == first["input_digest"]
        doc = json.loads(open(traj).read())                              # another file: one point moved
        doc["samples"][0]["frames"][0]["cameras"][CAM]["projection"]["points"]["block_center"]["uv_px"][0] += 1.0
        other = _files(tmp_path / "b")
        open(other, "w").write(json.dumps(doc))
        moved = _check(cli, mini_dataset, rd, other, "--resume")
        assert moved["skipped_existing"] == 0 and moved["input_digest"] != first["input_digest"]
        import hashlib

        sha = {r["details"]["input_file_sha256"] for r in results(rd, EEF).values()}
        assert sha == {hashlib.sha256(open(other, "rb").read()).hexdigest()}
        seed = os.path.join(os.path.dirname(other), "observations_seed", "mini_000001", f"{CAM}.jsonl")
        rows = open(seed).read().splitlines()
        open(seed, "w").write("\n".join(rows[:-1]) + "\n")                # other seeds for episode 1 only
        reseeded = _check(cli, mini_dataset, rd, other, "--resume")
        assert reseeded["skipped_existing"] == 2 and reseeded["input_digest"] != moved["input_digest"]
        changed = _check(cli, mini_dataset, rd, other, "--resume", "--param", f"{EEF}.lag_search_s=0.5")
        assert changed["skipped_existing"] == 0                          # another configuration
        other_model = cli("check", "--modules", EEF, "--input", mini_dataset, "--run-dir", rd, "--episodes", "0-2",
                          "--param", f"{EEF}.trajectory_json={other}", "--param", f"{EEF}.lag_search_s=0.5",
                          "--vlm-endpoint", URL, "--vlm-model", "fake-vlm-2", "--retry", "0", "--resume")
        assert other_model.rc == 0 and other_model.doc["modules"][EEF]["skipped_existing"] == 0


def test_without_a_gripper_reference_the_model_gives_an_advisory_opinion(cli, mini_dataset, tmp_path):
    """D-E15 (design doc 12 §10.5): no seeds, no template - no CPU reading; each camera's whole clip, marked,
    goes to the model; its mismatched stretches come with a confidence and evidence stills; every record
    passes and nobody is asked."""
    from curation.extensions.eef_consistency import opinion as OP

    alone = tmp_path / "alone" / "trajectory.json"
    alone.parent.mkdir()
    alone.write_text(open(_files(tmp_path / "src"), encoding="utf-8").read())
    from parity.fakevlm import _texts

    def asked(fake) -> list[str]:
        seen, answer = [], fake.answer
        fake.answer = lambda payload: (seen.append(_texts(payload)), answer(payload))[1]
        return seen

    rd = str(tmp_path / "run")
    with fake_vlm(tmp_path) as fake:
        texts = asked(fake)
        doc = _check(cli, mini_dataset, rd, str(alone))
    sent = [t for t in texts if "You check a recorded robot trajectory" in t]
    assert doc["episodes"]["total"] == 3 and doc["episodes"]["error"] == 0
    assert len(sent) == 3                                        # one clip per episode (one camera, < 60 s)
    recs = results(rd, EEF)
    assert all(verdict_of(r) == "pass" for r in recs.values())          # an opinion rejects nothing
    for e, r in recs.items():
        d = r["details"]
        assert d["assessment_mode"] == "vlm_opinion" and "decision" not in d
        assert "cameras" not in d or not d.get("summary")        # no CPU reading at all
        # design doc 25 §7: the opinion is the model's single channel, capped; its cells give the findings
        pos = next(c for c in d["merged"]["cells"] if c["subitem"] == "position_2d")
        assert pos["missing"] == "no_gripper_reference" and "single_source" in pos["flags"]
        if d["opinion"]["flagged"]:
            assert pos["p"] == 0.76 and d["merged"]["episode"]["label"] == "inconsistent"     # the fake's 0.8 segment
            assert codes_of(r) == ["inconsistent"] and r["findings"][0]["severity"] == "high"
        else:
            assert d["merged"]["episode"]["label"] == "consistent" and codes_of(r) == []
        cam = d["opinion"]["cameras"][CAM]
        assert cam["status"] == "answered" and cam["point_id"] == "block_center" and cam["axis_id"] is None
        assert cam["clips"] == [dict(cam["clips"][0], start_frame=0, end_frame=len(_truth(e)) - 1)]
        assert cam["clips"][0]["video"]["frames"] == len(_truth(e))
    flagged = [r for r in recs.values() if r["details"]["opinion"]["flagged"]]
    assert flagged, "the fake flags every other clip"
    for r in flagged:
        (seg,) = r["details"]["opinion"]["cameras"][CAM]["segments"]
        assert seg["confidence"] == 0.8 and seg["aspect"] == "position" and seg["evidence_frames"] == [0]
        assert seg["start_s"] == 0.0
    summary = OP.summary(recs)
    assert summary["opinion_episodes"] == 3 and summary["opinion_flagged"] == len(flagged)
    assert summary["opinion_segments"] == len(flagged)
    # resumed with the same inputs, nothing is asked again
    with fake_vlm(tmp_path) as fake:
        texts = asked(fake)
        again = _check(cli, mini_dataset, rd, str(alone), "--resume")
    assert again["skipped_existing"] == 3
    assert not [t for t in texts if "You check a recorded robot trajectory" in t]


def test_a_handheld_gripper_recording_gets_an_opinion_camera_by_camera(cli, tmp_path):
    """Design doc 22 §5.2 (F5.18): a DAS-like mcap and the bundle ``export-umi-mcap`` wrote from it. Each wrist
    camera's clip (H.264 from its first keyframe on), marked with its own hand only, goes to the model; the
    short pose gaps are bridged with the default and the record says so."""
    from parity.fakevlm import _texts

    from curation.extensions.eef_consistency.adapters import umi_mcap

    from ..eef import das_mcap as F

    data = tmp_path / "das"
    F.make_das(data)
    (tmp_path / "cal.json").write_text(json.dumps(F.calibration()))
    traj = tmp_path / "eef" / "trajectory.json"
    umi_mcap.export(data, tmp_path / "cal.json", traj, ego_check=False)
    rd = str(tmp_path / "run")
    sent: list[str] = []
    with fake_vlm(tmp_path) as fake:
        answer = fake.answer
        fake.answer = lambda payload: (sent.append(_texts(payload)), answer(payload))[1]
        res = cli("check", "--modules", EEF, "--input", str(data), "--run-dir", rd, "--episodes", "0",
                  "--param", f"{EEF}.trajectory_json={traj}", *VLM)
    assert res.rc == 0, res.doc
    assert res.doc["modules"][EEF]["episodes"]["error"] == 0
    (rec,) = results(rd, EEF).values()
    d = rec["details"]
    assert d["assessment_mode"] == "vlm_opinion" and verdict_of(rec) == "pass"
    op = d["opinion"]
    assert op["interpolation"] == {"max_gap_s": pytest.approx(0.1, abs=1e-4), "frames": {"robot0": 2, "robot1": 0}}
    assert set(op["cameras"]) == {"robot0_camera0", "robot1_camera0"}
    assert {c["status"] for c in op["cameras"].values()} == {"answered"}
    asked = [t for t in sent if "Only the camera's own hand" in t]
    assert len(asked) == 2 and any("robot0 is annotated" in t for t in asked) and any("robot1 is annotated" in t for t in asked)
    # each wrist camera's own motion against its poses (design doc 22 §5.3): robot1's plain pictures say nothing
    ego = d["ego_motion"]
    assert set(ego["cameras"]) == {"robot0_camera0", "robot1_camera0"} and ego["window_s"] == 0.5
    assert ego["cameras"]["robot1_camera0"]["status"] == "unknown"
    assert ego["cameras"]["robot0_camera0"]["metrics"]["attempted"] > 0 and ego["uncalibrated"]
    assert ego["assumed"].startswith("按假设值")
    assert not [f for f in rec.get("findings") or [] if f["code"] == "ego_motion_suspect"]


def test_a_handheld_gripper_recording_needs_no_trajectory(cli, tmp_path):
    """Design doc 22 §5.4 (F5.20): no trajectory.json on a handheld gripper's mcap - each episode's trajectory is
    derived from the recording with the built-in DAS DEMO calibration (or the task's), kept in the run directory for
    the overlay, and the record says where it came from. --resume keeps it; another calibration redoes it."""
    from curation.extensions.eef_consistency import derive_mcap

    from ..eef import das_mcap as F

    data = tmp_path / "das"
    F.make_das(data)
    rd = str(tmp_path / "run")

    def run(*extra):
        with fake_vlm(tmp_path):
            res = cli("check", "--modules", EEF, "--input", str(data), "--run-dir", rd, "--episodes", "0", *VLM, *extra)
        assert res.rc == 0, res.doc
        return res.doc["modules"][EEF]

    first = run()
    assert first["episodes"]["error"] == 0
    (rec,) = results(rd, EEF).values()
    d = rec["details"]
    src = d["trajectory_source"]
    assert src["kind"] == "derived" and src["calibration"]["builtin"] and src["calibration"]["gripper"] == "das_gripper"
    assert src["calibration"]["assumed"] == ["T_camera_tcp", "body_to_optical", "pose_frame"]
    # the fixture's robot1 has no camera_info, and the built-in calibration no intrinsics: robot0 alone is drawn
    assert src["cameras"]["robot1"]["status"] == "unsupported" and src["cameras"]["robot0"]["status"] == "ok"
    assert src["rows"] == F.N - F.POSE_FROM
    assert set(d["opinion"]["cameras"]) == {"robot0_camera0"} and d["opinion"]["status"] == "answered"
    assert set(d["ego_motion"]["cameras"]) == {"robot0_camera0"}
    out = os.path.join(rd, "checks", EEF)
    assert derive_mcap.bundle_path(out, 0).is_file() and derive_mcap.report_path(out, 0).is_file()
    assert run("--resume")["skipped_existing"] == 1
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps(F.calibration()))                 # with robot1's intrinsics as a fallback
    again = run("--resume", "--param", f"{EEF}.gripper_calibration={cal}")
    assert again["skipped_existing"] == 0
    (rec,) = results(rd, EEF).values()
    src = rec["details"]["trajectory_source"]
    assert not src["calibration"]["builtin"] and src["cameras"]["robot1"]["intrinsics"] == "intrinsics_fallback"
    assert set(rec["details"]["opinion"]["cameras"]) == {"robot0_camera0", "robot1_camera0"}


def test_an_episode_without_a_trajectory_asks_nobody(cli, tmp_path):
    """An episode the recording gives no trajectory for (another device's file next to the gripper's): the opinion
    says it could not look and reports nothing, so the task keeps it and asks nobody (as an episode without a task
    text, D72); having assessed nothing, it reads as an abstention in the 1.0 counts."""
    import shutil

    from ..eef import das_mcap as F
    from ..viz.mcap_fixtures import make_default

    data = tmp_path / "das"
    F.make_das(data)
    make_default(str(tmp_path / "arm"))
    shutil.copy(tmp_path / "arm" / "episode_0.mcap", data / "episode_1.mcap")
    rd = str(tmp_path / "run")
    with fake_vlm(tmp_path):
        res = cli("check", "--modules", EEF, "--input", str(data), "--run-dir", rd, "--episodes", "0-1", *VLM)
    assert res.rc == 0, res.doc
    recs = results(rd, EEF)
    arm = recs[1]
    assert arm["status"] == "ok" and arm["details"]["assessment_mode"] == "vlm_opinion" and verdict_of(arm) == "abstain"
    assert arm["details"]["opinion"]["status"] == "not_assessable"
    episode = arm["details"]["merged"]["episode"]                   # design doc 25 §7.5: "cannot tell", no card
    assert episode["label"] == "cannot_tell" and episode["p"] is None and "这一条推不出轨迹" in episode["reason"]
    assert arm["details"]["trajectory_source"]["reason"] == "trajectory_not_derived"
    assert "这一条推不出轨迹" in arm["details"]["opinion"]["failure"]
    assert {u["item"] for u in arm["unassessable"]} == {"MV-4", "AV-1"} and arm["findings"] == []
    assert recs[0]["details"]["opinion"]["status"] == "answered"


def test_without_a_trajectory_an_arm_mcap_fails_the_module(cli, tmp_path):
    """Nothing to generate a trajectory from (design doc 24) and no handheld gripper's recording to derive one from
    (design doc 22 §5.4): an arm's mcap fails the module, as a LeRobot dataset without poses and calibration does."""
    from ..viz.mcap_fixtures import make_default

    make_default(str(tmp_path / "arm"))
    res = cli("check", "--modules", EEF, "--input", str(tmp_path / "arm"), "--run-dir", str(tmp_path / "run"),
              "--episodes", "0", *VLM)
    assert res.rc != 0 and "no end-effector poses" in json.dumps(res.doc, ensure_ascii=False)
    assert "trajectory_json=PATH" in json.dumps(res.doc, ensure_ascii=False)


def test_opinion_answers_are_checked_against_the_clip():
    from curation.extensions.eef_consistency import opinion as OP

    ok = {"gripper_visible": True, "summary": "x",
          "segments": [{"start_frame": 10, "end_frame": 20, "aspect": "both", "confidence": 0.6,
                        "evidence_frames": [12, 20], "observation": "偏了"}]}
    assert OP.check_answer(json.dumps(ok), 0, 50) == (ok, None)
    assert OP.check_answer("```json\n" + json.dumps(ok) + "\n```", 0, 50)[0] == ok
    late = dict(ok, segments=[dict(ok["segments"][0], end_frame=60)])
    assert OP.check_answer(json.dumps(late), 0, 50)[1]["code"] == "bad_frame"
    outside = dict(ok, segments=[dict(ok["segments"][0], evidence_frames=[25])])
    assert OP.check_answer(json.dumps(outside), 0, 50)[1]["code"] == "bad_frame"
    sure = dict(ok, segments=[dict(ok["segments"][0], confidence=1.2)])
    assert OP.check_answer(json.dumps(sure), 0, 50)[1]["code"] == "schema_violation"
    assert OP.check_answer("no json", 0, 50)[1]["code"] == "malformed_json"
    assert OP.clip_ranges(list(range(2000)), 15.0) == [list(range(900)), list(range(900, 1800)),
                                                       list(range(1800, 2000))]


def test_a_remote_dataset_streams_the_media_it_needs(cli, cloud, mini_dataset, tmp_path, monkeypatch):
    cloud.upload_dir(mini_dataset, "src-bucket", "datasets/mini")
    monkeypatch.setenv("CURATION_INPUT_TOS_ACCESS_KEY", "in-ak")
    monkeypatch.setenv("CURATION_INPUT_TOS_SECRET_KEY", "in-sk")
    rd = str(tmp_path / "run")
    with fake_vlm(tmp_path):
        doc = _check(cli, "tos://src-bucket/datasets/mini", rd, _files(tmp_path))
    assert doc["episodes"]["error"] == 0 and doc["episodes"]["total"] == 3
    recs = results(rd, EEF)
    assert all(recs[e]["details"]["cameras"][CAM]["subitems"]["position_2d"]["points"]["block_center"]
               ["coverage"]["requested"] == len(_truth(e)) for e in range(3))
    fetched = {c[2] for c in cloud.calls if c[0] == "get" and "/videos/" in c[2]}
    assert fetched == {f"datasets/mini/videos/chunk-000/observation.images.exterior/episode_{e:06d}.mp4"
                       for e in range(3)}


@pytest.fixture(scope="module")
def chain(vlm_stage, tmp_path_factory):
    """The reference run through final + report as r0001, then the EEF module on the frame stage's
    survivors against the fake model, then r0002 with it."""
    from .fakevlm_server import FakeVlmServer

    tmp = tmp_path_factory.mktemp("eef-chain")
    rd = str(tmp / "run")
    shutil.copytree(vlm_stage["reference_dir"], rd)
    traj = _files(tmp)
    ds = vlm_stage["dataset"]
    survivors = [int(x) for x in open(vlm_stage["episodes"][1:]).read().split()]
    with FakeVlmServer() as vlm:
        c = Chain(ds, rd, vlm.url)
        c.post(revision=1)
        eef = c.step("eef", "check", "--modules", EEF, *c.common(), *c.vlm, "--episodes", vlm_stage["episodes"],
                     "--param", f"{EEF}.trajectory_json={traj}", "--survivors-out", c.path("stages", "eef.txt"))
        mods = f"{MODS},{EEF}"
        c.step("funnel2", "aggregate", "--run-dir", rd, "--phase", "funnel", "--revision", "2",
               "--episodes", "0-7", "--modules", mods)
        c.step("final2", "aggregate", "--run-dir", rd, "--phase", "final", "--revision", "2", "--episodes", "0-7",
               "--modules", mods, "--input", ds)
        c.step("report2", "report", "--run-dir", rd, "--revision", "2", "--modules", mods)
    return {"rd": rd, "chain": c, "eef": eef, "survivors": survivors}


def test_the_frame_survivors_are_judged(chain):
    rd, survivors = chain["rd"], chain["survivors"]
    recs = results(rd, EEF)
    assert sorted(recs) == sorted(survivors)                      # D49: the funnel's survivors, not the selection
    counts = chain["eef"].doc["modules"][EEF]["episodes"]
    assert counts["total"] == len(survivors) and counts["error"] == 0
    for ep, r in recs.items():
        d = r["details"]
        assert r["status"] == "ok" and "score" not in r["readings"] and d["assessment_mode"] == "verdict"
        # registry 5.0 (D81): an opinion with a confidence - never a reject, a conflict the only question
        assert passed_of(r) is not False and "decision" not in d
        assert set(codes_of(r)) <= {"inconsistent", "conflict", "record_mismatch"}
        episode = d["merged"]["episode"]
        assert episode["label"] in ("inconsistent", "possibly_inconsistent", "consistent", "cannot_tell")
        assert ("conflict" in codes_of(r)) == bool(episode["conflicts"])
        if ep == 7:                                                # not in the file: cannot tell, nobody asked
            assert episode["label"] == "cannot_tell" and codes_of(r) == [] and "MV-4" in [u["item"] for u in r["unassessable"]]
            continue
        assert d["schema_version"] == "eef-detail/0.1" and d["uncalibrated"] and d["input_file_sha256"]
        pos = d["cameras"][CAM]["subitems"]["position_2d"]
        cov = pos["points"]["block_center"]["coverage"]
        assert cov["requested"] == cov["media_mapped"] == len(_truth(ep))
        cell = next(c for c in d["merged"]["cells"] if c["subitem"] == "position_2d")
        if pos["status"] == "unknown":                            # honest abstention: the CPU says nothing here
            assert "coverage_insufficient" in pos["reasons"] and cell["sources"]["cpu"]["verdict"] == "cannot_tell"
        assert d["review"]["status"] in ("completed", "incomplete", "not_reviewed")
    kept = open(os.path.join(rd, "stages", "eef.txt")).read().split()
    assert kept == [str(e) for e in sorted(recs)]                   # it rejects nothing
    for rec in recs.values():
        for path in rec["evidence"]:
            assert os.path.isfile(os.path.join(rd, path))


def test_it_moves_no_verdict(chain):
    """D81 (design doc 25 §7.3): whatever the module says, every episode keeps the verdict it had without it; a
    conflict is asked on eef_check."""
    recs = results(chain["rd"], EEF)
    r1 = {x["episode_index"]: x for x in read_jsonl(os.path.join(chain["rd"], "revisions", "r0001", "verdicts.jsonl"))}
    r2 = {x["episode_index"]: x for x in read_jsonl(os.path.join(chain["rd"], "revisions", "r0002", "verdicts.jsonl"))}
    for ep in r1:
        assert r2[ep]["verdict"] == r1[ep]["verdict"], ep
        assert EEF not in [b["module"] for b in r2[ep].get("blocking") or []]


def test_the_report_shows_the_eef_section(chain):
    rep = json.loads(open(os.path.join(chain["rd"], "revisions", "r0002", "report.json")).read())
    (sec,) = [s for s in rep["modules"] if s["id"] == EEF]
    s = sec["summary"]
    n = len(chain["survivors"])
    assert sum(x["count"] for x in s["labels"]) == n and s["uncalibrated"] and s["confidence_uncalibrated"]
    assert s["judged_pass"] == s["judged_reject"] == s["to_human"] == 0           # the 4.x keys, nothing behind them
    assert sum(x["count"] for x in s["p_bins"]) == n - next(x["count"] for x in s["labels"] if x["name"] == "cannot_tell")
    assert s["threshold_profile"].startswith("demo ") and "gate" not in sec       # report 2.0
    assert s["assessed_episodes"] == n
    assert all(set(x) == {"name", "count"} for x in s["single_source_missing"] + s["cannot_tell_reasons"])
    assert s["windows"] >= s["windows_answered"] and "model_cpu_agreement" in s
    tables = {t["id"]: t for t in sec["tables"]}
    assert set(tables) == {"eef_camera_metrics", "eef_segments", "eef_diagnosis", "eef_review_windows", "eef_record",
                           "eef_ego_motion", "eef_opinions"}
    # no record mapping given (design doc 12 §8.7): nothing to report, not a row per episode saying so
    assert tables["eef_record"]["rows"] == 0 and not any(k.startswith("record_") for k in s)
    # no wrist camera to read its own motion (design doc 22 §5.3): likewise
    assert tables["eef_ego_motion"]["rows"] == 0 and not any(k.startswith("ego_motion") for k in s)
    import pandas as pd

    tdir = os.path.join(chain["rd"], "revisions", "r0002", "tables")
    cams = pd.read_parquet(os.path.join(tdir, "eef_camera_metrics.parquet"))
    assert {"episode_index", "camera", "position", "lag_s", "coverage"} <= set(cams.columns)
    assert set(cams["episode_index"]) == set(chain["survivors"])
    wins = pd.read_parquet(os.path.join(tdir, "eef_review_windows.parquet"))
    assert {"episode_index", "camera", "kind", "point", "status", "review_status"} <= set(wins.columns)
    ops = pd.read_parquet(os.path.join(tdir, "eef_opinions.parquet"))
    assert {"episode_index", "subitem", "camera", "label", "p", "cpu_p", "vlm_p", "flags"} <= set(ops.columns)
    md = open(os.path.join(chain["rd"], "revisions", "r0002", "report.md"), encoding="utf-8").read()
    assert "结论(意见,不判废)" in md and "冲突(两个渠道结论相反,转人工裁决)" in md and "判过" not in md
    # the cards the adjudication page asks (F5.12): since 5.0 only the conflicts (design doc 25 §7.3)
    review = json.load(open(os.path.join(chain["rd"], "revisions", "r0002", "review.json")))["episodes"]
    asked = sum(1 for e in review if any(i["kind"] == "eef_consistency" for i in e["review"]))
    assert sec["adjudication"]["pending"] == asked <= s["conflict_episodes"]
    assert f"- 人工裁决:待裁 {asked} 条" in md


def test_without_a_model_the_cpu_alone_gives_the_opinion(cli, mini_dataset, tmp_path):
    """Design doc 25 D84 (F5.24a acceptance ④): 「使用 VLM 辅助」 off, or on in a task without a model (--no-vlm) - the
    module runs on the CPU alone and asks nobody; the record has the same shape, its model channel missing and said
    why. A stage that needs a model refuses --no-vlm."""
    traj = _files(tmp_path / "f")
    shapes, temporal = {}, {}
    for name, extra in (("model", VLM), ("off", (*VLM, "--param", f"{EEF}.use_vlm=false")), ("none", ("--no-vlm",))):
        rd = str(tmp_path / name)
        sent: list = []
        with fake_vlm(tmp_path) as fake:
            answer = fake.answer
            fake.answer = lambda payload, answer=answer: (sent.append(1), answer(payload))[1]
            res = cli("check", "--modules", EEF, "--input", mini_dataset, "--run-dir", rd, "--episodes", "0-2",
                      "--param", f"{EEF}.trajectory_json={traj}", *extra)
        assert res.rc == 0, res.doc
        assert res.doc["modules"][EEF]["episodes"] == {**res.doc["modules"][EEF]["episodes"], "total": 3, "error": 0}
        recs = results(rd, EEF)
        assert sorted(recs) == [0, 1, 2] and bool(sent) == (name == "model"), (name, len(sent))
        shapes[name] = {e: (sorted(r), sorted(set(r["details"]["merged"]) - {"tracking"}),    # tracking: the model's
                            [(c["subitem"], c.get("camera")) for c in r["details"]["merged"]["cells"]])
                        for e, r in recs.items()}
        # episode 2 (offset 9 px): the CPU reads its time alignment; the model's side is what differs
        temporal[name] = next(c for c in recs[2]["details"]["merged"]["cells"] if c["subitem"] == "temporal_alignment")
        if name != "model":
            assert all(not r["details"]["review"] or r["details"]["review"].get("status") != "answered"
                       for r in recs.values() if "review" in r["details"])
    assert shapes["model"] == shapes["off"] == shapes["none"]
    assert temporal["model"]["missing"] == "model_cannot_see"
    assert (temporal["off"]["missing"], temporal["none"]["missing"]) == ("vlm_off", "no_vlm_backend")
    assert all("single_source" in t["flags"] and t["label"] == "consistent" for t in temporal.values())
    refused = cli("check", "--modules", "task_success", "--input", mini_dataset, "--run-dir", str(tmp_path / "ts"),
                  "--episodes", "0", "--no-vlm")
    assert refused.rc == 2 and "task_success needs a model" in refused.doc["error"]["message"]


def _essence(rec: dict) -> dict:
    """What a record says, without timings, paths of evidence and the halves' bookkeeping."""
    d = rec["details"]
    out = {"findings": sorted((f["code"], f.get("severity")) for f in rec.get("findings") or []),
           "label": d["merged"]["episode"]["label"], "p": d["merged"]["episode"]["p"],
           "cells": [(c["subitem"], c.get("camera"), c.get("label"), c.get("p"), c.get("missing"))
                     for c in d["merged"]["cells"]]}
    if "review" in d:
        out["review"] = (d["review"].get("status"), sorted((w.get("camera_id"), tuple(w.get("frames") or []),
                                                             w.get("status"), (w.get("answer") or {}).get("review_status"))
                                                            for cam in (d["review"].get("cameras") or {}).values()
                                                            for w in cam.get("windows") or []))
    if "opinion" in d:
        out["opinion"] = (d["opinion"]["status"], d["opinion"]["segments"], d["opinion"]["max_confidence"])
    return out


@pytest.mark.parametrize("reference", ["seeds", "none"])
def test_the_two_halves_make_the_records_one_call_makes(cli, mini_dataset, tmp_path, reference):
    """Registry 5.3 (design doc 23 §2.1-§2.3, design doc 25 F5.24b): ``check --prep`` measures, renders and keeps
    each episode's requests without asking (no result line, a package per episode); ``check --prepared`` reads them
    back, asks and writes the same records one call writes, then lets the packages go. --resume of the CPU half
    keeps what it kept; a model half without its package is an error line."""
    from curation.extensions.eef_consistency import package

    traj = _files(tmp_path / "f")
    if reference == "none":
        alone = tmp_path / "alone" / "trajectory.json"
        alone.parent.mkdir()
        alone.write_text(open(traj, encoding="utf-8").read())
        traj = str(alone)
    args = ("--modules", EEF, "--input", mini_dataset, "--episodes", "0-2", "--param", f"{EEF}.trajectory_json={traj}",
            *VLM)
    with fake_vlm(tmp_path / "one"):
        whole = cli("check", "--run-dir", str(tmp_path / "one"), *args)
    rd = str(tmp_path / "two")
    sent: list = []
    with fake_vlm(tmp_path / "two-a") as fake:
        answer = fake.answer
        fake.answer = lambda payload, answer=answer: (sent.append(1), answer(payload))[1]
        prep = cli("check", "--run-dir", rd, *args, "--prep")
    assert whole.rc == 0 and prep.rc == 0, (whole.doc, prep.doc)
    assert not sent, "the CPU half asks nobody"
    assert prep.doc["modules"][EEF]["episodes"] == {"total": 3, "ok": 3, "error": 0}
    assert results(rd, EEF) == {}                                         # no record yet: the model half writes it
    kept = [package.path(rd, EEF, e) for e in range(3)]
    assert all(package.read_prep(k) is not None for k in kept)
    again = cli("check", "--run-dir", rd, *args, "--prep", "--resume")
    assert again.rc == 0 and again.doc["modules"][EEF]["skipped_existing"] == 3
    with fake_vlm(tmp_path / "two-b"):
        ask = cli("check", "--run-dir", rd, *args, "--prepared")
    assert ask.rc == 0, ask.doc
    one, two = results(str(tmp_path / "one"), EEF), results(rd, EEF)
    assert sorted(one) == sorted(two) == [0, 1, 2]
    for e in one:
        assert _essence(one[e]) == _essence(two[e]), e
        assert set(two[e]["details"]["halves"]) == {"vlm_prep", "vlm"}
    assert not any(os.path.exists(k) for k in kept)                       # the packages went with the records (D77)
    with fake_vlm(tmp_path / "three-a"):
        lost = cli("check", "--run-dir", str(tmp_path / "three"), *args, "--prepared")
    assert lost.rc == 0 and lost.doc["modules"][EEF]["episodes"]["error"] == 3
    assert "kept nothing" in json.dumps(results(str(tmp_path / "three"), EEF)[0]["error"])


def test_without_a_model_the_cpu_half_writes_the_records(cli, mini_dataset, tmp_path):
    """Registry 5.3 (design doc 25 F5.24b acceptance ①): 「使用 VLM 辅助」 off - the CPU half is the whole module,
    its records written in vlm_prep, nothing kept for a model half."""
    from curation.extensions.eef_consistency import package

    rd = str(tmp_path / "run")
    res = cli("check", "--modules", EEF, "--input", mini_dataset, "--episodes", "0-1", "--run-dir", rd, "--prep",
              "--param", f"{EEF}.trajectory_json={_files(tmp_path / 'f')}", "--param", f"{EEF}.use_vlm=false")
    assert res.rc == 0, res.doc
    recs = results(rd, EEF)
    assert sorted(recs) == [0, 1] and all(set(r["details"]["halves"]) == {"vlm_prep"} for r in recs.values())
    assert not os.path.exists(package.root(rd))
    usage = cli("check", "--modules", "task_success", "--input", mini_dataset, "--episodes", "0", "--run-dir", rd,
                "--prep")
    assert usage.rc == 2 and "no CPU half" in usage.doc["error"]["message"]
