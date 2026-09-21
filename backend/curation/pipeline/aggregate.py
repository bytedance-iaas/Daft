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

**Final phase** - adds dedup, skill_profile and the applied human decisions
(``pipeline.adjudication``), and writes ``passed`` / ``reject`` / ``held``
(disjoint, complete) and the ``review`` view into ``revisions/r<NNNN>/``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..contracts import modules as registry
from ..export.report import CHECK_CN, check_detail_reason, hard_fail_reason
from .adjudication import Decisions
from .records import latest_results, revision_dir, write_json_atomic, write_text_atomic
from .tasktext import TaskText, load_autolabel
from .verdict import episode_verdict

FUNNEL_STAGES = ("numeric", "frame", "vlm")
FUNNEL_MODULES = tuple(m.id for m in registry.MODULES if m.stage in FUNNEL_STAGES)
NAMES_CN = {**CHECK_CN, "dedup": "精确去重", "skill_profile": "技能画像",
            "autolabel": "无标注补描述"}


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

    def __init__(self, run_dir: str, modules, episodes, cfg: dict):
        self.run_dir, self.cfg = run_dir, cfg
        self.modules = [m.id for m in registry.MODULES if m.id in set(modules)]
        self.funnel = [m for m in self.modules if m in FUNNEL_MODULES]
        self.episodes = sorted({int(e) for e in episodes})
        self.results = {m: latest_results(run_dir, m) for m in self.modules}
        self.autolabel = load_autolabel(run_dir)

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
        if stage in ("numeric", "frame"):
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


def write_funnel(out_dir: str, lines: list[Line]) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    verdicts = os.path.join(out_dir, "verdicts.jsonl")
    keep = os.path.join(out_dir, "keep.txt")
    write_text_atomic(verdicts, "".join(json.dumps(ln.to_json(), ensure_ascii=False,
                                                   allow_nan=True) + "\n" for ln in lines))
    write_text_atomic(keep, "".join(f"{ln.episode_index}\n" for ln in lines
                                    if ln.verdict == "keep"))
    return {"verdicts": verdicts, "keep": keep}


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


def _task_reject_only(line: Line) -> bool:
    """v1's appeal gate: the reject is attributed to task_success and nothing else."""
    return line.verdict == "drop" and line.hard_fails == ["task_success"]


def final(state: RunState, revision: int, decisions: Decisions, task_text: TaskText | None,
          profile_audit: dict | None) -> dict:
    """The four lists (``cli/final-list.schema.json``) plus the merged label audit."""
    selected = set(state.modules)
    passed, reject, held, review = [], [], [], []
    machine = {e: funnel_line(state, e) for e in state.episodes}
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
        line = machine[ep]
        reasons: list[dict] = []
        human_note: dict | None = None
        # human task verdicts and appeals act as a task_success result (v1 moves the entry)
        tv = decisions.human_task_verdict(ep)
        appeal = decisions.appeal(ep)
        ts_rec = (state.results.get("task_success") or {}).get(ep)
        if "task_success" in selected and ts_rec is not None and ts_rec["verdict"] != "error":
            if tv is not None:
                override = dict(_struct(ts_rec), passed=(tv == "success"))
                line = funnel_line(state, ep, {"task_success": override})
                human_note = {"module": "task_success",
                              "text": "人工裁决判成功" if tv == "success"
                              else "人工裁决判失败(任务未完成)", "kind": "human"}
            elif appeal == "restore" and _task_reject_only(line):
                override = dict(_struct(ts_rec), passed=True)
                line = funnel_line(state, ep, {"task_success": override})
        state_ = line.verdict                      # keep / drop / held
        # relabelled but not judged again with the new label yet -> held
        relabel = decisions.relabel(ep)
        if relabel and tv is None and "task_success" in selected and state_ == "keep" \
                and ts_rec is not None and ts_rec["verdict"] != "error":
            judged = (ts_rec.get("details") or {})
            if judged.get("task_desc_source") != "人工改标" \
                    or str(judged.get("task_desc") or "") != relabel[:80]:
                state_ = "held"
                reasons.append({"module": "task_success", "kind": "execution_error",
                                "text": "改标后尚未按新标注重跑任务成败判定"})
        if state_ == "keep" and "dedup" in selected:
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
        discard = decisions.discarded(ep)
        if discard is not None:                    # rule 1: discard wins, even over held
            state_ = "drop"
            reasons = [{"module": "skill_profile" if discard["line"] == "label"
                        else "task_success", "kind": "human",
                        "text": "人工裁决弃用"}]
        elif state_ == "drop" and not reasons:
            reasons = _drop_reasons(line)
            if human_note is not None and human_note["text"].startswith("人工裁决判失败"):
                reasons = [human_note]
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
        if state_ in ("keep", "drop") and discard is None:
            items = _review_items(state, ep, line, decisions, audit_items.get(ep) or [],
                                  appeal_pending=decisions.pending(ep, "reject_appeal")
                                  and _task_reject_only(machine[ep]))
            if items:
                review.append({"episode_index": ep, "review": items,
                               "current_list": "passed" if state_ == "keep" else "reject"})

    def doc(name, eps):
        return {"schema_version": "1.0", "list": name, "revision": int(revision),
                "count": len(eps), "episodes": eps}

    return {"passed": doc("passed", passed), "reject": doc("reject", reject),
            "held": doc("held", held), "review": doc("review", review),
            "label_audit": audit, "funnel": [machine[e] for e in state.episodes]}


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


def _review_items(state: RunState, ep: int, line: Line, decisions: Decisions,
                  audit_entries: list, *, appeal_pending: bool) -> list[dict]:
    """What a person is asked to decide about one episode (v1's review.json + label queue)."""
    items = []
    for m in line.undecidable:
        if m == "task_success" and decisions.human_task_verdict(ep) is not None:
            continue                              # a person already decided it
        why = check_detail_reason(line.checks.get(m) or {})
        if (line.checks.get(m, {}).get("detail") or {}).get("internal_error"):
            why = f"系统内部错误(非数据问题):{why}"
        items.append({"source_module": m, "kind": "task_verdict",
                      "reason": why or "未注明"})
    if not decisions.label_resolved(ep):
        for tier, entry in audit_entries:
            item = {"source_module": "task_success" if entry.get("guard_layer")
                    else "skill_profile", "kind": "label_conflict",
                    "reason": str(entry.get("reason") or tier)}
            if entry.get("priority"):
                item["priority"] = str(entry["priority"])
            items.append(item)
    if appeal_pending:
        items.append({"source_module": "task_success", "kind": "reject_appeal",
                      "reason": "被拒复议待定(拿不准)"})
    return items


def write_final(rev_dir: str, result: dict) -> dict:
    os.makedirs(rev_dir, exist_ok=True)
    files = {}
    for name in ("passed", "reject", "held", "review"):
        path = os.path.join(rev_dir, f"{name}.json")
        write_json_atomic(path, result[name])
        files[name] = path
    write_json_atomic(os.path.join(rev_dir, "label_audit.json"), result["label_audit"] or {})
    files.update(write_funnel(rev_dir, result["funnel"]))
    return files


def revision_path(run_dir: str, revision: int) -> str:
    return revision_dir(run_dir, revision)
