"""Human decisions of one task: storing them and what they do (design doc 02 §3.9, 06 §5).

``curation adjudicate-apply`` is the first step of v1's ``rejudge``: it records
the task's decisions as applied - never calling a model and never exporting (D9)
- and names what has to run next. The effect on the verdicts is computed by
``aggregate --phase final`` every time, from the applied decisions, so applying
is idempotent and a later decision on the same episode and line replaces an
earlier one (v1's CSVs: the last row wins).

v1's rules survive unchanged (``pipeline/rejudge.py``), in its order:

1. **"discard" wins over any task verdict** (label line or task line): the
   episode is rejected whatever else was decided for it;
2. a relabel is judged again by task_success with the new label - **unless a
   human already gave the task verdict for that episode**, which then stands and
   is recorded as human (no model re-checks a person's conclusion);
3. an appeal is only admitted for a reject by one appealable module alone
   (the registry's ``appealable``, D42: task_success, and dedup's byte-copy
   finding; ``aggregate.appeal_target``); the physical and structural gates and
   the soft score are final whatever the decision file says. "restore"
   overturns that module only;
4. "unsure" is a legal answer: recorded, the episode stays in the queue, nothing
   changes.

v2's own lines (C1 1.9, F5.11; 1.11, design doc 14): ``eef_check`` answers an episode the
EEF module could not settle (``passed=None``, design doc 12 D-E13), ``integrity_check`` one
the data integrity module suspects. "consistent" / "intact" count as the module passing it,
"inconsistent" / "broken" as the module rejecting it - the gate's result, as a human task
verdict is task_success's; nothing runs again.

A task verdict on an episode task_success did not abstain on is not the card's own
question but v1's verdict after a relabel - the registry's follow-up of the label
line (C1 1.3 ``follow_ups``). It counts only while the label's latest answer opens
that follow-up and was given before it; once the label answer changes it lapses:
no effect, and a relabel still in force is judged again (``Decisions.stands``, the
Daemon's ``Queue._stands``).

Decisions never cross tasks (D32): only this task's decisions are given, and
nothing is read from the delivery root.

How a relabel is judged again (D39) comes with the decisions: ``relabel_rerun``
of ``decisions.json`` - ``v1`` (default: what v1's rejudge runs, multi-view
scoring and the per-camera end-state vote) or ``full`` (the first run's whole
task_success flow). It is recorded with every relabel it applies, in
``applied.jsonl`` and ``labels.json``, so a later retry of those episodes
judges them the same way.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from typing import Callable

from .records import (ADJUDICATION_DIR, latest_results, read_jsonl, write_json_atomic,
                      write_text_atomic)
from .tasktext import LABELS_FILE

APPLIED_FILE = f"{ADJUDICATION_DIR}/applied.jsonl"
HUMAN_DIR = "human-decisions"

RELABEL_RERUN = ("v1", "full")
RELABEL_DECISIONS = ("adopt_suggestion", "custom_label")

LINE_DECISIONS = {
    "label": ("adopt_suggestion", "custom_label", "keep_label", "unsure", "discard"),
    "task_verdict": ("success", "failure", "unsure", "discard"),
    "reject_appeal": ("restore", "keep_rejected", "unsure"),
    "eef_check": ("consistent", "inconsistent", "unsure"),
    "integrity_check": ("intact", "broken", "unsure"),
}
#: The lines adjudicate-apply has an apply rule for, with the decisions each rule
#: knows (the registry's review lines, C1; a test keeps them equal). A line or a
#: decision without a rule is refused, never skipped.
#: v1's words in the self-contained CSV copies (``dataset_level/decisions.py``)
V1_WORDS = {"adopt_suggestion": "采纳建议改标", "custom_label": "采纳建议改标",
            "keep_label": "维持原标注", "unsure": "拿不准", "discard": "弃用该条",
            "success": "判成功", "failure": "判失败", "restore": "捞回",
            "keep_rejected": "维持拒绝", "consistent": "一致", "inconsistent": "不一致",
            "intact": "数据无误", "broken": "数据确有问题"}


class DecisionError(ValueError):
    """A decision that does not fit the contract (exit 2)."""


def check_decision(d: dict) -> None:
    line, decision = d.get("line"), d.get("decision")
    if line not in LINE_DECISIONS:
        raise DecisionError(f"decision {d.get('id')}: line {line!r} has no apply rule in "
                            f"adjudicate-apply (it applies {', '.join(LINE_DECISIONS)})")
    if decision not in LINE_DECISIONS[line]:
        raise DecisionError(f"decision {d.get('id')}: {decision!r} is not a {line} decision")
    if decision in ("adopt_suggestion", "custom_label") \
            and not str(d.get("new_label") or "").strip():
        raise DecisionError(f"decision {d.get('id')}: {decision} needs new_label")
    if not isinstance(d.get("id"), int) or isinstance(d.get("id"), bool) or d["id"] < 1:
        raise DecisionError(f"decision id must be a positive integer, got {d.get('id')!r}")
    if not isinstance(d.get("episode_index"), int) or d["episode_index"] < 0:
        raise DecisionError(f"decision {d.get('id')}: bad episode_index")


def load_applied(run_dir: str) -> list[dict]:
    return read_jsonl(os.path.join(run_dir, APPLIED_FILE))


def own_questions(run_dir: str) -> Callable[[int, str], bool]:
    """``asks(episode, line)``: whether the episode's card asks ``line`` itself, from the
    current results. It matters only for a line a follow-up asks as well: the task
    verdict is the card's own question where task_success abstained (the task_verdict
    item); anywhere else it is v1's verdict after a relabel (C1 1.3)."""
    abstained = {e for e, r in latest_results(run_dir, "task_success").items()
                 if r.get("verdict") == "abstain"}
    return lambda episode, line: line != "task_verdict" or int(episode) in abstained


