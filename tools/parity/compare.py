"""Compare two v2 run directories: ``python -m parity compare --golden G --candidate C``.

Both sides are ``run-v2`` directories (``checks/``, ``revisions/``, ``parity.json``): the golden
recorded with ``--fake-vlm``, the candidate replaying its tape with ``--replay`` (design doc 13, plan A).

Checks (all on by default):

* **strict** modules - every record must match exactly (floats by ``repr``); for
  ``duplicate_check`` the groups of identical episodes and the action-hash collision groups.
* **verdict-only** modules (model-driven): ``task_success``'s verdict and the rules it went
  through; the share of differing episodes must stay under ``--max-diff-rate``.
  ``--all-strict`` compares them record by record too (replay parity).
* **final** lists (passed / reject / review) - episodes that are ``error`` on either side
  are left out and listed separately (D24, D33).
* **replay** - when the candidate was replayed, every request must have been on the tape
  and every recorded call asked.

An **adjudication** golden (``run-v2 --from ... --decisions ... --fake-vlm``) is compared
with a replay of the same decisions: the task_success records of the episodes judged
again, exactly; the final lists; the replay. The other modules did not run.

Exit code 0 when every enabled check passes, 1 otherwise, 2 on bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import records as R

STRICT_DEFAULT = ("timestamp_check", "kinematic_limits", "motion_quality",
                  "visual_quality", "video_action_sync", "duplicate_check", "camera_defects")
VERDICT_DEFAULT = ("task_success",)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

class RunDir:
    """A v2 run directory (design doc 06 §1); the revision read is the newest committed one,
    else the newest one. ``parity.json`` (written by ``run-v2``) carries the tape statistics."""

    def __init__(self, path: str):
        if not (os.path.isdir(os.path.join(path, "checks"))
                or os.path.isdir(os.path.join(path, "revisions"))):
            raise ValueError(f"{path}: not a v2 run directory (no checks/ or revisions/)")
        self.path = path
        self._cache: dict = {}
        self.meta: dict = {}
        meta_path = os.path.join(path, "parity.json")
        if os.path.isfile(meta_path):
            with open(meta_path, encoding="utf-8") as fh:
                self.meta = json.load(fh)
        self.revision_dir = self._revision()

    def _json(self, *parts: str, default=None):
        p = os.path.join(self.path, *parts)
        if not os.path.isfile(p):
            return default
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def _revision(self) -> str | None:
        base = os.path.join(self.path, "revisions")
        names = sorted(n for n in (os.listdir(base) if os.path.isdir(base) else [])
                       if len(n) == 5 and n[0] == "r" and n[1:].isdigit())
        committed = [n for n in names if os.path.isfile(os.path.join(base, n, "commit.json"))]
        pick = (committed or names or [None])[-1]
        return os.path.join(base, pick) if pick else None

    def records(self, module: str) -> dict[int, dict]:
        key = ("records", module)
        if key not in self._cache:
            p = os.path.join(self.path, "checks", module, "results.jsonl")
            rows = R.read_jsonl(p) if os.path.isfile(p) else []
            self._cache[key] = {r["episode_index"]: r for r in rows}
        return self._cache[key]

    def has_records(self, module: str) -> bool:
        return os.path.isfile(os.path.join(self.path, "checks", module, "results.jsonl"))

    def duplicates(self) -> dict:
        return self._json("checks", "duplicate_check", "groups.json", default={})

    def final(self) -> dict:
        """The revision's ``passed`` / ``reject`` / ``held`` and the ``review`` view without pure
        appeal entries."""
        if not self.revision_dir:
            return {}
        out: dict = {}
        for name in ("passed", "reject", "held", "review"):
            doc = self._json(os.path.relpath(os.path.join(self.revision_dir, f"{name}.json"), self.path))
            if doc is None:
                return {}
            eps = doc.get("episodes") or []
            if name == "review":
                eps = [e for e in eps if any(i.get("kind") != "reject_appeal" for i in e.get("review") or [])]
            out[name] = sorted(int(e["episode_index"]) for e in eps)
        return out

    def judged_again(self, module: str) -> set[int]:
        """Episodes whose current record comes from a part after the first run's."""
        d = os.path.join(self.path, "checks", module, "parts")
        parts = sorted(n[:-len(".jsonl")] for n in (os.listdir(d) if os.path.isdir(d) else [])
                       if n.endswith(".jsonl"))
        latest: dict[int, str] = {}
        for part in parts:
            for rec in R.read_jsonl(os.path.join(d, f"{part}.jsonl")):
                latest[int(rec["episode_index"])] = part
        return {e for e, part in latest.items() if parts and part != parts[0]}

    def error_episodes(self) -> set[int]:
        out = set()
        base = os.path.join(self.path, "checks")
        for name in os.listdir(base) if os.path.isdir(base) else []:
            for idx, rec in self.records(name).items():
                if R.is_error(rec):
                    out.add(idx)
        return out | set(self.final().get("held") or [])


