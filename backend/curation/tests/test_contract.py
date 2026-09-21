"""P0 契约层验收:注册机制玩具用例(register → get → 跑通;重复注册报错)。"""
from __future__ import annotations

import numpy as np
import pytest

from curation.core.contract import (
    CheckResult,
    Episode,
    all_checks,
    clear_registry,
    get_check,
    register_check,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _toy_episode() -> Episode:
    return Episode(
        episode_id="ep000",
        embodiment_id="toy_arm",
        action=np.zeros((10, 7), dtype=np.float32),
    )


def test_register_and_run_toy_check():
    def action_present(ep: Episode) -> CheckResult:
        ok = ep.action is not None and len(ep.action) > 0
        return CheckResult(name="action_present", passed=ok)

    spec = register_check(action_present, name="action_present", gate="hard", needs=("action",))
    assert spec.gate == "hard" and spec.gpus == 0.0

    result = get_check("action_present").fn(_toy_episode())
    assert result.passed is True


def test_soft_check_returns_score():
    def constant_score(ep: Episode) -> CheckResult:
        return CheckResult(name="constant", score=0.5)

    register_check(constant_score, name="constant")  # 默认 soft
    r = get_check("constant").fn(_toy_episode())
    assert r.score == 0.5 and r.passed is None


def test_duplicate_registration_rejected():
    fn = lambda ep: CheckResult(name="x", score=1.0)  # noqa: E731
    register_check(fn, name="x")
    with pytest.raises(ValueError, match="已注册"):
        register_check(fn, name="x")


def test_unknown_check_rejected():
    with pytest.raises(KeyError):
        get_check("不存在的检查")


def test_invalid_gate_rejected():
    fn = lambda ep: CheckResult(name="y")  # noqa: E731
    with pytest.raises(ValueError):
        register_check(fn, name="y", gate="medium")  # type: ignore[arg-type]


def test_all_checks_snapshot_is_copy():
    fn = lambda ep: CheckResult(name="z")  # noqa: E731
    register_check(fn, name="z")
    snapshot = all_checks()
    snapshot.clear()
    assert "z" in all_checks(), "all_checks() 应返回副本,外部改动不能污染注册表"
