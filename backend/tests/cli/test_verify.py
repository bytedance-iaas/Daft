"""curation verify (design doc 02 §3.10): read the delivery back; _COMPLETE only when all pass."""
from __future__ import annotations

import json
import os
import shutil

import numpy as np
import pandas as pd
import pytest

from curation.cli import verify as verify_mod
from curation.contracts import schemas

VIDEO = "export/lerobot_curated/videos/chunk-000/observation.images.wrist/episode_000000.mp4"
DATA = "export/lerobot_curated/data/chunk-000/episode_000000.parquet"
REMOTE_ONLY = ("export/lerobot_curated/videos/chunk-000/observation.images.exterior/"
               "episode_000000.mp4")


def _valid(doc: dict) -> dict:
    assert schemas.errors("cli/verify.schema.json", doc) == []
    return doc


def _write(root, rel, data) -> None:
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as fh:
        fh.write(data)


def _image(ext: str) -> bytes:
    import cv2

    ok, buf = cv2.imencode(ext, np.full((8, 8, 3), 127, dtype=np.uint8))
    assert ok
    return buf.tobytes()


def make_run(root: str, mini: str) -> str:
    """A run directory shaped like design doc 06 §1, with real JSON / parquet / mp4 / images."""
    wrist = os.path.join(mini, "videos/chunk-000/observation.images.wrist/episode_000000.mp4")
    exterior = os.path.join(mini, "videos/chunk-000/observation.images.exterior/"
                                  "episode_000000.mp4")
    _write(root, "run.json", json.dumps({"run_id": "20260921-101500"}))
    _write(root, "passed.json", json.dumps({"schema_version": "1.0", "episodes": [0, 1, 3]}))
    _write(root, "reject.json", json.dumps({"schema_version": "1.0", "episodes": [2, 5, 7]}))
    _write(root, "report.md", "# QA report\n\nall good\n")
    _write(root, "checks/timestamp_check/results.jsonl",
           '{"episode_index": 0, "verdict": "pass"}\n{"episode_index": 1, "verdict": "pass"}\n')
    pd.DataFrame({"episode_index": [0, 1], "score": [0.9, 0.8]}).to_parquet(
        os.path.join(root, "checks", "timestamp_check", "table.parquet"))
    _write(root, "details/evidence/ep000000/probe_f0010.jpg", _image(".jpg"))
    _write(root, "details/plots/sync_ep000000.png", _image(".png"))
    with open(wrist, "rb") as fh:
        _write(root, VIDEO, fh.read())
    with open(os.path.join(mini, "data/chunk-000/episode_000000.parquet"), "rb") as fh:
        _write(root, DATA, fh.read())
    _write(root, "export/lerobot_curated/meta/info.json", json.dumps({"codebase_version": "v2.1"}))
    manifest = {"schema_version": "1.0", "source_format": "lerobot_v2",
                "fingerprint": "sha256:" + "0" * 64,
                "episodes": [{"episode_index": 0, "new_index": 0,
                              "content_key": "sha256:" + "1" * 64,
                              "task_key": "sha256:" + "2" * 64,
                              "artifacts": {"parquet": DATA.split("lerobot_curated/")[1],
                                            "videos": {
                                                "wrist": VIDEO.split("lerobot_curated/")[1],
                                                "exterior": REMOTE_ONLY.split(
                                                    "lerobot_curated/")[1]}}}],
                "meta_files": ["meta/info.json"]}
    assert schemas.errors("cli/export-manifest.schema.json", manifest) == []
    _write(root, "export/manifest.json", json.dumps(manifest))
    # work in progress, not part of the delivery
    _write(root, "logs/verify.jsonl", '{"ts": 1, "kind": "log"}\n')
    _write(root, "checks/timestamp_check/inflight.json", "{}")
    _write(root, ".curation-out-123.tmp", "x")
    root_out = root + "-remote-only"
    with open(exterior, "rb") as fh:          # uploaded by the exporter, no local copy
        _write(root_out, REMOTE_ONLY, fh.read())
    return root


DELIVERED = ["run.json", "passed.json", "reject.json", "report.md",
             "checks/timestamp_check/results.jsonl", "checks/timestamp_check/table.parquet",
             "details/evidence/ep000000/probe_f0010.jpg", "details/plots/sync_ep000000.png",
             VIDEO, DATA, "export/lerobot_curated/meta/info.json", "export/manifest.json",
             REMOTE_ONLY]


