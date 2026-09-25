"""The verdicts of a run directory: pure computation, recomputed in full (doc 02 §3.6, 06 §3).

**Funnel phase** - the six checks -> keep / drop / held per episode, v1's
rules to the letter (``pipeline/verdict.py`` is called, never rewritten):

* stages are walked in v1's funnel order; a hard gate that failed in the
  numeric or frame stage kills the episode there, exactly as v1's filters do
  (``hard_fails`` = the first failing gate, no soft score, the reason from
  ``report.hard_fail_reason``);
* an episode that survives both is judged by ``episode_verdict`` over every
  check it has: a hard ``passed=False`` drops, ``passed=None`` is only recorded
  as undecidable, a weighted soft score under the threshold drops.

What v2 adds (D24, D33, D35): a module that could not judge the episode
(``verdict = error``, or no result at a stage the episode reached) holds it -
unless the modules that did judge it already reject it: a hard gate failed, or
every selected soft module scored it and the weighted score is under the
threshold. Then it is dropped and the failed modules are still named. An
episode that errored does not go on to later stages (their results are not
expected); a module that failed as a whole leaves its gate open, so the walk
goes on.

``verdicts.jsonl`` is the machine's funnel verdict. ``keep.txt`` - what dedup
and skill_profile work on - also follows the applied human decisions (v1's
rejudge moves entries between its lists, and its profile follows the delivered
set): a discarded episode or one a person judged failed leaves it, a restored
appeal (or a human "success" on a reject) joins it. Before any decision the two
agree.

**Final phase** - adds dedup, skill_profile and the applied human decisions
(``pipeline.adjudication``), and writes ``passed`` / ``reject`` / ``held``
(disjoint, complete) and the ``review`` view into ``revisions/r<NNNN>/``. Dedup
runs once, on the first revision's keep set; after an adjudication its result
stands, and an episode a person brought into the delivery is never deduplicated
- exactly as v1's rejudge, which never deduplicates again.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..contracts import modules as registry
from ..export.report import CHECK_CN, check_detail_reason, hard_fail_reason
from .adjudication import Decisions, judged_with
from .records import latest_results, revision_dir, write_json_atomic, write_text_atomic
from .tasktext import TaskText, load_autolabel
from .verdict import episode_verdict

FUNNEL_STAGES = ("integrity", "numeric", "frame", "vlm")
#: The modules that vote on keep / drop / held. Advisory modules (registry 1.4,
#: ``affects_dataset_verdict=False``) are filtered here, at the call boundary; verdict.py is v1's.
FUNNEL_MODULES = tuple(m.id for m in registry.MODULES if m.stage in FUNNEL_STAGES
                       and m.affects_dataset_verdict)
NAMES_CN = {**CHECK_CN, "dedup": "精确去重", "skill_profile": "技能画像",
            "autolabel": "无标注补描述"}
#: v2's EEF gate and its person's question (C1 1.9, design doc 12 D-E13): an episode it
#: could not settle (``passed=None``) is kept and asked; the answer is the gate's result
EEF = "eef_video_consistency"
EEF_HUMAN = {"consistent": "人工裁决判为一致", "inconsistent": "人工裁决判为 EEF 与视频不一致"}
#: v2's data integrity gate (C1 1.11, design doc 14 §4.4): its suspects are kept and asked
INTEGRITY = "data_integrity"
#: v2's own gates a person settles: module -> (review line, review.json kind, the decisions that
#: settle it and the result each stands for, the reason each gives). The answer is the gate's
#: result, as a human task verdict is task_success's; "unsure" settles nothing.
HUMAN_GATES: dict[str, tuple[str, str, dict[str, bool], dict[str, str]]] = {
    EEF: ("eef_check", "eef_consistency", {"consistent": True, "inconsistent": False}, EEF_HUMAN),
    INTEGRITY: ("integrity_check", "integrity_suspect", {"intact": True, "broken": False},
                {"intact": "人工裁决判为数据无误", "broken": "人工裁决判为数据确有问题"}),
}


def human_answer(decisions: Decisions, ep: int, gate: str) -> str | None:
    """A person's settling answer on ``gate``'s line, if any."""
    line, _kind, settles, _texts = HUMAN_GATES[gate]
    return decisions.human_gate(ep, line, tuple(settles))


def _stage(m: str) -> str:
    return registry.get(m).stage


def _struct(rec: dict) -> dict:
    return {"passed": rec.get("passed"), "score": rec.get("score"),
            "detail": rec.get("details") or {}}


def _cause(rec: dict | None) -> str:
    incs = ((rec or {}).get("error") or {}).get("incidents") or []
    if not incs:
        return "没有结果(模块未对它执行)"
    inc = incs[0]
    where = inc.get("camera") or inc.get("call_kind") or ""
    text = f"{inc.get('step')}{('/' + where) if where else ''}: {inc.get('cause', '')}"
    return text.strip(": ") + (f" 等 {len(incs)} 处" if len(incs) > 1 else "")


@dataclass
class Line:
    """One funnel verdict (``cli/verdict-line.schema.json``) plus what aggregate needs."""

    episode_index: int
    verdict: str
    hard_fails: list = field(default_factory=list)
    soft_score: float | None = None
    undecidable: list = field(default_factory=list)
    error_modules: list = field(default_factory=list)
    reason: str = ""
    checks: dict = field(default_factory=dict)            # normal results, by module
    error_detail: dict = field(default_factory=dict)      # module -> cause text

    def to_json(self) -> dict:
        return {"episode_index": self.episode_index, "verdict": self.verdict,
                "hard_fails": list(self.hard_fails), "soft_score": self.soft_score,
                "undecidable": list(self.undecidable),
                "error_modules": list(self.error_modules), "reason": self.reason}


class RunState:
    """The selected modules' current results, read once."""

    def __init__(self, run_dir: str, modules, episodes, cfg: dict,
                 *, results: dict[str, dict[int, dict]] | None = None,
                 autolabel: dict | None = None):
        self.run_dir = run_dir
        self.modules = [m.id for m in registry.MODULES if m.id in set(modules)]
        native = [m for m in self.modules if m in registry.native_ids() and m not in cfg["checks"]]
        if native:                     # v2's own gates join v1's verdict config here, at the call boundary
            cfg = {**cfg, "checks": {**cfg["checks"],
                                     **{m: {"enable": True, "gate": registry.get(m).gate} for m in native}}}
        self.cfg = cfg
        self.funnel = [m for m in self.modules if m in FUNNEL_MODULES]
        self.episodes = sorted({int(e) for e in episodes})
        self.results = (results if results is not None else
                        {m: latest_results(run_dir, m) for m in self.modules})
        self.autolabel = autolabel if autolabel is not None else load_autolabel(run_dir)

    def soft_modules(self) -> list[str]:
        return [m for m in self.funnel
                if (self.cfg["checks"].get(m) or {}).get("gate", "soft") == "soft"]


