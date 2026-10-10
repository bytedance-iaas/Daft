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
