"""The two policy presets end to end on the 8-episode fixture (design doc 17 §4, D58; F12.3 acceptance 2, 4).

``report_only`` reports every finding and rejects nobody: every stage of the funnel sees every episode,
every episode a module could judge is kept and nobody is asked. The run is then switched to the default
policy by its ``run.json`` alone: ``aggregate`` runs again - nothing else does - the module records stay
byte for byte, and the verdicts are today's (``test_pipeline_chain``: 2 and 5 stop on their timestamps,
7 is a byte copy of 3).
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from curation.pipeline import policy as policy_mod

from .conftest import ENV_VARS
from .fakevlm_server import FakeVlmServer
from .pipeline import Chain, read_jsonl, run


def _set_policy(run_dir: str, preset: str) -> None:
    """What the Daemon freezes into ``run.json`` at start (here: what a person switched it to)."""
    path = os.path.join(run_dir, "run.json")
    doc = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    doc["policy"] = policy_mod.Policy.of(preset).to_json()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def _records(run_dir: str) -> str:
    """A digest of every module record file of the run."""
    h = hashlib.sha256()
    base = os.path.join(run_dir, "checks")
    for dirpath, dirs, files in os.walk(base):
        dirs.sort()
        for name in sorted(files):
            if name.endswith(".jsonl"):
                path = os.path.join(dirpath, name)
                h.update(os.path.relpath(path, base).encode())
                with open(path, "rb") as fh:
                    h.update(fh.read())
    return h.hexdigest()


def _list(run_dir: str, revision: int, name: str) -> dict[int, dict]:
    with open(os.path.join(run_dir, "revisions", f"r{revision:04d}", f"{name}.json"), encoding="utf-8") as fh:
        return {e["episode_index"]: e for e in json.load(fh)["episodes"]}


def _policy(run_dir: str, revision: int) -> str:
    with open(os.path.join(run_dir, "revisions", f"r{revision:04d}", "policy.json"), encoding="utf-8") as fh:
        return json.load(fh)["preset"]


@pytest.fixture(scope="module")
def chain(tmp_path_factory, mini_dataset):
    tmp = tmp_path_factory.mktemp("presets")
    rd = str(tmp / "run")
    os.makedirs(rd)
    _set_policy(rd, "report_only")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV_VARS:
            mp.delenv(name, raising=False)
        with FakeVlmServer(delay_s=0.003) as vlm:
            c = Chain(mini_dataset, rd, vlm.url)
            c.front()
            c.funnel()
            c.post()
    c.digest = _records(rd)
    return c


def test_report_only_keeps_what_was_judged_and_asks_nobody(chain):
    s = chain.steps
    # nothing blocks: every stage sees all eight
    for stage, module in (("numeric", "timestamp_check"), ("frame", "visual_quality"), ("vlm", "task_success"),
                          ("dedup", "dedup")):
        assert s[stage].doc["modules"][module]["episodes"]["total"] == 8, stage
    assert s["numeric"].doc["modules"]["timestamp_check"]["findings"] == {"gap": 1, "fragment": 1}
    # every module judged every episode here: all eight are kept - 7, the byte copy of 3, too, and the
    # profile files it (a duplicate the policy does not reject is no copy to leave out)
    assert sorted(_list(chain.rd, 1, "passed")) == list(range(8))
    assert _list(chain.rd, 1, "reject") == _list(chain.rd, 1, "held") == _list(chain.rd, 1, "review") == {}
    lines = {ln["episode_index"]: ln for ln in
             read_jsonl(os.path.join(chain.rd, "revisions", "r0001", "verdicts.jsonl"))}
    assert all(ln["blocking"] == [] and ln["review"] == [] for ln in lines.values())
    assert lines[2]["info_count"] >= 1 and lines[5]["info_count"] >= 1      # the findings are all kept
    assert _policy(chain.rd, 1) == "report_only"


def test_switching_to_the_default_reruns_aggregate_only(chain):
    _set_policy(chain.rd, "default")
    for phase in ("funnel", "final"):
        res = run("aggregate", "--run-dir", chain.rd, "--phase", phase, "--revision", "2", "--episodes", "0-7")
        assert res.rc == 0, res.doc
    assert _records(chain.rd) == chain.digest            # the module records stay as they were
    assert _list(chain.rd, 2, "held") == {}
    reject = _list(chain.rd, 2, "reject")
    assert sorted(reject) == [1, 2, 4, 5, 6, 7]
    assert [r["code"] for r in reject[2]["reasons"]][:1] == ["gap"]
    assert [r["code"] for r in reject[5]["reasons"]][:1] == ["fragment"]
    assert reject[7]["reasons"][0]["kind"] == "duplicate" and reject[7]["reasons"][0]["duplicate_of"] == 3
    assert sorted(_list(chain.rd, 2, "passed")) == [0, 3]
    assert sorted(_list(chain.rd, 2, "review")) == [0, 1, 3, 4, 6, 7]
    assert (_policy(chain.rd, 1), _policy(chain.rd, 2)) == ("report_only", "default")
