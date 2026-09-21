"""curation preflight: contract output and the F2.7 rules (design doc 05, sections 2-4)."""
from __future__ import annotations

import json
import os

import pandas as pd
import pytest

from curation.contracts import modules as registry_modules
from curation.contracts import schemas

from .conftest import edit_info

REGISTRY = ["agibot", "aloha", "franka", "google_robot", "pusht", "so100", "so101", "ur5",
            "widowx"]


def _mod(doc: dict, module_id: str) -> dict:
    return next(m for m in doc["modules"] if m["id"] == module_id)


def _valid(doc: dict) -> dict:
    assert schemas.errors("cli/preflight.schema.json", doc) == []
    return doc


def _rewrite_episodes(root: str, fn) -> None:
    path = os.path.join(root, "meta", "episodes.jsonl")
    rows = [json.loads(ln) for ln in open(path, encoding="utf-8") if ln.strip()]
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(fn(row)) + "\n")


# ---------------------------------------------------------------- LeRobot v2


def test_v2_fixture_matches_the_contract(cli, dataset):
    res = cli("preflight", "--input", dataset)
    assert res.rc == 0
    doc = _valid(res.doc)
    assert doc["format"] == {"kind": "lerobot", "version": "v2", "supported": True,
                             "detail": "LeRobot v2.1, 8 episodes, 2 cameras"}
    assert doc["validation"] == []
    ds = doc["dataset"]
    assert ds["episode_count"] == 8 and ds["cameras"] == ["exterior", "wrist"]
    assert ds["fps"] == 15.0 and ds["robot_type"] == "franka"
    assert ds["labels"] == {"with_task": 6, "without_task": 2}      # eps 4 and 6 unlabelled
    assert ds["total_frames"] == json.load(open(f"{dataset}/meta/info.json"))["total_frames"]
    assert ds["profile"] is None
    # every registry module, in registry order
    assert [m["id"] for m in doc["modules"]] == list(registry_modules.ids())
    for mid in ("timestamp_check", "kinematic_limits", "motion_quality", "visual_quality",
                "video_action_sync", "dedup"):
        assert _mod(doc, mid) == {"id": mid, "availability": "available"}
    assert doc["meta_fingerprint"].startswith("sha256:")
    progress = [e for e in res.events if e["kind"] == "progress"]
    assert progress[-1]["stage"] == "preflight" and progress[-1]["done"] == progress[-1]["total"]


def test_no_vlm_backend_means_needs_input(cli, dataset):
    doc = cli("preflight", "--input", dataset).doc
    for mid in ("task_success", "skill_profile"):
        m = _mod(doc, mid)
        assert m["availability"] == "needs_input"
        assert m["input_hint"] == {"field": "vlm"}
        assert "VLM backend" in m["reason"]


def test_missing_task_text_does_not_grey_out_vlm_modules(cli, dataset):
    doc = _valid(cli("preflight", "--input", dataset, "--vlm-backend", "ark-prod").doc)
    for mid in ("task_success", "skill_profile"):
        m = _mod(doc, mid)
        assert m["availability"] == "available"
        assert m["notes"] == ["2 episodes have no task text; the model will caption them first"]


def test_fully_unlabelled_dataset_still_runs_vlm_modules(cli, dataset):
    _rewrite_episodes(dataset, lambda row: {**row, "tasks": [""]})
    doc = _valid(cli("preflight", "--input", dataset, "--vlm-backend", "ark").doc)
    assert doc["dataset"]["labels"] == {"with_task": 0, "without_task": 8}
    assert _mod(doc, "task_success")["availability"] == "available"


def test_fully_labelled_dataset_has_no_caption_note(cli, dataset):
    _rewrite_episodes(dataset, lambda row: {**row, "tasks": ["pick the block"]})
    doc = _valid(cli("preflight", "--input", dataset, "--vlm-backend", "ark").doc)
    assert doc["dataset"]["labels"] == {"with_task": 8, "without_task": 0}
    assert "notes" not in _mod(doc, "task_success")


# ---------------------------------------------------------------- robot type (doc 05 §4)


