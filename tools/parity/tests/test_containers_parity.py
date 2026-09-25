"""v2 on the synthetic dataset as mcap and as lance (D44, v1's PR #155), against its own golden.

The LeRobot fixture's eight episodes in the two new formats (``make-fixture --format``).
Design 13 retired the v1 comparison (plan A, 2026-09-24): the frozen v1 asks image probes
and v2 sends continuous videos, so v1's tape answers none of v2's requests. As for LeRobot
(``test_v2_parity.py``), ``run-v2 --fake-vlm`` records a golden and a tape of every model
call, and ``run-v2 --replay`` of that tape must reproduce it exactly (``compare
--all-strict``): every module's records, the final lists and the call graph. Read from
either container, the eight episodes end in the LeRobot fixture's final lists, and the
export keeps the input's format. Deselect with ``-m "not e2e"``.
"""
from __future__ import annotations

import json
import os

import pytest

from .conftest import run_parity

pytestmark = pytest.mark.e2e

pytest.importorskip("mcap")
pytest.importorskip("mcap_ros2")
pytest.importorskip("lance")


def run_v2(tmp, name: str, dataset: str, *mode: str) -> tuple[str, dict]:
    out = str(tmp / name)
    proc = run_parity("run-v2", "--out", out, "--input", dataset,
                      "--delivery", str(tmp / f"{name}-delivery"), *mode)
    assert proc.returncode == 0, proc.stderr[-4000:]
    with open(os.path.join(out, "parity.json"), encoding="utf-8") as fh:
        return out, json.load(fh)


def final_lists(run_dir: str) -> dict[str, list[int]]:
    rev = os.path.join(run_dir, "revisions", "r0001")
    out = {}
    for name in ("passed", "reject", "held", "review"):
        with open(os.path.join(rev, f"{name}.json"), encoding="utf-8") as fh:
            out[name] = [e["episode_index"] for e in json.load(fh)["episodes"]]
    return out


@pytest.fixture(scope="module")
def lerobot_final(mini_dataset, tmp_path_factory) -> dict[str, list[int]]:
    golden, _ = run_v2(tmp_path_factory.mktemp("lerobot"), "golden", mini_dataset, "--fake-vlm")
    return final_lists(golden)


@pytest.fixture(scope="module", params=["mcap", "lance"])
def container(request, tmp_path_factory):
    fmt = request.param
    tmp = tmp_path_factory.mktemp(f"parity-{fmt}")
    dataset = str(tmp / f"mini-{fmt}")
    proc = run_parity("make-fixture", "--out", dataset, "--format", fmt)
    assert proc.returncode == 0, proc.stderr
    return fmt, tmp, dataset


def test_v2_replays_its_golden_on_the_format(container, lerobot_final):
    fmt, tmp, dataset = container
    golden, doc = run_v2(tmp, "golden", dataset, "--fake-vlm")
    assert doc["tape"]["mode"] == "record"
    assert final_lists(golden) == lerobot_final
    assert lerobot_final["passed"] == [0, 1, 3, 4, 6] and lerobot_final["reject"] == [2, 5, 7]

    out, doc = run_v2(tmp, "v2", dataset, "--replay", os.path.join(golden, "vlm_tape.jsonl.gz"))
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
