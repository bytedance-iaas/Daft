"""GET /tasks/{id}/episodes/{index}: one episode across every module of a revision."""
from __future__ import annotations

import json
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from daemon.results import store_of

from .conftest import (API, CAMERAS, EPISODES, TEXT, T0, World, assert_error, assert_schema,
                       build_run_dir, finish_main_run, make_dataset, make_task, record,
                       source_manifest)


def _view(world, ep, **q):
    r = world.get(f"/episodes/{ep}", **q)
    assert r.status_code == 200, r.text
    body = r.json()
    assert_schema("EpisodeView", body)
    for rec in body["modules"].values():
        assert_schema("cli/result-record.schema.json", rec)
    return body


def test_every_episode_of_the_revision(world):
    lists = {0: "passed", 1: "reject", 2: "reject", 3: "passed", 4: "passed", 5: "passed",
             6: "held", 7: "reject", 8: "passed"}
    for ep in EPISODES:
        body = _view(world, ep)
        assert body["episode_index"] == ep and body["revision"] == 1
        assert body["list"] == lists[ep], ep
    ep1 = _view(world, 1)          # rejected on its timestamps; every module judged it all the same (D57)
    assert sorted(ep1["modules"]) == ["kinematic_limits", "motion_quality", "task_success", "timestamp_check",
                                      "video_action_sync", "visual_quality"]
    assert ep1["reasons"] == [{"module": "timestamp_check", "kind": "finding", "code": "fragment",
                               "item": "STRM-5", "appealable": False, "text": "fragment"}]
    ep0 = _view(world, 0)
    assert sorted(ep0["modules"]) == sorted(
        ["timestamp_check", "kinematic_limits", "motion_quality", "visual_quality",
         "video_action_sync", "task_success", "dedup", "skill_profile"])
    assert ep0["reasons"] == [] and ep0["review"] == [] and ep0["evidence"] == []
    ep6 = _view(world, 6)
    assert ep6["modules"]["task_success"]["status"] == "error"
    assert ep6["reasons"][0]["kind"] == "execution_error"
    ep7 = _view(world, 7)
    assert ep7["reasons"][0] == {"module": "dedup", "kind": "duplicate", "code": "duplicate", "item": "SET-1",
                                 "appealable": True, "text": "与 ep000000 字节级完全重复", "duplicate_of": 0}


def test_every_finding_with_the_level_it_has(world):
    """C4 2.3.0 ``findings``: each finding of the records with its level in the revision (the list entry's),
    the review line it is asked on and whether it may be appealed; IMG-2 is every episode's underexposure."""
    def rows(view):
        return [(f["module"], f["finding"]["code"], f["finding"]["item"], f["level"], f.get("line"),
                 f["appealable"]) for f in view["findings"]]

    ep5 = _view(world, 5)
    assert rows(ep5) == [("visual_quality", "exposure_low", "IMG-2", "info", None, False)] * 2 + [
        ("task_success", "uncertain", "TASK-5", "review", "task_verdict", False),
        ("skill_profile", "label_disagreement", "LABEL-5", "review", "label", False)]
    assert ep5["findings"][2]["finding"] == ep5["modules"]["task_success"]["findings"][0]   # the record's own
    ep7 = _view(world, 7)
    assert rows(ep7)[-1] == ("dedup", "duplicate", "SET-1", "blocking", None, True)
    assert ("timestamp_check", "fragment", "STRM-5", "blocking", None, False) in rows(_view(world, 1))
    assert [f["finding"]["code"] for f in _view(world, 6)["findings"]] == ["exposure_low"] * 2   # failed: none


def test_a_person_s_conclusion_is_a_finding_of_its_own(world):
    """After a person judges ep 3 a failure, r2 lists it as a blocking finding of task_success, with its text;
    the model's abstention it answered is gone."""
    from .conftest import T0

    assert world.decide((3, "task_verdict", "failure")).status_code == 200
    sub = world.start_subtask(at=T0 + 10 * 60_000)
    world.apply(sub)
    world.revision(2, subtask_id=sub.id)
    world.finish_subtask(sub, at=T0 + 20 * 60_000)
    world.switch(2)
    ep3 = _view(world, 3)
    assert ep3["list"] == "reject"
    human = [f for f in ep3["findings"] if f.get("human")]
    assert human == [{"module": "task_success", "level": "blocking", "appealable": False, "human": True,
                      "finding": {"code": "failure", "item": "TASK-5", "severity": "high",
                                  "message_zh": "人工裁决判失败（任务未完成）"}}]
    assert not any(f["finding"]["code"] == "uncertain" for f in ep3["findings"])
    assert any(f["finding"]["code"] == "uncertain" for f in _view(world, 3, rev=1)["findings"])


