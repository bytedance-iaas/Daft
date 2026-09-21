"""The merge executor on the example modules: merge, split, fallback, receipts (04 §4.2)."""
from __future__ import annotations

import json

import pytest

from curation.contracts import schemas
from curation.planner import (MergeExecutor, MergeLimits, MergeReceipts, NoMerge, RetryPolicy,
                              RetryStats, UsageLedger, VlmRequest, VlmResponse, VlmTransportError,
                              strategy_for_stage)
from curation.planner.merge import compose_merged_prompt, estimate_tokens

from . import examples as X


def executor(fake, **kw):
    kw.setdefault("frames", X.frames)
    kw.setdefault("model", "fake-vlm")
    return MergeExecutor(fake, **kw)


def answers(run):
    return {o.unit.key: (o.result.passed if o.ok else "error") for o in run.outcomes}


def truths(episodes):
    return {(ep, spec.id, ""): X.VOTES[X.FakeVlm.truth(ep, q)]
            for ep in episodes for spec, q in ((X.EXAMPLE_GRASP, "grasp"), (X.EXAMPLE_TABLE, "table"))}


# ---------------------------------------------------------------- merging

def test_two_modules_share_one_request_per_episode():
    fake = X.FakeVlm()
    run = executor(fake).run(X.units_for(range(4)))
    assert len(fake.requests) == 4
    assert all(r.call_kind == "merged" and r.modules == ("example_grasp", "example_table")
               and r.keys == ("task_1", "task_2") for r in fake.requests)
    assert all(len(r.images) == 24 for r in fake.requests)         # 3 cameras x 8 frames, sent once
    assert all(r.max_tokens == 32 for r in fake.requests)          # 16 + 16
    assert {o.receipt for o in run.outcomes} == {"merged"}
    assert answers(run) == truths(range(4))
    grasp = run.by_key()[(2, "example_grasp", "")]
    assert grasp.answer in ("yes", "no", "unclear") and grasp.requests == 1


def test_single_mode_sends_each_module_its_own_prompt():
    fake = X.FakeVlm()
    run = executor(fake, enabled=False).run(X.units_for(range(3)))
    assert len(fake.requests) == 6
    assert {r.text for r in fake.requests} == {X.GRASP_PROMPT, X.TABLE_PROMPT}
    assert all(r.call_kind == "caption" and not r.keys for r in fake.requests)
    assert {o.receipt for o in run.outcomes} == {"single"}
    assert answers(run) == truths(range(3))


def test_nearly_equal_policies_are_decoded_and_sent_apart():
    from curation.planner import FramePolicy, MergeUnit

    near = FramePolicy("interval", 0.5000001, 448, 4, "linspace:8")
    grasp = X.EXAMPLE_GRASP.merge_units(0)[0]
    table = MergeUnit(0, "example_table", near, X.TABLE_PROMPT, parser=grasp.parser,
                      call_kind="caption")
    calls = []

    def frames(ep, policy):
        calls.append(policy)
        return X.frames(ep, policy)

    fake = X.FakeVlm()
    run = executor(fake, frames=frames).run([grasp, table])
    assert calls == [X.FRAME_POLICY, near] and len(fake.requests) == 2
    assert {o.receipt for o in run.outcomes} == {"single"}


def test_frames_are_decoded_once_per_episode_and_policy():
    calls = []

    def counting(ep, policy):
        calls.append(ep)
        return X.frames(ep, policy)

    fake = X.FakeVlm(garble={(1, "table")})
    executor(fake, frames=counting).run(X.units_for(range(3)))
    assert sorted(calls) == [0, 1, 2]                             # the fallback reused episode 1's frames
    assert len(fake.requests) == 4


# ---------------------------------------------------------------- single-unit fallback

def test_a_part_that_does_not_parse_falls_back_alone():
    fake = X.FakeVlm(garble={(1, "table")})
    run = executor(fake).run(X.units_for(range(3)))
    assert len(fake.requests) == 4                                # 3 merged + 1 single, no pack retry
    fallback = [r for r in fake.requests if r.call_kind != "merged"]
    assert [(r.episode_index, r.modules, r.text) for r in fallback] == \
        [(1, ("example_table",), X.TABLE_PROMPT)]
    by_key = run.by_key()
    assert by_key[(1, "example_grasp", "")].receipt == "merged"   # its sibling kept the merged answer
    table = by_key[(1, "example_table", "")]
    assert table.receipt == "fallback" and table.requests == 2
    assert "task_2" in table.fallback_reason and "ValueError" in table.fallback_reason
    assert answers(run) == truths(range(3))


