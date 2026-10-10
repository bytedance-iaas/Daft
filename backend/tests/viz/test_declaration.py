"""The dataset declaration over REST (F5.23, design doc 25 §3, D83): a draft from the dataset, what the EEF module
would make of it, a new version checked against the dataset (errors stop it, suspects stay on it), the registration's
preflight taken again, the version a task freezes; an mcap mapping is the declaration's first layer."""
from __future__ import annotations

import json
import shutil

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from .conftest import assert_error, assert_schema

API = "/curation/api/v1"
JSON = {"content-type": "application/json"}
SIX = ["x", "y", "z", "roll", "pitch", "yaw"]
FRONT, WRIST = "observation.images.front", "observation.images.wrist"


@pytest.fixture
def arm(client_for, data_root, tmp_path):
    """lerobot_v2 with an end-effector pose column, registered through the API (its preflight run for real)."""
    inputs = tmp_path / "inputs"
    root = inputs / "arm"
    shutil.copytree(data_root / "lerobot_v2", root)
    info = json.loads((root / "meta" / "info.json").read_text())
    info["features"]["observation.state.cartesian_position"] = {"dtype": "float32", "shape": [6], "names": {"axes": SIX}}
    (root / "meta" / "info.json").write_text(json.dumps(info))
    for path in sorted((root / "data").glob("chunk-*/episode_*.parquet")):
        table = pq.read_table(path)
        n = table.num_rows
        pose = np.column_stack([np.linspace(0.3, 0.5, n), np.zeros(n), np.full(n, 0.3), np.full(n, np.pi),
                                np.zeros(n), np.zeros(n)]).astype(np.float32)
        pq.write_table(table.append_column("observation.state.cartesian_position",
                                           pa.array(pose.tolist(), pa.list_(pa.float32()))), path)
    c = client_for(base_path="/curation", local_data_root=inputs)
    r = c.post(f"{API}/datasets", headers=JSON, json={"input": {"source": "local", "uri": str(root)}})
    assert r.status_code in (200, 201), r.text
    c.ds = r.json()
    c.info = info
    return c


def _complete(draft: dict) -> dict:
    d = json.loads(json.dumps(draft))
    d["calibration"]["cameras"][FRONT].update(
        intrinsics={"fx_cx_fy_cy": [100.0, 64.0, 100.0, 48.0], "model": "pinhole", "source": "declared",
                    "assurance": "declared"},
        extrinsics={"mode": "static", "xyz_rpy": [1.2, 0.0, 0.6, -2.0, 0.0, 1.57], "assurance": "declared"})
    return d


def test_a_draft_says_what_a_person_still_has_to_declare(arm):
    ds = arm.ds
    assert ds["declaration"] == {"state": "none", "version": 0, "updated_at": None, "name": None, "layers": [],
                                 "assumed": 0, "suspects": 0}
    doc = arm.get(f"{API}/datasets/{ds['id']}/declaration").json()
    assert_schema("DatasetDeclaration", doc)
    assert (doc["state"], doc["format"], doc["declaration"]) == ("none", "lerobot", None)
    draft = doc["draft"]
    assert draft["semantics"]["pose"]["key"] == "observation.state.cartesian_position"
    assert draft["semantics"]["pose"]["frame_id"] == "panda_link8"          # robot_type franka: assumed
    mounts = {src: c["mount"] for src, c in draft["calibration"]["cameras"].items()}
    assert mounts == {FRONT: "fixed_external", WRIST: "wrist"}
    assert [c["source"] for c in doc["cameras"]] == [FRONT, WRIST] and doc["cameras"][0]["width"]
    assert doc["trajectory"]["kind"] == "missing_declaration"
    codes = {(m["field"], m["code"]) for m in doc["trajectory"]["missing"]}
    assert (f"calibration.cameras.{FRONT}.intrinsics", "intrinsics_missing") in codes
    assert {(u["field"], u["code"]) for u in doc["unresolved"]} >= {(f"calibration.cameras.{FRONT}.intrinsics",
                                                                      "intrinsics_missing")}
    entry = next(m for m in arm.get(f"{API}/datasets/{ds['id']}").json()["preflight"]["modules"]
                 if m["id"] == "eef_video_consistency")
    assert (entry["reason_code"], entry["trajectory_source"]["kind"]) == ("declaration_incomplete", "missing_declaration")


