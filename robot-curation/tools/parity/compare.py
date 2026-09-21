"""Compare two parity dumps: ``python -m parity compare --golden G --candidate C``.

Checks (all on by default):

* **strict** modules - every normalized record must match exactly (floats by
  ``repr``); for ``dedup`` the duplicate pairs and the action-hash collision
  groups must match.
* **verdict-only** modules (model-driven, not reproducible):
    - ``autolabel``     : whether each unlabeled episode got a caption;
    - ``task_success``  : final verdict + the rules it went through;
    - ``skill_profile`` : whether each episode got a family + its label-audit tier.
  The share of differing episodes must stay under ``--max-diff-rate`` and, with
  ``--noise-floor`` (a second v1 dump of the same data), under
  ``--noise-multiplier`` x v1's own difference rate.
* **final** lists (passed / reject / review) - episodes that are ``error`` on
  either side are left out and listed separately (D24, D33).
* **replay** - when the candidate was replayed, every request must have been on
  the tape.

Exit code 0 when every enabled check passes, 1 otherwise, 2 on bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import records as R

STRICT_DEFAULT = ("timestamp_check", "kinematic_limits", "motion_quality",
                  "visual_quality", "video_action_sync", "dedup")
VERDICT_DEFAULT = ("autolabel", "task_success", "skill_profile")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

class Side:
    """One dump directory, loaded lazily."""

    def __init__(self, path: str):
        self.path = path
        meta_path = os.path.join(path, "dump.json")
        if not os.path.isfile(meta_path):
            raise ValueError(f"{path}: no dump.json (not a parity dump; v2 run directories "
                             "get their own loader once the layout is frozen in W2)")
        with open(meta_path, encoding="utf-8") as fh:
            self.meta = json.load(fh)
        self._cache: dict = {}

    def _json(self, name: str, default):
        p = os.path.join(self.path, name)
        if not os.path.isfile(p):
            return default
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def records(self, module: str) -> dict[int, dict]:
        key = ("records", module)
        if key not in self._cache:
            p = os.path.join(self.path, "records", f"{module}.jsonl")
            rows = R.read_jsonl(p) if os.path.isfile(p) else []
            self._cache[key] = {r["episode_index"]: r for r in rows}
        return self._cache[key]

    def has_records(self, module: str) -> bool:
        return os.path.isfile(os.path.join(self.path, "records", f"{module}.jsonl"))

    def autolabel(self) -> dict[int, dict]:
        p = os.path.join(self.path, "autolabel.jsonl")
        rows = R.read_jsonl(p) if os.path.isfile(p) else []
        return {r["episode_index"]: r for r in rows}

    def dedup(self) -> dict:
        return self._json("dedup.json", {})

    def skill_profile(self) -> dict:
        return self._json("skill_profile.json", {})

    def final(self) -> dict:
        return self._json("final.json", {})

    def error_episodes(self) -> set[int]:
        out = set()
        for name in os.listdir(os.path.join(self.path, "records")) \
                if os.path.isdir(os.path.join(self.path, "records")) else []:
            if name.endswith(".jsonl"):
                for idx, rec in self.records(name[:-len(".jsonl")]).items():
                    if rec.get("verdict") == "error":
                        out.add(idx)
        out |= set(self.final().get("held") or [])
        return out


# ---------------------------------------------------------------------------
# Strict comparison
# ---------------------------------------------------------------------------

def _strict_rows(side: Side, module: str) -> dict[int, dict] | None:
    """Per-episode rows compared exactly; None when the module left no output."""
    if module == "autolabel":
        rows = side.autolabel()
        return rows or None
    if module == "skill_profile":
        sp = side.skill_profile()
        if not sp.get("ran"):
            return None
        rows: dict[int, dict] = {}
        for a in sp.get("assignments") or []:
            rows.setdefault(a["episode_index"], {})["assignment"] = a
        for q in sp.get("label_audit_queue") or []:
            rows.setdefault(q["episode_index"], {}).setdefault("audit", []).append(q)
        return rows
    if not side.has_records(module):
        return None
    return {i: R.comparable(r) for i, r in side.records(module).items()}


def compare_strict_module(g: Side, c: Side, module: str, max_diffs: int) -> dict:
    if module == "dedup":
        return compare_dedup(g, c, max_diffs)
    grows, crows = _strict_rows(g, module), _strict_rows(c, module)
    if grows is None and crows is None:
        return {"mode": "strict", "status": "skipped", "note": "no output on either side"}
    gr, cr = grows or {}, crows or {}
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


def compare_dedup(g: Side, c: Side, max_diffs: int) -> dict:
    gd, cd = g.dedup(), c.dedup()
    if not gd and not cd:
        return {"mode": "strict", "status": "skipped", "note": "no dedup on either side"}
    problems = []
    for key in ("dropped", "action_collisions"):
        d = R.diff_values(gd.get(key), cd.get(key), path=key, limit=max_diffs)
        problems += d
    return {"mode": "strict", "status": "fail" if problems else "pass",
            "golden_dropped": len(gd.get("dropped") or []),
            "candidate_dropped": len(cd.get("dropped") or []),
            "examples": problems[:max_diffs]}


# ---------------------------------------------------------------------------
# Verdict-level comparison
# ---------------------------------------------------------------------------

def _task_key(rec: dict):
    rules = (rec.get("details") or {}).get("rules") or []
    return (rec.get("verdict"), tuple(rules))


def verdict_keys(side: Side, module: str) -> dict[int, object]:
    """Per-episode value compared for a model-driven module."""
    if module == "task_success":
        return {i: _task_key(r) for i, r in side.records("task_success").items()}
    if module == "autolabel":
        return {i: bool(r.get("has_caption")) for i, r in side.autolabel().items()}
    if module == "skill_profile":
        sp = side.skill_profile()
        tiers = {r["episode_index"]: r["tier"] for r in sp.get("label_audit_queue") or []}
        out = {}
        for a in sp.get("assignments") or []:
            fam = a.get("family") or ""
            out[a["episode_index"]] = (fam not in ("", "未归类"), tiers.get(a["episode_index"]))
        for idx, tier in tiers.items():
            out.setdefault(idx, (False, tier))
        return out
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


def compare_verdict_module(g: Side, c: Side, module: str, *, noise: Side | None,
                           max_rate: float, multiplier: float, exclude: set[int],
                           max_diffs: int) -> dict:
    ga, ca = verdict_keys(g, module), verdict_keys(c, module)
    if not ga and not ca:
        return {"mode": "verdict", "status": "skipped", "note": "not run on either side"}
    res = diff_rate(ga, ca, exclude)
    out = {"mode": "verdict", "compared": res["compared"], "different": res["different"],
           "rate": round(res["rate"], 4), "only_in_golden": res["only_in_golden"],
           "only_in_candidate": res["only_in_candidate"],
           "examples": [{"episode_index": i, "golden": _fmt(ga[i]), "candidate": _fmt(ca[i])}
                        for i in res["differing"][:max_diffs]]}
    ok = res["rate"] < max_rate and not res["only_in_golden"] and not res["only_in_candidate"]
    if noise is not None:
        nf = diff_rate(ga, verdict_keys(noise, module), exclude)
        allowed = multiplier * nf["rate"]
        out["noise_floor"] = {"rate": round(nf["rate"], 4), "compared": nf["compared"],
                              "allowed": round(allowed, 4)}
        ok = ok and res["rate"] <= allowed + 1e-12
    out["status"] = "pass" if ok else "fail"
    return out


# ---------------------------------------------------------------------------
# Final lists and replay
# ---------------------------------------------------------------------------

def compare_final(g: Side, c: Side, exclude: set[int], max_diffs: int) -> dict:
    gf, cf = g.final(), c.final()
    if not gf or not cf:
        return {"status": "skipped", "note": "final.json missing"}
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


def check_replay(c: Side) -> dict:
    tape = c.meta.get("tape") or {}
    if tape.get("mode") not in ("replay", "replay-record"):
        return {"status": "skipped", "note": f"candidate tape mode is {tape.get('mode')!r}"}
    hooks = tape.get("hooks") or {}
    misses = int(hooks.get("misses") or 0)
    hits = sum((hooks.get("hits") or {}).values())
    if tape.get("mode") == "replay-record":
        # not a replay-parity run: whatever missed was asked live and recorded
        return {"status": "skipped", "hits": hits, "misses": misses,
                "note": f"replay-record run, {misses} calls recorded live"}
    return {"status": "pass" if misses == 0 else "fail", "hits": hits, "misses": misses,
            "unused_on_tape": hooks.get("unused")}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _csv(value: str) -> list[str]:
    return [x.strip() for x in str(value or "").replace("\n", ",").split(",") if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m parity compare",
                                description="Compare a candidate parity dump with the golden one.")
    p.add_argument("--golden", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--strict", default=",".join(STRICT_DEFAULT),
                   help="modules compared record by record, exactly")
    p.add_argument("--verdict-only", default=",".join(VERDICT_DEFAULT),
                   help="model-driven modules compared at verdict level")
    p.add_argument("--all-strict", action="store_true",
                   help="replay parity: every module, the model-driven ones included, "
                        "must match exactly (overrides --strict / --verdict-only)")
    p.add_argument("--noise-floor", metavar="DUMP",
                   help="a second v1 dump of the same data (v1's own variance)")
    p.add_argument("--max-diff-rate", type=float, default=0.02)
    p.add_argument("--noise-multiplier", type=float, default=1.5)
    p.add_argument("--no-final", action="store_true", help="skip the final-list comparison")
    p.add_argument("--max-diffs", type=int, default=20, help="examples kept per check")
    p.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    return p


def run_compare(args) -> dict:
    g, c = Side(args.golden), Side(args.candidate)
    noise = Side(args.noise_floor) if args.noise_floor else None
    exclude = g.error_episodes() | c.error_episodes()
    strict, verdict_only = _csv(args.strict), _csv(args.verdict_only)
    if args.all_strict:
        strict, verdict_only = list(STRICT_DEFAULT) + list(VERDICT_DEFAULT), []
    result: dict = {"golden": args.golden, "candidate": args.candidate, "modules": {}}
    for module in strict:
        result["modules"][module] = compare_strict_module(g, c, module, args.max_diffs)
    for module in verdict_only:
        result["modules"][module] = compare_verdict_module(
            g, c, module, noise=noise, max_rate=args.max_diff_rate,
            multiplier=args.noise_multiplier, exclude=exclude, max_diffs=args.max_diffs)
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
        extra = ""
        if m.get("mode") == "verdict" and m.get("status") != "skipped":
            extra = f" rate={m['rate']:.2%}"
            if "noise_floor" in m:
                extra += f" (v1 noise {m['noise_floor']['rate']:.2%}, allowed " \
                         f"{m['noise_floor']['allowed']:.2%})"
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
