"""Pure parts of the orchestration: rules, the delivery directory, settings, admission."""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from daemon.orchestr import delivery as D
from daemon.orchestr import rules
from daemon.orchestr.config import OrchestratorConfig, OrchestratorConfigError, load_site_config
from daemon.repo import protocol as P

from ..secrets.fakes import AK2, SK2
from .conftest import ListingTos


def _module(mid, state, selected=True, errors=0):
    return P.TaskModule(task_id="t", module_id=mid, selected=selected, availability="available",
                        state=state, episodes_error=errors)


def test_the_terminal_state_rule():
    ok = [_module("timestamp_check", "succeeded"), _module("task_success", "completed_with_errors",
                                                           errors=2)]
    assert rules.terminal_state(ok, held=0) == "succeeded"          # D35: errors all rejected
    assert rules.terminal_state(ok, held=1) == "completed_with_errors"
    failed = ok + [_module("skill_profile", "failed")]
    assert rules.terminal_state(failed, held=0) == "completed_with_errors"
    unselected = ok + [_module("dedup", "failed", selected=False)]
    assert rules.terminal_state(unselected, held=0) == "succeeded"
    assert rules.failed_modules(failed) == ["skill_profile"]


def test_episode_selections():
    assert rules.selected_episodes({"mode": "all"}, 4) == [0, 1, 2, 3]
    assert rules.selected_episodes({"mode": "head", "n": 10}, 4) == [0, 1, 2, 3]
    assert rules.selected_episodes({"mode": "explicit", "expr": "1,9", "indices": [1, 9]}, 4) == [1]
    assert rules.max_episodes({"mode": "head", "n": 50}) == 50
    assert rules.max_episodes({"mode": "all"}) is None


def test_the_fallback_summary_of_a_revision(tmp_path):
    def write(name, count, extra=None):
        (tmp_path / f"{name}.json").write_text(json.dumps(
            {"list": name, "count": count, "episodes": extra or []}))
    write("passed", 5)
    write("reject", 2)
    write("held", 1)
    write("review", 1, [{"episode_index": 1, "review": [{"kind": "task_verdict"}]}])
    out = rules.summary(tmp_path)
    assert out == {"total": 8, "passed": 5, "rejected": 2, "held": 1, "review": 1,
                   "pass_rate": 0.625}               # the queue is counted by W5b's readers only
    (tmp_path / "report.json").write_text(json.dumps({"overview": {"counts": {"skipped": 2}}}))
    assert rules.summary(tmp_path)["skipped"] == 2               # D40, not part of total


def test_listing_digest_matches_the_cli(tmp_path):
    from curation.cli import source_manifest
    from curation.cli.storage import ObjectInfo

    objs = [ObjectInfo("meta/info.json", 10, etag='"abc"'),
            ObjectInfo("data/chunk-000/episode_000000.parquet", 99, etag='"def"'),
            ObjectInfo("meta/episodes.jsonl", 7, etag='"x"')]
    doc = source_manifest.build("tos://b/d", objs)
    assert rules.listing_digest(doc["objects"]) == doc["summary"]["digest"]
    from curation.cli.lerobot_meta import fingerprint

    assert rules.meta_fingerprint(doc) == fingerprint([o for o in objs
                                                       if o.key.startswith("meta/")])
    assert rules.fingerprint_of(doc) == {"objects": 3, "bytes": 116,
                                         "digest": doc["summary"]["digest"]}


def test_source_change_between_two_listings():
    old = {"objects": [{"key": "meta/info.json", "size": 1, "etag": "a"},
                       {"key": "data/a.parquet", "size": 5, "etag": "b"},
                       {"key": "data/gone.parquet", "size": 5, "etag": "c"}]}
    new = {"objects": [{"key": "meta/info.json", "size": 1, "etag": "a"},
                       {"key": "data/a.parquet", "size": 6, "etag": "b2"},
                       {"key": "data/new.parquet", "size": 5, "etag": "d"}]}
    change = rules.source_change(old, new, meta_changed=False, preflighted_at=7)
    assert change == {"meta_changed": False, "added": 1, "removed": 1, "modified": 1,
                      "sample_keys": ["data/new.parquet", "data/a.parquet", "data/gone.parquet"],
                      "preflighted_at": 7}
    blind = rules.source_change(None, new, meta_changed=True, preflighted_at=7,
                                old_fingerprint={"objects": 2})
    assert (blind["added"], blind["removed"], blind["sample_keys"]) == (1, 0, [])


