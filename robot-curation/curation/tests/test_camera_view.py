"""相机朝向与左右镜像提示(2026-09-02 libero ep000004 教训)。"""
from __future__ import annotations

import numpy as np

from curation.core.checks.camera_view import camera_hints, lateral, view_hint


def test_lateral_words_detected_only_as_whole_words():
    assert lateral("put the pudding to the right of the plate")
    assert lateral("pick the leftmost cup") and lateral("把杯子放在盘子左边")
    assert not lateral("turn on the bright lamp")      # bright ≠ right
    assert not lateral("stack the cubes") and not lateral("")


def test_hint_only_for_front_and_only_when_lateral():
    """用户定:只纠正 front;wrist/rear/side/unknown/未声明一律不加(对 droid 零差异)。"""
    assert view_hint("front", "stack the cubes") == ""
    h = view_hint("front", "put it on the right plate")
    assert "faces the robot" in h and "robot's point of view" in h
    for v in ("wrist", "rear", "side", "unknown", None, ""):
        assert view_hint(v, "left cup") == ""


def test_camera_hints_dict_shapes():
    views = {"image": "front", "image2": {"view": "wrist"}}
    h = camera_hints(views, "put the mug on the right plate", ["image", "image2", "extra"])
    assert set(h) == {"image"} and "faces the robot" in h["image"]
    assert camera_hints(views, "stack the cubes", ["image"]) == {}
    assert camera_hints(None, "left", ["a"]) == {}
    # droid 型声明(外部机位 unknown、腕部 wrist):含左右词也零提示
    droid = {"exterior_image_1_left": "unknown", "exterior_image_2_left": "unknown", "wrist_image_left": "wrist"}
    assert camera_hints(droid, "put the cup on the left shelf", list(droid)) == {}


def test_review_label_and_arbitration_question_carry_hint():
    """复核:提示随机位标签进 prompt;仲裁:提示挂在该路的验证问题上,别的路不带。"""
    from curation.core.checks.task_success import arbitration_review, endstate_review
    from curation.core.contract import CheckResult
    res = CheckResult(name="task_success", passed=True, detail={"verdict": "success"})
    calls = []

    def voter(starts, ends, label, desc):
        calls.append(label)
        return "yes"
    cams = {"image": [np.zeros((4, 4, 3), np.uint8)] * 16, "image2": [np.zeros((4, 4, 3), np.uint8)] * 16}
    endstate_review(res, "left cup", voter, cams,
                    cam_hints={"image": "FRONT-HINT"})
    assert any("FRONT-HINT" in c for c in calls) and any("image2" in c and "FRONT-HINT" not in c for c in calls)

    seen = []

    def judge(imgs, *, target, question, scene):
        seen.append(question)
        return "yes"
    res2 = CheckResult(name="task_success", passed=None, detail={"verdict": "uncertain"})
    frames = {"image": [np.zeros((16, 16, 3), np.uint8)] * 10, "wrist_cam": [np.zeros((16, 16, 3), np.uint8)] * 10}
    ts = {k: np.arange(10) * 0.5 for k in frames}
    arbitration_review(res2, caption="put the cup on the left plate", cam_frames=frames, cam_ts=ts,
                       question_writer=lambda i: {"task_type": "persistent", "target_location": "plate",
                                                  "target_visual": "", "object": "cup",
                                                  "verify_question": "Is the cup on the left plate?"},
                       grounder=lambda img, t, v, o: [(1, 1, 8, 8)], judge=judge, same_task=None,
                       cam_hints={"image": "FRONT-HINT"})
    assert any("FRONT-HINT" in q for q in seen) and any("FRONT-HINT" not in q for q in seen)