# ---------------------------------------------------------------------------
# Strict comparison
# ---------------------------------------------------------------------------

def _strict_diff(gr: dict, cr: dict, max_diffs: int) -> dict:
    only_g, only_c = sorted(set(gr) - set(cr)), sorted(set(cr) - set(gr))
    diffs, n_diff = [], 0
    for idx in sorted(set(gr) & set(cr)):
        d = R.diff_values(gr[idx], cr[idx], limit=10)
        if d:
            n_diff += 1
            if len(diffs) < max_diffs:
                diffs.append({"episode_index": idx, "differences": d})
    ok = not (only_g or only_c or n_diff)
    return {"mode": "strict", "status": "pass" if ok else "fail",
            "compared": len(set(gr) & set(cr)), "different": n_diff,
            "only_in_golden": only_g, "only_in_candidate": only_c, "examples": diffs}


def compare_strict_module(g: RunDir, c: RunDir, module: str, max_diffs: int) -> dict:
    if module == "duplicate_check":
        return compare_duplicates(g, c, max_diffs)
    if not g.has_records(module) and not c.has_records(module):
        return {"mode": "strict", "status": "skipped", "note": "no output on either side"}
    return _strict_diff({i: R.comparable(r) for i, r in g.records(module).items()},
                        {i: R.comparable(r) for i, r in c.records(module).items()}, max_diffs)


def duplicate_groups(doc: dict) -> list[list[int]]:
    """The groups of identical episodes (D87), each sorted."""
    return sorted(sorted(int(e) for e in g) for g in doc.get("groups") or [])


def compare_duplicates(g: RunDir, c: RunDir, max_diffs: int) -> dict:
    gd, cd = g.duplicates(), c.duplicates()
    if not gd and not cd:
        return {"mode": "strict", "status": "skipped", "note": "no duplicate check on either side"}
    gg, cg = duplicate_groups(gd), duplicate_groups(cd)
    problems = R.diff_values(gg, cg, path="groups", limit=max_diffs)
    problems += R.diff_values(gd.get("action_collisions"), cd.get("action_collisions"),
                              path="action_collisions", limit=max_diffs)
    return {"mode": "strict", "status": "fail" if problems else "pass",
            "golden_groups": len(gg), "candidate_groups": len(cg),
            "examples": problems[:max_diffs]}


def compare_adjudicated_task(g: RunDir, c: RunDir, max_diffs: int,
                             exclude: set[int] = frozenset()) -> dict:
    """The task_success records the adjudication judged again (parts after the first run's),
    exactly; a re-judge that failed on either side is left out and listed."""
    g_eps = g.judged_again("task_success") - set(exclude)
    c_eps = c.judged_again("task_success") - set(exclude)
    gr = {i: R.comparable(r) for i, r in g.records("task_success").items() if i in g_eps}
    cr = {i: R.comparable(r) for i, r in c.records("task_success").items() if i in c_eps}
    out = _strict_diff(gr, cr, max_diffs)
    out["judged_again"] = sorted(set(gr) | set(cr))
    out["excluded_errors"] = sorted(set(exclude) & (g.judged_again("task_success")
                                                   | c.judged_again("task_success")))
    return out


