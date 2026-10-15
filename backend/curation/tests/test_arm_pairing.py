"""MV-2's measured half: which arm each wrist camera rides (``pipeline.arm_pairing``).

Video decoding is not what these check - ``picture_motion`` is replaced by the series it would
return, so the pairing itself is tested on numbers whose answer is known.
"""
import numpy as np
import pytest

from curation.pipeline import arm_pairing as AP

#: 40 samples: the left arm alone moves over the first half, the right arm alone over the second
LEFT_SOLO = [1.0] * 20 + [0.0] * 20
RIGHT_SOLO = [0.0] * 20 + [1.0] * 20


def _video(*cameras):
    return {c: {"path": f"/nowhere/{c}.mp4", "from_ts": 0.0, "to_ts": 4.0} for c in cameras}


def _action():
    """80 recorded rows at 20 Hz: the left arm travels over the first two seconds, the right arm
    over the last two, so each has the other's stillness to be told apart by."""
    action = np.zeros((80, 6))
    action[:40, 0] = np.arange(40) * 0.01
    action[40:, 0] = action[39, 0]
    action[40:, 3] = np.arange(40) * 0.01
    return action


def _claim(**over):
    claim = {"arms": ["left", "right"], "channels": {"left": [0, 1, 2], "right": [3, 4, 5]},
             "cameras": {}}
    claim.update(over)
    return claim


def test_each_arms_speed_comes_from_its_own_channels():
    """The columns the dataset's channel names gave, sampled at the rate the picture is sampled at:
    a 20 Hz recording against a 10 Hz series is every second row."""
    action = np.zeros((40, 6))
    action[:, 0] = np.arange(40) * 0.1              # the left arm travels, the right stands still
    speeds = AP.arm_speeds(action, {"left": [0, 1, 2], "right": [3, 4, 5]}, fps=20.0)
    assert len(speeds["left"]) == 20 and len(speeds["right"]) == 20
    assert speeds["left"][0] == pytest.approx(0.1) and max(speeds["right"]) == 0.0
    assert AP.arm_speeds(np.zeros((2, 6)), {"left": [0, 1]}) == {}          # too short to difference
    assert AP.arm_speeds(action, {"left": [0]}) == {}                       # one column is not an arm


def test_the_moments_that_carry_the_answer_are_the_solo_ones():
    """Only the samples where one arm moves and the other is still; two hands moving together say
    nothing about which camera rides which, and an episode of that answers unknown."""
    found = AP.moments({"left": LEFT_SOLO, "right": RIGHT_SOLO}, 40)
    assert found["left"] and found["right"]
    assert all(i < 20 for i in found["left"]) and all(i >= 20 for i in found["right"])
    together = [1.0] * 40
    assert AP.moments({"left": together, "right": together}, 40) == {"left": [], "right": []}
    assert AP.moments({"left": LEFT_SOLO}, 40) == {}                        # one arm, nothing to tell apart


def test_the_pairing_is_decided_for_the_episode_not_per_camera():
    """Two cameras and two arms admit two pairings; the better one wins and only by a margin. Both
    cameras picking the same arm is a contradiction, not a crossing - it has no margin."""
    crossed = {"left_cam": {"left": 0.0, "right": 8.0}, "right_cam": {"left": 5.0, "right": 0.0}}
    paired, margin = AP.match(crossed, ["left", "right"])
    assert paired == {"left_cam": "right", "right_cam": "left"} and margin == 2.0
    straight = {"left_cam": {"left": 6.0, "right": 0.0}, "right_cam": {"left": 0.0, "right": 4.0}}
    assert AP.match(straight, ["left", "right"])[0] == {"left_cam": "left", "right_cam": "right"}
    both_right = {"left_cam": {"left": 0.0, "right": 2.0}, "right_cam": {"left": 0.0, "right": 9.0}}
    assert AP.match(both_right, ["left", "right"])[1] == 0.0
    assert AP.match({}, ["left", "right"]) == ({}, 0.0)