def test_run_ids_are_the_site_time():
    t = 1758300000000                                            # 2025-09-19T16:40:00Z
    assert rules.run_id_for(t, 8 * 60) == "20250920-004000"
    assert rules.run_id_for(t, 0) == "20250919-164000"


# ---------------------------------------------------------------- the delivery directory

def test_local_delivery_basics(tmp_path):
    d = D.LocalDelivery(tmp_path / "deliveries" / "x", "tos://deliveries/x")
    assert d.cli_uri("r1").endswith("deliveries/x/r1") and not d.exists("r1")
    d.put_bytes("r1/a.json", b"{}")
    src = tmp_path / "f.bin"
    src.write_bytes(b"12345")
    d.put_file("r1/sub/f.bin", str(src))
    assert d.exists("r1") and d.list("r1") == {"r1/a.json": 2, "r1/sub/f.bin": 5}
    assert d.get_bytes("r1/a.json") == b"{}" and d.get_bytes("nope") is None
    d.get_file("r1/sub/f.bin", tmp_path / "back" / "f.bin")
    assert (tmp_path / "back" / "f.bin").read_bytes() == b"12345"
    with pytest.raises(D.DeliveryError):
        d.get_file("r1/none", tmp_path / "back" / "none")
    assert sorted(os.listdir(tmp_path / "back")) == ["f.bin"]            # no partial file
    d.delete("r1/sub/f.bin")
    assert not (tmp_path / "deliveries" / "x" / "r1" / "sub").exists()   # empty dirs tidied
    with pytest.raises(D.DeliveryError):
        d.put_bytes("../../escape", b"x")


def test_tos_delivery_through_the_client(tmp_path):
    tos = ListingTos()
    tos.add_key(AK2, SK2, read={"deliveries"}, write={"deliveries"})
    from daemon.secrets.tos import TosKey

    client = tos.factory("https://tos", "cn-beijing", TosKey(AK2, SK2))
    d = D.TosDelivery(client, "tos://deliveries/droid")
    assert not d.exists("r1")
    src = tmp_path / "f.txt"
    src.write_text("hello")
    d.put_file("r1/report.md", str(src))
    d.put_bytes("latest", b"r1\n")
    assert d.exists("r1") and d.list("r1") == {"r1/report.md": 5}
    assert D.read_latest(d) == "r1" and d.get_bytes("r1/none") is None
    d.get_file("r1/report.md", tmp_path / "back" / "report.md")
    assert (tmp_path / "back" / "report.md").read_text() == "hello"
    with pytest.raises(D.DeliveryError):
        d.get_file("r1/none", tmp_path / "back" / "none")
    assert os.listdir(tmp_path / "back") == ["report.md"]
    d.delete("r1/report.md")
    assert d.list("r1") == {}
    tos.pairs[AK2] = "revoked"
    with pytest.raises(D.DeliveryError):
        d.put_bytes("x", b"y")


def test_run_ids_never_collide(tmp_path):
    d = D.LocalDelivery(tmp_path, "tos://b/x")
    d.put_bytes("20260921-120000/run.json", b"{}")
    assert D.allocate_run_id(d, "20260921-120000") == "20260921-120000-2"
    assert D.allocate_run_id(d, "20260921-120000",
                             taken=lambda n: n.endswith("-2")) == "20260921-120000-3"


def test_the_sync_sends_what_changed_and_never_work_in_progress(tmp_path):
    root = tmp_path / "run"
    files = {"plan.json": "{}", "checks/a/parts/0001.jsonl": "{}\n",
             "checks/a/inflight.json": "{}", "export/lerobot_curated/data/x.parquet": "p",
             "export/manifest.json": "{}", ".orchestr/main.json": "{}", "_COMPLETE": "",
             "logs/numeric.jsonl": "{}\n", "report.md.tmp-1": "x"}
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    d = D.LocalDelivery(tmp_path / "tos", "tos://b/x")
    state = tmp_path / "sync.json"
    assert D.sync_run_dir(d, "r1", root, state) == {"uploaded": 4}
    assert sorted(d.list("r1")) == ["r1/checks/a/parts/0001.jsonl", "r1/export/manifest.json",
                                    "r1/logs/numeric.jsonl", "r1/plan.json"]
    assert D.sync_run_dir(d, "r1", root, state) == {"uploaded": 0}
    (root / "plan.json").write_text('{"changed": 1}')
    os.utime(root / "plan.json", ns=(1, 10**18))
    assert D.sync_run_dir(d, "r1", root, state) == {"uploaded": 1}
    D.forget_sync(state)
    assert D.sync_run_dir(d, "r1", root, state) == {"uploaded": 4}
    assert D.sync_run_dir(d, "r2", root, state) == {"uploaded": 4}   # another batch: from zero
    D.forget_sync(state)
    D.mark_synced(state, "r2", root, ["plan.json", "logs/numeric.jsonl", "gone.json"])
    assert D.sync_run_dir(d, "r2", root, state) == {"uploaded": 2}   # fetched ones stay put