def test_a_missing_key_falls_back_alone():
    fake = X.FakeVlm(drop={(0, "grasp")})
    run = executor(fake).run(X.units_for([0]))
    by_key = run.by_key()
    assert by_key[(0, "example_grasp", "")].receipt == "fallback"
    assert by_key[(0, "example_grasp", "")].fallback_reason == "no task_1 in the answer"
    assert by_key[(0, "example_table", "")].receipt == "merged"
    assert answers(run) == truths([0])


def test_an_answer_that_is_not_json_falls_back_every_unit_once():
    fake = X.FakeVlm(not_json={0})
    run = executor(fake).run(X.units_for([0]))
    assert len(fake.requests) == 3
    assert {o.receipt for o in run.outcomes} == {"fallback"}
    assert answers(run) == truths([0])


def test_a_single_request_that_does_not_parse_is_an_execution_error():
    class Mumbles(X.FakeVlm):
        def __call__(self, request):
            body = super().__call__(request)
            body["choices"][0]["message"]["content"] = "hmm"
            return body

    run = executor(Mumbles(), enabled=False).run(X.units_for([0]))
    for o in run.outcomes:
        assert not o.ok and o.error["kind"] == "execution"
        assert o.error["incidents"][0]["step"] == "parse"
        schemas.validate("cli/common.schema.json#/$defs/execution_error", o.error)


# ---------------------------------------------------------------- splitting

def test_over_limit_group_is_split():
    fake = X.FakeVlm()
    run = executor(fake, limits=MergeLimits(max_units=1)).run(X.units_for(range(2)))
    assert len(fake.requests) == 4
    assert all(not r.keys and r.text in X.QUESTIONS for r in fake.requests)   # pieces of one go out as is
    assert {o.receipt for o in run.outcomes} == {"split"}
    assert answers(run) == truths(range(2))


def test_prompt_budget_splits_three_units_into_two_requests():
    third = X.EXAMPLE_GRASP.merge_units(0)[0]
    extra = type(third)(0, "example_grasp", X.FRAME_POLICY, X.GRASP_PROMPT, parser=third.parser,
                        call_kind="caption", question="again", max_tokens=16)
    units = X.units_for([0]) + [extra]
    budget = estimate_tokens(compose_merged_prompt(units[:2], 32))
    fake = X.FakeVlm()
    run = executor(fake, limits=MergeLimits(max_prompt_tokens=budget)).run(units)
    assert [r.call_kind for r in fake.requests] == ["merged", "caption"]
    assert [o.receipt for o in run.outcomes] == ["split", "split", "split"]
    assert all(o.ok for o in run.outcomes)


# ---------------------------------------------------------------- kill switch and plan stage

def test_kill_switch_turns_merging_off():
    fake = X.FakeVlm()
    run = executor(fake, enabled=False).run(X.units_for(range(2)))
    assert not any(r.call_kind == "merged" for r in fake.requests)
    assert {o.receipt for o in run.outcomes} == {"single"}


def test_executor_follows_the_plan_stage():
    stage = {"merge": {"strategy": "none", "groups": []}}
    fake = X.FakeVlm()
    executor(fake, strategy=strategy_for_stage(stage)).run(X.units_for([0]))
    assert len(fake.requests) == 2
    with pytest.raises(ValueError):
        MergeExecutor(fake, enabled="no")


def test_units_are_checked():
    units = X.units_for([0])
    with pytest.raises(ValueError, match="twice"):
        executor(X.FakeVlm()).run(units + units[:1])
    assert executor(X.FakeVlm()).run([]).outcomes == []

    class Loser:
        name = "loser"

        def group(self, units, limits):
            return []

    with pytest.raises(RuntimeError, match="lost"):
        executor(X.FakeVlm(), strategy=Loser()).run(units)


# ---------------------------------------------------------------- receipts

def test_receipts_count_per_module_and_fit_check_json():
    fake = X.FakeVlm(garble={(1, "table")})
    receipts = MergeReceipts()
    receipts.add(executor(fake).run(X.units_for(range(3))))
    receipts.add(executor(fake, limits=MergeLimits(max_units=1)).run(X.units_for([5])))
    assert receipts.for_module("example_grasp") == {
        "requests": 4, "merged_units": 3, "split_units": 1, "fallback_units": 0}
    assert receipts.for_module("example_table") == {
        "requests": 5, "merged_units": 2, "split_units": 1, "fallback_units": 1}
    assert receipts.for_module("nobody") == {
        "requests": 0, "merged_units": 0, "split_units": 0, "fallback_units": 0}
    doc = {"schema_version": "1.0", "modules": {
        module: {"part": "0001", "input_digest": "sha256:" + "a" * 64,
                 "episodes": {"total": 4, "pass": 1, "fail": 1, "abstain": 2, "scored": 0, "error": 0},
                 "error_episodes": [], "merge": counts}
        for module, counts in receipts.to_json().items()}}
    schemas.validate("cli/check.schema.json", doc)