def test_robot_type_outside_the_registry_skips_only_kinematics(cli, dataset):
    edit_info(dataset, robot_type="umi_dual_handheld_gripper")
    res = cli("preflight", "--input", dataset, "--vlm-backend", "ark")
    assert res.rc == 0                                   # the task does not fail
    doc = _valid(res.doc)
    kin = _mod(doc, "kinematic_limits")
    assert kin["availability"] == "unsupported"
    assert kin["reason"].startswith("robot_type 'umi_dual_handheld_gripper' is not in the "
                                    "embodiment registry")
    assert "franka" in kin["reason"]                    # names what is supported
    others = [m for m in doc["modules"] if m["id"] != "kinematic_limits"]
    assert all(m["availability"] == "available" for m in others)


@pytest.mark.parametrize("robot_type", [None, "", "unknown"])
def test_unreadable_robot_type_needs_input(cli, dataset, robot_type):
    edit_info(dataset, robot_type=robot_type)          # None removes the key
    doc = _valid(cli("preflight", "--input", dataset).doc)
    kin = _mod(doc, "kinematic_limits")
    assert kin["availability"] == "needs_input"
    assert kin["input_hint"] == {"field": "embodiment_id", "options": REGISTRY}
    assert kin["reason"].endswith("pick a model or skip this module")
    if robot_type == "unknown":
        assert doc["dataset"]["robot_type"] == "unknown"
    else:
        assert kin["reason"] == ("robot_type not found in info.json; pick a model or skip "
                                 "this module")
        assert doc["dataset"]["robot_type"] is None


def test_no_question_when_the_module_is_not_selected(cli, dataset):
    edit_info(dataset, robot_type=None)
    doc = _valid(cli("preflight", "--input", dataset, "--modules",
                     "timestamp_check,visual_quality,dedup").doc)
    assert [m["id"] for m in doc["modules"]] == ["timestamp_check", "visual_quality", "dedup"]
    assert all(m["availability"] == "available" for m in doc["modules"])
    # and no VLM question either when no VLM module is selected
    assert not any(m.get("input_hint") for m in doc["modules"])


def test_embodiment_id_overrides_robot_type(cli, dataset):
    edit_info(dataset, robot_type="unknown")
    doc = _valid(cli("preflight", "--input", dataset, "--embodiment-id", "Franka").doc)
    kin = _mod(doc, "kinematic_limits")
    assert kin["availability"] == "available"
    assert kin["notes"] == ["embodiment franka given by the caller (info.json robot_type: "
                            "'unknown')"]
    doc = _valid(cli("preflight", "--input", dataset, "--embodiment-id", "koch").doc)
    kin = _mod(doc, "kinematic_limits")
    assert kin["availability"] == "unsupported"
    assert kin["reason"].startswith("embodiment 'koch' is not in the embodiment registry")


def test_dataset_profile_is_reported_and_suggests_an_embodiment(cli, mini_dataset, tmp_path):
    import shutil

    root = tmp_path / "droid_100"                  # droid_100.yaml matches by dataset name
    shutil.copytree(mini_dataset, root)
    edit_info(str(root), robot_type="unknown")
    doc = _valid(cli("preflight", "--input", str(root)).doc)
    assert doc["dataset"]["profile"] == {"matched": "droid_100", "by": "dataset_name"}
    kin = _mod(doc, "kinematic_limits")
    assert kin["availability"] == "needs_input"
    assert kin["notes"] == ["the dataset profile droid_100 suggests franka"]


# ---------------------------------------------------------------- capabilities (doc 05 §2)


def test_missing_state_greys_out_motion_quality(cli, dataset):
    info = json.load(open(f"{dataset}/meta/info.json"))
    feats = {k: v for k, v in info["features"].items() if k != "observation.state"}
    edit_info(dataset, features=feats)
    doc = _valid(cli("preflight", "--input", dataset, "--vlm-backend", "ark").doc)
    mq = _mod(doc, "motion_quality")
    assert mq == {"id": "motion_quality", "availability": "unsupported",
                  "reason": "the dataset has no observation.state feature"}
    assert _mod(doc, "kinematic_limits")["availability"] == "available"