def judged_with(record: dict | None, text: str) -> bool:
    """Whether a task_success record judged its episode with the relabel ``text``."""
    details = (record or {}).get("details") or {}
    return details.get("task_desc_source") == "人工改标" \
        and str(details.get("task_desc") or "") == str(text)[:80]


def _follow_up_owners(line: str) -> tuple[str, ...]:
    """The registry lines with a follow-up that asks ``line`` (C1 1.3)."""
    from ..contracts import modules as registry

    return tuple(ln.id for ln in registry.REVIEW_LINES
                 if any(f.line == line for f in ln.follow_ups))


class Decisions:
    """The applied decisions in force, per episode and line.

    ``asks(episode, line)`` says whether the episode's card asks a line itself
    (:func:`own_questions`; None: every answer counts as one to the card's own
    question). An answer to a follow-up counts only while it stands (:meth:`stands`).
    """

    def __init__(self, applied: list[dict],
                 asks: Callable[[int, str], bool] | None = None):
        self.applied = sorted(applied, key=lambda d: int(d["id"]))
        self.asks = asks
        #: the latest answer per episode and line, standing or not (it opens follow-ups)
        self.answered: dict[tuple[int, str], dict] = {}
        for d in self.applied:
            self.answered[(int(d["episode_index"]), d["line"])] = d
        self.effective: dict[tuple[int, str], dict] = {}    # latest standing non-unsure
        self.latest: dict[tuple[int, str], dict] = {}       # latest standing
        for d in self.applied:
            if not self.stands(d):
                continue
            key = (int(d["episode_index"]), d["line"])
            self.latest[key] = d
            if d["decision"] != "unsure":
                self.effective[key] = d

    @classmethod
    def of(cls, run_dir: str) -> Decisions:
        return cls(load_applied(run_dir), asks=own_questions(run_dir))

    def stands(self, d: dict) -> bool:
        """Whether an applied answer counts - the one place of the lapse rule (C1 1.3
        ``follow_ups``, as the Daemon's ``Queue._stands``). An answer to a question the
        card asks itself always counts. An answer on a line only a follow-up asks (v1's
        task verdict after adopting a new label, on an episode task_success did not
        abstain on) counts while the owner line's latest answer opens that follow-up
        and was given before it (by decision id), and only with one of the follow-up's
        decisions; otherwise it lapsed and has no effect."""
        from ..contracts import modules as registry

        episode, line = int(d["episode_index"]), d["line"]
        owners = _follow_up_owners(line)
        if not owners or self.asks is None or self.asks(episode, line):
            return True
        for owner in owners:
            opener = self.answered.get((episode, owner))
            f = registry.follow_up(owner, opener["decision"], line) if opener else None
            if f is not None:
                return int(d["id"]) > int(opener["id"]) and d["decision"] in f.decisions
        return False

    def ids(self) -> list[int]:
        return [int(d["id"]) for d in self.applied]

    def get(self, episode: int, line: str) -> dict | None:
        return self.effective.get((int(episode), line))

    def pending(self, episode: int, line: str) -> bool:
        """An "unsure" is the last word and nothing was decided before it."""
        last = self.latest.get((int(episode), line))
        return bool(last) and last["decision"] == "unsure" \
            and (int(episode), line) not in self.effective

    def discarded(self, episode: int) -> dict | None:
        for line in ("label", "task_verdict"):
            d = self.get(episode, line)
            if d is not None and d["decision"] == "discard":
                return d
        return None

    def human_task_verdict(self, episode: int) -> str | None:
        """``success`` / ``failure`` when a person concluded the task line."""
        d = self.get(episode, "task_verdict")
        return d["decision"] if d is not None and d["decision"] in ("success", "failure") \
            else None

    def human_eef(self, episode: int) -> str | None:
        """``consistent`` / ``inconsistent`` when a person settled the EEF module's question."""
        return self.human_gate(episode, "eef_check", ("consistent", "inconsistent"))

    def human_gate(self, episode: int, line: str, settles: tuple[str, ...]) -> str | None:
        """The decision on a v2 gate's line when it is one of ``settles`` (not "unsure")."""
        d = self.get(episode, line)
        return d["decision"] if d is not None and d["decision"] in settles else None

    def relabel(self, episode: int) -> str | None:
        d = self.get(episode, "label")
        if d is not None and d["decision"] in RELABEL_DECISIONS:
            return str(d["new_label"]).strip()
        return None

    def relabel_rerun(self, episode: int) -> str:
        """How the relabel in force is judged again (D39): recorded when it was applied."""
        d = self.get(episode, "label")
        mode = (d or {}).get("relabel_rerun") or "v1"
        return mode if mode in RELABEL_RERUN else "v1"

    def label_resolved(self, episode: int) -> bool:
        return self.get(episode, "label") is not None

    def appeal(self, episode: int) -> str | None:
        d = self.get(episode, "reject_appeal")
        return d["decision"] if d is not None else None

    def episodes(self) -> set[int]:
        return {int(d["episode_index"]) for d in self.applied}


