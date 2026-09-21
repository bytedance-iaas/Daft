"""Usage accounting: parsing, the two ledgers, exact attribution, C3 lines (04 §5, 01 §2.6)."""
from __future__ import annotations

import random

import pytest

from curation.contracts import schemas
from curation.planner import (MergeExecutor, MergeLimits, Usage, UsageAccumulator, UsageLedger,
                              apportion, parse_usage, usage_from_response)
from curation.planner.usage import FIELDS

from . import examples as X

FULL = {"prompt_tokens": 1820, "completion_tokens": 64,
        "completion_tokens_details": {"reasoning_tokens": 40},
        "prompt_tokens_details": {"cached_tokens": 1536}, "total_tokens": 1884}


# ---------------------------------------------------------------- parsing

def test_parse_openai_usage():
    assert parse_usage(FULL) == Usage(1820, 64, 40, 1536)
    assert parse_usage({"prompt_tokens": 10, "completion_tokens": 2}) == Usage(10, 2, 0, 0)
    assert parse_usage({"prompt_tokens": 10, "completion_tokens": 2,
                        "completion_tokens_details": None,
                        "prompt_tokens_details": {"cached_tokens": None}}) == Usage(10, 2, 0, 0)
    assert parse_usage({"prompt_tokens": 10.0, "completion_tokens": 2}) == Usage(10, 2, 0, 0)
    assert usage_from_response({"choices": [], "usage": FULL}) == Usage(1820, 64, 40, 1536)


@pytest.mark.parametrize("bad", [None, {}, {"prompt_tokens": 10}, {"prompt_tokens": -1, "completion_tokens": 2},
                                 {"prompt_tokens": "10", "completion_tokens": 2},
                                 {"prompt_tokens": True, "completion_tokens": 2},
                                 {"prompt_tokens": 1.5, "completion_tokens": 2}, [1, 2]])
def test_unusable_usage_is_unknown_not_estimated(bad):
    assert parse_usage(bad) is None


def test_response_without_usage():
    assert usage_from_response({"choices": []}) is None
    assert usage_from_response("text") is None


# ---------------------------------------------------------------- apportion

def test_apportion_is_exact_and_deterministic():
    rng = random.Random(7)
    for _ in range(2000):
        n = rng.randint(1, 6)
        weights = [rng.randint(0, 5000) for _ in range(n)]
        total = rng.randint(0, 100000)
        shares = apportion(total, weights)
        assert sum(shares) == total and shares == apportion(total, weights)
        whole = sum(weights) or n
        for share, w in zip(shares, weights if sum(weights) else [1] * n):
            assert abs(share - total * w / whole) < 1                  # never off by a whole token


def test_apportion_edges():
    assert apportion(10, [0, 0]) == [5, 5]
    assert apportion(1, [3, 3]) == [1, 0]                          # ties go to the earlier weight
    assert apportion(1, [2, 3]) == [0, 1]
    assert apportion(7, [1, 0]) == [7, 0]
    assert apportion(0, [1, 2]) == [0, 0]
    for bad in ((1, []), (-1, [1]), (1, [-1]), (1.5, [1]), (1, [0.5])):
        with pytest.raises(ValueError):
            apportion(*bad)


# ---------------------------------------------------------------- the two ledgers

def test_single_request_is_identical_in_both_ledgers():
    lines = UsageLedger(clock=lambda: 1758300001.0).record_single(
        module="task_success", call_kind="endstate", model="doubao", response={"usage": FULL})
    assert [(l["ledger"], l["module"]) for l in lines] == [("actual", "task_success"),
                                                           ("attributed", "task_success")]
    assert lines[0] == {"ts": 1758300001000, "kind": "usage", "ledger": "actual", "model": "doubao",
                        "module": "task_success", "call_kind": "endstate", "requests": 1,
                        "requests_unknown_usage": 0, "prompt_tokens": 1820, "completion_tokens": 64,
                        "reasoning_tokens": 40, "cached_tokens": 1536}
    assert {k: v for k, v in lines[1].items() if k != "ledger"} == \
        {k: v for k, v in lines[0].items() if k != "ledger"}
    for line in lines:
        schemas.validate("progress.schema.json", line)