def test_missing_videos(cli, dataset):
    import shutil

    os.remove(f"{dataset}/videos/chunk-000/observation.images.wrist/episode_000003.mp4")
    doc = _valid(cli("preflight", "--input", dataset, "--vlm-backend", "ark").doc)
    assert any("1 episode miss" in w and "(3)" in w for w in doc["warnings"])
    assert _mod(doc, "visual_quality")["availability"] == "available"
    shutil.rmtree(f"{dataset}/videos")
    doc = _valid(cli("preflight", "--input", dataset, "--vlm-backend", "ark").doc)
    for mid in ("visual_quality", "video_action_sync", "task_success", "skill_profile"):
        m = _mod(doc, mid)
        assert m["availability"] == "unsupported"
        assert m["reason"] == "no video files were found for the declared cameras"
    assert _mod(doc, "timestamp_check")["availability"] == "available"


# ---------------------------------------------------------------- formats (D6, D34)


def _touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


@pytest.mark.parametrize("kind,files,words", [
    ("rrd", ["episode_000000.rrd", "episode_000001.rrd"], "detected .rrd"),
    ("mcap", ["bags/ep0/data.mcap"], "detected mcap"),
    ("lancedb", ["table.lance/_versions/1.manifest", "table.lance/data/a.lance"],
     "detected LanceDB"),
    ("unknown", ["README.md"], "not a recognised dataset"),
    ("unknown", ["a/meta/info.json", "b/meta/info.json"], "a directory of 2 LeRobot datasets"),
])
def test_other_formats_grey_out_every_module(cli, tmp_path, kind, files, words):
    for rel in files:
        _touch(str(tmp_path / "in" / rel), b"{}")
    res = cli("preflight", "--input", str(tmp_path / "in"), "--vlm-backend", "ark")
    assert res.rc == 0
    doc = _valid(res.doc)
    assert doc["format"]["kind"] == kind and doc["format"]["supported"] is False
    assert doc["format"]["version"] is None and doc["dataset"] is None
    assert words in doc["format"]["detail"]
    assert len(doc["modules"]) == 8
    for m in doc["modules"]:
        assert m["availability"] == "unsupported"
        assert m["reason"].startswith("only LeRobot v2/v3 is supported in this version")


def test_other_lerobot_versions_are_unsupported(cli, dataset):
    edit_info(dataset, codebase_version="v1.6")
    doc = _valid(cli("preflight", "--input", dataset).doc)
    assert doc["format"]["kind"] == "lerobot" and doc["format"]["supported"] is False
    assert all("detected LeRobot v1.6" in m["reason"] for m in doc["modules"])


# ---------------------------------------------------------------- validation (doc 05 §3 ③)


def test_invalid_info_is_reported_verbatim(cli, dataset):
    edit_info(dataset, fps=None)
    doc = _valid(cli("preflight", "--input", dataset).doc)
    assert doc["format"]["supported"] is False
    assert len(doc["validation"]) == 1 and "fps" in doc["validation"][0]
    assert "缺少必需字段" in doc["validation"][0]           # v1's validate_info, untouched
    assert all(m["availability"] == "unsupported" for m in doc["modules"])


def test_declared_version_contradicting_the_layout(cli, dataset):
    edit_info(dataset, codebase_version="v3.0")
    doc = _valid(cli("preflight", "--input", dataset).doc)
    assert doc["format"]["version"] == "v3" and doc["format"]["supported"] is False
    assert "declares codebase_version=v3.0 but meta/ has the v2.x layout" in doc["validation"][0]


def test_episode_without_length_is_invalid(cli, dataset):
    """v1 reads int(ep["length"]) for every episode; preflight says so before a run crashes."""
    _rewrite_episodes(dataset, lambda row: {k: v for k, v in row.items() if k != "length"})
    doc = _valid(cli("preflight", "--input", dataset).doc)
    assert doc["format"]["supported"] is False
    assert "(episode 0) has no valid length" in doc["validation"][0]


def test_info_json_that_is_not_json(cli, dataset):
    with open(f"{dataset}/meta/info.json", "w") as fh:
        fh.write("{not json")
    doc = _valid(cli("preflight", "--input", dataset).doc)
    assert doc["validation"][0].startswith("meta/info.json is not valid JSON")


# ---------------------------------------------------------------- LeRobot v3