def relabel_rerun_of(doc: dict) -> str:
    """``relabel_rerun`` of a ``decisions.json`` (default ``v1``, D39)."""
    mode = doc.get("relabel_rerun", "v1")
    if mode not in RELABEL_RERUN:
        raise DecisionError(f"relabel_rerun must be one of {', '.join(RELABEL_RERUN)}, "
                            f"got {mode!r}")
    return mode


def apply(run_dir: str, doc: dict, *, now_ms: int | None = None,
          appeal_admissible=None) -> dict:
    """Record ``decisions.json`` as applied; returns ``adjudicate-apply --json``.

    ``appeal_admissible(episode) -> bool``: whether that episode is now a reject a
    person may appeal (D42); an appeal on any other episode is refused.
    """
    new = list(doc.get("decisions") or [])
    mode = relabel_rerun_of(doc)
    for d in new:
        check_decision(d)
    ids = [d["id"] for d in new]
    if len(set(ids)) != len(ids):
        raise DecisionError("decision ids repeat in decisions.json")
    before = load_applied(run_dir)
    asks = own_questions(run_dir)
    seen = {int(d["id"]) for d in before}
    fresh = [d for d in new if d["id"] not in seen]
    if appeal_admissible is not None:
        for d in fresh:
            if d["line"] == "reject_appeal" and not appeal_admissible(int(d["episode_index"])):
                raise DecisionError(
                    f"decision {d['id']}: episode {d['episode_index']} has no reject a person "
                    f"may appeal (a hard gate of its own, a soft score, a discarded episode "
                    f"and an episode that is not rejected are final)")
    stamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
    # every relabel applied now carries how it is judged again (D39)
    fresh = [dict(d, relabel_rerun=mode) if d["line"] == "label"
             and d["decision"] in RELABEL_DECISIONS else dict(d) for d in fresh]
    if fresh:
        text = "".join(json.dumps({**d, "applied_at": stamp}, ensure_ascii=False,
                                  sort_keys=True) + "\n" for d in sorted(fresh, key=lambda d: d["id"]))
        path = os.path.join(run_dir, APPLIED_FILE)
        existing = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                existing = fh.read()
        write_text_atomic(path, existing + text)
    decisions = Decisions(before + [{**d, "applied_at": stamp} for d in fresh], asks=asks)
    write_labels(run_dir, decisions)
    write_human_copies(run_dir, decisions)
    touched = sorted({int(d["episode_index"]) for d in fresh})
    # a relabel in force that no person concluded and task_success has not judged with
    # its text yet: a fresh relabel, or one whose follow-up verdict just lapsed
    judged = latest_results(run_dir, "task_success")
    rerun = sorted(e for e in touched
                   if decisions.relabel(e) and decisions.human_task_verdict(e) is None
                   and decisions.discarded(e) is None
                   and not judged_with(judged.get(e), decisions.relabel(e)))
    resync = sorted(e for e in touched
                    if any(int(d["episode_index"]) == e and d["decision"] != "unsure"
                           and d["decision"] != "keep_rejected" for d in fresh))
    changes = [{"episode_index": e, "new_label": decisions.relabel(e)}
               for e in touched if decisions.relabel(e)
               and any(d["line"] == "label" and int(d["episode_index"]) == e
                       and d["decision"] in ("adopt_suggestion", "custom_label") for d in fresh)]
    return {"schema_version": "1.0", "applied": len(fresh),
            "skipped_already_applied": len(new) - len(fresh),
            "rerun_task_success": rerun, "profile_resync": resync, "label_changes": changes,
            "relabel_rerun": mode}


