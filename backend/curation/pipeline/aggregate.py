"""The verdicts of a run directory: pure computation, recomputed in full (doc 02 §3.6, 06 §3, 17 §4).

**Policy verdicts** (C2 2.0, design doc 17 §4, D58). Modules only report findings; the task's policy
(``run.json``'s ``policy``, :mod:`.policy`) grades every finding blocking / review / info and
:mod:`.verdicts` decides each episode: a blocking finding drops it (every one of them is a reason; a module
that failed on it is still named, D35), else a module it needs that failed or has no record holds it, else
it is kept and its review findings go to their review lines. The default policy is today's gates (P18):
only the soft-score rejects are gone.

**Every module judges every episode** (D57): the two blocks run side by side and no stage stops an
episode, so an episode needs a judgement of every selected module (:func:`expected`) - one without is held.

**Funnel phase** - the machine's verdicts of the per-episode modules (``verdicts.jsonl``, 2.0 lines) and
``keep.txt``, the episodes kept after the applied human decisions (a funnel run's input of dedup and
skill_profile; the Daemon's two-block runs go straight to the final phase).

**Final phase** - adds dedup, skill_profile and the applied human decisions (:mod:`.adjudication`):
a discard wins over everything; a person's answer is the conclusion of the findings it answers; an appeal
lifts the blocking of appealable findings when every blocking finding of the episode is one (D42 by
finding); a relabel not judged again yet holds the episode. Dedup reports its groups; here, after the human
decisions, each group keeps its first member not rejected for another reason, and an episode a person
brought into the delivery is never deduplicated (§4.5). Writes ``passed`` / ``reject`` / ``held``
(disjoint, complete) and the ``review`` view (2.0) plus the frozen ``policy.json`` into
``revisions/r<NNNN>/``.

A run directory with 1.0 records was made by an earlier version (D59): it is read as it is, never
aggregated again (:class:`LegacyRun`).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..contracts import modules as registry
from . import policy as policy_mod
from .adjudication import Decisions, judged_with
from .records import (is_error, is_v2, latest_results, revision_dir, write_json_atomic,
                      write_text_atomic)
from .tasktext import TaskText, load_autolabel
from .verdicts import Graded, Verdict, also_failed, judge, name_of

#: the stages whose modules judge one episode at a time (the full-set stages, dedup and profile, judge them all)
EPISODE_STAGES = ("integrity", "numeric", "frame", "vlm")
#: the full-set modules (dedup, skill_profile): one run over the whole selection (a funnel run's: over its kept episodes)
FULL_SET = tuple(m.id for m in registry.MODULES if m.stage in registry.FULL_SET_STAGES)
NAMES_CN = {m.id: m.name_zh for m in registry.MODULES} | {"autolabel": "无标注补描述"}
DEDUP = "dedup"
PROFILE = "skill_profile"
TASK = "task_success"
#: the order of one episode's questions on its card (v1's, then v2's own lines as they came)
LINE_ORDER = ("task_verdict", "eef_check", "integrity_check", "label", "reject_appeal")


class LegacyRun(RuntimeError):
    """The run directory holds records of C2 1.0: a task made before (D59), read only."""


@dataclass
class RunState:
    """The selected modules' current results, read once, and the policy that grades them."""

    run_dir: str
    modules: list[str]
    episodes: list[int]
    policy: policy_mod.Policy | None = None
    results: dict[str, dict[int, dict]] | None = None
    autolabel: dict | None = None

    def __post_init__(self) -> None:
        chosen = set(registry.with_riders(self.modules))
        self.modules = [m.id for m in registry.MODULES if m.id in chosen]
        self.episodes = sorted({int(e) for e in self.episodes})
        if self.policy is None:
            self.policy = policy_mod.load(self.run_dir)
        if self.results is None:
            self.results = {m: latest_results(self.run_dir, m) for m in self.modules}
        if self.autolabel is None:
            self.autolabel = load_autolabel(self.run_dir)
        for m, recs in self.results.items():
            if any(not is_v2(r) for r in recs.values()):
                raise LegacyRun(f"{self.run_dir}: {m} has result records of C2 1.0 - a task made by an earlier "
                                f"version is read as it is and never aggregated again (D59)")

    def records(self, ep: int) -> dict[str, dict | None]:
        return {m: self.results[m].get(ep) for m in self.modules}