def funnel_line(state: RunState, ep: int, overrides: dict | None = None) -> Line:
    """The funnel verdict of one episode. ``overrides``: ``{module: struct}`` in place
    of a result (a human task verdict is a normal task_success result)."""
    overrides = overrides or {}
    normal: dict[str, dict] = {}
    errors: list[str] = []
    detail: dict[str, str] = {}
    killed: str | None = None
    for stage in FUNNEL_STAGES:
        mods = [m for m in state.funnel if _stage(m) == stage]
        if not mods:
            continue
        stage_error = False
        for m in mods:
            if m in overrides:
                normal[m] = overrides[m]
                continue
            rec = state.results[m].get(ep)
            if rec is None:
                errors.append(m)
                detail[m] = _cause(None)
            elif rec["verdict"] == "error":
                who = m
                incs = rec["error"]["incidents"]
                if m == "task_success" and incs and all(i.get("step") == "autolabel"
                                                        for i in incs):
                    who = "autolabel"
                errors.append(who)
                detail[who] = _cause(rec)
                stage_error = True
            else:
                normal[m] = _struct(rec)
        if stage in ("integrity", "numeric", "frame"):
            gates = [m for m in mods if registry.get(m).gate == "hard" and m in normal]
            fails = [m for m in gates if normal[m].get("passed") is False]
            if fails:
                killed = fails[0]
                break
        if stage_error:
            break
    line = Line(ep, "keep", checks=normal, error_detail=detail)
    if killed is not None:                       # v1: killed mid-funnel by a hard gate
        line.verdict, line.hard_fails = "drop", [killed]
        line.reason = hard_fail_reason([killed], {killed: normal[killed]})
        line.error_modules = errors
        if errors:
            line.reason += _also_failed(errors)
        return line
    v = episode_verdict(normal, state.cfg)
    line.hard_fails, line.soft_score = list(v["hard_fails"]), v["soft_score"]
    line.undecidable = list(v["undecidable"])
    line.error_modules = errors
    if not errors:
        line.verdict, line.reason = v["verdict"], v["reason"]
        return line
    threshold = state.cfg["verdict"]["soft_threshold"]
    all_scored = all(m in normal and normal[m].get("score") is not None
                     for m in state.soft_modules())
    if v["hard_fails"]:                                   # D35: a gate already rejects it
        line.verdict, line.reason = "drop", v["reason"] + _also_failed(errors)
    elif all_scored and v["soft_score"] is not None and v["soft_score"] < threshold:
        line.verdict, line.reason = "drop", v["reason"] + _also_failed(errors)
    else:
        line.verdict = "held"
        line.hard_fails = []
        line.reason = "待补跑:" + ";".join(
            f"「{NAMES_CN.get(m, m)}」执行出错({detail.get(m, '')})" for m in errors)
    return line