def write_labels(run_dir: str, decisions: Decisions) -> None:
    labels = {}
    for e in sorted(decisions.episodes()):
        text = decisions.relabel(e)
        if text and decisions.discarded(e) is None:
            labels[str(e)] = {"text": text, "decision_id": int(decisions.get(e, "label")["id"]),
                              "relabel_rerun": decisions.relabel_rerun(e)}
    write_json_atomic(os.path.join(run_dir, LABELS_FILE), {"labels": labels})


def write_human_copies(run_dir: str, decisions: Decisions) -> None:
    """``human-decisions/*.csv``: a self-contained copy of this task's decisions, in v1's
    CSV schema and words (06 §1; the Daemon's database stays the authority)."""
    tables = {
        "label": ("label_decisions.csv", ["episode_id", "decision", "new_label", "note", "at"]),
        "task_verdict": ("task_verdicts.csv", ["episode_id", "verdict", "note", "at"]),
        "reject_appeal": ("reject_appeals.csv", ["episode_id", "appeal", "note", "at"]),
        "eef_check": ("eef_checks.csv", ["episode_id", "decision", "note", "at"]),
        "integrity_check": ("integrity_checks.csv", ["episode_id", "decision", "note", "at"]),
    }
    for line, (name, fields) in tables.items():
        rows = [d for d in decisions.applied if d["line"] == line]
        if not rows:
            continue
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for d in rows:
            at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(d["decided_at"]) / 1000))
            row = {"episode_id": f"ep{int(d['episode_index']):06d}", "note": d.get("note") or "",
                   "at": at}
            word = V1_WORDS[d["decision"]]
            if line == "label":
                row.update(decision=word, new_label=d.get("new_label") or "")
            elif line in ("eef_check", "integrity_check"):
                row["decision"] = word
            elif line == "task_verdict":
                row["verdict"] = word
            else:
                row["appeal"] = word
            w.writerow(row)
        write_text_atomic(os.path.join(run_dir, HUMAN_DIR, name), buf.getvalue())