# ---------------------------------------------------------------- transport failures and the outer retry

def test_rate_limited_requests_are_retried_and_rescued():
    fake = X.FakeVlm(transient={0: 2})
    stats, waits, seen = RetryStats(), [], []
    run = executor(fake, retry=RetryPolicy(max_retries=3), retry_stats=stats, sleep=waits.append,
                   on_attempt=lambda kind, outcome: seen.append(outcome)).run(X.units_for([0, 1]))
    assert answers(run) == truths([0, 1])
    assert waits == [1.0, 2.0]                                    # 1 s, 2 s; Retry-After 0 is shorter
    assert seen == ["rate_limited", "rate_limited", "ok", "ok"]
    assert stats.to_json() == {"calls": 2, "retries": 2, "rescued": 1, "exhausted": 0, "failed": 0}
    assert [s.attempts for s in run.sent] == [3, 1]


def test_exhausted_retries_make_every_unit_of_the_request_an_error():
    fake = X.FakeVlm(transient={0: 9})
    ledger = UsageLedger()
    run = executor(fake, retry=RetryPolicy(max_retries=2), sleep=lambda s: None,
                   ledger=ledger).run(X.units_for([0]))
    for o in run.outcomes:
        assert o.error == {"kind": "execution", "incidents": [
            {"step": "merged", "call_kind": "merged", "cause": "rate_limited", "attempts": 3}]}
        schemas.validate("cli/common.schema.json#/$defs/execution_error", o.error)
    assert ledger.task_totals()["requests_unknown_usage"] == 3 and ledger.balanced()


def test_no_retry_by_default_like_the_cli():
    fake = X.FakeVlm(transient={0: 1})
    run = executor(fake).run(X.units_for([0]))
    assert all(not o.ok for o in run.outcomes) and len(fake.requests) == 1


def test_non_retryable_failures_are_not_retried():
    def refuse(request):
        raise VlmTransportError("401", cause="http_error", status=401)

    run = executor(refuse, retry=RetryPolicy(max_retries=3), sleep=lambda s: None).run(
        X.units_for([0]))
    assert run.sent[0].attempts == 1
    assert run.outcomes[0].error["incidents"][0]["cause"] == "http_error"


def test_send_may_return_a_response_object_or_text():
    def plain(request: VlmRequest):
        return VlmResponse('{"task_1": "yes", "task_2": "no"}', unknown_usage_requests=1)

    ledger = UsageLedger()
    run = executor(plain, ledger=ledger).run(X.units_for([0]))
    assert [o.result.passed for o in run.outcomes] == [True, False]
    assert ledger.task_totals()["requests_unknown_usage"] == 2     # the call and its lost hedge

    run = executor(lambda request: "yes", enabled=False).run(X.units_for([0]))
    assert [o.result.passed for o in run.outcomes] == [True, True]

    def weird(request):
        return 42

    run = executor(weird).run(X.units_for([0]))
    assert run.outcomes[0].error["incidents"][0]["cause"] == "error"


def test_a_new_strategy_needs_no_executor_change():
    """04 §4.4: cross-episode batching is one more MergeStrategy; the executor runs it as is."""
    from curation.planner import MergeGroup

    class CrossEpisodePairs:
        name = "cross_episode_pairs"

        def group(self, units, limits):
            pairs: dict[int, list] = {}
            for u in units:
                pairs.setdefault(u.episode_index // 2, []).append(u)
            return [MergeGroup(tuple(p)) for p in pairs.values()]

        def compose(self, units, n_images):
            episodes = ",".join(str(e) for e in dict.fromkeys(u.episode_index for u in units))
            return f"EPISODES {episodes} ({n_images} frames)\n" + "\n".join(
                f"task_{i}: episode {u.episode_index}, {u.module_id}" for i, u in enumerate(units, 1))

    seen = []

    def send(request):
        seen.append(request)
        return {"choices": [{"message": {"content": json.dumps({k: "yes" for k in request.keys})}}]}

    run = executor(send, strategy=CrossEpisodePairs()).run(X.units_for(range(4)))
    assert [r.episodes for r in seen] == [(0, 1), (2, 3)]
    assert len(seen[0].images) == 48 and seen[0].text.startswith("EPISODES 0,1 (48 frames)")
    assert all(o.ok and o.receipt == "merged" and o.result.passed for o in run.outcomes)


def test_merged_request_carries_nothing_but_verbatim_prompts():
    fake = X.FakeVlm()
    executor(fake).run(X.units_for([0]))
    text = fake.requests[0].text
    for prompt in (X.GRASP_PROMPT, X.TABLE_PROMPT):
        assert text.count(prompt) == 1
    assert isinstance(NoMerge().name, str)