def publish(run: str, out: str) -> str:
    """Copy what the Daemon would upload (the delivered files) to a local delivery."""
    for rel in DELIVERED:
        src = os.path.join(run + "-remote-only" if rel == REMOTE_ONLY else run, rel)
        dst = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    return out


@pytest.fixture
def run_and_out(tmp_path, mini_dataset):
    run = make_run(str(tmp_path / "run"), mini_dataset)
    out = publish(run, str(tmp_path / "delivery" / "20260921-101500"))
    return run, out


def _verify(cli, run, out, *extra, **kw):
    return cli("verify", "--run-dir", run, "--output", out, "--visibility-timeout", "0",
               *extra, **kw)


def test_a_good_delivery_gets_complete(cli, run_and_out):
    run, out = run_and_out
    res = _verify(cli, run, out)
    assert res.rc == 0
    doc = _valid(res.doc)
    assert doc == {"schema_version": "1.0", "output": out, "checked": len(DELIVERED),
                   "failed": [], "complete_marker": True}
    marker = json.load(open(os.path.join(out, "_COMPLETE")))
    assert marker["checked"] == len(DELIVERED)
    progress = [e for e in res.events if e["kind"] == "progress"]
    assert progress[-1] == {**progress[-1], "stage": "verify", "done": len(DELIVERED),
                            "total": len(DELIVERED)}


