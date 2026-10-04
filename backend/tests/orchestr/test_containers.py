"""mcap and lance datasets in the Daemon (D44, C4 1.11): registration, browsing, the episode
grid, and (slow) tasks that run the real CLI end to end and deliver the format."""
from __future__ import annotations

import json
import os
import pathlib
import shutil

import pytest

from .conftest import API, JSON, assert_schema
from .test_api import _page_schema, api  # noqa: F401 - the fixture is registered by importing it

pytest.importorskip("mcap")
pytest.importorskip("mcap_ros2")
pytest.importorskip("lance")


@pytest.fixture(scope="session")
def containers(tmp_path_factory) -> dict:
    from parity.fixtures import make_mini_lance, make_mini_mcap

    base = tmp_path_factory.mktemp("containers")
    return {"mcap": make_mini_mcap(str(base / "mini_mcap")),
            "lance": make_mini_lance(str(base / "mini_lance"))}


@pytest.fixture
def capi(api, containers, tmp_path):  # noqa: F811 - the imported fixture
    c, tos, dataset = api()
    root = pathlib.Path(dataset).parent
    paths = {}
    for fmt, src in containers.items():
        shutil.copytree(src, root / f"mini_{fmt}")
        paths[fmt] = str(root / f"mini_{fmt}")
    return c, tos, paths


def test_register_recheck_and_repreflight(capi):
    c, _, paths = capi
    for fmt, n_objects in (("mcap", 8), ("lance", None)):
        r = c.post(f"{API}/datasets", headers=JSON,
                   json={"input": {"source": "local", "uri": paths[fmt]}})
        assert r.status_code == 201, r.text
        ds = r.json()
        assert_schema("DatasetDetail", ds)
        assert ds["format"] == fmt and ds["episode_count"] == 8 and ds["robot_type"] == "franka"
        assert ds["meta_fingerprint"] == ds["preflight"]["meta_fingerprint"]
        if n_objects:
            assert ds["listing"]["objects"] == n_objects      # the episode files
        page = c.get(f"{API}/datasets", params={"format": fmt}).json()
        assert [d["id"] for d in page["items"]] == [ds["id"]]
        same = c.post(f"{API}/datasets/{ds['id']}/recheck", headers=JSON).json()
        assert same["result"] == "same", same
    ds = c.get(f"{API}/datasets", params={"format": "mcap"}).json()["items"][0]
    target = pathlib.Path(paths["mcap"]) / "episode_1.mcap"
    os.utime(target, ns=(target.stat().st_atime_ns, target.stat().st_mtime_ns + 10**9))
    changed = c.post(f"{API}/datasets/{ds['id']}/recheck", headers=JSON).json()
    assert changed["result"] == "changed" and changed["change"]["modified"] == 1
    assert changed["change"]["meta_changed"] is True      # an mcap file is its own metadata
    assert changed["change"]["sample_keys"] == ["episode_1.mcap"]
    r = c.post(f"{API}/datasets/{ds['id']}/repreflight", headers=JSON)
    assert r.status_code == 200 and r.json()["check_state"] == "ok"


def test_browse_hints_the_formats(capi):
    c, _, paths = capi
    r = c.get(f"{API}/datasets/browse", params={"source": "local"})
    assert r.status_code == 200, r.text
    _page_schema("BrowsedDataset", r.json())
    hints = {i["name"]: (i["format_hint"], i["episodes"]) for i in r.json()["items"]}
    assert hints == {"mini": ("lerobot_v2", 8), "mini_lance": ("lance", 8),
                     "mini_mcap": ("mcap", 8)}


def test_episode_grid_of_the_formats(capi):
    c, _, paths = capi
    r = c.get(f"{API}/datasets/episodes", params={"source": "local", "uri": paths["lance"],
                                                   "limit": 10})
    assert r.status_code == 200, r.text
    _page_schema("EpisodePreview", r.json())
    by = {e["index"]: e for e in r.json()["items"]}
    assert len(by) == 8 and all(e["cameras"] == [] for e in by.values())
    assert by[0]["task_source"] == "原始标注" and by[0]["length_s"] == 5.0
    assert by[4]["task_source"] == "无"
    r = c.get(f"{API}/datasets/episodes", params={"source": "local", "uri": paths["mcap"],
                                                   "limit": 10})
    assert r.status_code == 200, r.text
    _page_schema("EpisodePreview", r.json())
    by = {e["index"]: e for e in r.json()["items"]}
    assert len(by) == 8 and all(e["cameras"] == [] for e in by.values())
    assert by[0]["length_s"] == pytest.approx(74 / 15, abs=1e-3)   # first to last message
    assert by[0]["task"] == "" and by[0]["task_unread"] is True      # a /task topic
    assert by[4]["task_source"] == "无" and "task_unread" not in by[4]


