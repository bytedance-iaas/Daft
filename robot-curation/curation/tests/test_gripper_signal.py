"""仲裁取证的夹爪信号来源(2026-09-18 umi ep3):规格库优先,本体未注册退到数据集档案随行的
semantics_extras["gripper"](含极性);都没有 → None(core 侧自选兜底帧)。"""
from __future__ import annotations

import json

import numpy as np

from curation.pipeline.funnel import gripper_signal


class _Prof:
    def __init__(self, dims):
        self.gripper_dims = dims


class _Reg:
    def __init__(self, known):
        self.known = known

    def get(self, eid):
        if eid in self.known:
            return _Prof(self.known[eid])
        raise KeyError(eid)


A = np.arange(40.0).reshape(4, 10)
TS = np.array([0.0, 0.1, 0.2, 0.3])
UMI = json.dumps({"gripper": {"dims": [9, 19], "closed": "low"}, "cameras": {}})


def test_registry_wins_and_keeps_droid_polarity():
    g, ts, closed_high, src = gripper_signal(A, TS, "franka", UMI, _Reg({"franka": (6,)}))
    assert g.shape == (4, 1) and np.array_equal(g[:, 0], A[:, 6])
    assert ts.tolist() == TS.tolist() and closed_high is True and src == "registry"


def test_unregistered_embodiment_falls_back_to_profile_with_polarity():
    a = np.arange(80.0).reshape(4, 20)
    g, ts, closed_high, src = gripper_signal(a, TS, "umi_dual_handheld_gripper", UMI, _Reg({}))
    assert g.shape == (4, 2) and np.array_equal(g, a[:, [9, 19]])   # 两列原样给出,仲裁自己选段
    assert closed_high is False and src == "profile"


def test_profile_dims_beyond_action_width_are_dropped():
    """档案列下标超出 action 宽度(数据集 10 维却声明 [9, 19]):只保留够得着的列。"""
    g, _, closed_high, src = gripper_signal(A, TS, "umi_dual_handheld_gripper", UMI, _Reg({}))
    assert g.shape == (4, 1) and np.array_equal(g[:, 0], A[:, 9]) and src == "profile"


def test_nothing_declared_returns_none():
    for extras in ("", "{}", "not json", json.dumps({"gripper": {"dims": []}})):
        assert gripper_signal(A, TS, "nope", extras, _Reg({})) == (None, None, True, "")
    assert gripper_signal(None, TS, "nope", UMI, _Reg({})) == (None, None, True, "")


def test_layout_only_extras_are_the_last_fallback():
    """只带 layout 没带 gripper 段的行(老缓存/别的读取器):退到布局识别的夹爪列,极性随布局。"""
    ex = json.dumps({"layout": {"gripper_dims": [6], "gripper_closed": "low"}})
    g, ts, closed_high, src = gripper_signal(A, TS, "nope", ex, _Reg({}))
    assert np.array_equal(g[:, 0], A[:, 6]) and closed_high is False and src == "layout"
    ex2 = json.dumps({"layout": {"gripper_dims": [6]}, "gripper": {"dims": [3], "closed": "high"}})
    g2, _, ch2, src2 = gripper_signal(A, TS, "nope", ex2, _Reg({}))
    assert np.array_equal(g2[:, 0], A[:, 3]) and ch2 is True and src2 == "profile"   # gripper 段优先