def _corrupt(path: str, how: str) -> None:
    data = open(path, "rb").read()
    if how == "zero":
        data = b"\0" * len(data)
    elif how == "grow":
        data += b" "
    elif how == "nul":
        data = data[:3] + b"\0" + data[4:]
    elif how == "json":
        data = b"{" * len(data)
    elif how == "magic":
        data = b"XXXX" + data[4:]
    elif how == "tail":
        data = data[:-4] + b"XXXX"
    elif how == "cut":
        data = data[: len(data) // 2]
    with open(path, "wb") as fh:
        fh.write(data)


@pytest.mark.parametrize("rel,how,both,reason", [
    ("passed.json", "zero", False, "zero_filled"),
    ("passed.json", "grow", False, "size_mismatch"),
    ("reject.json", "json", False, "unparseable"),
    ("report.md", "nul", False, "unparseable"),
    ("checks/timestamp_check/results.jsonl", "json", False, "unparseable"),
    ("checks/timestamp_check/table.parquet", "tail", True, "unparseable"),
    ("details/evidence/ep000000/probe_f0010.jpg", "magic", True, "unparseable"),
    ("details/plots/sync_ep000000.png", "magic", True, "unparseable"),
    (VIDEO, "cut", True, "unparseable"),              # the moov box sits at the end
    (VIDEO, "zero", True, "zero_filled"),
    (REMOTE_ONLY, "zero", False, "zero_filled"),
])
def test_bad_files_are_named_and_block_complete(cli, run_and_out, rel, how, both, reason):
    run, out = run_and_out
    _write(out, "_COMPLETE", "{}")                     # stale marker from an earlier export
    _corrupt(os.path.join(out, rel), how)
    if both:                                           # the run dir holds the same bytes
        shutil.copyfile(os.path.join(out, rel), os.path.join(run, rel))
    res = _verify(cli, run, out)
    assert res.rc == 0                                 # the Daemon reads `failed`, not the code
    doc = _valid(res.doc)
    assert doc["failed"] == [{"path": rel, "reason": reason}]
    assert doc["complete_marker"] is False
    assert not os.path.exists(os.path.join(out, "_COMPLETE"))


def test_missing_files(cli, run_and_out):
    run, out = run_and_out
    os.remove(os.path.join(out, "report.md"))
    os.remove(os.path.join(out, REMOTE_ONLY))            # listed in export/manifest.json only
    doc = _valid(_verify(cli, run, out).doc)
    assert doc["failed"] == [{"path": REMOTE_ONLY, "reason": "missing"},
                             {"path": "report.md", "reason": "missing"}]


def test_export_manifest_is_read_from_the_delivery_when_not_kept(cli, run_and_out):
    """The exporter may upload the dataset and its manifest and keep no local copy."""
    run, out = run_and_out
    for rel in ("export/manifest.json", VIDEO, DATA):
        os.remove(os.path.join(run, rel))
    os.remove(os.path.join(out, REMOTE_ONLY))
    doc = _valid(_verify(cli, run, out).doc)
    assert doc["checked"] == len(DELIVERED) - 1          # everything but the manifest itself
    assert doc["failed"] == [{"path": REMOTE_ONLY, "reason": "missing"}]


def test_run_dir_errors(cli, tmp_path, run_and_out):
    run, out = run_and_out
    res = cli("verify", "--run-dir", str(tmp_path / "nope"), "--output", out)
    assert res.rc == 2 and "--run-dir" in res.doc["error"]["message"]
    (tmp_path / "empty").mkdir()
    assert cli("verify", "--run-dir", str(tmp_path / "empty"), "--output", out).rc == 2
    assert cli("verify", "--run-dir", run, "--output", out, "--visibility-timeout", "-1").rc == 2


# ---------------------------------------------------------------- TOS delivery


@pytest.fixture
def tos_delivery(cloud, run_and_out, monkeypatch):
    run, out = run_and_out
    cloud.upload_dir(out, "dst-bucket", "deliveries/droid-50/20260921-101500")
    cloud.bucket("dst-bucket", readers={"out-ak"})
    monkeypatch.setenv("CURATION_INPUT_TOS_ACCESS_KEY", "in-ak")
    monkeypatch.setenv("CURATION_INPUT_TOS_SECRET_KEY", "in-sk")
    monkeypatch.setenv("CURATION_OUTPUT_TOS_ACCESS_KEY", "out-ak")
    monkeypatch.setenv("CURATION_OUTPUT_TOS_SECRET_KEY", "out-sk")
    return run, "tos://dst-bucket/deliveries/droid-50/20260921-101500"


def test_tos_delivery_uses_the_output_keys_and_ranged_reads(cli, cloud, tos_delivery):
    run, url = tos_delivery
    res = _verify(cli, run, url, "--output-region", "cn-beijing")
    assert res.rc == 0
    doc = _valid(res.doc)
    assert doc["complete_marker"] is True and doc["output"] == url
    assert "deliveries/droid-50/20260921-101500/_COMPLETE" in cloud.buckets["dst-bucket"]
    assert {c["access_key"] for c in cloud.clients} == {"out-ak"}
    video_gets = [c for c in cloud.calls if c[0] == "get" and c[2].endswith(VIDEO)]
    assert video_gets and all(c[3] is not None for c in video_gets)     # never the whole file
    size = len(cloud.buckets["dst-bucket"]["deliveries/droid-50/20260921-101500/" + VIDEO])
    assert sum(c[4] - c[3] + 1 for c in video_gets) < size


def test_files_that_become_readable_within_the_window(cli, cloud, tos_delivery, monkeypatch):
    run, url = tos_delivery
    monkeypatch.setattr(verify_mod, "_POLL_S", 0.05)
    key = "deliveries/droid-50/20260921-101500/passed.json"
    cloud.invisible[("dst-bucket", key)] = 1            # listed, first read fails
    res = cli("verify", "--run-dir", run, "--output", url, "--visibility-timeout", "5")
    assert _valid(res.doc)["complete_marker"] is True
    cloud.invisible[("dst-bucket", key)] = 10 ** 6       # never readable in time
    res = cli("verify", "--run-dir", run, "--output", url, "--visibility-timeout", "0.2")
    assert _valid(res.doc)["failed"] == [{"path": "passed.json",
                                          "reason": "not_visible_in_time"}]
    assert "deliveries/droid-50/20260921-101500/_COMPLETE" not in cloud.buckets["dst-bucket"]


def test_tos_delivery_errors(cli, cloud, tos_delivery, monkeypatch):
    run, url = tos_delivery
    res = _verify(cli, run, "tos://no-such-bucket/deliveries/x")
    assert res.rc == 3 and res.doc["error"]["code"] == "output_unreachable"
    monkeypatch.delenv("CURATION_OUTPUT_TOS_ACCESS_KEY")
    monkeypatch.delenv("CURATION_OUTPUT_TOS_SECRET_KEY")
    res = _verify(cli, run, url)                         # the input keys are not borrowed
    assert res.rc == 2 and "CURATION_OUTPUT_TOS_ACCESS_KEY" in res.doc["error"]["message"]


def test_human_output(cli, run_and_out):
    run, out = run_and_out
    os.remove(os.path.join(out, "run.json"))
    res = _verify(cli, run, out, json_mode=False)
    assert res.rc == 0
    assert "run.json: missing" in res.out and "_COMPLETE not written" in res.out


def test_what_counts_as_delivered():
    assert verify_mod.is_delivered("revisions/r0001/report.json")
    assert not verify_mod.is_delivered("logs/check.jsonl")
    assert not verify_mod.is_delivered("checks/task_success/inflight.json")
    assert not verify_mod.is_delivered(".hidden/x.json")
    assert not verify_mod.is_delivered("passed.json.tmp-123")
    assert not verify_mod.is_delivered("_COMPLETE")