def test_merged_request_is_one_actual_row_and_split_by_share():
    ledger = UsageLedger()
    lines = ledger.record(model="m", call_kind="merged", usage=Usage(1001, 99, 10, 500),
                          shares={"example_table": (300, 30), "example_grasp": (100, 60)})
    actual = [l for l in lines if l["ledger"] == "actual"]
    attributed = {l["module"]: l for l in lines if l["ledger"] == "attributed"}
    assert len(actual) == 1 and actual[0]["module"] == "example_grasp+example_table"
    assert actual[0]["call_kind"] == "merged" and actual[0]["requests"] == 1
    assert attributed["example_grasp"]["prompt_tokens"] == 250       # 100 / 400 of 1001, rounded
    assert attributed["example_table"]["prompt_tokens"] == 751
    assert attributed["example_grasp"]["completion_tokens"] == 66    # 60 / 90 of 99
    assert attributed["example_table"]["completion_tokens"] == 33
    assert attributed["example_grasp"]["cached_tokens"] + attributed["example_table"]["cached_tokens"] == 500
    assert attributed["example_table"]["requests"] == 1 and attributed["example_grasp"]["requests"] == 0
    for name in FIELDS:
        assert sum(l[name] for l in attributed.values()) == actual[0][name]
    assert ledger.balanced()
    for line in lines:
        schemas.validate("progress.schema.json", line)


def test_request_counts_are_credited_fairly_and_deterministically():
    """A merged request cannot be split; over many requests each module gets its share."""
    def credit(order):
        ledger = UsageLedger()
        for i in order:
            ledger.record(model="m", call_kind="merged", usage=Usage(100, 10),
                          shares={"a": (48, 1), "b": (52, 1)}, seed=f"ep{i}")
        return {r["module"]: r["requests"] for r in ledger.rows("attributed")}, ledger

    forward, ledger = credit(range(400))
    backward, _ = credit(reversed(range(400)))
    assert forward == backward                                     # order does not matter
    assert forward["a"] + forward["b"] == 400 and 150 <= forward["a"] <= 234
    assert ledger.balanced()
    # without a seed the largest remainder decides, and the bigger share always wins
    lines = UsageLedger().record(model="m", call_kind="merged", usage=Usage(100, 10),
                                 shares={"a": (48, 1), "b": (52, 1)})
    assert {l["module"]: l["requests"] for l in lines if l["ledger"] == "attributed"} == {"a": 0, "b": 1}


def test_draw_edges():
    from curation.planner.usage import draw

    assert draw(3, [0, 0], "s") in ([3, 0], [2, 1], [1, 2], [0, 3])
    assert draw(5, [1, 0], "s") == [5, 0]
    assert draw(0, [1, 2], "s") == [0, 0]
    assert draw(7, [2, 5], "x") == draw(7, [2, 5], "x")
    with pytest.raises(ValueError):
        draw(1, [], "s")


def test_answer_without_segments_falls_back_to_prompt_share():
    lines = UsageLedger().record(model="m", call_kind="merged", usage=Usage(100, 10, 0, 0),
                                 shares={"a": (1, 0), "b": (3, 0)})
    by = {l["module"]: l for l in lines if l["ledger"] == "attributed"}
    assert (by["a"]["completion_tokens"], by["b"]["completion_tokens"]) == (3, 7)   # 10 split 1:3, rounded


def test_unknown_usage_is_counted_not_estimated():
    ledger = UsageLedger()
    ledger.record(model="m", call_kind="probe", shares={"task_success": (1, 1)}, usage=None, count=3)
    totals = ledger.task_totals()
    assert totals["requests_unknown_usage"] == 3 and totals["requests"] == 0
    assert all(totals[k] == 0 for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                                        "cached_tokens"))
    with pytest.raises(ValueError):
        ledger.record(model="m", call_kind="probe", shares={"a": (1, 1)}, usage=Usage(1, 1), count=2)