def expected(state: RunState, ep: int, recs: dict[str, dict | None]) -> list[str]:
    """The per-episode modules ``ep`` needs a judgement of: every selected one (D57, D58) - a rider is answered
    in its host's requests and never holds an episode of its own; the full-set modules are added for a kept
    episode (:func:`decide`)."""
    return [m for m in state.modules
            if registry.get(m).stage in EPISODE_STAGES and not registry.get(m).rides_on]


def _without(recs: dict[str, dict | None], module: str) -> dict[str, dict | None]:
    return {m: r for m, r in recs.items() if m != module}


def machine(state: RunState, ep: int) -> Verdict:
    """The machine's verdict of the funnel modules (no human decisions, no dedup / profile)."""
    recs = _without(_without(state.records(ep), DEDUP), PROFILE)
    return judge(ep, recs, expected(state, ep, recs), state.policy)


def task_passed(rec: dict | None) -> bool | None:
    """task_success's tri-state from its findings (the label audit's task line)."""
    if rec is None or is_error(rec):
        return None
    codes = {f.get("code") for f in rec.get("findings") or []}
    if "failure" in codes:
        return False
    if codes & {"uncertain", "label_conflict_suspect"}:
        return None
    return True


@dataclass
class Decided:
    """One episode after the applied human decisions."""

    machine: Verdict
    verdict: Verdict
    discard: dict | None = None
    restored: bool = False                       # a person brought a reject into the delivery
    admissible: bool = False                     # its reject may be appealed (D42 by finding)
    pending_relabel: bool = False                # relabelled, not judged with the new label yet

    @property
    def state(self) -> str:
        if self.discard is not None:
            return "drop"
        if self.pending_relabel and self.verdict.verdict == "keep":
            return "held"
        return self.verdict.verdict

    @property
    def kept(self) -> bool:
        return self.state == "keep"


def answers_of(decisions: Decisions, ep: int) -> dict[str, str | None]:
    return {"task_verdict": decisions.human_task_verdict(ep),
            "integrity_check": decisions.human_gate(ep, "integrity_check", ("intact", "broken")),
            "eef_check": decisions.human_gate(ep, "eef_check", ("consistent", "inconsistent"))}


def _strip_duplicate(rec: dict | None) -> dict | None:
    """dedup's record of the member a group keeps: its duplicate finding no longer applies (§4.5)."""
    if rec is None or is_error(rec):
        return rec
    return {**rec, "findings": [f for f in rec.get("findings") or [] if f.get("code") != "duplicate"]}


def decide(state: RunState, ep: int, decisions: Decisions, *, phase: str = "final",
           keeper: dict[int, bool] | None = None) -> Decided:
    """``ep`` after the human decisions. ``phase``: ``funnel`` judges the funnel modules only; ``final`` adds
    dedup (``keeper[ep]``: this member is the one its group keeps, so its duplicate finding is dropped) and
    skill_profile. A kept episode a person did not bring in needs a judgement of both."""
    recs = state.records(ep)
    funnel_recs = _without(_without(recs, DEDUP), PROFILE)
    need = expected(state, ep, funnel_recs)
    mach = judge(ep, funnel_recs, need, state.policy)
    answers = answers_of(decisions, ep)
    use = dict(funnel_recs)
    if phase == "final":
        if DEDUP in state.modules:
            use[DEDUP] = _strip_duplicate(recs.get(DEDUP)) if (keeper or {}).get(ep) else recs.get(DEDUP)
        if PROFILE in state.modules:
            use[PROFILE] = recs.get(PROFILE)
    base = judge(ep, use, need, state.policy, answers=answers)
    discard = decisions.discarded(ep)
    admissible = discard is None and base.appeal_admissible()
    appeal = decisions.appeal(ep) if admissible else None
    verdict = judge(ep, use, need, state.policy, answers=answers, appeal=appeal)
    restored = verdict.verdict == "keep" and (mach.verdict == "drop" or base.verdict == "drop") \
        and (answers.get("task_verdict") == "success" or appeal == "restore")
    if phase == "final" and verdict.verdict == "keep":
        extra = [m for m in (DEDUP, PROFILE) if m in state.modules and not (m == DEDUP and restored)]
        if extra:
            verdict = judge(ep, use, need + extra, state.policy, answers=answers, appeal=appeal)
    relabel = decisions.relabel(ep)
    ts = recs.get(TASK)
    pending = bool(relabel and answers.get("task_verdict") is None and TASK in state.modules
                   and ts is not None and not is_error(ts) and not judged_with(ts, relabel))
    return Decided(mach, verdict, discard, restored, admissible, pending)