# ---------------------------------------------------------------------------
# Verdict-level comparison
# ---------------------------------------------------------------------------

def verdict_keys(side: RunDir, module: str) -> dict[int, object]:
    """Per-episode value compared for a model-driven module."""
    if module == "task_success":
        return {i: (R.verdict_of(r), tuple((r.get("details") or {}).get("rules") or []))
                for i, r in side.records("task_success").items()}
    raise ValueError(f"no verdict-level rule for module {module!r}")


def diff_rate(ga: dict, ca: dict, exclude: set[int] = frozenset()) -> dict:
    common = sorted((set(ga) & set(ca)) - set(exclude))
    differing = [i for i in common if ga[i] != ca[i]]
    return {"compared": len(common), "different": len(differing),
            "rate": (len(differing) / len(common)) if common else 0.0,
            "differing": differing,
            "only_in_golden": sorted(set(ga) - set(ca) - set(exclude)),
            "only_in_candidate": sorted(set(ca) - set(ga) - set(exclude))}


def _fmt(v):
    return list(v) if isinstance(v, tuple) else v


def compare_verdict_module(g: RunDir, c: RunDir, module: str, *, max_rate: float,
                           exclude: set[int], max_diffs: int) -> dict:
    ga, ca = verdict_keys(g, module), verdict_keys(c, module)
    if not ga and not ca:
        return {"mode": "verdict", "status": "skipped", "note": "not run on either side"}
    res = diff_rate(ga, ca, exclude)
    ok = res["rate"] < max_rate and not res["only_in_golden"] and not res["only_in_candidate"]
    return {"mode": "verdict", "status": "pass" if ok else "fail",
            "compared": res["compared"], "different": res["different"],
            "rate": round(res["rate"], 4), "only_in_golden": res["only_in_golden"],
            "only_in_candidate": res["only_in_candidate"],
            "examples": [{"episode_index": i, "golden": _fmt(ga[i]), "candidate": _fmt(ca[i])}
                         for i in res["differing"][:max_diffs]]}


# ---------------------------------------------------------------------------
# Final lists and replay
# ---------------------------------------------------------------------------

def compare_final(g: RunDir, c: RunDir, exclude: set[int], max_diffs: int) -> dict:
    gf, cf = g.final(), c.final()
    if not gf or not cf:
        return {"status": "skipped", "note": "result lists missing"}
    out, ok = {"excluded_errors": sorted(exclude)}, True
    for key in ("passed", "reject", "review"):
        gs, cs = set(gf.get(key) or []) - exclude, set(cf.get(key) or []) - exclude
        miss, extra = sorted(gs - cs), sorted(cs - gs)
        out[key] = {"golden": len(gs), "candidate": len(cs),
                    "missing_in_candidate": miss[:max_diffs],
                    "extra_in_candidate": extra[:max_diffs]}
        ok = ok and not miss and not extra
    out["status"] = "pass" if ok else "fail"
    return out


