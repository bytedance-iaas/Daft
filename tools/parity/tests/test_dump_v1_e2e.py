"""dump-v1 and compare on the synthetic dataset, with the fake model.

These run the real v1 pipeline (decode, optical flow, the whole task-success
chain, dedup, skill profile, export) in subprocesses, about half a minute per
run. Deselect with ``-m "not e2e"``.
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import shutil

import jsonschema
import pytest

from parity import vlm_tape as T

from .conftest import load_schema, run_parity

pytestmark = pytest.mark.e2e

V1_RUN = ["run", "--vlm-endpoint", "http://fake-vlm.local/v1", "--vlm-model", "fake-vlm"]


V1_SRC: list[str] = []          # set once by the autouse fixture below


@pytest.fixture(autouse=True, scope="module")
def _v1_source(v1_src):
    V1_SRC[:] = [v1_src]


def dump(tmp, name, dataset, *extra):
    out = str(tmp / name)
    src = [] if "--v1-src" in extra else ["--v1-src", V1_SRC[0]]
    proc = run_parity("dump-v1", "--out", out, *src, *extra, "--", *V1_RUN,
                      "--input", dataset, "--output", str(tmp / f"{name}-delivery"))
    return out, proc


def compare(golden, candidate, *extra):
    return run_parity("compare", "--golden", golden, "--candidate", candidate, *extra)


def meta(out):
    with open(os.path.join(out, "dump.json")) as fh:
        return json.load(fh)


def rewrite_tape(src, dst, edit):
    header, entries = T.read_tape(src)
    entries = edit(entries)
    with gzip.open(dst, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(header) + "\n")
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return dst


@pytest.fixture(scope="module")
def recorded(tmp_path_factory, mini_dataset):
    tmp = tmp_path_factory.mktemp("e2e")
    out, proc = dump(tmp, "rec1", mini_dataset, "--fake-vlm")
    assert proc.returncode == 0, proc.stderr[-4000:]
    return tmp, out


def test_record_run_is_clean_and_walks_the_whole_funnel(recorded):
    _, out = recorded
    m = meta(out)
    assert m["status"] == "clean", m["problems"]
    assert m["v1_source"]["ok"] and m["v1_source"]["commit"].startswith("45bdf929")
    assert m["counts"]["records"] == {"timestamp_check": 8, "kinematic_limits": 8,
                                      "motion_quality": 8, "visual_quality": 6,
                                      "video_action_sync": 6, "task_success": 6}
    with open(os.path.join(out, "final.json")) as fh:
        final = json.load(fh)
    assert final["passed"] == [0, 1, 3, 4, 6] and final["reject"] == [2, 5, 7]
    # v1 lists the dedup-removed episode in passed.json as well (see build_final)
    assert 7 in final["v1_views"]["passed_json"] and 7 in final["v1_views"]["reject_json"]
    with open(os.path.join(out, "dedup.json")) as fh:
        assert json.load(fh)["dropped"] == [{"duplicate_of": 3, "episode_index": 7}]
    with open(os.path.join(out, "autolabel.jsonl")) as fh:
        assert [json.loads(x)["episode_index"] for x in fh] == [4, 6]
    schema = load_schema("cli", "result-record.schema.json")
    for path in glob.glob(os.path.join(out, "records", "*.jsonl")):
        with open(path) as fh:
            for line in fh:
                jsonschema.validate(json.loads(line), schema)
    tags = {row["tag"] for row in m["tape"]["summary"]["by_kind_tag_outcome"]}
    assert {"probe", "endstate", "caption", "arbitration"} <= tags


def test_replay_matches_bit_for_bit(recorded, mini_dataset):
    tmp, out = recorded
    rep, proc = dump(tmp, "replay1", mini_dataset,
                     "--replay", os.path.join(out, "vlm_tape.jsonl.gz"))
    assert proc.returncode == 0, proc.stderr[-4000:]
    hooks = meta(rep)["tape"]["hooks"]
    assert hooks["misses"] == 0 and hooks["unused"] == 0
    res = compare(out, rep, "--all-strict")
    assert res.returncode == 0, res.stdout


def test_v1_run_twice_is_identical(recorded, mini_dataset):
    """W0 acceptance 1: the tool finds v1 identical to itself."""
    tmp, out = recorded
    again, proc = dump(tmp, "rec2", mini_dataset, "--fake-vlm")
    assert proc.returncode == 0, proc.stderr[-4000:]
    res = compare(out, again, "--noise-floor", again, "--json")
    assert res.returncode == 0, res.stdout
    assert json.loads(res.stdout)["conclusion"] == "pass"
    assert compare(out, again, "--all-strict").returncode == 0


def test_a_changed_model_answer_is_caught(recorded, mini_dataset, tmp_path):
    _, out = recorded

    def edit(entries):
        # Tape order follows thread scheduling during the recording, so pick the answer by
        # request hash: the same probe is edited on every run.
        e = min((e for e in entries if e.get("tag") == "probe"), key=lambda e: e["hash"])
        body = json.loads(e["body"])
        old = body["choices"][0]["message"]["content"]
        body["choices"][0]["message"]["content"] = "0" if old != "0" else "100"
        e["body"] = json.dumps(body)
        return entries

    tape = rewrite_tape(os.path.join(out, "vlm_tape.jsonl.gz"),
                        str(tmp_path / "edited.jsonl.gz"), edit)
    # The changed answer may send task_success down a path whose requests were never
    # recorded (arbitration, the reject guard); those replay misses are expected here.
    rep, proc = dump(tmp_path, "replay-edited", mini_dataset, "--replay", tape,
                     "--allow-failures")
    assert proc.returncode == 0, proc.stderr[-4000:]
    res = compare(out, rep, "--all-strict", "--json")
    assert res.returncode == 1
    assert json.loads(res.stdout)["modules"]["task_success"]["status"] == "fail"


def test_a_request_missing_from_the_tape_is_a_replay_miss(recorded, mini_dataset, tmp_path):
    _, out = recorded

    def edit(entries):
        idx = next(i for i, e in enumerate(entries) if e.get("tag") == "endstate")
        return entries[:idx] + entries[idx + 1:]

    tape = rewrite_tape(os.path.join(out, "vlm_tape.jsonl.gz"),
                        str(tmp_path / "short.jsonl.gz"), edit)
    rep, proc = dump(tmp_path, "replay-short", mini_dataset, "--replay", tape)
    assert proc.returncode == 3                       # dirty: the run saw a miss
    assert "replay_miss" in [p["kind"] for p in meta(rep)["problems"]]
    res = compare(out, rep, "--json")
    assert json.loads(res.stdout)["replay"]["misses"] >= 1


def test_replay_record_repairs_a_failed_call(recorded, mini_dataset, tmp_path):
    _, out = recorded

    def edit(entries):
        for e in entries:
            if e.get("tag") == "probe":
                e.pop("body", None), e.pop("status", None)
                e.update(outcome="exception", exc_type="requests.exceptions.ReadTimeout",
                         exc_message="read timed out")
                break
        return entries

    tape = rewrite_tape(os.path.join(out, "vlm_tape.jsonl.gz"),
                        str(tmp_path / "failed.jsonl.gz"), edit)
    # as a plain replay the failure comes back and the run is dirty
    rep, proc = dump(tmp_path, "replay-failed", mini_dataset, "--replay", tape)
    assert proc.returncode == 3
    # replay + record re-asks the failed call and writes a complete, clean tape
    fixed, proc = dump(tmp_path, "replay-record", mini_dataset,
                       "--replay", tape, "--record-missing", "--fake-vlm")
    assert proc.returncode == 0, proc.stderr[-4000:]
    _, entries = T.read_tape(os.path.join(fixed, "vlm_tape.jsonl.gz"))
    assert not T.tape_failures(entries)
    assert sum(1 for e in entries if e.get("replayed_from") is None) == 1
    assert compare(out, fixed, "--all-strict").returncode == 0


def test_v1_source_drift_is_refused(tmp_path, mini_dataset, v1_src):
    src = tmp_path / "v1"
    shutil.copytree(os.path.join(v1_src, "curation"), src / "curation",
                    ignore=shutil.ignore_patterns("__pycache__"))
    with open(src / "curation" / "pipeline" / "verdict.py", "a") as fh:
        fh.write("\n# drift\n")
    out, proc = dump(tmp_path, "drift", mini_dataset, "--fake-vlm", "--v1-src", str(src))
    assert proc.returncode == 2
    m = meta(out)
    assert m["status"] == "refused" and m["v1_source"]["modified"] == ["pipeline/verdict.py"]


def test_archive_and_fetch_round_trip(recorded, tmp_path):
    _, out = recorded
    arch = tmp_path / "archive"
    proc = run_parity("archive", "--dump", out, "--to", str(arch),
                      "--manifest-out", str(tmp_path / "manifest.json"))
    assert proc.returncode == 0, proc.stderr
    with open(tmp_path / "manifest.json") as fh:
        manifest = json.load(fh)
    assert manifest["status"] == "clean" and "vlm_tape.jsonl.gz" in manifest["files"]
    back = tmp_path / "back"
    assert run_parity("fetch", "--from", str(arch), "--to", str(back)).returncode == 0
    with open(back / "final.json", "a") as fh:
        fh.write(" ")
    again = tmp_path / "again"
    shutil.copytree(back, again)
    from parity.archive import verify
    assert verify(str(again)) == ["final.json"]
