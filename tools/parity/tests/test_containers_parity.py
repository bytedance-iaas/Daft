"""v1 against v2 on the synthetic dataset as mcap and as lance (D44, v1's PR #155).

The LeRobot fixture's eight episodes in the two new formats (``make-fixture --format``):
v1 at the freeze point (``dump-v1 -- run``, the fake model, its calls taped) and v2's
command chain (``run-v2`` replaying that tape, with ``--selection`` as the Daemon passes
it) must agree on every module's records, the final lists and the call graph:
``compare --all-strict`` - the same bar as the LeRobot parity. Deselect with ``-m "not e2e"``.
"""
from __future__ import annotations

import json
import os

import pytest

from .conftest import V1_RUN, run_parity

pytestmark = pytest.mark.e2e

pytest.importorskip("mcap")
pytest.importorskip("mcap_ros2")
pytest.importorskip("lance")


@pytest.fixture(scope="module", params=["mcap", "lance"])
def container(request, tmp_path_factory):
    fmt = request.param
    tmp = tmp_path_factory.mktemp(f"parity-{fmt}")
    dataset = str(tmp / f"mini-{fmt}")
    proc = run_parity("make-fixture", "--out", dataset, "--format", fmt)
    assert proc.returncode == 0, proc.stderr
    return fmt, tmp, dataset


def test_v2_matches_v1_on_the_format(container, v1_src):
    fmt, tmp, dataset = container
    golden = str(tmp / "v1")
    proc = run_parity("dump-v1", "--out", golden, "--v1-src", v1_src, "--fake-vlm", "--",
                      *V1_RUN, "--input", dataset, "--output", str(tmp / "v1-delivery"))
    assert proc.returncode == 0, proc.stderr[-4000:]
    with open(os.path.join(golden, "final.json"), encoding="utf-8") as fh:
        final = json.load(fh)
    assert final["passed"] == [0, 1, 3, 4, 6] and final["reject"] == [2, 5, 7]

    out = str(tmp / "v2")
    proc = run_parity("run-v2", "--out", out, "--input", dataset, "--delivery",
                      str(tmp / "v2-delivery"), "--replay",
                      os.path.join(golden, "vlm_tape.jsonl.gz"))
    assert proc.returncode == 0, proc.stderr[-4000:]
    with open(os.path.join(out, "parity.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc["tape"]["hooks"]["misses"] == 0 and doc["tape"]["hooks"]["unused"] == 0
    export = next(s["output"] for s in doc["steps"] if s["step"] == "export")
    assert export["format"] == fmt
    assert doc["steps"][-1]["output"]["complete_marker"] is True

    res = run_parity("compare", "--golden", golden, "--candidate", out, "--all-strict", "--json")
    report = json.loads(res.stdout)
    assert res.returncode == 0, json.dumps(report, ensure_ascii=False)[:3000]
    assert report["conclusion"] == "pass"
    assert set(report["modules"]) >= {"timestamp_check", "kinematic_limits", "motion_quality",
                                      "visual_quality", "video_action_sync", "task_success",
                                      "dedup", "autolabel", "skill_profile"}
    assert {m: r["status"] for m, r in report["modules"].items()} == {
        m: "pass" for m in report["modules"]}