def dedup_groups(state: RunState) -> dict[int, list[int]]:
    """group id -> its members in dedup's traversal order (ascending), from dedup's findings."""
    groups: dict[int, set[int]] = {}
    for ep, rec in (state.results.get(DEDUP) or {}).items():
        for f in (rec or {}).get("findings") or []:
            if f.get("code") == "duplicate":
                gid = int((f.get("readings") or {}).get("group_id", ep))
                groups.setdefault(gid, {gid}).add(int(ep))
    return {g: sorted(m) for g, m in groups.items()}


def decide_all(state: RunState, decisions: Decisions, *, phase: str = "final") -> dict[int, Decided]:
    """Every episode after the human decisions; in the final phase each dedup group keeps its first member
    not rejected for another reason (§4.5), the others stay duplicates unless a person brought them in."""
    if phase != "final" or DEDUP not in state.modules:
        return {e: decide(state, e, decisions, phase=phase) for e in state.episodes}
    groups = dedup_groups(state)
    keeper: dict[int, bool] = {}
    if groups:
        # every member counts, also one outside ``state.episodes`` (adjudicate-apply asks about a few)
        alone = {e: decide(state, e, decisions, phase="funnel")
                 for e in sorted({m for members in groups.values() for m in members})}
        for members in groups.values():
            first = next((m for m in members if alone[m].state != "drop"), None)
            for m in members:
                keeper[m] = m == first
    return {e: decide(state, e, decisions, phase=phase, keeper=keeper) for e in state.episodes}


# ---------------------------------------------------------------- the lists

def _reason(g: Graded) -> dict:
    kind = "human" if g.human else ("duplicate" if g.module == DEDUP else "finding")
    out = {"module": g.module, "kind": kind, "code": g.code, "item": g.item, "appealable": bool(g.appealable and not g.human),
           "text": str(g.finding.get("message_zh") or g.code)}
    if g.module == DEDUP:
        dup = (g.finding.get("readings") or {}).get("duplicate_of")
        if dup is not None:
            out["duplicate_of"] = int(dup)
    return out


def _error_reasons(v: Verdict, *, rejected: bool) -> list[dict]:
    tail = "，不影响结论" if rejected else ""
    return [{"module": m, "kind": "execution_error",
             "text": f"「{name_of(m)}」执行出错（{v.error_detail.get(m, '')}）{tail}"} for m in v.error_modules]


def reasons_of(d: Decided) -> list[dict]:
    if d.discard is not None:
        return [{"module": PROFILE if d.discard["line"] == "label" else TASK, "kind": "human", "text": "人工裁决弃用"}]
    v = d.verdict
    if d.state == "drop":
        return [_reason(g) for g in v.blocking] + _error_reasons(v, rejected=True)
    if d.state == "held":
        out = _error_reasons(v, rejected=False)
        if d.pending_relabel:
            out.append({"module": TASK, "kind": "execution_error", "text": "改标后尚未按新标注重跑任务成败判定"})
        return out
    return []


def merged_label_audit(state: RunState, profile_audit: dict | None) -> dict | None:
    """v1's label-conflict queue: skill_profile's audit with the kill guard's holds from task_success merged
    in front, each entry tagged with the task line (``dataset_level.audit``)."""
    from ..dataset_level.audit import attach_task_context, guard_hold_entries, merge_guard_holds

    task_detail, task_of = {}, {}
    for ep, rec in (state.results.get(TASK) or {}).items():
        if is_error(rec):
            continue
        d = rec.get("details") or {}
        eid = f"ep{int(ep):06d}"
        task_detail[eid] = d
        task_of[eid] = {"passed": task_passed(rec), "verdict": d.get("verdict", "")}
    audit = profile_audit if profile_audit else None
    holds = guard_hold_entries(task_detail)
    if holds:
        audit = merge_guard_holds(audit, holds)
    if audit is None:
        return None
    return attach_task_context(audit, task_of)