def test_a_new_version_is_checked_against_the_dataset_and_the_preflight_follows(arm):
    ds = arm.ds["id"]
    draft = arm.get(f"{API}/datasets/{ds}/declaration").json()["draft"]
    bad = _complete(draft)
    bad["semantics"]["pose"]["key"] = "observation.state.nope"
    r = arm.put(f"{API}/datasets/{ds}/declaration", json={"declaration": bad})
    body = assert_error(r, "validation_failed")
    assert {e["field"] for e in body["error"]["details"]["errors"]} == {"declaration.semantics.pose.key"}
    good = _complete(draft)
    good["calibration"]["tool"]["max_opening_m"] = 0.3                          # doubtful, not wrong
    r = arm.put(f"{API}/datasets/{ds}/declaration", json={"declaration": good})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert_schema("DatasetDeclaration", doc)
    assert (doc["state"], doc["version"], doc["trajectory"]["kind"]) == ("confirmed", 1, "generate")
    assert [s["code"] for s in doc["suspects"]] == ["opening_range"]
    assert [c["drawable"] for c in doc["trajectory"]["cameras"]] == [True, False]     # the wrist camera: no T_camera_tcp
    item = arm.get(f"{API}/datasets/{ds}").json()
    assert_schema("DatasetDetail", item)
    assert item["declaration"]["version"] == 1 and item["declaration"]["layers"] == ["semantics", "calibration"]
    assert item["declaration"]["suspects"] == 1 and item["viz_mapping"] is None
    entry = next(m for m in item["preflight"]["modules"] if m["id"] == "eef_video_consistency")
    assert entry["trajectory_source"]["kind"] == "generate" and entry["trajectory_source"]["declaration"]["drafted"] is False
    events = [e.action for e in arm.app.state.runtime.repo.list_events(resource=ds, limit=20).items]
    assert "dataset.repreflight" in events
    assert_error(arm.get(f"{API}/datasets/{ds}/mapping"), "validation_failed")      # the mapping is mcap's


def test_a_task_freezes_the_version_it_starts_with(arm, tmp_path):
    from daemon.orchestr import planning

    ds = arm.ds["id"]
    rt = arm.app.state.runtime
    draft = arm.get(f"{API}/datasets/{ds}/declaration").json()["draft"]
    assert arm.put(f"{API}/datasets/{ds}/declaration", json={"declaration": _complete(draft)}).status_code == 200

    class Task:
        dataset_id, owner_id = ds, rt.repo.get_dataset(ds).owner_id

    class Run:
        task, repo = Task, rt.repo

    frozen = planning.frozen_declaration(Run)
    assert frozen["version"] == 1 and frozen["document"]["semantics"]["pose"]["key"] == "observation.state.cartesian_position"
    assert len(frozen["sha256"]) == 64
    second = _complete(draft)
    second["name"] = "second"
    assert arm.put(f"{API}/datasets/{ds}/declaration", json={"declaration": second}).json()["version"] == 2
    assert frozen["document"].get("name") != "second"                            # a run keeps what it froze


def _overlay(c, ds: str, ep: int, *, wait_s: float = 60.0) -> dict:
    """The dataset's overlay once its episode's trajectory is made (202 while it is)."""
    import time

    deadline = time.monotonic() + wait_s
    while True:
        r = c.get(f"{API}/datasets/{ds}/episodes/{ep}/eef-overlay")
        if r.status_code != 202:
            break
        assert_schema("VizMediaPending", r.json())
        assert time.monotonic() < deadline, "the trajectory was not made in time"
        time.sleep(0.1)
    assert r.status_code == 200, r.text
    assert_schema("EefOverlay", r.json())
    return r.json()


def test_the_full_visualizer_draws_what_the_declaration_generates(arm):
    """F5.25 (design doc 25 §5.1, D85): a registration's overlay from its own record and declaration - 404 until the
    declaration lets the platform generate the trajectory; then made once (202 while it is), kept by the
    declaration's version; the declared marks only (nothing a model saw); the cameras it cannot draw on, and why."""
    ds = arm.ds["id"]
    body = assert_error(arm.get(f"{API}/datasets/{ds}/episodes/0/eef-overlay"), "not_found")
    assert body["error"]["details"]["reason"] == "no_trajectory"
    assert {m["code"] for m in body["error"]["details"]["missing"]} >= {"intrinsics_missing"}
    draft = arm.get(f"{API}/datasets/{ds}/declaration").json()["draft"]
    assert arm.put(f"{API}/datasets/{ds}/declaration", json={"declaration": _complete(draft)}).status_code == 200
    got = _overlay(arm, ds, 0)
    assert (got["task_id"], got["dataset_id"], got["episode_index"]) == (None, ds, 0)
    assert got["trajectory"] == {"kind": "generate", "declaration_version": 1}
    (cam,) = got["cameras"]
    assert (cam["viz_camera"], cam["mount"], cam["skipped"]) == ("front", "fixed_external", None)
    layers = {x["id"]: x for x in cam["layers"]}
    assert {"point", "axis_x", "axis_y", "axis_z", "trail_past"} <= set(layers)
    assert not any(x["in_model"] for x in cam["layers"])
    assert got["unavailable_cameras"] == [{"source": WRIST, "camera_id": "wrist", "viz_camera": "wrist",
                                           "reason": "intrinsics_missing"}]
    assert all(t is not None for t in cam["times_s"])
    svc = arm.app.state.runtime
    from daemon.viz.service import viz_of

    kept = list((viz_of(svc).disk.root / "eef").rglob("ep000000.json"))
    assert len(kept) == 1
    made = kept[0].stat().st_ino                                    # a file made again is a new one (written, renamed)
    assert _overlay(arm, ds, 0)["cameras"][0]["layers"] == cam["layers"]       # kept: not made again
    assert kept[0].stat().st_ino == made
    assert_error(arm.get(f"{API}/datasets/{ds}/episodes/99/eef-overlay"), "not_found")