def check_replay(c: RunDir) -> dict:
    tape = c.meta.get("tape") or {}
    if tape.get("mode") != "replay":
        return {"status": "skipped", "note": f"candidate tape mode is {tape.get('mode')!r}"}
    hooks = tape.get("hooks") or {}
    misses = int(hooks.get("misses") or 0)
    hits = sum((hooks.get("hits") or {}).values())
    unused = hooks.get("unused")
    # the call graph must be the same both ways: nothing asked that the golden did not ask
    # (misses), nothing the golden asked left unasked (unused)
    ok = misses == 0 and not unused
    return {"status": "pass" if ok else "fail", "hits": hits, "misses": misses,
            "unused_on_tape": unused}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _csv(value: str) -> list[str]:
    return [x.strip() for x in str(value or "").replace("\n", ",").split(",") if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m parity compare",
                                description="Compare a candidate v2 run directory with the golden one.")
    p.add_argument("--golden", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--strict", default=",".join(STRICT_DEFAULT),
                   help="modules compared record by record, exactly")
    p.add_argument("--verdict-only", default=",".join(VERDICT_DEFAULT),
                   help="model-driven modules compared at verdict level")
    p.add_argument("--all-strict", action="store_true",
                   help="replay parity: every module, the model-driven ones included, "
                        "must match exactly (overrides --strict / --verdict-only)")
    p.add_argument("--max-diff-rate", type=float, default=0.02)
    p.add_argument("--no-final", action="store_true", help="skip the final-list comparison")
    p.add_argument("--max-diffs", type=int, default=20, help="examples kept per check")
    p.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    return p


def run_compare(args) -> dict:
    g, c = RunDir(args.golden), RunDir(args.candidate)
    exclude = g.error_episodes() | c.error_episodes()
    strict, verdict_only = _csv(args.strict), _csv(args.verdict_only)
    if args.all_strict:
        strict, verdict_only = list(STRICT_DEFAULT) + list(VERDICT_DEFAULT), []
    result: dict = {"golden": args.golden, "candidate": args.candidate, "modules": {}}
    if g.meta.get("kind") == "v2-adjudication":
        strict, verdict_only = [], []
        result["modules"]["task_success"] = compare_adjudicated_task(g, c, args.max_diffs, exclude)
    for module in strict:
        result["modules"][module] = compare_strict_module(g, c, module, args.max_diffs)
    for module in verdict_only:
        result["modules"][module] = compare_verdict_module(
            g, c, module, max_rate=args.max_diff_rate, exclude=exclude, max_diffs=args.max_diffs)
    if not args.no_final:
        result["final"] = compare_final(g, c, exclude, args.max_diffs)
    result["replay"] = check_replay(c)
    statuses = [m["status"] for m in result["modules"].values()]
    statuses += [result.get("final", {}).get("status", "skipped"), result["replay"]["status"]]
    result["conclusion"] = "fail" if "fail" in statuses else "pass"
    return result


def render(result: dict) -> str:
    lines = [f"golden:    {result['golden']}", f"candidate: {result['candidate']}", ""]
    for name, m in result["modules"].items():
        extra = f" rate={m['rate']:.2%}" if m.get("mode") == "verdict" and m.get("status") != "skipped" else ""
        count = (f"{m.get('different', 0)}/{m.get('compared', 0)} differ"
                 if "compared" in m else "")
        lines.append(f"  {m['status'].upper():7} {name:18} {m.get('mode', ''):8} {count}{extra}")
        for ex in (m.get("examples") or [])[:3]:
            lines.append(f"           e.g. {json.dumps(ex, ensure_ascii=False)[:240]}")
    if "final" in result:
        f = result["final"]
        lines.append(f"  {f['status'].upper():7} final lists")
        for key in ("passed", "reject", "review"):
            if key in f:
                lines.append(f"           {key}: golden {f[key]['golden']}, candidate "
                             f"{f[key]['candidate']}, missing {f[key]['missing_in_candidate']}, "
                             f"extra {f[key]['extra_in_candidate']}")
        if f.get("excluded_errors"):
            lines.append(f"           excluded (error on either side): {f['excluded_errors']}")
    r = result["replay"]
    lines.append(f"  {r['status'].upper():7} replay         "
                 + (f"hits={r.get('hits')} misses={r.get('misses')}" if "hits" in r
                    else r.get("note", "")))
    lines += ["", f"conclusion: {result['conclusion'].upper()}"]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_compare(args)
    except (ValueError, OSError) as e:
        print(f"compare: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1, allow_nan=True))
    else:
        print(render(result))
    return 0 if result["conclusion"] == "pass" else 1
