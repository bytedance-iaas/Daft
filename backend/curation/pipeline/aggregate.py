"""The verdicts of a run directory: pure computation, recomputed in full (doc 02 §3.6, 06 §3, 17 §4).

**Policy verdicts** (C2 2.0, design doc 17 §4, D58). Modules only report findings; the task's policy
(``run.json``'s ``policy``, :mod:`.policy`) grades every finding blocking / review / info and
:mod:`.verdicts` decides each episode: a blocking finding drops it (every one of them is a reason; a module
that failed on it is still named, D35), else a module it needs that failed or has no record holds it, else
it is kept and its review findings go to their review lines. The default policy is today's gates (P18):
only the soft-score rejects are gone.

**Every module judges every episode** (D57): the two blocks run side by side and no stage stops an
episode, so an episode needs a judgement of every selected module (:func:`expected`) - one without is held.

**The final lists** - the machine's verdicts plus the applied human decisions (:mod:`.adjudication`):
a discard wins over everything; a person's answer is the conclusion of the findings it answers; an appeal
lifts the blocking of appealable findings when every blocking finding of the episode is one (D42 by
finding); a relabel not judged again yet holds the episode. The duplicate check (D87) is a module like the
others: its findings are reported, nothing is kept first or dropped. Writes ``passed`` / ``reject`` / ``held``
(disjoint, complete), the ``review`` view, the frozen ``policy.json``, the machine's verdict lines
(``verdicts.jsonl``) and the kept set after the decisions (``keep.txt``) into ``revisions/r<NNNN>/``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from ..contracts import modules as registry
from . import policy as policy_mod
from .adjudication import Decisions, judged_with
from .records import is_error, latest_results, write_json_atomic, write_text_atomic
from .tasktext import TaskText
from .verdicts import Graded, Verdict, judge, name_of

#: the stages whose modules judge every episode (the duplicate check compares one with the others, but it
#: writes a record for each like the rest, D87)
EPISODE_STAGES = ("integrity", "numeric", "frame", "duplicate", "vlm")
NAMES_CN = {m.id: m.name_zh for m in registry.MODULES}
TASK = "task_success"
#: the order of one episode's questions on its card (v1's, then v2's own lines as they came)
LINE_ORDER = ("task_verdict", "eef_check", "integrity_check", "sync_check", "reject_appeal")


@dataclass
class RunState:
    """The selected modules' current results, read once, and the policy that grades them."""

    run_dir: str
    modules: list[str]
    episodes: list[int]
    policy: policy_mod.Policy | None = None
    results: dict[str, dict[int, dict]] | None = None

    def __post_init__(self) -> None:
        chosen = set(registry.with_riders(self.modules))
        self.modules = [m.id for m in registry.MODULES if m.id in chosen]
        self.episodes = sorted({int(e) for e in self.episodes})
        if self.policy is None:
            self.policy = policy_mod.load(self.run_dir)
        if self.results is None:
            self.results = {m: latest_results(self.run_dir, m) for m in self.modules}

    def records(self, ep: int) -> dict[str, dict | None]:
        return {m: self.results[m].get(ep) for m in self.modules}


def expected(state: RunState, ep: int, recs: dict[str, dict | None]) -> list[str]:
    """The per-episode modules ``ep`` needs a judgement of: every selected one (D57, D58) - a rider is answered
    in its host's requests and never holds an episode of its own."""
    return [m for m in state.modules
            if registry.get(m).stage in EPISODE_STAGES and not registry.get(m).rides_on]


def machine(state: RunState, ep: int) -> Verdict:
    """The machine's verdict of the modules (no human decisions)."""
    recs = state.records(ep)
    return judge(ep, recs, expected(state, ep, recs), state.policy)


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
            "eef_check": decisions.human_gate(ep, "eef_check", ("consistent", "inconsistent")),
            "sync_check": decisions.human_gate(ep, "sync_check", ("usable", "misaligned", "shifted"))}