def _also_failed(errors: list[str]) -> str:
    names = "、".join(f"「{NAMES_CN.get(m, m)}」" for m in errors)
    return f";另有{names}执行出错,不影响结论"


def funnel(state: RunState) -> list[Line]:
    return [funnel_line(state, e) for e in state.episodes]


@dataclass
class Decided:
    """One episode after the applied human decisions, before dedup and skill_profile."""

    machine: Line                         # the funnel verdict of the checks alone
    line: Line                            # with a human task verdict / restored appeal
    state: str                            # keep / drop / held
    reasons: list = field(default_factory=list)
    human_note: dict | None = None
    discard: dict | None = None           # the "discard" decision, when there is one
    #: a person brought a reject into the delivery (an appeal restore, or a "success"
    #: verdict on a reject): v1's rejudge never deduplicates it
    restored: bool = False
    #: the module a person may overturn this episode's reject on (D42), if any
    appeal_target: str | None = None

    @property
    def kept(self) -> bool:
        """In ``keep.txt``: what dedup and skill_profile work on."""
        return self.state == "keep" and self.discard is None


def appeal_target(state: RunState, ep: int, machine: Line, decisions: Decisions) -> str | None:
    """The module a person may overturn this episode's reject on (D42), or None.

    The one admission rule (adjudicate-apply and review.json both use it): a
    funnel reject attributed to one hard gate alone - v1's
    ``is_task_success_reject``: no other hard gate, never a soft score - whose
    module the registry marks ``appealable`` and that no person settled (a human
    task verdict for task_success, an ``eef_check`` answer for the EEF gate), or
    dedup's byte-copy finding on an episode the funnel kept (when dedup is
    appealable). A discarded episode has none: the discard is final.
    """
    appealable = registry.appealable
    if decisions.discarded(ep) is not None:
        return None
    tv = decisions.human_task_verdict(ep)
    if machine.verdict == "drop":
        if len(machine.hard_fails) == 1 and appealable(machine.hard_fails[0]):
            gate = machine.hard_fails[0]
            settled = human_answer(decisions, ep, gate) if gate in HUMAN_GATES else tv
            return gate if settled is None else None
        return None
    if machine.verdict == "keep" and tv != "failure" and "dedup" in state.modules \
            and appealable("dedup"):
        rec = (state.results.get("dedup") or {}).get(ep)
        if rec is not None and rec["verdict"] == "fail":
            return "dedup"
    return None