def test_the_arm_a_camera_is_declared_on():
    """The declaration answers first, the camera's own name second - which is the dataset's claim
    too, and the one MV-2 is about. A camera the declaration calls fixed rides no arm."""
    arms = ["left", "right"]
    said = {"cam_a": {"mount": "wrist", "owner": "right"}, "top": {"mount": "fixed_external"}}
    assert AP.declared_arm("cam_a", said, arms) == "right"           # the declaration, not the name
    assert AP.declared_arm("observation.images.left_wrist", {}, arms) == "left"
    assert AP.declared_arm("top", said, arms) is None
    assert AP.declared_arm("observation.images.left_right_rig", {}, arms) is None
    assert AP.declared_arm("cam", {}, arms) is None


def test_a_crossed_pair_is_measured_and_a_tie_stays_unknown(monkeypatch):
    """End to end over one episode: the camera the dataset calls left moves with the right arm, so
    the pair is crossed and the record says which cameras and how clear the answer was."""
    action = _action()
    series = {"left_cam": RIGHT_SOLO, "right_cam": LEFT_SOLO}
    monkeypatch.setattr(AP, "picture_motion", lambda path, **kw: series[path.split("/")[-1][:-4]])
    got = AP.pair(_video("left_cam", "right_cam"), action, _claim(), fps=20.0)
    assert got["identity"] == "crossed" and got["crossed"] == ["left_cam", "right_cam"]
    assert got["basis"] == "motion_pairing" and got["readings"]["margin"] > AP.MIN_MARGIN
    assert got["readings"]["paired"] == {"left_cam": "right", "right_cam": "left"}

    both = {"left_cam": LEFT_SOLO, "right_cam": LEFT_SOLO}    # both pictures follow the left arm
    monkeypatch.setattr(AP, "picture_motion", lambda path, **kw: both[path.split("/")[-1][:-4]])
    tie = AP.pair(_video("left_cam", "right_cam"), action, _claim(), fps=20.0)
    assert tie["identity"] == "unknown" and "判不出" in tie["note"]


def test_a_third_camera_on_one_of_the_arms_still_pairs(monkeypatch):
    """Two cameras on the left hand and one on the right: the mappings enumerated are those that
    leave no arm without a camera, so the declared one is among them and nothing raises."""
    action = _action()
    series = {"left_cam": LEFT_SOLO, "left_aux": LEFT_SOLO, "right_cam": RIGHT_SOLO}
    monkeypatch.setattr(AP, "picture_motion", lambda path, **kw: series[path.split("/")[-1][:-4]])
    got = AP.pair(_video("left_cam", "left_aux", "right_cam"), action, _claim(), fps=20.0)
    assert got["identity"] == "consistent" and got["readings"]["paired"] == {
        "left_aux": "left", "left_cam": "left", "right_cam": "right"}
    # and cameras that are all declared on the same arm have no crossing to look for
    only_left = AP.pair(_video("left_cam", "left_aux"), action, _claim(), fps=20.0)
    assert only_left["identity"] == "unknown" and "同一条臂" in only_left["note"]


def test_nothing_to_measure_against_is_unknown_not_a_verdict(monkeypatch):
    """Every half that MV-2 needs can be missing, and a missing half is never a verdict: one arm in
    the dataset, one camera on an arm, no video, an unreadable video."""
    action = _action()
    one_arm = AP.pair(_video("left_cam", "right_cam"), action, {"arms": ["left"], "channels": {}})
    assert one_arm["identity"] == "unknown" and "一条臂" in one_arm["note"]
    lone = AP.pair(_video("left_cam", "top"), action, _claim(), fps=20.0)
    assert lone["identity"] == "unknown" and "一路相机" in lone["note"]
    assert AP.pair({}, action, _claim())["identity"] == "unknown"
    assert AP.pair(_video("left_cam", "right_cam"), None, _claim())["identity"] == "unknown"

    def boom(path, **kw):
        raise RuntimeError("no such file")

    monkeypatch.setattr(AP, "picture_motion", boom)
    broken = AP.pair(_video("left_cam", "right_cam"), action, _claim(), fps=20.0)
    assert broken["identity"] == "unknown" and "读不出" in broken["note"]