@pytest.mark.parametrize("kw", [dict(call_kind="vision"), dict(shares={}),
                                dict(shares={"a": (1, 1), "b": (1, 1)}), dict(count=0),
                                dict(model=None)])
def test_record_rejects_bad_input(kw):
    args = dict(model="m", call_kind="probe", shares={"a": (1, 1)}, usage=None)
    args.update(kw)
    with pytest.raises(ValueError):
        UsageLedger().record(**args)


def test_task_totals_come_from_the_actual_ledger_only():
    acc = UsageAccumulator()
    acc.add({"kind": "usage", "ledger": "actual", "model": "m", "module": "a+b", "call_kind": "merged",
             "requests": 1, "prompt_tokens": 100, "completion_tokens": 10, "reasoning_tokens": 0,
             "cached_tokens": 0})
    for module, p in (("a", 40), ("b", 60)):
        acc.add({"kind": "usage", "ledger": "attributed", "model": "m", "module": module,
                 "call_kind": "merged", "requests": 1 if module == "b" else 0, "prompt_tokens": p,
                 "completion_tokens": 5, "reasoning_tokens": 0, "cached_tokens": 0})
    acc.add({"kind": "usage", "model": "m", "module": "c", "call_kind": "caption", "requests": 1,
             "prompt_tokens": 7, "completion_tokens": 1, "reasoning_tokens": 0, "cached_tokens": 0})
    assert acc.task_totals()["prompt_tokens"] == 107                # never 207: no double count
    assert acc.totals("attributed")["prompt_tokens"] == 100
    assert acc.totals("actual", module="c")["requests"] == 1        # a line without ledger is actual
    with pytest.raises(ValueError):
        acc.add({"kind": "progress", "stage": "x", "done": 1, "total": 2})
    with pytest.raises(ValueError):
        acc.add({"kind": "usage", "ledger": "estimated", "model": "m", "module": "c",
                 "call_kind": "caption"})


# ---------------------------------------------------------------- end to end on the example modules

def test_attributed_ledger_sums_to_actual_on_the_example_modules():
    """Acceptance: merged, split, fallback and usage-less requests; the ledgers balance exactly."""
    emitted = []
    ledger = UsageLedger(emit=emitted.append)
    fake = X.FakeVlm(garble={(3, "table"), (9, "grasp")}, drop={(12, "grasp")}, not_json={20},
                     no_usage=lambda r: r.episode_index % 11 == 5)
    units = X.units_for(range(40))
    MergeExecutor(fake, frames=X.frames, model="fake-vlm", ledger=ledger).run(units)
    MergeExecutor(fake, frames=X.frames, model="fake-vlm", ledger=ledger,
                  limits=MergeLimits(max_units=1)).run(X.units_for(range(40, 44)))
    assert ledger.balanced()
    actual, attributed = ledger.totals("actual"), ledger.totals("attributed")
    assert actual == attributed
    sent = len(fake.requests)
    assert actual["requests"] + actual["requests_unknown_usage"] == sent
    assert actual["requests_unknown_usage"] == sum(1 for r in fake.requests if r.episode_index % 11 == 5)
    # task totals are the sum of the actual lines, never both ledgers
    expected = {name: sum(l[name] for l in emitted if l["ledger"] == "actual") for name in FIELDS}
    assert ledger.task_totals() == expected
    both = {name: sum(l[name] for l in emitted) for name in FIELDS}
    assert both["prompt_tokens"] == 2 * expected["prompt_tokens"]
    merged_rows = [r for r in ledger.rows("actual") if r["call_kind"] == "merged"]
    assert [r["module"] for r in merged_rows] == ["example_grasp+example_table"]
    assert {r["module"] for r in ledger.rows("attributed")} == {"example_grasp", "example_table"}
    for line in emitted:
        schemas.validate("progress.schema.json", line)