def test_an_upload_that_overrides_the_record_gets_the_records_own_marks_dashed(arm, tmp_path):
    """F5.25 (design doc 25 §5.3, D85): a task given a trajectory.json while its frozen declaration generates one -
    the mini player's overlay adds the dataset record's own TCP trail and axes, group ``record``, dashed, each with
    its own calibration; a task without the override has none."""
    from curation.contracts import modules as registry
    from daemon.repo import protocol as P
    from daemon.results.store import store_of
    from daemon.viz.service import viz_of

    ds = arm.ds["id"]
    draft = arm.get(f"{API}/datasets/{ds}/declaration").json()["draft"]
    decl = _complete(draft)
    assert arm.put(f"{API}/datasets/{ds}/declaration", json={"declaration": decl}).status_code == 200
    _overlay(arm, ds, 0)                                          # the generated bundle, kept: the "upload" here
    (kept,) = list((viz_of(arm.app.state.runtime).disk.root / "eef").rglob("ep000000.json"))
    rt = arm.app.state.runtime
    handle = "upload:up_override"

    def task(params: dict):
        rows = [P.TaskModule(task_id="", module_id=m, selected=m == "eef_video_consistency", availability="available",
                             params=params if m == "eef_video_consistency" else None) for m in registry.ids()]
        t = rt.repo.create_task(P.TaskCreate(
            name="eef", input_source="local", input_uri=arm.ds["uri"], output_uri="tos://b/out",
            delivery_key="tos://b/out", episode_selector={"mode": "all"}, params={}, modules=rows, dataset_id=ds))
        run_dir = store_of(rt).task_dir(t.id)
        (run_dir / "inputs").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(kept, run_dir / "inputs" / "trajectory.json")
        (run_dir / "inputs" / "uploads.json").write_text(json.dumps({handle: {"path": "inputs/trajectory.json"}}))
        (run_dir / "inputs" / "declaration.json").write_text(json.dumps(
            rt.repo.get_dataset(ds, owner=t.owner_id).viz_mapping))
        return t

    over = arm.get(f"{API}/tasks/{task({'trajectory_json': handle}).id}/episodes/0/eef-overlay")
    assert over.status_code == 200, over.text
    assert_schema("EefOverlay", over.json())
    assert over.json()["dataset_id"] == ds
    (cam,) = [c for c in over.json()["cameras"] if c["viz_camera"] == "front"]
    record = [x for x in cam["layers"] if x["group"] == "record"]
    assert {x["id"] for x in record} == {"record_point", "record_trail_past", "record_axis_x", "record_axis_y",
                                         "record_axis_z"}
    assert all(x["dash"] == [6, 4] and not x["in_model"] and x["title"].startswith("记录 · ") for x in record)
    plain = {x["id"]: x for x in cam["layers"] if x["group"] != "record"}
    assert record[0]["frames"] == plain[record[0]["id"][len("record_"):]]["frames"]   # the same trajectory: same marks


def test_a_handheld_grippers_recording_is_told_by_its_preflight_or_its_mapping():
    """Design doc 22 §5.4: the reader recognises a handheld gripper's topics (the registration's preflight then
    derives the trajectory), or the registration's mapping is the built-in UMI one; a dataset ``profile`` is the
    semantics profile matched, never the recording's."""
    from daemon.repo import protocol as P
    from daemon.viz.declaration import _handheld

    def ds(modules, mapping=None):
        return P.Dataset(id="ds-x", name="x", source="local", uri="/x", viz_mapping=mapping,
                         preflight={"format": {"kind": "mcap"}, "dataset": {"profile": None}, "modules": modules},
                         meta_fingerprint="sha256:" + "c" * 64, source_fingerprint={}, preflighted_at=1)

    derived = [{"id": "eef_video_consistency", "trajectory_source": {"kind": "mcap_derive"}}]
    assert _handheld(ds(derived), None)
    assert _handheld(ds([]), {"base": "builtin:umi"})
    assert not _handheld(ds([{"id": "eef_video_consistency", "trajectory_source": {"kind": "missing_pose"}}]), None)
    assert not _handheld(ds([]), None)