def test_episode_grid_of_an_mcap_dataset_on_tos(capi, containers):
    c, tos, _ = capi
    for name in os.listdir(containers["mcap"]):
        with open(os.path.join(containers["mcap"], name), "rb") as fh:
            tos.put("datasets", f"robot/mini_mcap/{name}", fh.read())
    r = c.get(f"{API}/datasets/browse", params={"source": "tos", "uri": "tos://datasets/robot",
                                                 "credential": "in-key"})
    assert r.status_code == 200, r.text
    assert r.json()["items"] == [{"name": "mini_mcap", "uri": "tos://datasets/robot/mini_mcap",
                                  "format_hint": "mcap", "episodes": 8}]
    r = c.get(f"{API}/datasets/episodes", params={
        "source": "tos", "uri": "tos://datasets/robot/mini_mcap", "credential": "in-key",
        "limit": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    _page_schema("EpisodePreview", body)
    assert [e["index"] for e in body["items"]] == [0, 1, 2] and body["has_more"] is True
    assert body["items"][0]["length_s"] == pytest.approx(74 / 15, abs=1e-3)
    gets = [x for x in tos.calls if x["op"] == "get_object" and x["key"].endswith(".mcap")]
    assert len(gets) == 3                                   # one summary read per episode


# ---------------------------------------------------------------- end to end (slow)


def _run(daemon, containers, fmt, **create):
    d = daemon()
    target = os.path.join(d.root, f"mini_{fmt}")
    shutil.copytree(containers[fmt], target)
    d.dataset = target
    task = d.wait(d.create(**create)["id"])
    return d, task


@pytest.mark.slow
@pytest.mark.parametrize("fmt", ["mcap", "lance"])
def test_a_task_on_the_format_runs_and_delivers(daemon, containers, fmt):
    d, task = _run(daemon, containers, fmt)
    assert task["state"] == "succeeded", json.dumps(task, ensure_ascii=False)[:3000]
    assert task["summary"] == {"total": 8, "passed": 5, "rejected": 3, "held": 0, "review": 3,
                               "pass_rate": 0.625}
    assert task["delivery_stale"] is False
    batch = d.delivery(task["run_id"])
    assert os.path.isfile(os.path.join(batch, "_COMPLETE"))
    with open(os.path.join(batch, "export", "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    assert manifest["source_format"] == fmt
    root = os.path.join(batch, "export", manifest["dataset_dir"])
    if fmt == "mcap":
        assert sorted(n for n in os.listdir(root) if n.endswith(".mcap")) == [
            f"episode_{i}.mcap" for i in (0, 1, 3, 4, 6)]
    else:
        assert os.path.isdir(os.path.join(root, "episodes_parquet"))
    ds = d.api("GET", f"/datasets/{task['dataset_id']}").json()
    assert ds["format"] == fmt
    logs = d.api("GET", f"/tasks/{task['id']}/logs", params={"limit": 200,
                                                             "stage": "export"}).json()
    msgs = " ".join(x.get("msg", "") for x in logs.get("items") or [])
    assert f"export/{manifest['dataset_dir']}" in msgs, msgs[:2000]
    if fmt == "lance":
        assert "原格式交付本版本未做" in msgs
    # the run's source cache went with the run
    assert not os.path.exists(os.path.join(d.rt.settings.source_cache_dir, task["id"]))
    with open(os.path.join(d.run_dir(task["id"]), "revisions", "r0001", "report.json"),
              encoding="utf-8") as fh:
        assert json.load(fh)["integrity"]["container"]["format"] == fmt


@pytest.mark.slow
def test_a_changed_mcap_file_stops_the_start(daemon, containers):
    """D37 on an mcap dataset: a file touched after registration is a change."""
    d = daemon()
    target = os.path.join(d.root, "mini_mcap")
    shutil.copytree(containers["mcap"], target)
    d.dataset = target
    created = d.create(modules=["timestamp_check"], start_now=False)
    path = pathlib.Path(target) / "episode_2.mcap"
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10**9))
    r = d.action(created["id"], "start")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "source_changed"
    assert r.json()["error"]["details"]["sample_keys"] == ["episode_2.mcap"]


@pytest.mark.slow
def test_a_confirmed_mapping_reaches_every_command_and_changes_no_verdict(daemon, containers):
    """D62 (design doc 18 §6): the registration's confirmed mcap mapping goes to the CLI as the
    derived ingest.mcap_mapping and is frozen into run.json; the draft of the reader's own
    convention reads the same rows, so the verdicts are those of the run without a mapping."""
    d = daemon()
    target = os.path.join(d.root, "mini_mcap")
    shutil.copytree(containers["mcap"], target)
    d.dataset = target
    probe = d.api("POST", "/viz/mcap-probe", json={"input": {"source": "local", "uri": target}})
    assert probe.status_code == 200, probe.text
    r = d.api("POST", "/datasets", json={"input": {"source": "local", "uri": target},
                                         "viz_mapping": probe.json()["draft"]})
    assert r.status_code == 201, r.text
    assert r.json()["viz_mapping"]["version"] == 1
    task = d.wait(d.create()["id"])
    assert task["state"] == "succeeded", json.dumps(task, ensure_ascii=False)[:3000]
    assert task["summary"] == {"total": 8, "passed": 5, "rejected": 3, "held": 0, "review": 3,
                               "pass_rate": 0.625}
    with open(os.path.join(d.run_dir(task["id"]), "run.json"), encoding="utf-8") as fh:
        frozen = json.load(fh)["viz_mapping"]
    assert frozen["version"] == 1 and frozen["check_mapping"]["action"] == "/action"
    runs, cursor = [], None
    while True:
        page = d.api("GET", f"/tasks/{task['id']}/logs",
                     params={"limit": 200, "level": "debug", **({"cursor": cursor} if cursor else {})})
        assert page.status_code == 200, page.text
        runs += [x["msg"] for x in page.json()["items"] if x["msg"].startswith("run: curation ")]
        cursor = page.json().get("next_cursor")
        if not cursor:
            break
    reading = [m for m in runs if " --input " in m]
    assert reading and all("ingest.mcap_mapping=" in m for m in reading), runs
    # a later confirmation is a new version for new tasks; this task keeps what it ran with
    changed = dict(probe.json()["draft"], name="改过的映射")
    r = d.api("PUT", f"/datasets/{task['dataset_id']}/mapping", json={"mapping": changed})
    assert r.status_code == 200 and r.json()["version"] == 2, r.text
    viz = d.api("GET", f"/tasks/{task['id']}/viz").json()
    assert viz["mapping"] == {"state": "frozen", "version": 1, "name": probe.json()["draft"].get("name")}
    assert d.api("GET", f"/datasets/{task['dataset_id']}/viz").json()["mapping"]["version"] == 2


# ---------------------------------------------------------------- the janitor


def test_the_janitor_sweeps_source_caches_a_crash_left(daemon, monkeypatch):
    """A run removes its source cache when it ends; what a crash left goes at the next
    janitor round for a task with nothing running - even with the work-directory cleaning
    off (CURATOR_WORK_RETENTION_DAYS=0): a dataset copy is never worth keeping."""
    import dataclasses

    d = daemon()
    j = d.orch.janitor
    j.stop()
    root = pathlib.Path(d.rt.settings.source_cache_dir)
    left = root / "task_crashed" / "mcap-0123456789abcdef" / "mini"
    left.mkdir(parents=True)
    (left / "episode_0.mcap").write_bytes(b"\0" * 16)
    (root / "task_running" / "tmp").mkdir(parents=True)
    monkeypatch.setattr(j, "_busy", lambda task_id: task_id == "task_running")
    monkeypatch.setattr(d.orch, "cfg", dataclasses.replace(d.orch.cfg, work_retention_s=0))
    assert j.sweep() == []
    assert sorted(p.name for p in root.iterdir()) == ["task_running"]


def test_the_janitor_thread_runs_without_the_work_retention():
    import types

    from daemon.orchestr.janitor import Janitor

    j = Janitor(types.SimpleNamespace(cfg=types.SimpleNamespace(work_retention_s=0,
                                                                janitor_interval_s=3600.0)))
    j.start()
    try:
        assert j._thread is not None and j._thread.is_alive()
    finally:
        j.stop()
        j._thread.join(timeout=5)