def _audit_entries(audit: dict | None) -> dict[int, list[tuple[str, dict]]]:
    out: dict[int, list[tuple[str, dict]]] = {}
    for tier in ("high", "mid_for_review", "low_caption_unstable"):
        for entry in (audit or {}).get(tier) or []:
            try:
                idx = int(str(entry.get("id")).lstrip("ep"))
            except ValueError:
                continue
            out.setdefault(idx, []).append((tier, entry))
    return out


def review_items(state: RunState, ep: int, d: Decided, decisions: Decisions,
                 audit_entries: list[tuple[str, dict]], reasons: list[dict]) -> list[dict]:
    """What a person is asked about one episode (D42, D43): a kept episode's review findings nobody settled,
    one item per line and module (the label line from the merged audit); an admissible reject's appeal."""
    items: list[dict] = []
    if d.state == "keep":
        by_line: dict[tuple[str, str], list[Graded]] = {}
        for g in d.verdict.review:
            by_line.setdefault((g.line or "", g.module), []).append(g)
        for (line, module), gs in by_line.items():
            spec = registry.review_line(line)
            if line == "label":
                continue                          # from the audit entries below
            if line == "task_verdict" and decisions.human_task_verdict(ep) is not None:
                continue
            reason = "；".join(dict.fromkeys(str(g.finding.get("message_zh") or g.code) for g in gs))
            items.append({"source_module": module, "kind": spec.review_kind, "line": line,
                          "codes": [g.code for g in gs], "items": [g.item for g in gs if g.item],
                          "reason": reason or "未注明"})
        label = [g for g in d.verdict.review if g.line == "label"]
        if label and not decisions.label_resolved(ep):
            spec = registry.review_line("label")
            entries = audit_entries or [("", {})]
            for tier, entry in entries:
                src = TASK if entry.get("guard_layer") else next((g.module for g in label), PROFILE)
                mine = [g for g in label if g.module == src] or label
                item = {"source_module": src, "kind": spec.review_kind, "line": "label",
                        "codes": [g.code for g in mine], "items": [g.item for g in mine if g.item],
                        "reason": str(entry.get("reason") or tier or mine[0].finding.get("message_zh") or "标注分歧")}
                if entry.get("priority"):
                    item["priority"] = str(entry["priority"])
                items.append(item)
    if d.state == "drop" and d.discard is None and d.admissible and decisions.appeal(ep) in (None, "unsure") \
            and not d.restored:
        spec = registry.review_line("reject_appeal")
        blocking = d.verdict.blocking
        why = "；".join(dict.fromkeys(str(g.finding.get("message_zh") or g.code) for g in blocking))
        if decisions.pending(ep, "reject_appeal"):
            why = f"{why}（复议拿不准，待定）" if why else "复议拿不准，待定"
        module = blocking[0].module if blocking else TASK
        item = {"source_module": module, "kind": spec.review_kind, "line": spec.id,
                "codes": [g.code for g in blocking], "items": [g.item for g in blocking if g.item],
                "reason": why or "可复议"}
        dup = next((r.get("duplicate_of") for r in reasons if r.get("duplicate_of") is not None), None)
        if dup is not None:
            item["duplicate_of"] = int(dup)
        items.append(item)
    current = "passed" if d.state == "keep" else "reject"
    items.sort(key=lambda i: LINE_ORDER.index(i["line"]) if i["line"] in LINE_ORDER else len(LINE_ORDER))
    return [i for i in items if registry.review_line(i["line"]).applies_to == current]


def graded_findings(state: RunState, ep: int, d: Decided) -> list[dict]:
    """A list entry's ``findings`` (C2 final-list 2.0, design doc 17 §5.4): every finding of the episode with
    the level it has after the human decisions, pointing at its place in the module's record; a person's
    conclusion is no finding of a record and carries its text."""
    recs = state.records(ep)
    out = []
    for g in d.verdict.graded:
        row: dict = {"module": g.module, "code": g.code, "item": g.item, "level": g.level}
        if g.line:
            row["line"] = g.line
        found = (recs.get(g.module) or {}).get("findings") or []
        index = next((i for i, f in enumerate(found) if f is g.finding), None)
        if g.human or index is None:
            row.update(human=True, message_zh=str(g.finding.get("message_zh") or g.code))
        else:
            row["index"] = index
        row["appealable"] = bool(g.appealable and not g.human)
        out.append(row)
    return out