def decide(state: RunState, ep: int, decisions: Decisions,
           machine: Line | None = None) -> Decided:
    """The human decisions on one episode, in v1's order (``pipeline/rejudge.py``)."""
    machine = machine or funnel_line(state, ep)
    line, reasons, human_note = machine, [], None
    selected = set(state.modules)
    # human task verdicts, EEF answers and appeals act as a module result (v1 moves the entry)
    tv = decisions.human_task_verdict(ep)
    appeal = decisions.appeal(ep)
    target = appeal_target(state, ep, machine, decisions)
    ts_rec = (state.results.get("task_success") or {}).get(ep)
    overrides: dict[str, dict] = {}
    if tv is not None and "task_success" in selected and ts_rec is not None \
            and ts_rec["verdict"] != "error":
        overrides["task_success"] = dict(_struct(ts_rec), passed=(tv == "success"))
        human_note = {"module": "task_success",
                      "text": "人工裁决判成功" if tv == "success"
                      else "人工裁决判失败(任务未完成)", "kind": "human"}
    for gate, (_line, _kind, settles, texts) in HUMAN_GATES.items():
        answer = human_answer(decisions, ep, gate)
        gate_rec = (state.results.get(gate) or {}).get(ep)
        if answer is not None and gate in selected and gate_rec is not None \
                and gate_rec["verdict"] != "error":
            rec = _struct(gate_rec)
            overrides[gate] = {**rec, "passed": settles[answer],
                               "detail": {**rec["detail"], "reason": texts[answer]}}
    if appeal == "restore" and target is not None and target != "dedup" \
            and target not in overrides:
        # restore overturns the appealed gate only: another module that could not
        # judge the episode still holds it (P11)
        rec = (state.results.get(target) or {}).get(ep)
        if rec is not None and rec["verdict"] != "error":
            overrides[target] = dict(_struct(rec), passed=True)
    if overrides:
        line = funnel_line(state, ep, overrides)
    state_ = line.verdict                          # keep / drop / held
    # relabelled but not judged again with the new label yet -> held
    relabel = decisions.relabel(ep)
    if relabel and tv is None and "task_success" in selected and state_ == "keep" \
            and ts_rec is not None and ts_rec["verdict"] != "error":
        if not judged_with(ts_rec, relabel):
            state_ = "held"
            reasons.append({"module": "task_success", "kind": "execution_error",
                            "text": "改标后尚未按新标注重跑任务成败判定"})
    restored = (machine.verdict == "drop" and state_ == "keep"
                and (tv == "success" or (appeal == "restore" and target is not None))) \
        or (target == "dedup" and appeal == "restore" and state_ == "keep")
    return Decided(machine, line, state_, reasons, human_note, decisions.discarded(ep),
                   restored, target)


def decide_all(state: RunState, decisions: Decisions,
               lines: list[Line] | None = None) -> dict[int, Decided]:
    machine = {ln.episode_index: ln for ln in (lines or funnel(state))}
    return {e: decide(state, e, decisions, machine.get(e)) for e in state.episodes}


def write_funnel(out_dir: str, lines: list[Line], keep: list[int] | None = None) -> dict:
    """``verdicts.jsonl`` (the machine's funnel verdicts) and ``keep.txt`` (``keep``: the
    episodes kept after the human decisions; without any, the machine's keeps)."""
    os.makedirs(out_dir, exist_ok=True)
    verdicts = os.path.join(out_dir, "verdicts.jsonl")
    keep_path = os.path.join(out_dir, "keep.txt")
    if keep is None:
        keep = [ln.episode_index for ln in lines if ln.verdict == "keep"]
    write_text_atomic(verdicts, "".join(json.dumps(ln.to_json(), ensure_ascii=False,
                                                   allow_nan=True) + "\n" for ln in lines))
    write_text_atomic(keep_path, "".join(f"{e}\n" for e in sorted(keep)))
    return {"verdicts": verdicts, "keep": keep_path}