def test_what_a_restore_brings_back():
    for rel in ("plan.json", "revisions/r0001/commit.json", "checks/dedup/groups.json",
                "logs/numeric.jsonl", "human-decisions/task_verdicts.csv",
                "details/vlm_latency.csv", "export/manifest.json"):
        assert D.restorable(rel), rel
    for rel in ("_COMPLETE", "export/lerobot_curated/meta/info.json", ".orchestr/main.json",
                "details/evidence/task_success/ep000001_0.jpg", "details/audit_clips/a.mp4",
                "checks/video_action_sync/curves/ep000001.json", "checks/a/inflight.json",
                "report.md.tmp-1"):
        assert not D.restorable(rel), rel


# ---------------------------------------------------------------- settings and admission

def test_settings_from_the_environment(tmp_path):
    cfg = OrchestratorConfig.from_env({})
    assert cfg.max_running == 1 and cfg.term_grace_s == 90 and cfg.int_grace_s == 10
    assert cfg.local_delivery_root is None and cfg.enabled
    site = tmp_path / "site.yaml"
    site.write_text("concurrency: {cpu: 2}\nvlm: {merge: {enabled: false}}\nother: 1\n")
    cfg = OrchestratorConfig.from_env({"CURATOR_MAX_RUNNING_TASKS": "3",
                                       "CURATOR_CLI": "python -m curation.cli",
                                       "CURATOR_SITE_CONFIG": str(site),
                                       "CURATOR_ORCHESTRATOR": "off"})
    assert cfg.max_running == 3 and cfg.program == ("python", "-m", "curation.cli")
    assert cfg.site_config == {"concurrency": {"cpu": 2}, "vlm": {"merge": {"enabled": False}}}
    assert not cfg.enabled
    # the chart's site.yaml, which the CLI reads through CURATION_CONFIG
    assert OrchestratorConfig.from_env({"CURATION_CONFIG": str(site)}).site_config == \
        cfg.site_config
    with pytest.raises(OrchestratorConfigError):
        OrchestratorConfig.from_env({"CURATOR_MAX_RUNNING_TASKS": "0"})
    with pytest.raises(OrchestratorConfigError):
        OrchestratorConfig.from_env({"CURATOR_TERM_GRACE_S": "soon"})
    (tmp_path / "bad.json").write_text("[1]")
    with pytest.raises(OrchestratorConfigError):
        load_site_config(str(tmp_path / "bad.json"))


def test_memory_admission_waits_until_memory_is_back():
    from daemon.orchestr.resources import admit_memory

    class Run:
        cfg = OrchestratorConfig(memory_admission=0.8)

        def __init__(self):
            self.logs, self.notes = [], []

        def log(self, stage, level, msg):
            self.logs.append((level, msg))

        def progress(self, stage, **kw):
            self.notes.append(kw.get("note"))

        def check_intent(self):
            pass

    readings = iter([0.95, 0.9, 0.5])
    run = Run()
    slept = []
    admit_memory(run, "frame", probe=lambda: next(readings), sleep=slept.append)
    assert len(slept) == 2 and run.logs[0][0] == "warn" and "95%" in run.logs[0][1]
    assert run.logs[-1][0] == "info"
    run2 = Run()
    admit_memory(run2, "frame", probe=lambda: None, sleep=slept.append)   # unknown: go
    assert run2.logs == []