def decide(state: RunState, ep: int, decisions: Decisions) -> Decided:
    """``ep`` after the human decisions."""
    recs = state.records(ep)
    need = expected(state, ep, recs)
    mach = judge(ep, recs, need, state.policy)
    answers = answers_of(decisions, ep)
    use = dict(recs)
    base = judge(ep, use, need, state.policy, answers=answers)
    discard = decisions.discarded(ep)
    admissible = discard is None and base.appeal_admissible()
    appeal = decisions.appeal(ep) if admissible else None
    verdict = judge(ep, use, need, state.policy, answers=answers, appeal=appeal)
    restored = verdict.verdict == "keep" and (mach.verdict == "drop" or base.verdict == "drop") \
        and (answers.get("task_verdict") == "success" or appeal == "restore")
    relabel = decisions.relabel(ep)
    ts = recs.get(TASK)
    pending = bool(relabel and answers.get("task_verdict") is None and TASK in state.modules
                   and ts is not None and not is_error(ts) and not judged_with(ts, relabel))
    return Decided(mach, verdict, discard, restored, admissible, pending)


def decide_all(state: RunState, decisions: Decisions) -> dict[int, Decided]:
    """Every episode after the human decisions."""
    return {e: decide(state, e, decisions) for e in state.episodes}


# ---------------------------------------------------------------- the lists

def _reason(g: Graded) -> dict:
    kind = "human" if g.human else "finding"
    return {"module": g.module, "kind": kind, "code": g.code, "item": g.item, "appealable": bool(g.appealable and not g.human),
            "text": str(g.finding.get("message_zh") or g.code)}


def _error_reasons(v: Verdict, *, rejected: bool) -> list[dict]:
    tail = "，不影响结论" if rejected else ""
    return [{"module": m, "kind": "execution_error",
             "text": f"「{name_of(m)}」执行出错（{v.error_detail.get(m, '')}）{tail}"} for m in v.error_modules]


def reasons_of(d: Decided) -> list[dict]:
    if d.discard is not None:
        return [{"module": TASK, "kind": "human", "text": "人工裁决弃用"}]
    v = d.verdict
    if d.state == "drop":
        return [_reason(g) for g in v.blocking] + _error_reasons(v, rejected=True)
    if d.state == "held":
        out = _error_reasons(v, rejected=False)
        if d.pending_relabel:
            out.append({"module": TASK, "kind": "execution_error", "text": "改标后尚未按新标注重跑任务成败判定"})
        return out
    return []


def review_items(state: RunState, ep: int, d: Decided, decisions: Decisions, reasons: list[dict]) -> list[dict]:
    """What a person is asked about one episode (D42, D43): a kept episode's review findings nobody
    settled, one item per line and module; an admissible reject's appeal."""
    items: list[dict] = []
    if d.state == "keep":
        by_line: dict[tuple[str, str], list[Graded]] = {}
        for g in d.verdict.review:
            by_line.setdefault((g.line or "", g.module), []).append(g)
        for (line, module), gs in by_line.items():
            spec = registry.review_line(line)
            if line == "task_verdict" and decisions.human_task_verdict(ep) is not None:
                continue
            codes = [g.code for g in gs]
            reason = "；".join(dict.fromkeys(str(g.finding.get("message_zh") or g.code) for g in gs))
            item = {"source_module": module, "kind": spec.review_kind, "line": line,
                    "codes": codes, "items": [g.item for g in gs if g.item],
                    "reason": reason or "未注明"}
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


def final(state: RunState, revision: int, decisions: Decisions, task_text: TaskText | None) -> dict:
    """The four lists (``cli/final-list.schema.json`` 2.0), the verdict lines and the kept set."""
    decided = decide_all(state, decisions)
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
            items = review_items(state, ep, d, decisions, reasons)
            if items:
                item_entry = {"episode_index": ep, "review": items,
                              "current_list": "passed" if d.state == "keep" else "reject"}
                if d.state == "drop":
                    item_entry["reasons"] = reasons
                review.append(item_entry)

    def doc(name, eps):
        return {"schema_version": "2.0", "list": name, "revision": int(revision), "count": len(eps),
                "episodes": eps}

    # verdicts.jsonl: the machine's verdicts; keep.txt: the kept set after the human decisions (the delivered
    # set is passed.json)
    verdicts = [decided[e].machine for e in state.episodes]
    keep = [e for e in state.episodes if decided[e].kept]
    return {"passed": doc("passed", passed), "reject": doc("reject", reject), "held": doc("held", held),
            "review": doc("review", review), "verdicts": verdicts, "keep": keep, "policy": state.policy}


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
    pol = os.path.join(rev_dir, policy_mod.POLICY_NAME)
    write_json_atomic(pol, result["policy"].to_json())
    files["policy"] = pol
    files.update(write_verdicts(rev_dir, result["verdicts"], result["keep"]))
    return files