# ---------------------------------------------------------------- final phase

def merged_label_audit(state: RunState, profile_audit: dict | None) -> dict | None:
    """v1's review queue for label conflicts: skill_profile's audit, the kill guard's
    holds from task_success merged in front, each entry tagged with the task line
    (``dataset_level.audit``, exactly as ``run.py`` assembles the report)."""
    from ..dataset_level.audit import attach_task_context, guard_hold_entries, merge_guard_holds

    task_detail, task_of = {}, {}
    for ep, rec in (state.results.get("task_success") or {}).items():
        if rec["verdict"] == "error":
            continue
        d = rec.get("details") or {}
        eid = f"ep{int(ep):06d}"
        task_detail[eid] = d
        task_of[eid] = {"passed": rec.get("passed"), "verdict": d.get("verdict", "")}
    audit = profile_audit if profile_audit else None
    holds = guard_hold_entries(task_detail)
    if holds:
        audit = merge_guard_holds(audit, holds)
    if audit is None:
        return None
    return attach_task_context(audit, task_of)


def final(state: RunState, revision: int, decisions: Decisions, task_text: TaskText | None,
          profile_audit: dict | None) -> dict:
    """The four lists (``cli/final-list.schema.json``) plus the merged label audit."""
    selected = set(state.modules)
    passed, reject, held, review = [], [], [], []
    machine = {e: funnel_line(state, e) for e in state.episodes}
    decided = decide_all(state, decisions, list(machine.values()))
    audit = merged_label_audit(state, profile_audit) if "skill_profile" in selected \
        or "task_success" in selected else None
    audit_items: dict[int, list] = {}
    for tier in ("high", "mid_for_review", "low_caption_unstable"):
        for entry in (audit or {}).get(tier) or []:
            try:
                idx = int(str(entry.get("id")).lstrip("ep"))
            except ValueError:
                continue
            audit_items.setdefault(idx, []).append((tier, entry))
    dedup = state.results.get("dedup") or {}
    profile = state.results.get("skill_profile") or {}

    for ep in state.episodes:
        d = decided[ep]
        line, state_, human_note = d.line, d.state, d.human_note
        reasons: list[dict] = list(d.reasons)
        # dedup ran on the first revision's keep set; an episode a person brought in
        # afterwards was never compared, and v1 never deduplicates it; a restored dedup
        # appeal overturns dedup's finding (D42)
        if state_ == "keep" and "dedup" in selected and not d.restored:
            rec = dedup.get(ep)
            if rec is None or rec["verdict"] == "error":
                state_ = "held"
                reasons.append({"module": "dedup", "kind": "execution_error",
                                "text": f"「精确去重」执行出错({_cause(rec)})"})
            elif rec["verdict"] == "fail":
                dup = int((rec.get("details") or {}).get("duplicate_of", -1))
                state_ = "drop"
                reasons.append({"module": "dedup", "kind": "duplicate",
                                "text": f"与 ep{dup:06d} 字节级完全重复",
                                "duplicate_of": dup} if dup >= 0 else
                               {"module": "dedup", "kind": "duplicate",
                                "text": "字节级完全重复"})
        if state_ == "keep" and "skill_profile" in selected:
            rec = profile.get(ep)
            if rec is None or rec["verdict"] == "error":
                state_ = "held"
                reasons.append({"module": "skill_profile", "kind": "execution_error",
                                "text": f"「技能画像」执行出错({_cause(rec)})"})
        discard = d.discard
        if discard is not None:                    # rule 1: discard wins, even over held
            state_ = "drop"
            reasons = [{"module": "skill_profile" if discard["line"] == "label"
                        else "task_success", "kind": "human",
                        "text": "人工裁决弃用"}]
        elif state_ == "drop" and not reasons:
            reasons = _drop_reasons(line)
            if human_note is not None and human_note["text"].startswith("人工裁决判失败"):
                reasons = [human_note] + [r for r in reasons if r.get("module") in HUMAN_GATES]
            for gate, (_line, _kind, settles, texts) in HUMAN_GATES.items():
                answer = human_answer(decisions, ep, gate)
                if answer is not None and settles[answer] is False:
                    reasons = [{"module": gate, "kind": "human", "text": texts[answer]}
                               if r.get("module") == gate else r for r in reasons]
        elif state_ == "held" and not reasons:
            reasons = [{"module": m, "kind": "execution_error",
                        "text": f"「{NAMES_CN.get(m, m)}」执行出错({line.error_detail.get(m, '')})"}
                       for m in line.error_modules]
        entry = {"episode_index": ep}
        if state_ in ("keep", "drop"):
            entry["soft_score"] = line.soft_score
        if state_ == "keep":
            if task_text is not None:
                tt = task_text.delivered(ep)
                if tt is not None:
                    entry["task_text"] = tt
            passed.append(entry)
        elif state_ == "drop":
            reject.append({**entry, "reasons": reasons})
        else:
            held.append({**entry, "reasons": reasons})
        if state_ in ("keep", "drop") and discard is None:     # held: nothing to ask yet
            items = _review_items(state, ep, line, state_, d, decisions,
                                  audit_items.get(ep) or [], reasons)
            if items:
                item_entry = {"episode_index": ep, "review": items,
                              "current_list": "passed" if state_ == "keep" else "reject"}
                if state_ == "drop":
                    item_entry["reasons"] = reasons          # what the appeal is about
                review.append(item_entry)

    def doc(name, eps):
        return {"schema_version": "1.0", "list": name, "revision": int(revision),
                "count": len(eps), "episodes": eps}

    return {"passed": doc("passed", passed), "reject": doc("reject", reject),
            "held": doc("held", held), "review": doc("review", review),
            "label_audit": audit, "funnel": [machine[e] for e in state.episodes],
            "keep": [e for e in state.episodes if decided[e].kept]}