def test_the_daemon_asks_for_a_lower_oom_score_and_children_a_higher_one(monkeypatch):
    from daemon.exec import runner

    written = []
    monkeypatch.setattr(runner, "set_oom_score_adj", lambda pid, v: written.append((pid, v)) or True)
    from daemon.orchestr import resources

    monkeypatch.setattr(resources, "set_oom_score_adj",
                        lambda pid, v: written.append((pid, v)) or True)
    assert resources.lower_daemon_oom_score()
    assert written == [(os.getpid(), -500)]


def test_model_arguments_carry_the_reasoning_effort_only_when_there_is_one():
    from daemon.orchestr.runbase import Run, TaskFailure

    run = Run.__new__(Run)                       # vlm_args only reads the task
    run.task = P.Task(id="t", name="n", state="running", input_source="local", input_uri="/x",
                      output_uri="tos://b/o", delivery_key="tos://b/o",
                      episode_selector={"mode": "all"},
                      params={"vlm_retry": 2, "vlm_hedge": False,
                              "vlm_timeouts_s": {"probe": 90}},
                      vlm_snapshot={"endpoint": "http://m/v1", "model": "m1",
                                    "reasoning_effort": None})
    args = run.vlm_args()
    assert args[:4] == ["--vlm-endpoint", "http://m/v1", "--vlm-model", "m1"]
    assert args[args.index("--retry") + 1] == "2" and "--hedge" not in args
    assert "--vlm-reasoning-effort" not in args            # v1 never sends it (parity)
    assert "checks.task_success.vlm.timeouts_s.probe=90" in args
    run.task.vlm_snapshot["reasoning_effort"] = "low"
    run.task.params = {}
    args = run.vlm_args()
    assert args[args.index("--vlm-reasoning-effort") + 1] == "low"
    assert "--hedge" in args and args[args.index("--retry") + 1] == "3"   # the defaults
    run.task.vlm_snapshot = None
    with pytest.raises(TaskFailure):
        run.vlm_args()


def test_workdir_layout(tmp_path):
    from daemon.orchestr.workdir import Journal, WorkDir, read_lines, write_lines

    wd = WorkDir(tmp_path, "task_1")
    wd.ensure()
    assert wd.private == pathlib.Path(tmp_path) / "task_1" / ".orchestr" and not wd.started()
    j = Journal(wd.journal("main"))
    j.mark("numeric", done=True, state="succeeded")
    j.set(revision=2)
    again = Journal(wd.journal("main"))
    assert again.done("numeric") and again.get("revision") == 2 and not again.done("frame")
    path = write_lines(wd.episodes_file("main", "x"), [3, 1, 3])
    assert read_lines(path) == [1, 3] and read_lines(tmp_path / "missing") is None


def test_a_pause_lets_a_finished_stages_upload_finish_and_a_stop_cuts_it(tmp_path):
    """The pipelined funnel uploads a finished stage next to later ones: a pause coming in meanwhile
    waits for that upload (a paused task has its finished stages delivered), a stop cuts it short."""
    import contextlib
    from types import SimpleNamespace

    from daemon.orchestr.runbase import Run as RunBase, Interrupt

    root = tmp_path / "run"
    (root / "checks" / "timestamp_check").mkdir(parents=True)
    for i in range(3):
        (root / "checks" / "timestamp_check" / f"part{i}.jsonl").write_text("{}\n")

    class Run:
        check_stop, check_intent, sync_quietly = RunBase.check_stop, RunBase.check_intent, RunBase.sync_quietly

        def __init__(self, intent):
            self.intent, self._pipeline_abort, self.logs = intent, None, []
            self.task = SimpleNamespace(run_id="r1")
            n = len(list(tmp_path.glob("out-*")))                  # a delivery and a sync state of its own
            self.wd = SimpleNamespace(root=root, sync_state=tmp_path / f"sync-{n}.json")
            self.out = tmp_path / f"out-{n}"
            self.out.mkdir()

        def delivery(self):
            return contextlib.nullcontext(D.LocalDelivery(self.out, "tos://out"))

        def log(self, *args):
            self.logs.append(args)

    paused = Run("pause")
    paused.sync_quietly("numeric", through_pause=True)
    assert sorted(os.listdir(paused.out / "r1" / "checks" / "timestamp_check")) == [
        "part0.jsonl", "part1.jsonl", "part2.jsonl"]
    for run, kw in ((Run("stop"), {"through_pause": True}), (Run("pause"), {})):
        with pytest.raises(Interrupt):
            run.sync_quietly("numeric", **kw)
        assert not (run.out / "r1").exists()
