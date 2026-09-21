"""任务类型判定(方案 2,2026-09-18):规则优先、出题器兜底、异常按持久。"""
from curation.core.task_type import PERSISTENT, TRANSIENT, classify_by_rule, resolve


def test_rule_transient_vs_persistent():
    assert classify_by_rule("take the gray box out of the drawer") == TRANSIENT
    assert classify_by_rule("pick up the spray bottle") == TRANSIENT
    assert classify_by_rule("lift the cup") == TRANSIENT
    assert classify_by_rule("remove the lid from the jar") == TRANSIENT
    # 开头是拿起但后面有放置线索 → 持久("pick up X and put it in Y")
    assert classify_by_rule("pick up the marker and put it in the pot") == PERSISTENT
    assert classify_by_rule("take the marker out of the mug onto the table") == PERSISTENT
    assert classify_by_rule("put the box into the drawer") == PERSISTENT
    assert classify_by_rule("fold the white shirt") == PERSISTENT
    assert classify_by_rule("pour water from the bottle into the cup") == PERSISTENT
    assert classify_by_rule("open the drawer") == PERSISTENT
    assert classify_by_rule("Tidy up the books") == PERSISTENT
    assert classify_by_rule("") is None and classify_by_rule("unclear") is None


def test_resolve_uses_writer_only_when_rule_is_silent():
    calls = []

    def writer(intent):
        calls.append(intent)
        return {"task_type": "transient", "target_location": "table",
                "verify_question": "Is it lifted?"}
    assert resolve("pick up the cup", writer) == (TRANSIENT, None, "rule")
    assert calls == []                                  # 规则判得出就不烧出题器
    tt, spec, src = resolve("wiggle the thing", writer)
    assert tt == TRANSIENT and src == "writer" and spec["verify_question"] == "Is it lifted?"
    assert calls == ["wiggle the thing"]


def test_resolve_defaults_to_persistent_on_failure():
    def bad(intent):
        raise RuntimeError("boom")
    assert resolve("wiggle the thing", bad) == (PERSISTENT, None, "default")
    assert resolve("wiggle the thing", None) == (PERSISTENT, None, "default")

    def odd(intent):
        return {"task_type": "weird", "target_location": "x", "verify_question": "y"}
    assert resolve("wiggle the thing", odd)[0] == PERSISTENT