def _drop_reasons(line: Line) -> list[dict]:
    """Why the funnel rejects an episode, one entry per failed gate (or the soft score),
    followed by the modules that failed to judge it (D35: they change nothing)."""
    out = []
    if line.hard_fails:
        for m in line.hard_fails:
            why = check_detail_reason(line.checks.get(m) or {})
            cn = NAMES_CN.get(m, m)
            out.append({"module": m, "kind": "hard_gate",
                        "text": f"未通过「{cn}」:{why}" if why else f"未通过「{cn}」"})
    else:
        soft = [m for m, c in line.checks.items()
                if registry.get(m).gate == "soft" and c.get("score") is not None]
        text = line.reason.split(";另有", 1)[0]
        out.append({"module": soft[0] if soft else "motion_quality", "kind": "soft_score",
                    "text": text})
    for m in line.error_modules:
        out.append({"module": m, "kind": "execution_error",
                    "text": f"「{NAMES_CN.get(m, m)}」执行出错"
                            f"({line.error_detail.get(m, '')}),不影响结论"})
    return out


def _review_items(state: RunState, ep: int, line: Line, state_: str, d: Decided,
                  decisions: Decisions, audit_entries: list, reasons: list) -> list[dict]:
    """What a person is asked to decide about one episode (v1's queues, D42).

    * ``task_verdict``: a delivered episode (in passed) that task_success could not
      judge and no person has - other modules' abstentions stay in the verdict line
      and the report; asking whether the task succeeded is moot for a reject;
    * ``label_conflict``: skill_profile's audit (and the kill guard's holds);
    * ``reject_appeal``: a reject by one appealable module alone
      (:func:`appeal_target`) with no appeal decided yet - "unsure" keeps it listed;
    * ``eef_consistency``: a delivered episode the EEF module could not settle (conflicting
      CPU and model, no model opinion, what the model cannot see, not judgeable) and no
      person has (C1 1.9) - "unsure" keeps it listed;
    * ``integrity_suspect``: a delivered episode the data integrity module suspects (C1 1.11,
      design doc 14 §4.4), likewise.
    """
    items = []
    current = "passed" if state_ == "keep" else "reject"
    if state_ == "keep" and "task_success" in line.undecidable \
            and decisions.human_task_verdict(ep) is None:
        why = check_detail_reason(line.checks.get("task_success") or {})
        if (line.checks.get("task_success", {}).get("detail") or {}).get("internal_error"):
            why = f"系统内部错误(非数据问题):{why}"
        items.append({"source_module": "task_success", "kind": "task_verdict",
                      "reason": why or "未注明"})
    for gate, (_line, kind, _settles, _texts) in HUMAN_GATES.items():
        if state_ == "keep" and gate in line.undecidable and human_answer(decisions, ep, gate) is None:
            why = check_detail_reason(line.checks.get(gate) or {}).removeprefix("需要人工裁决：")
            items.append({"source_module": gate, "kind": kind, "reason": why or "未注明"})
    if not decisions.label_resolved(ep):
        for tier, entry in audit_entries:
            item = {"source_module": "task_success" if entry.get("guard_layer")
                    else "skill_profile", "kind": "label_conflict",
                    "reason": str(entry.get("reason") or tier)}
            if entry.get("priority"):
                item["priority"] = str(entry["priority"])
            items.append(item)
    if state_ == "drop" and d.appeal_target is not None and decisions.appeal(ep) is None:
        why = next((r["text"] for r in reasons if r.get("module") == d.appeal_target),
                   "")
        if decisions.pending(ep, "reject_appeal"):
            why = f"{why}(复议拿不准,待定)" if why else "复议拿不准,待定"
        item = {"source_module": d.appeal_target, "kind": "reject_appeal",
                "reason": why or "可复议"}
        dup = next((r["duplicate_of"] for r in reasons if r.get("module") == d.appeal_target
                    and "duplicate_of" in r), None)
        if dup is not None:
            item["duplicate_of"] = int(dup)
        items.append(item)
    # each item names its registry line and is only asked where that line applies (C1)
    out = []
    for item in items:
        spec = registry.review_line_of_kind(item["kind"])
        if spec.applies_to == current:
            out.append({**item, "line": spec.id})
    return out


def write_final(rev_dir: str, result: dict) -> dict:
    os.makedirs(rev_dir, exist_ok=True)
    files = {}
    for name in ("passed", "reject", "held", "review"):
        path = os.path.join(rev_dir, f"{name}.json")
        write_json_atomic(path, result[name])
        files[name] = path
    write_json_atomic(os.path.join(rev_dir, "label_audit.json"), result["label_audit"] or {})
    files.update(write_funnel(rev_dir, result["funnel"], result["keep"]))
    return files


def revision_path(run_dir: str, revision: int) -> str:
    return revision_dir(run_dir, revision)


def profile_members(state: RunState, decisions: Decisions) -> tuple[list[int], set[int]]:
    """(the episodes of ``state`` skill_profile files, the ones a person restored).

    Leaves out the byte copies dedup found (its first run stands after an
    adjudication, D9 / v1's rejudge), except an episode a person brought into
    the delivery: v1 never deduplicates it (``_sync_profile`` files it back).
    """
    dedup = latest_results(state.run_dir, "dedup")
    decided = decide_all(state, decisions)
    restored = {e for e, d in decided.items() if d.restored}
    members = [e for e in state.episodes
               if e in restored or (dedup.get(e) or {}).get("verdict") != "fail"]
    return members, restored
