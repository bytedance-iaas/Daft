"""Fixtures for the W5b tests: hand-made run directories, revisions made by the real CLI.

Like ``tests/cli/test_aggregate.py``, the module records are written by hand as part
files (the ``check`` record format); ``curation aggregate --phase final`` and
``curation report`` then run in process - the real W3 commands, every ``--json``
checked against C2 - so the readers are tested against the files the CLI writes.

The story of the main task (nine episodes, all eight modules):

====  =====================================================  ==========================
ep    what happens                                            where it ends (r1)
====  =====================================================  ==========================
0     every check passes                                      passed
1     timestamp fragment (hard gate, numeric stage)           reject - final
2     task_success judges it failed                           reject - appealable
3     task_success abstains                                   passed + review (verdict)
4     label conflict (skill_profile's audit)                  passed + review (label)
5     label conflict and a task_success abstention            passed + review (both)
6     task_success execution error                            held
7     byte-for-byte duplicate of ep 0                         reject - appealable (D42)
8     motion_quality cannot score it                          passed (no review item, C2 1.4)
====  =====================================================  ==========================
"""
from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest

from curation.contracts import modules as registry
from daemon.repo import protocol as P

from ..daemon.conftest import (  # noqa: F401 - fixtures are registered by importing them
    T0,
    FakeClock,
    assert_error,
    assert_schema,
    clean_env,
    client_for,
    clock,
    make_app,
    make_settings,
)

API = "/curation/api/v1"
JSON = {"Content-Type": "application/json"}
MODULES = tuple(m.id for m in registry.MODULES if m.affects_dataset_verdict and m.id not in registry.native_ids())   # v1's eight
GATE = {m.id: m.gate for m in registry.MODULES}
CAMERAS = ("observation.images.wrist", "observation.images.exterior_1")
SHORT = ("wrist", "exterior_1")
EPISODES = range(9)
MIN = 60_000
TEXT = {ep: f"put block {ep} in the bin" for ep in EPISODES}
CAPTION = {4: "wipe the table with the cloth", 5: "open the drawer"}


def record(module: str, ep: int, verdict: str, *, score=None, details=None, evidence=(),
           incidents=None) -> dict:
    """One C2 result record, as ``check`` writes it."""
    passed = {"pass": True, "fail": False}.get(verdict)
    error = None
    if verdict == "error":
        error = {"kind": "execution",
                 "incidents": incidents or [{"step": "arbitration", "call_kind": "arbitration",
                                             "cause": "timeout 60s", "attempts": 3}]}
    return {"episode_index": ep, "module": module, "verdict": verdict, "passed": passed,
            "score": score, "gate": GATE[module], "details": details or {},
            "evidence": list(evidence), "elapsed_s": 0.25, "error": error}


