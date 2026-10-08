"""The policy verdict of one episode (design doc 17 §4.3-§4.4, D58): pure computation over its records.

Every finding of every selected module is graded by the task's policy (:mod:`.policy`), a person's answers
change the grades of the findings they answer, and the episode is

* **dropped** when a finding blocks - every blocking finding is a reason, and a module that failed on it is
  still named (D35: re-running it cannot change the drop);
* else **held** when a module it needs failed on it or has no record yet (D24, D33, P11);
* else **kept**; its review findings that no person has settled go to their review lines.

A person's answer is the conclusion of the findings it answers (§4.4): on ``integrity_check`` / ``eef_check``
"broken" / "inconsistent" makes the module's review finding blocking (kind human) and "intact" / "consistent"
settles it; on ``task_verdict`` "failure" is a human blocking finding of task_success and "success" withdraws
its failure and settles its abstention; "unsure" settles nothing. An appeal (``reject_appeal`` "restore") lifts
the blocking of appealable findings - admitted only when every blocking finding is appealable and no person
concluded it (D42 by finding). A discard and a relabel waiting to be judged again are the caller's (aggregate).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import modules as registry
from .policy import Policy
from .records import is_error

#: a person's answers on the lines that settle a module's review findings: line -> module, {decision: blocks?}
HUMAN_LINES: dict[str, tuple[str, dict[str, bool]]] = {
    "integrity_check": ("data_integrity", {"intact": False, "broken": True}),
    "eef_check": ("eef_video_consistency", {"consistent": False, "inconsistent": True}),
}
#: the human conclusions written as a reason
HUMAN_TEXT = {"broken": "人工裁决判为数据确有问题", "inconsistent": "人工裁决判为 EEF 与视频不一致",
              "failure": "人工裁决判失败（任务未完成）"}


@dataclass
class Graded:
    module: str
    finding: dict
    level: str
    line: str | None = None
    appealable: bool = False
    human: bool = False                           # a person's conclusion (never appealable)

    @property
    def code(self) -> str:
        return str(self.finding.get("code"))

    @property
    def item(self):
        return self.finding.get("item")

    def ref(self) -> dict:
        return {"module": self.module, "code": self.code, "item": self.item}


@dataclass
class Verdict:
    episode_index: int
    verdict: str                                  # keep / drop / held
    graded: list[Graded] = field(default_factory=list)
    error_modules: list[str] = field(default_factory=list)
    error_detail: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> list[Graded]:
        return [g for g in self.graded if g.level == "blocking"]

    @property
    def review(self) -> list[Graded]:
        return [g for g in self.graded if g.level == "review"]

    @property
    def info(self) -> list[Graded]:
        return [g for g in self.graded if g.level == "info"]

    def appeal_admissible(self) -> bool:
        """A reject a person may appeal (D42 by finding): every blocking finding appealable, none a person's."""
        blocking = self.blocking
        return self.verdict == "drop" and bool(blocking) and all(g.appealable and not g.human for g in blocking)

    def to_line(self) -> dict:
        """A ``verdicts.jsonl`` line 2.0 (C2 ``verdict_line_2``)."""
        return {"episode_index": self.episode_index, "verdict": self.verdict,
                "blocking": [g.ref() for g in self.blocking],
                "review": [{**g.ref(), "line": g.line} for g in self.review],
                "info_count": len(self.info), "error_modules": list(self.error_modules),
                "reason": self.reason()}

    def reason(self) -> str:
        if self.verdict == "drop":
            text = "；".join(dict.fromkeys(str(g.finding.get("message_zh") or g.code) for g in self.blocking))
            return text + also_failed(self.error_modules)
        if self.verdict == "held":
            return "待补跑：" + "；".join(f"「{name_of(m)}」执行出错（{self.error_detail.get(m, '')}）"
                                         for m in self.error_modules)
        return ""


def name_of(module: str) -> str:
    try:
        return registry.get(module).name_zh
    except KeyError:
        return {"autolabel": "无标注补描述"}.get(module, module)   # verdicts from before D72


def also_failed(errors: list[str]) -> str:
    if not errors:
        return ""
    return "；另有" + "、".join(f"「{name_of(m)}」" for m in errors) + "执行出错，不影响结论"


def cause(rec: dict | None) -> str:
    """Why a module has no judgement of the episode: its first incident, or no record at all."""
    incs = ((rec or {}).get("error") or {}).get("incidents") or []
    if not incs:
        return "没有结果（模块未对它执行）"
    inc = incs[0]
    where = inc.get("camera") or inc.get("call_kind") or ""
    text = f"{inc.get('step')}{('/' + where) if where else ''}: {inc.get('cause', '')}"
    return text.strip(": ") + (f" 等 {len(incs)} 处" if len(incs) > 1 else "")


def grade(records: dict[str, dict | None], policy: Policy) -> list[Graded]:
    """Every finding of the judged records, graded by ``policy``."""
    out: list[Graded] = []
    for module, rec in records.items():
        if rec is None or is_error(rec):
            continue
        for f in rec.get("findings") or []:
            level = policy.level(module, f)
            out.append(Graded(module, f, level, policy.review_line(module, f) if level == "review" else None,
                              policy.appealable(module, f)))
    return out


def apply_answers(graded: list[Graded], answers: dict[str, str | None], appeal: str | None) -> list[Graded]:
    """``graded`` after a person's answers (line -> decision) and an appeal decision (§4.4)."""
    out = list(graded)
    for line, (module, blocks) in HUMAN_LINES.items():
        answer = answers.get(line)
        if answer not in blocks:
            continue
        mine = [g for g in out if g.module == module and g.level == "review" and g.line == line]
        if not mine:
            continue                              # nothing of the module's waits for this answer
        out = [g for g in out if g not in mine]
        if blocks[answer]:                        # the person's conclusion, on the finding it answers
            out.append(Graded(module, {**mine[0].finding, "message_zh": HUMAN_TEXT[answer]}, "blocking", human=True))
    task = answers.get("task_verdict")
    if task in ("success", "failure"):
        out = [g for g in out if not (g.module == "task_success" and g.code in ("failure", "uncertain"))]
        if task == "failure":
            spec = registry.finding_code("task_success", "failure")
            out.append(Graded("task_success", {"code": "failure", "item": spec.item, "severity": spec.severity,
                                               "message_zh": HUMAN_TEXT["failure"]}, "blocking", human=True))
    if appeal == "restore":
        blocking = [g for g in out if g.level == "blocking"]
        if blocking and all(g.appealable and not g.human for g in blocking):
            out = [Graded(g.module, g.finding, "info", None, g.appealable) if g.level == "blocking" else g for g in out]
    return out


def judge(ep: int, records: dict[str, dict | None], expected: list[str], policy: Policy, *,
          answers: dict[str, str | None] | None = None, appeal: str | None = None) -> Verdict:
    """The verdict of episode ``ep`` from the records of its selected modules. ``expected``: the modules
    whose judgement it needs (a missing record of one of them holds it)."""
    graded = grade(records, policy)
    if answers or appeal:
        graded = apply_answers(graded, answers or {}, appeal)
    errors: list[str] = []
    detail: dict[str, str] = {}
    for m in expected:
        rec = records.get(m)
        if rec is None or is_error(rec):
            errors.append(m)
            detail[m] = cause(rec)
    out = Verdict(ep, "keep", graded, errors, detail)
    if any(g.level == "blocking" for g in graded):
        out.verdict = "drop"
    elif errors:
        out.verdict = "held"
    return out