def make_v3(root, *, tasks_column: bool = True) -> str:
    """A metadata-complete LeRobot v3 layout (data and videos are placeholders)."""
    cams = ["observation.images.front"]
    info = {"codebase_version": "v3.0", "robot_type": "so101", "fps": 30,
            "total_episodes": 3, "total_frames": 90, "chunks_size": 1000,
            "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
            "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
            "features": {"action": {"dtype": "float32", "shape": [6]},
                         "observation.state": {"dtype": "float32", "shape": [6]},
                         "timestamp": {"dtype": "float32", "shape": [1]},
                         cams[0]: {"dtype": "video", "shape": [96, 128, 3]}}}
    _touch(os.path.join(root, "meta", "info.json"), json.dumps(info).encode())
    table = {"episode_index": [0, 1, 2], "length": [30, 30, 30],
             "data/chunk_index": [0, 0, 0], "data/file_index": [0, 0, 1],
             f"videos/{cams[0]}/chunk_index": [0, 0, 0],
             f"videos/{cams[0]}/file_index": [0, 0, 0]}
    if tasks_column:
        table["tasks"] = [["stack the cups"], [], ["stack the cups"]]
    os.makedirs(os.path.join(root, "meta", "episodes", "chunk-000"))
    pd.DataFrame(table).to_parquet(os.path.join(root, "meta", "episodes", "chunk-000",
                                                "file-000.parquet"))
    for rel in ("data/chunk-000/file-000.parquet", "data/chunk-000/file-001.parquet",
                f"videos/{cams[0]}/chunk-000/file-000.mp4"):
        _touch(os.path.join(root, rel))
    return str(root)


def test_v3_dataset(cli, tmp_path):
    root = make_v3(tmp_path / "v3")
    doc = _valid(cli("preflight", "--input", root, "--vlm-backend", "ark").doc)
    assert doc["format"] == {"kind": "lerobot", "version": "v3", "supported": True,
                             "detail": "LeRobot v3.0, 3 episodes, 1 camera"}
    assert doc["dataset"]["labels"] == {"with_task": 2, "without_task": 1}
    assert doc["dataset"]["cameras"] == ["front"]
    assert _mod(doc, "kinematic_limits")["availability"] == "available"     # so101
    assert doc["warnings"] == []


def test_v3_without_tasks_column_warns(cli, tmp_path):
    root = make_v3(tmp_path / "v3", tasks_column=False)
    doc = _valid(cli("preflight", "--input", root).doc)
    assert doc["dataset"]["labels"] == {"with_task": 0, "without_task": 3}
    assert any("no tasks column" in w for w in doc["warnings"])


# ---------------------------------------------------------------- inputs and credentials


def test_tos_input_is_read_with_the_input_key_set(cli, cloud, mini_dataset, monkeypatch):
    cloud.upload_dir(mini_dataset, "src-bucket", "datasets/mini")
    cloud.bucket("src-bucket", readers={"in-ak"})
    monkeypatch.setenv("CURATION_INPUT_TOS_ACCESS_KEY", "in-ak")
    monkeypatch.setenv("CURATION_INPUT_TOS_SECRET_KEY", "in-sk")
    monkeypatch.setenv("CURATION_OUTPUT_TOS_ACCESS_KEY", "out-ak")
    monkeypatch.setenv("CURATION_OUTPUT_TOS_SECRET_KEY", "out-sk")
    monkeypatch.setenv("TOS_ACCESS_KEY", "shared-ak")
    monkeypatch.setenv("TOS_SECRET_KEY", "shared-sk")
    res = cli("preflight", "--input", "tos://src-bucket/datasets/mini",
              "--input-region", "cn-shanghai")
    assert res.rc == 0
    doc = _valid(res.doc)
    assert doc["dataset"]["episode_count"] == 8
    assert [c["access_key"] for c in cloud.clients] == ["in-ak"]
    assert cloud.clients[0]["region"] == "cn-shanghai"
    assert cloud.clients[0]["endpoint"] == "https://tos-cn-shanghai.volces.com"
    assert "in-sk" not in res.out + res.err


def test_tos_input_falls_back_to_the_shared_pair(cli, cloud, mini_dataset, monkeypatch):
    cloud.upload_dir(mini_dataset, "src-bucket", "mini")
    monkeypatch.setenv("TOS_ACCESS_KEY", "shared-ak")
    monkeypatch.setenv("TOS_SECRET_KEY", "shared-sk")
    assert cli("preflight", "--input", "tos://src-bucket/mini").rc == 0
    assert cloud.clients[0]["access_key"] == "shared-ak"