def test_review_items_task_text_and_evidence(world):
    ep5 = _view(world, 5)
    assert [(i["module"], i["kind"]) for i in ep5["review"]] == [
        ("task_success", "task_verdict"), ("skill_profile", "label_conflict")]
    assert ep5["review"][1]["priority"] == "重点"
    assert ep5["task_text"] == {"text": TEXT[5], "source": "原始标注"}
    assert ep5["evidence"] == [{"module": "task_success", "kind": "frame",
                                "path": "details/evidence/task_success/ep000005_0.jpg"}]
    ep2 = _view(world, 2)
    assert [(i["module"], i["kind"]) for i in ep2["review"]] == [("task_success", "reject_appeal")]
    assert _view(world, 8)["review"] == []           # C2 1.4: not a review item any more


def test_unknown_negative_and_skipped_episodes(world):
    body = assert_error(world.get("/episodes/99"), "not_found")
    assert body["error"]["details"] == {"episode_index": 99, "revision": 1}
    assert_error(world.get("/episodes/-1"), "validation_failed")
    assert_error(world.get("/episodes/x"), "validation_failed")
    # D40: an episode left out for missing source files, named by the source manifest when
    # the report does not list them (a report written before contract 1.4)
    report = world.run_dir / "revisions" / "r0001" / "report.json"
    doc = json.loads(report.read_text(encoding="utf-8"))
    (doc.get("integrity") or {}).pop("skipped_episodes", None)
    report.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    manifest = json.loads((world.run_dir / "source_manifest.json").read_text(encoding="utf-8"))
    manifest["skipped_episodes"] = [
        {"episode_index": 9, "missing": ["videos/observation.images.wrist/chunk-000/file-001.mp4"]}]
    (world.run_dir / "source_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    body = assert_error(world.get("/episodes/9"), "not_found")
    assert body["error"]["details"] == {
        "episode_index": 9, "revision": 1, "reason": "source_missing",
        "missing": ["videos/observation.images.wrist/chunk-000/file-001.mp4"]}
    assert "源文件缺失" in body["error"]["message"] and "没有参与质检" in body["error"]["message"]


def test_skipped_episodes_of_the_report_win_and_pass_through(world):
    from .test_report import REPORT

    path = world.run_dir / "revisions" / "r0001" / "report.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["overview"]["counts"]["skipped"] = 1
    doc["integrity"]["skipped_episodes"] = [{"episode_index": 12, "missing": ["data/x.parquet"]}]
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    body = world.get("/report").json()
    assert_schema(REPORT, body)
    assert body["report"]["overview"]["counts"]["skipped"] == 1
    assert body["report"]["integrity"]["skipped_episodes"] == [
        {"episode_index": 12, "missing": ["data/x.parquet"]}]
    assert assert_error(world.get("/episodes/12"), "not_found")["error"]["details"]["missing"] == [
        "data/x.parquet"]
    # the task summary follows (C4 1.4 Summary.skipped) once it is refreshed
    from daemon.results import refresh_summary

    refresh_summary(store_of(world.rt), world.repo, world.task_id)
    task = world.client.get(f"{API}/tasks/{world.task_id}").json()
    assert_schema("Task", task)
    assert task["summary"]["skipped"] == 1 and task["summary"]["total"] == 9
    item = world.client.get(f"{API}/tasks").json()["items"][0]
    assert_schema("TaskListItem", item)
    assert item["summary"]["skipped"] == 1


def test_source_videos_of_a_v3_dataset_carry_their_windows(world):
    ep2 = _view(world, 2)
    assert ep2["videos"] == [
        {"camera": "wrist", "scope": "input", "origin": "source_dataset",
         "path": "videos/observation.images.wrist/chunk-000/file-000.mp4",
         "from_ts": 28.0, "to_ts": 42.0},
        {"camera": "exterior_1", "scope": "input", "origin": "source_dataset",
         "path": "videos/observation.images.exterior_1/chunk-000/file-000.mp4",
         "from_ts": 28.0, "to_ts": 42.0}]
    ep7 = _view(world, 7)
    assert ep7["videos"][0]["path"].endswith("/file-001.mp4") and ep7["videos"][0]["from_ts"] == 28.0


def test_clip_then_delivery_then_source(world):
    rd = world.run_dir
    clip = rd / "details" / "audit_clips" / "ep000004__wrist.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"\0\0\0\x18ftypmp42")
    # a v3 delivered dataset: ep 4 became episode 2 of the export
    lr = rd / "export" / "lerobot_curated"
    info = json.loads((world.dataset / "meta" / "info.json").read_text(encoding="utf-8"))
    (lr / "meta" / "episodes" / "chunk-000").mkdir(parents=True)
    (lr / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
    cols = {"episode_index": [0, 1, 2]}
    for cam in CAMERAS:
        cols[f"videos/{cam}/chunk_index"] = [0, 0, 0]
        cols[f"videos/{cam}/file_index"] = [0, 0, 0]
        cols[f"videos/{cam}/from_timestamp"] = [0.0, 14.0, 28.0]
        cols[f"videos/{cam}/to_timestamp"] = [14.0, 28.0, 42.0]
    pq.write_table(pa.table(cols), lr / "meta" / "episodes" / "chunk-000" / "file-000.parquet")
    manifest = {"schema_version": "1.0", "source_format": "lerobot_v3",
                "fingerprint": "sha256:" + "d" * 64, "meta_files": ["meta/info.json"],
                "episodes": [{"episode_index": 4, "new_index": 2, "content_key": "sha256:" + "e" * 64,
                              "task_key": "sha256:" + "f" * 64,
                              "artifacts": {"parquet": "data/chunk-000/file-000.parquet", "chunk": 0,
                                            "videos": {cam: f"videos/{cam}/chunk-000/file-000.mp4"
                                                       for cam in CAMERAS}}}]}
    (rd / "export" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    videos = _view(world, 4)["videos"]
    assert videos == [
        {"camera": "wrist", "scope": "delivery", "origin": "clip",
         "path": "details/audit_clips/ep000004__wrist.mp4"},
        {"camera": "exterior_1", "scope": "delivery", "origin": "delivery_dataset",
         "path": "export/lerobot_curated/videos/observation.images.exterior_1/chunk-000/file-000.mp4",
         "from_ts": 28.0, "to_ts": 42.0}]
    # an export changing the dataset in place: the delivered copy is not offered meanwhile
    (rd / "export" / "_EXPORTING").write_text("{}", encoding="utf-8")
    videos = _view(world, 4)["videos"]
    assert [v["origin"] for v in videos] == ["clip", "source_dataset"]


def test_mcap_cameras_are_streamed_by_the_daemon(world):
    """An mcap task's cameras have no file to sign: the episode view names them by a
    virtual stream path, and /media/sign resolves that to this Daemon's own URL."""
    path = world.run_dir / "preflight.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["format"]["kind"] = "mcap"
    doc["dataset"]["cameras"] = ["wrist", "exterior_1"]
    path.write_text(json.dumps(doc), encoding="utf-8")
    videos = _view(world, 3)["videos"]
    assert videos == [
        {"camera": "wrist", "scope": "input", "origin": "source_dataset",
         "path": "stream/cameras/ep000003__wrist.mp4"},
        {"camera": "exterior_1", "scope": "input", "origin": "source_dataset",
         "path": "stream/cameras/ep000003__exterior_1.mp4"}]
    r = world.client.get(f"{API}/media/sign", params={"task": world.task_id, "scope": "input",
                                                      "path": videos[0]["path"]})
    assert r.status_code == 200, r.text
    assert r.json()["url"].endswith(f"/api/v1/tasks/{world.task_id}/episodes/3/cameras/wrist.mp4")


def test_clip_source_muxes_a_local_mcap_episode_in_memory(tmp_path):
    pytest.importorskip("mcap")
    pytest.importorskip("mcap_ros2")
    from contextlib import contextmanager
    from types import SimpleNamespace

    from curation.cli.storage import LocalStorage
    from parity.fixtures import make_mini_mcap
    from daemon.results import clips
    from daemon.results.store import ResultStore

    ds = make_mini_mcap(str(tmp_path / "mini_mcap"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "preflight.json").write_text(json.dumps(
        {"format": {"kind": "mcap"}, "dataset": {"cameras": ["wrist", "exterior"]}}), encoding="utf-8")
    objects = [{"key": f"episode_{i}.mcap", "size": os.path.getsize(os.path.join(ds, f"episode_{i}.mcap")),
                "etag": None} for i in range(8)]
    (run_dir / "source_manifest.json").write_text(json.dumps(
        {"input": ds, "objects": objects, "summary": {"digest": "sha256:" + "a" * 64}}), encoding="utf-8")

    @contextmanager
    def open_input(task):
        yield LocalStorage(ds)

    store = ResultStore(tmp_path, open_input=open_input)
    src = clips.ClipSource(store, SimpleNamespace(id="t-mcap"), run_dir)
    assert clips.cameras_of(run_dir, store.docs, 2) == {
        "wrist": "stream/cameras/ep000002__wrist.mp4",
        "exterior": "stream/cameras/ep000002__exterior.mp4"}
    data = src.clip(2, "wrist")
    assert data and data[4:8] == b"ftyp"
    assert src.clip(2, "exterior")[4:8] == b"ftyp" and src.clip(2, "nope") is None
    assert store.clips.get(("clips", "t-mcap", "sha256:" + "a" * 64, 2)) is not None   # one read, both cameras
    status, body, headers = clips.slice_range(data, "bytes=0-1023")
    assert status == 206 and len(body) == 1024 and headers["Content-Range"] == f"bytes 0-1023/{len(data)}"
    status, body, _ = clips.slice_range(data, "bytes=-100")
    assert status == 206 and body == data[-100:]
    assert clips.slice_range(data, None)[0] == 200 and clips.slice_range(data, f"bytes={len(data)}-")[0] == 416
    assert clips.parse_stream_path("stream/cameras/ep000012__cam_a.mp4") == (12, "cam_a")
    assert clips.parse_stream_path("details/audit_clips/ep000012__cam_a.mp4") is None


def test_v2_delivery_and_v2_source(client_for, tmp_path):
    c = client_for(base_path="/curation")
    rt = c.app.state.runtime
    ds = make_dataset(tmp_path / "v2", version="v2")
    task = make_task(rt.repo, ds)
    finish_main_run(rt.repo, task.id)
    run_dir = rt.settings.work_dir / task.id
    build_run_dir(run_dir, ds)
    w = World(client=c, rt=rt, task_id=task.id, run_dir=run_dir, dataset=ds, clock=None)
    w.revision(1)
    w.switch(1)
    manifest = {"schema_version": "1.0", "source_format": "lerobot_v2",
                "fingerprint": "sha256:" + "d" * 64, "meta_files": ["meta/info.json"],
                "episodes": [{"episode_index": 0, "new_index": 0, "content_key": "sha256:" + "e" * 64,
                              "task_key": "sha256:" + "f" * 64,
                              "artifacts": {"parquet": "data/chunk-000/episode_000000.parquet",
                                            "videos": {CAMERAS[0]: "videos/chunk-000/"
                                                       f"{CAMERAS[0]}/episode_000000.mp4"}}}]}
    (run_dir / "export").mkdir()
    (run_dir / "export" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    videos = _view(w, 0)["videos"]
    assert videos == [
        {"camera": "wrist", "scope": "delivery", "origin": "delivery_dataset",
         "path": f"export/lerobot_curated/videos/chunk-000/{CAMERAS[0]}/episode_000000.mp4"},
        {"camera": "exterior_1", "scope": "input", "origin": "source_dataset",
         "path": f"videos/chunk-000/{CAMERAS[1]}/episode_000000.mp4"}]


def test_changed_or_unreadable_source_metadata_leaves_the_source_out(world):
    info = world.dataset / "meta" / "info.json"
    info.write_text(info.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")   # size changed
    assert _view(world, 2)["videos"] == []
    calls = []
    store = store_of(world.rt)
    store.sources.clear()

    def broken(task):
        calls.append(task.id)
        raise OSError("TOS unreachable")

    store.open_input = broken
    assert _view(world, 3)["videos"] == []
    assert _view(world, 5)["videos"] == []
    assert calls == [world.task_id]                     # not retried for a minute


def test_source_on_tos_is_read_with_the_tasks_input_key(client_for, tmp_path):
    from ..secrets.conftest import add_access_key
    from ..secrets.fakes import AK, SK, FakeTos
    from daemon.secrets import service_of

    tos = FakeTos()
    tos.add_key(AK, SK, read={"datasets"}, write=set())
    ds = make_dataset(tmp_path / "local-copy")
    for path in sorted(ds.rglob("*")):
        if path.is_file():
            tos.put("datasets", f"droid_9/{path.relative_to(ds).as_posix()}", path.read_bytes())
    c = client_for(base_path="/curation")
    rt = c.app.state.runtime
    service_of(rt).tos_factory = tos.factory
    cred = add_access_key(c)
    from curation.contracts import modules as registry
    from daemon.repo import protocol as P

    rows = [P.TaskModule(task_id="", module_id=m, selected=True, availability="available")
            for m in registry.ids()]
    task = rt.repo.create_task(P.TaskCreate(
        name="tos", input_source="tos", input_uri="tos://datasets/droid_9", input_region="cn-beijing",
        input_cred_id=cred["id"], output_uri="tos://deliveries/x", delivery_key="tos://deliveries/x",
        episode_selector={"mode": "all"}, params={}, modules=rows))
    finish_main_run(rt.repo, task.id)
    run_dir = rt.settings.work_dir / task.id
    build_run_dir(run_dir, ds)
    manifest = source_manifest(ds)
    manifest["input"] = "tos://datasets/droid_9"
    (run_dir / "source_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    w = World(client=c, rt=rt, task_id=task.id, run_dir=run_dir, dataset=ds, clock=None)
    w.revision(1)
    w.switch(1)
    videos = _view(w, 3)["videos"]
    assert [(v["camera"], v["scope"], v["from_ts"]) for v in videos] == [
        ("wrist", "input", 42.0), ("exterior_1", "input", 42.0)]
    reads = [op for op in tos.ops("get_object") if op["ak"] == AK]
    assert {op["key"] for op in reads} == {"droid_9/meta/info.json",
                                           "droid_9/meta/episodes/chunk-000/file-000.parquet"}
    assert tos.opened == tos.closed                     # every client closed again


def test_records_are_the_revision_s_parts_not_the_latest(world):
    """r1 keeps what r1 saw after a later part replaced a record (commit.json parts)."""
    later = world.run_dir / "checks" / "task_success" / "parts" / "0002.jsonl"
    rec = record("task_success", 0, "pass", details={"verdict": "success", "task_desc": "re-judged",
                                                     "task_desc_source": "人工改标"})
    later.write_text(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    world.decide((3, "task_verdict", "success"))
    sub = world.start_subtask(at=T0 + 10 * 60_000)
    world.apply(sub)
    world.revision(2, subtask_id=sub.id)
    world.switch(2)
    assert _view(world, 0)["modules"]["task_success"]["details"]["task_desc"] == "re-judged"
    old = _view(world, 0, rev=1)
    assert old["revision"] == 1
    assert old["modules"]["task_success"]["details"]["task_desc"] == TEXT[0]
    machine = _view(world, 3)["modules"]["task_success"]                         # machine record
    assert [f["code"] for f in machine["findings"]] == ["uncertain"]
    assert _view(world, 3)["list"] == "passed"


def test_an_index_fooled_by_a_nested_key_still_finds_the_record(world):
    """Records written without sorted keys can nest another episode_index after the real one."""
    path = world.run_dir / "checks" / "dedup" / "parts" / "0001.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    tricky = record("dedup", 5, "pass", details={})
    tricky["details"] = {"pair": {"episode_index": 3}}
    text = json.dumps({k: tricky[k] for k in ("episode_index", "module", "status", "findings", "assessed",
                                              "unassessable", "readings", "evidence", "elapsed_s", "error",
                                              "details")})
    path.write_text("\n".join([ln for ln in lines if '"episode_index": 5' not in ln] + [text]) + "\n",
                    encoding="utf-8")
    store_of(world.rt).derived.clear()
    assert _view(world, 5)["modules"]["dedup"]["details"] == {"pair": {"episode_index": 3}}
    assert _view(world, 3)["modules"]["dedup"]["details"] == {}


@pytest.mark.parametrize("rev", [None, 1])
def test_episode_endpoint_validates_for_every_list(world, rev):
    for ep in EPISODES:
        q = {} if rev is None else {"rev": rev}
        _view(world, ep, **q)
