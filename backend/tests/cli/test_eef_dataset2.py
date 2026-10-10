"""F5.9 acceptance ① on the DEMO dataset2 (design doc 12 appendix C.9), read as opinions (design doc 25 §7).

The whole ``check`` command runs the module one episode per call with a stand-in model that answers as
an ideal observer of a few crops would, from the evaluator's truth of that episode (never seen by the
module): a trajectory drift is visibly off in position and direction, a rotated gripper in direction
only, a camera with a wrong extrinsic shows P beside the gripper on that camera only; jitter and a
time shift are not clear on three crops (uncertain); a shaking video and the original look right. The
calls are recorded on a tape and replayed offline into a fresh run directory: the same records. The
original reads consistent; every fault is at least possibly inconsistent on the sub-item it touches, its
grounds naming the sub-item, the camera and what each channel said; nothing is rejected (D81). Skipped
without the DEMO data (outside the repo); about three minutes with it.
"""
from __future__ import annotations

import json
import re

from ..eef import demo_data
from .pipeline import passed_of, results
from .test_eef_check import EEF, URL
from .test_eef_review import _details, _hooks

CAMERA_KEY = {"exterior_1_left": "27432424_left", "exterior_2_left": "28221883_left"}
#: fault -> (position, direction) as an ideal observer of a few marked crops would answer
SEEN = {"baseline": ("support", "support"), "traj_drift": ("refute", "refute"),
        "gripper_orient": ("support", "refute"), "traj_jitter": ("uncertain", "uncertain"),
        "video_shake": ("support", "support"), "sync_offset": ("uncertain", "uncertain"),
        "calib_offset": ("refute", "uncertain")}


def observer(fault: dict):
    pos, ori = SEEN[fault["kind"]]
    only = CAMERA_KEY.get(fault["camera"]) if fault["kind"] == "calib_offset" else None

    def answer(payload: dict) -> str:
        if "You review ONE point P" not in json.dumps(payload):
            return "pong"
        prompt = payload["messages"][0]["content"][0]["text"]
        frames = [int(x) for x in re.findall(r"\d+", prompt.split("Frames ", 1)[1].split("(", 1)[0])]
        p, o = (pos, ori) if only is None or f"Camera {only}." in prompt else ("support", "support")
        if "ONE direction A" not in prompt:
            o = "not_observable"
        return json.dumps({"review_status": p, "target_visible": True, "tracking_target_correct": "support",
                           "position_support": p, "orientation_support": o,
                           "offset_direction": "left" if p == "refute" else "none",
                           "offset_magnitude_class": "one_to_two_finger_widths" if p == "refute" else "none",
                           "evidence_frame_ids": frames[:1], "reason_codes": [],
                           "explanation": {"refute": "红圈偏离夹爪", "support": "红圈落在夹爪上"}.get(p, "看不太准")},
                          ensure_ascii=False)
    return answer


def _check(cli, root, rd, ep):
    res = cli("check", "--modules", EEF, "--input", str(demo_data.lerobot_root("dataset2")), "--run-dir", rd,
              "--episodes", str(ep), "--param", f"{EEF}.trajectory_json={root / 'trajectory.json'}",
              "--vlm-endpoint", URL, "--vlm-model", "fake-vlm", "--retry", "0")
    assert res.rc == 0, res.doc
    return res.doc["modules"][EEF]


def test_the_original_reads_consistent_and_every_fault_is_flagged(cli, tmp_path):
    from eef_eval import truth
    from parity import vlm_tape as T
    from parity.fakevlm import FakeVlm

    from curation.extensions.eef_consistency import combine as M

    root = demo_data.require("dataset2")
    faults = truth.faults("dataset2")                          # the evaluator's side only
    rd, tape = str(tmp_path / "run"), str(tmp_path / "tape.jsonl.gz")
    fake = FakeVlm("fake-vlm")
    hooks = _hooks("record", tape_out=tape, transport=fake.transport())
    try:
        for ep in range(7):
            fake.answer = observer(faults[ep])
            assert _check(cli, root, rd, ep)["episodes"]["error"] == 0
    finally:
        hooks.uninstall()
    recs = results(rd, EEF)
    out = {ep: r["details"]["merged"] for ep, r in sorted(recs.items())}
    print(json.dumps({ep: [m["episode"]["label"], m["episode"]["p"], m["episode"]["reason"],
                           [(c["subitem"], c["camera"], c["p"], c["flags"]) for c in m["cells"] if c["p"] is not None]]
                      for ep, m in out.items()}, ensure_ascii=False, indent=1))
    for r in recs.values():                                    # an opinion: nothing is rejected (D81); a conflict asks
        assert passed_of(r) is (None if r["details"]["merged"]["episode"]["conflicts"] else True), r["details"]["reason"]
    assert out[0]["episode"]["label"] == M.CONSISTENT, out[0]["episode"]["reason"]
    for ep in range(1, 7):
        head = out[ep]["episode"]
        assert head["label"] in (M.INCONSISTENT, M.POSSIBLY), (ep, head["reason"])
        assert M.SUB_ZH[head["subitem"]] in head["reason"], head
        if head["camera"]:
            assert f"相机 {head['camera']}" in head["reason"], head
        for c in out[ep]["cells"]:
            if M.CONFLICT in c["flags"]:                       # both channels, each with what it said
                assert len(c["sources"]) == 2 and all(s["p"] is not None for s in c["sources"].values()), c
    # replayed offline into a fresh run directory: the same records
    _, entries = T.read_tape(tape)
    fresh = str(tmp_path / "replay")
    hooks = _hooks("replay", replay_entries=entries)
    try:
        for ep in range(7):
            _check(cli, root, fresh, ep)
    finally:
        hooks.uninstall()
    assert _details(fresh) == _details(rd)