class RunDir:
    """A run directory: module records as part files, plus the JSON files around them."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.parts: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))

    def put(self, module: str, ep: int, verdict: str, part: str = "0001", **kw) -> RunDir:
        self.parts[module][part].append(record(module, ep, verdict, **kw))
        return self

    def write_parts(self) -> None:
        for module, parts in self.parts.items():
            d = self.path / "checks" / module / "parts"
            d.mkdir(parents=True, exist_ok=True)
            for part, recs in parts.items():
                (d / f"{part}.jsonl").write_text(
                    "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in recs),
                    encoding="utf-8")

    def write_json(self, rel: str, doc) -> Path:
        path = self.path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path


def cli(*argv):
    from ..cli.pipeline import run

    res = run(*argv)
    assert res.rc == 0, (argv, res.doc, res.err[-2000:])
    return res.doc


def task_details(ep: int, verdict: str) -> dict:
    d = {"verdict": {"pass": "success", "fail": "failure"}.get(verdict, "review_conflict"),
         "task_desc": TEXT[ep], "task_desc_source": "原始标注", "rules": ["probe"]}
    if verdict == "abstain":
        d["reason"] = "两层证据矛盾，进人工"
    if verdict == "fail":
        d["reason"] = "3 路复核一致判未完成"
    return d


def story(rd: RunDir) -> None:
    """The records of the table in the module docstring."""
    for ep in EPISODES:
        rd.put("timestamp_check", ep, "fail" if ep == 1 else "pass",
               details={"duration_s": 0.5 if ep == 1 else 12.0 + ep, "max_dt": 0.07,
                        "reason": "fragment" if ep == 1 else ""})
        rd.put("kinematic_limits", ep, "pass",
               details={"violations": [{"type": "velocity", "joint": 3, "frame": 12,
                                        "value": 2.5, "limit": 2.0}]} if ep == 0 else {})
        if ep == 8:
            rd.put("motion_quality", ep, "abstain", details={"reason": "too short to score"})
        else:
            rd.put("motion_quality", ep, "scored", score=round(0.70 + 0.03 * ep, 2),
                   details={"fluency": 0.8, "active_ratio": 0.9, "stuck_joints": []})
        if ep == 1:
            continue                                   # killed in the numeric stage
        rd.put("visual_quality", ep, "scored", score=round(0.6 + 0.04 * ep, 2), details={
            "per_camera_detail": {cam: {"score": None if (ep, k) == (3, 1) else
                                        round(0.5 + 0.05 * ep + 0.01 * k, 3),
                                        "sharpness": 100 + ep, "exposure": 0.5, "integrity": 1.0}
                                  for k, cam in enumerate(CAMERAS)}})
        rd.put("video_action_sync", ep, "pass", details={
            "per_camera": {cam: {"lag_s": 0.01 * ep, "corr_peak": 0.8, "code": "aligned"}
                           for cam in CAMERAS}})
        verdict = {2: "fail", 3: "abstain", 5: "abstain", 6: "error"}.get(ep, "pass")
        evidence = [f"details/evidence/task_success/ep{ep:06d}_0.jpg"] \
            if verdict in ("fail", "abstain") else []
        rd.put("task_success", ep, verdict, details=task_details(ep, verdict)
               if verdict != "error" else {}, evidence=evidence)
    for ep in (0, 3, 4, 5, 7, 8):                      # the keep set of the funnel
        if ep == 7:
            rd.put("dedup", ep, "fail", details={"duplicate_of": 0})
        else:
            rd.put("dedup", ep, "pass", details={})
    for ep in (0, 3, 4, 5, 8):                         # what dedup kept
        rd.put("skill_profile", ep, "pass",
               details={"family": "放置", "subskill": "放进容器", "caption": CAPTION.get(ep, TEXT[ep]),
                        "grouping_text": TEXT[ep], "grouping_text_source": "原始标注"})
    rd.write_json("checks/skill_profile/profile.json",
                  {"families": {"放置": {"subskills": {"放进容器": {}}}}, "undersampled": []})
    rd.write_json("checks/skill_profile/label_audit.json", {"high": [
        {"id": f"ep{ep:06d}", "label": TEXT[ep], "caption": CAPTION[ep],
         "reason": "分歧(文本对判官):描述的不是同一任务——自产描述由 VLM 生成,需人工判定"}
        for ep in (4, 5)], "mid_for_review": []})


def preflight_doc() -> dict:
    return {"schema_version": "1.0",
            "format": {"kind": "lerobot", "version": "v3", "supported": True, "detail": "LeRobot v3.0"},
            "validation": [],
            "dataset": {"episode_count": 9, "cameras": list(SHORT), "fps": 15.0,
                        "robot_type": "franka", "total_frames": 9 * 200,
                        "labels": {"with_task": 9, "without_task": 0}, "profile": None},
            "modules": [{"id": m, "availability": "available"} for m in MODULES],
            "meta_fingerprint": "sha256:" + "a" * 64, "warnings": []}


def make_dataset(root: Path, version: str = "v3") -> Path:
    """A LeRobot dataset with metadata only (no data or video bytes): what the readers open."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    root.mkdir(parents=True, exist_ok=True)
    features = {cam: {"dtype": "video", "shape": [224, 224, 3]} for cam in CAMERAS}
    features["observation.state"] = {"dtype": "float32", "shape": [7]}
    if version == "v2":
        info = {"codebase_version": "v2.1", "chunks_size": 1000, "fps": 15,
                "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
                "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/"
                              "episode_{episode_index:06d}.mp4", "features": features}
    else:
        info = {"codebase_version": "v3.0", "chunks_size": 1000, "fps": 15,
                "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
                "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
                "features": features}
        cols: dict[str, list] = {"episode_index": list(EPISODES), "length": [200] * 9,
                                 "data/chunk_index": [0] * 9, "data/file_index": [0] * 9}
        for cam in CAMERAS:
            cols[f"videos/{cam}/chunk_index"] = [0] * 9
            cols[f"videos/{cam}/file_index"] = [ep // 5 for ep in EPISODES]
            cols[f"videos/{cam}/from_timestamp"] = [float((ep % 5) * 14) for ep in EPISODES]
            cols[f"videos/{cam}/to_timestamp"] = [float((ep % 5) * 14 + 14) for ep in EPISODES]
        path = root / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(cols), path)
    (root / "meta").mkdir(parents=True, exist_ok=True)
    (root / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
    return root


def source_manifest(root: Path) -> dict:
    objects = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            st = path.stat()
            objects.append({"key": path.relative_to(root).as_posix(), "size": st.st_size,
                            "mtime_ns": st.st_mtime_ns})
    return {"schema_version": "1.0", "input": str(root), "objects": objects,
            "summary": {"count": len(objects), "bytes": sum(o["size"] for o in objects),
                        "digest": "sha256:" + "c" * 64}}


def latency_csv(rows: list[tuple]) -> str:
    """``details/vlm_latency.csv`` rows: (kind, seconds, ok, started_at_s, call_id, attempt, fail)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["call_type", "seconds", "ok", "started_at", "call_id", "attempt", "fail_kind"])
    for kind, secs, ok, started, cid, attempt, fail in rows:
        w.writerow([kind, secs, int(ok), started, cid, attempt, fail])
    return buf.getvalue()


def main_latency() -> list[tuple]:
    t = T0 / 1000.0
    return [("probe", 2.0, True, t + 60, "c1", 0, ""), ("probe", 4.0, True, t + 61, "c2", 0, ""),
            ("probe", 60.0, False, t + 62, "c3", 0, "timeout"),
            ("probe", 3.0, True, t + 92, "c3", 1, ""),
            ("endstate", 5.0, True, t + 120, "c4", 0, ""),
            ("arbitration", 60.0, False, t + 200, "c5", 0, "timeout")]


#: the modules whose rejects can be appealed (C2 1.4; dedup since D42), and the reason kind
APPEALABLE = {"task_success": "hard_gate", "dedup": "duplicate"}


def review_as_of_c2_1_4(run_dir: Path, n: int) -> None:
    """Make revision ``n``'s ``review.json`` what the C2 1.4 CLI writes (``final-list``, and
    D42): ``task_verdict`` items only for task_success abstentions of episodes that are not
    rejected, and a ``reject_appeal`` item for every reject attributed to one appealable
    module alone (task_success, or dedup since D42) that has no effective appeal and was
    not discarded (an ``unsure`` appeal keeps it). W3's fix for this is on its way; once
    it lands this is a no-op. Runs before ``report``, so the commit covers it."""
    rev = run_dir / "revisions" / f"r{n:04d}"
    review = json.loads((rev / "review.json").read_text(encoding="utf-8"))
    reject = json.loads((rev / "reject.json").read_text(encoding="utf-8"))
    effective: dict[int, str] = {}
    applied = run_dir / "adjudication" / "applied.jsonl"
    if applied.is_file():
        for line in sorted((json.loads(x) for x in applied.read_text(encoding="utf-8").splitlines()
                            if x.strip()), key=lambda d: d["id"]):
            if line["line"] == "reject_appeal" and line["decision"] != "unsure":
                effective[int(line["episode_index"])] = line["decision"]
    rejected = {e["episode_index"] for e in reject["episodes"]}
    line_of = {"label_conflict": "label"}                       # C2 1.5: items name their line
    by_ep: dict[int, dict] = {}
    for entry in review["episodes"]:
        items = [{**i, "line": i.get("line") or line_of.get(i["kind"], i["kind"])}
                 for i in entry["review"]
                 if not (i["kind"] == "task_verdict" and (i["source_module"] != "task_success"
                                                          or entry["episode_index"] in rejected))]
        if items:
            by_ep[entry["episode_index"]] = {**entry, "review": items}
    for entry in reject["episodes"]:
        ep = entry["episode_index"]
        deciding = [r for r in entry.get("reasons") or [] if r.get("kind") != "execution_error"]
        modules = {r["module"] for r in deciding}
        if len(modules) != 1 or ep in effective:
            continue
        module = modules.pop()
        if APPEALABLE.get(module) and all(r.get("kind") == APPEALABLE[module] for r in deciding):
            item = by_ep.setdefault(ep, {"episode_index": ep, "review": [], "current_list": "reject"})
            if not any(i["kind"] == "reject_appeal" for i in item["review"]):
                appeal = {"source_module": module, "kind": "reject_appeal",
                          "line": "reject_appeal", "reason": "被拒的条目可以复议"}
                dup = next((r["duplicate_of"] for r in deciding if "duplicate_of" in r), None)
                if dup is not None:
                    appeal["duplicate_of"] = dup
                item["review"].append(appeal)
    review["episodes"] = [by_ep[ep] for ep in sorted(by_ep)]
    review["count"] = len(review["episodes"])
    (rev / "review.json").write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")


@dataclass
class World:
    client: object
    rt: object
    task_id: str
    run_dir: Path
    dataset: Path
    clock: FakeClock

    @property
    def repo(self) -> P.Repository:
        return self.rt.repo

    @property
    def rd(self) -> RunDir:
        return RunDir(self.run_dir)

    def task(self) -> P.Task:
        return self.repo.get_task(self.task_id)

    def get(self, rel: str, **params):
        return self.client.get(f"{API}/tasks/{self.task_id}{rel}", params=params)

    def decide(self, *items, headers=None):
        decisions = []
        for ep, line, decision, *rest in items:
            d = {"episode_index": ep, "line": line, "decision": decision}
            if rest and rest[0] is not None:
                d["new_label"] = rest[0]
            decisions.append(d)
        return self.client.post(f"{API}/tasks/{self.task_id}/adjudication",
                                json={"decisions": decisions}, headers={**JSON, **(headers or {})})

    def revision(self, n: int, *, subtask_id: str | None = None, modules: tuple = MODULES) -> None:
        rd = str(self.run_dir)
        cli("aggregate", "--run-dir", rd, "--phase", "final", "--revision", str(n),
            "--modules", ",".join(modules), "--episodes", "0-8")
        review_as_of_c2_1_4(self.run_dir, n)
        argv = ["report", "--run-dir", rd, "--revision", str(n), "--modules", ",".join(modules)]
        if subtask_id:
            argv += ["--subtask-id", subtask_id]
        cli(*argv)

    def switch(self, n: int) -> None:
        from daemon.results import refresh_summary, store_of

        assert self.repo.switch_result_rev(self.task_id, n - 1, n)
        refresh_summary(store_of(self.rt), self.repo, self.task_id)

    def start_subtask(self, kind: str = "apply_adjudication", *, at: int) -> P.Subtask:
        sub = self.repo.create_subtask(P.Subtask(id="", task_id=self.task_id, kind=kind,
                                                 scope={}, state="queued"))
        assert self.repo.update_subtask_state(sub.id, {"queued"}, "running", at=at)
        return self.repo.get_subtask(sub.id)

    def finish_subtask(self, sub: P.Subtask, *, at: int) -> None:
        assert self.repo.update_subtask_state(sub.id, {"running"}, "succeeded", at=at)

    def apply(self, sub: P.Subtask) -> dict:
        """What the apply subtask does first: the decisions that still stand -> the CLI."""
        from daemon.results import Queue, store_of

        rows = Queue(store_of(self.rt), self.repo, self.task()).executable()
        doc = {"schema_version": "1.0", "decisions": [
            {"id": a.id, "episode_index": a.episode_index, "line": a.line, "decision": a.decision,
             "new_label": a.new_label, "note": a.note, "decided_by": a.decided_by,
             "decided_at": a.decided_at} for a in rows]}
        path = self.run_dir.parent / f"decisions-{sub.id}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        out = cli("adjudicate-apply", "--run-dir", str(self.run_dir), "--decisions", str(path))
        self.repo.mark_adjudications_applied([a.id for a in rows], sub.id)
        # stands in for ``check --modules skill_profile --incremental``: re-filed episodes
        parts = self.run_dir / "checks" / "skill_profile" / "parts"
        part = f"{len(list(parts.glob('*.jsonl'))) + 1:04d}"
        with open(parts / f"{part}.jsonl", "w", encoding="utf-8") as fh:
            for ep in out["profile_resync"]:
                fh.write(json.dumps(record("skill_profile", ep, "pass", details={
                    "family": "放置", "subskill": "放进容器", "caption": TEXT[ep]}),
                    ensure_ascii=False, sort_keys=True) + "\n")
        return out


def make_task(repo: P.Repository, dataset: Path, *, owner: str = P.DEFAULT_OWNER,
              name: str = "droid 抽检") -> P.Task:
    rows = [P.TaskModule(task_id="", module_id=m, selected=True, availability="available")
            for m in MODULES]
    return repo.create_task(P.TaskCreate(
        name=name, input_source="local", input_uri=str(dataset),
        output_uri="tos://deliveries/droid-9", delivery_key="tos://deliveries/droid-9",
        episode_selector={"mode": "all"}, params={"export": True}, modules=rows,
        owner_id=owner, output_region="cn-beijing"))


def finish_main_run(repo: P.Repository, task_id: str) -> None:
    repo.freeze_task_inputs(task_id, run_id="20250919-164000", preflight=preflight_doc(),
                            source_fingerprint={"objects": 3, "bytes": 1000,
                                                "digest": "sha256:" + "c" * 64},
                            vlm_snapshot={"backend_id": "vb_1", "backend": "ark-prod", "kind": "ark",
                                          "endpoint": "https://ark.example/api/v3", "model_id": "vm_1",
                                          "model": "doubao-seed", "reasoning_effort": None,
                                          "max_concurrency": 64})
    assert repo.update_task_state(task_id, {"queued"}, "running", at=T0)
    repo.set_task_progress(task_id, {"stages": [
        {"id": "numeric", "state": "succeeded", "done": 9, "total": 9, "elapsed_s": 10.0},
        {"id": "frame", "state": "succeeded", "done": 8, "total": 8, "elapsed_s": 30.0},
        {"id": "vlm", "state": "completed_with_errors", "done": 8, "total": 8, "elapsed_s": 60.0}]})
    assert repo.update_task_state(task_id, {"running"}, "completed_with_errors", at=T0 + 5 * MIN)


def build_run_dir(run_dir: Path, dataset: Path) -> RunDir:
    rd = RunDir(run_dir)
    story(rd)
    rd.write_parts()
    rd.write_json("preflight.json", preflight_doc())
    rd.write_json("source_manifest.json", source_manifest(dataset))
    (run_dir / "details").mkdir(parents=True, exist_ok=True)
    (run_dir / "details" / "vlm_latency.csv").write_text(latency_csv(main_latency()),
                                                         encoding="utf-8")
    return rd


@pytest.fixture
def world(client_for, tmp_path, clock) -> World:
    """The main task after its main run: revision 1 committed and in force."""
    c = client_for(base_path="/curation")
    rt = c.app.state.runtime
    dataset = make_dataset(tmp_path / "datasets" / "droid_9")
    task = make_task(rt.repo, dataset)
    finish_main_run(rt.repo, task.id)
    run_dir = Path(rt.settings.work_dir) / task.id
    build_run_dir(run_dir, dataset)
    w = World(client=c, rt=rt, task_id=task.id, run_dir=run_dir, dataset=dataset, clock=clock)
    w.revision(1)
    w.switch(1)
    return w


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def all_pages(get, *, limit: int, **params) -> tuple[list[dict], list[dict]]:
    """Every page of a cursor listing: (items, page bodies)."""
    items, bodies, cursor = [], [], None
    for _ in range(1000):
        q = dict(params, limit=limit)
        if cursor:
            q["cursor"] = cursor
        r = get(**q)
        assert r.status_code == 200, r.text
        body = r.json()
        bodies.append(body)
        items += body["items"]
        if not body["has_more"]:
            assert body["next_cursor"] is None
            return items, bodies
        cursor = body["next_cursor"]
        assert cursor
    raise AssertionError("paging never ended")