def final(state: RunState, revision: int, decisions: Decisions, task_text: TaskText | None,
          profile_audit: dict | None) -> dict:
    """The four lists (``cli/final-list.schema.json`` 2.0) plus the merged label audit."""
    decided = decide_all(state, decisions)
    audit = merged_label_audit(state, profile_audit) if PROFILE in state.modules or TASK in state.modules else None
    entries = _audit_entries(audit)
    passed, reject, held, review = [], [], [], []
    for ep in state.episodes:
        d = decided[ep]
        reasons = reasons_of(d)
        entry: dict = {"episode_index": ep}
        if d.state != "keep":
            entry["reasons"] = reasons
        entry["findings"] = graded_findings(state, ep, d)
        if d.state == "keep":
            tt = task_text.delivered(ep) if task_text is not None else None
            if tt is not None:
                entry["task_text"] = tt
            passed.append(entry)
        elif d.state == "drop":
            reject.append(entry)
        else:
            held.append(entry)
        if d.state in ("keep", "drop") and d.discard is None:
            items = review_items(state, ep, d, decisions, entries.get(ep) or [], reasons)
            if items:
                item_entry = {"episode_index": ep, "review": items,
                              "current_list": "passed" if d.state == "keep" else "reject"}
                if d.state == "drop":
                    item_entry["reasons"] = reasons
                review.append(item_entry)

    def doc(name, eps):
        return {"schema_version": "2.0", "list": name, "revision": int(revision), "count": len(eps),
                "episodes": eps}

    # keep.txt stays the funnel phase's: the kept set after the human decisions, before dedup (the input
    # of dedup and of the profile; the delivered set is passed.json)
    verdicts, keep = funnel(state, decisions)
    return {"passed": doc("passed", passed), "reject": doc("reject", reject), "held": doc("held", held),
            "review": doc("review", review), "label_audit": audit, "verdicts": verdicts, "keep": keep,
            "policy": state.policy}


def funnel(state: RunState, decisions: Decisions) -> tuple[list[Verdict], list[int]]:
    """(the machine's verdicts of the funnel modules, the episodes kept after the human decisions)."""
    decided = {e: decide(state, e, decisions, phase="funnel") for e in state.episodes}
    return [decided[e].machine for e in state.episodes], [e for e in state.episodes if decided[e].kept]


def write_verdicts(out_dir: str, verdicts: list[Verdict], keep: list[int]) -> dict:
    """``verdicts.jsonl`` (2.0 lines) and ``keep.txt``."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "verdicts.jsonl")
    keep_path = os.path.join(out_dir, "keep.txt")
    write_text_atomic(path, "".join(json.dumps(v.to_line(), ensure_ascii=False, allow_nan=True) + "\n"
                                    for v in verdicts))
    write_text_atomic(keep_path, "".join(f"{e}\n" for e in sorted(keep)))
    return {"verdicts": path, "keep": keep_path}


def write_final(rev_dir: str, result: dict) -> dict:
    os.makedirs(rev_dir, exist_ok=True)
    files = {}
    for name in ("passed", "reject", "held", "review"):
        path = os.path.join(rev_dir, f"{name}.json")
        write_json_atomic(path, result[name])
        files[name] = path
    write_json_atomic(os.path.join(rev_dir, "label_audit.json"), result["label_audit"] or {})
    pol = os.path.join(rev_dir, policy_mod.POLICY_NAME)
    write_json_atomic(pol, result["policy"].to_json())
    files["policy"] = pol
    files.update(write_verdicts(rev_dir, result["verdicts"], result["keep"]))
    return files


def profile_members(state: RunState, decisions: Decisions) -> tuple[list[int], set[int]]:
    """(the episodes of ``state`` skill_profile files, the ones a person restored): dedup's duplicates
    (the members a group does not keep) are left out, except one a person brought into the delivery."""
    decided = decide_all(state, decisions)
    restored = {e for e, d in decided.items() if d.restored}
    dups = {e for e, d in decided.items() if any(g.module == DEDUP for g in d.verdict.blocking)}
    members = [e for e in state.episodes if e in restored or e not in dups]
    return members, restored