def test_output_keys_are_never_used_for_the_input(cli, cloud, mini_dataset, monkeypatch):
    cloud.upload_dir(mini_dataset, "src-bucket", "mini")
    monkeypatch.setenv("CURATION_OUTPUT_TOS_ACCESS_KEY", "out-ak")
    monkeypatch.setenv("CURATION_OUTPUT_TOS_SECRET_KEY", "out-sk")
    res = cli("preflight", "--input", "tos://src-bucket/mini")
    assert res.rc == 2 and "CURATION_INPUT_TOS_ACCESS_KEY" in res.doc["error"]["message"]
    assert cloud.clients == []


def test_half_configured_keys_are_refused(cli, cloud, monkeypatch):
    monkeypatch.setenv("CURATION_INPUT_TOS_ACCESS_KEY", "in-ak")
    monkeypatch.setenv("TOS_ACCESS_KEY", "shared-ak")
    monkeypatch.setenv("TOS_SECRET_KEY", "shared-sk")
    res = cli("preflight", "--input", "tos://src-bucket/mini")
    assert res.rc == 2
    assert "CURATION_INPUT_TOS_SECRET_KEY is not set" in res.doc["error"]["message"]


def test_rejected_key_and_missing_bucket_are_unreachable(cli, cloud, mini_dataset, monkeypatch):
    cloud.upload_dir(mini_dataset, "src-bucket", "mini")
    cloud.bucket("src-bucket", readers={"someone-else"})
    monkeypatch.setenv("TOS_ACCESS_KEY", "ak")
    monkeypatch.setenv("TOS_SECRET_KEY", "sk")
    res = cli("preflight", "--input", "tos://src-bucket/mini")
    assert res.rc == 3 and res.doc["error"]["code"] == "input_unreachable"
    assert "AccessDenied" in res.doc["error"]["message"]
    res = cli("preflight", "--input", "tos://no-such-bucket/mini")
    assert res.rc == 3 and "NoSuchBucket" in res.doc["error"]["message"]
    cloud.bucket("src-bucket", readers={"ak"})
    res = cli("preflight", "--input", "tos://src-bucket/not-there")
    assert res.rc == 3 and "nothing found" in res.doc["error"]["message"]


def test_public_source_reads_anonymously(cli, cloud, mini_dataset, tmp_path):
    cloud.upload_dir(mini_dataset, "hf-cache", "dataset/mini")
    site = tmp_path / "site.yaml"
    site.write_text("public_datasets:\n  bucket: hf-cache\n  region: cn-beijing\n")
    res = cli("preflight", "--input", "mini", "--source", "public", "--config", str(site))
    assert res.rc == 0
    assert _valid(res.doc)["dataset"]["episode_count"] == 8
    assert cloud.clients[0]["access_key"] == ""                 # no key sent
    res = cli("preflight", "--input", "mini", "--source", "public")
    assert res.rc == 2 and "public_datasets.bucket" in res.doc["error"]["message"]


def test_local_input_errors(cli, tmp_path, dataset):
    res = cli("preflight", "--input", str(tmp_path / "missing"))
    assert res.rc == 3 and res.doc["error"]["code"] == "input_unreachable"
    (tmp_path / "empty").mkdir()
    assert cli("preflight", "--input", str(tmp_path / "empty")).rc == 3
    assert cli("preflight", "--input", dataset, "--source", "tos").rc == 2
    assert cli("preflight", "--input", "tos://b-1/x", "--source", "local").rc == 2
    res = cli("preflight", "--input", dataset, "--modules", "timestamp_check,nope")
    assert res.rc == 2 and "unknown module 'nope'" in res.doc["error"]["message"]
    res = cli("preflight", "--input", dataset, "--input-region", "Bei Jing")
    assert res.rc == 2 and "--input-region" in res.doc["error"]["message"]


def test_human_output(cli, dataset):
    res = cli("preflight", "--input", dataset, json_mode=False)
    assert res.rc == 0
    assert res.out.startswith("LeRobot v2.1, 8 episodes, 2 cameras (supported)")
    assert "task_success" in res.out and "needs input" in res.out
    assert "listed" in res.err                     # logs stay on stderr
