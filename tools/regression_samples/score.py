#!/usr/bin/env python
"""Score platform runs against a regression sample set's expectation.json (design doc 16 §8).

One run directory per subset (what ``curation check`` / the Daemon write: ``checks/<module>/``, ``revisions/rNNNN/``,
``preflight.json``). ``finding_map.json`` turns each module's per-episode record into check items; the expectation
says which items each episode should (present) or should not (absent) show. Per item:

    TP = expected present, reported      FN = expected present, not reported
    FP = expected absent, reported       TN = expected absent, not reported

An episode whose mapped modules did not run on it (funnel short circuit, module not selected) is ``not_assessed``;
one whose mapped modules all failed to execute is ``error``; neither counts in precision or recall. Items without a
rule are ``no_check`` - the platform's gaps. A camera-scoped expectation only matches a finding on the same camera.

    PYTHONPATH=tools python -m regression_samples.score --expectation <set>/expectation.json \\
        --runs-root <runs> --runs-map <runs>/runs.json --out score.json --markdown score.md \\
        [--baseline previous.json --max-drop 0.05 --min-support 5]

Exit codes: 0 scored; 2 bad input (or --require-all-runs and a subset has no run); 3 a metric fell against
the baseline by more than --max-drop.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SCHEMA_VERSION = "1.0"
TIMESTAMP_FAIL_KINDS = ("out_of_order", "gap", "fragment", "jitter")
#: preflight availabilities under which a module never runs on the subset
UNAVAILABLE = ("unsupported", "needs_input", "unavailable")


class InputError(Exception):
    pass


# ---------------------------------------------------------------- scopes

def short_camera(name) -> str:
    return str(name).split("observation.images.")[-1].lower()


def scope_cameras(scope):
    """The cameras an expectation entry is about, or None when it is about the whole episode."""
    if not scope:
        return None
    if isinstance(scope, str):                       # clean entries carry "k=v k=v"
        parsed = dict(p.split("=", 1) for p in scope.split() if "=" in p)
        scope = parsed
    cams = []
    if isinstance(scope.get("streams"), list):
        cams = [s for s in scope["streams"]]
    elif scope.get("stream"):
        cams = [scope["stream"]]
    elif scope.get("camera"):
        cams = [scope["camera"]]
    cams = [short_camera(c) for c in cams if str(c) not in ("action", "timestamp", "observation.state")]
    return frozenset(cams) or None


# ---------------------------------------------------------------- inputs

def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as ex:
        raise InputError(f"cannot read {path}: {ex}") from None


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def _platform_loader():
    """The platform's own reader of the current records (handles the episode-state index), when importable."""
    backend = os.path.join(REPO, "backend")
    if os.path.isdir(backend) and backend not in sys.path:
        sys.path.insert(0, backend)
    try:
        from curation.pipeline.records import latest_results  # noqa: WPS433
        return latest_results
    except Exception:  # noqa: BLE001 - scoring works from the files alone
        return None


_LOADER = None


def module_records(run_dir, module):
    """episode index -> the current record of ``module`` in ``run_dir``."""
    global _LOADER
    mdir = os.path.join(run_dir, "checks", module)
    if not os.path.isdir(mdir):
        return {}
    if _LOADER is None:
        _LOADER = _platform_loader() or False
    if _LOADER:
        try:
            return {int(k): v for k, v in _LOADER(run_dir, module).items()}
        except Exception:  # noqa: BLE001 - fall back to the files
            pass
    out = {}
    parts = sorted(glob.glob(os.path.join(mdir, "parts", "*.jsonl")))
    for path in parts or [os.path.join(mdir, "results.jsonl")]:
        if os.path.exists(path):
            for rec in read_jsonl(path):
                out[int(rec["episode_index"])] = rec
    return out


def funnel_verdicts(run_dir):
    revs = sorted(glob.glob(os.path.join(run_dir, "revisions", "r*", "verdicts.jsonl")))
    if not revs:
        return {}
    return {int(r["episode_index"]): r for r in read_jsonl(revs[-1])}


def preflight(run_dir):
    """(format supported or None, {module: availability})."""
    path = os.path.join(run_dir, "preflight.json")
    if not os.path.exists(path):
        return None, {}
    p = load_json(path)
    avail = {m.get("id"): m.get("availability") for m in p.get("modules") or [] if isinstance(m, dict)}
    return bool((p.get("format") or {}).get("supported")), avail


# ---------------------------------------------------------------- rules

def _details(rec):
    return rec.get("details") or rec.get("detail") or {}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _dig(d, dotted):
    for k in dotted.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def timestamp_fail_kind(d):
    """Why a timestamp check failed, from the keys the check leaves (as the platform's report reads them)."""
    if d.get("gap_frames"):
        return "gap"
    if "jitter_ratio" in d:
        return "jitter"
    if "ts" in d and "frame" in d:
        return "out_of_order"
    if "duration_s" in d and "dt_nominal" not in d:
        return "fragment"
    return "other"


def apply_rule(rule, rec):
    """The (camera or None) hits of one rule on one record."""
    m = rule["match"]
    d = _details(rec)
    hits = []
    if "finding_codes" in m:
        rx = re.compile(m["message_regex"]) if m.get("message_regex") else None
        for f in d.get("findings") or []:
            if f.get("code") in m["finding_codes"] and (rx is None or rx.search(str(f.get("message", "")) + str((f.get("args") or {}).get("error", "")))):
                hits.append(short_camera(f["camera"]) if f.get("camera") else None)
    if "timestamp_fail_kinds" in m and rec.get("verdict") == "fail":
        if timestamp_fail_kind(d) in m["timestamp_fail_kinds"] and (
                not m.get("reason_regex") or re.search(m["reason_regex"], str(d.get("reason", "")))):
            hits.append(None)
    if "violation_types" in m:
        if any(isinstance(v, dict) and v.get("type") in m["violation_types"] for v in d.get("violations") or []):
            hits.append(None)
    if "subscore" in m:
        v = _num(d.get(m["subscore"]))
        if v is not None and v < m["below"]:
            hits.append(None)
    if "detail_nonempty" in m and d.get(m["detail_nonempty"]):
        hits.append(None)
    if "camera_detail" in m:
        per = d.get("per_camera_detail") or {}
        for cam, cd in per.items():
            v = _num((cd or {}).get(m["camera_detail"])) if isinstance(cd, dict) else None
            if v is None:
                continue
            if ("below" in m and v < m["below"]) or ("at_least" in m and v >= m["at_least"]):
                hits.append(short_camera(cam))
    if "camera_list" in m:
        for cam in _dig(d, m["camera_list"]) or []:
            hits.append(short_camera(cam))
    if "detail_verdicts" in m and d.get("verdict") in m["detail_verdicts"]:
        hits.append(None)
    if "camera_codes" in m:
        for cam, cd in (d.get("per_camera") or {}).items():
            if isinstance(cd, dict) and cd.get("code") in m["camera_codes"] and cd.get("trusted", True):
                hits.append(short_camera(cam))
    if "detail_outcomes" in m and d.get("outcome") in m["detail_outcomes"]:
        hits.append(None)
    if "passed" in m and rec.get("passed") in m["passed"] and rec.get("verdict") != "error":
        hits.append(None)
    return hits


# ---------------------------------------------------------------- scoring

def expectation_index(exp):
    """(dataset, episode) -> {item: {"present": [scopes], "absent": [scopes]}}, plus lineage and verdict."""
    idx, meta = {}, {}
    for e in exp["episodes"]:
        key = (e["dataset"], int(e["episode_index"]))
        items = defaultdict(lambda: {"present": [], "absent": []})
        for group, side in (("problems", "present"), ("phenomena", "present"), ("clean", "absent")):
            for entry in e.get(group) or []:
                items[entry["item"]][side].append(scope_cameras(entry.get("scope")))
        idx[key] = dict(items)
        meta[key] = {"episode_id": e.get("episode_id") or f"{key[0]}:{key[1]}", "lineage": e.get("lineage") or f"{key[0]}:{key[1]}",
                     "verdict": e.get("verdict")}
    return idx, meta


def _same_camera(a, b):
    """Camera names from different writers: 'wrist' / 'observation.images.wrist', 'robot0' / 'robot0_sensor_camera0_compressed'."""
    return a == b or a.startswith(b + "_") or b.startswith(a + "_")


def _matches(hit_cams, scopes):
    """Whether any hit (a camera or None) lands on any of the expectation scopes (a camera set or None)."""
    for s in scopes:
        for h in hit_cams:
            if h is None or s is None or any(_same_camera(h, c) for c in s):
                return True
    return False


def _ratio(a, b):
    return round(a / b, 4) if b else None


def score(expectation, taxonomy, fmap, runs, by_lineage=False, sample_cap=20):
    """runs: {subset: run_dir}. Returns the score document."""
    idx, meta = expectation_index(expectation)
    items_meta = {i["id"]: i for i in taxonomy["items"]}
    rules = fmap["rules"]
    for r in rules:
        unknown = [i for i in r["items"] if i not in items_meta]
        if unknown:
            raise InputError(f"rule {r['id']} names items not in the taxonomy: {unknown}")
    item_modules = defaultdict(set)
    for r in rules:
        for i in r["items"]:
            item_modules[i].add(r["module"])
    modules = sorted({r["module"] for r in rules})
    controls = (fmap.get("controls") or {}).get("items") or {}
    ingestion_items = set((fmap.get("ingestion") or {}).get("items") or [])

    lineage_size = Counter(m["lineage"] for m in meta.values())
    weight = {k: (1.0 / lineage_size[m["lineage"]] if by_lineage else 1.0) for k, m in meta.items()}

    subsets = sorted({k[0] for k in idx})
    missing = [s for s in subsets if s not in runs]
    stats = defaultdict(lambda: Counter())
    samples = defaultdict(lambda: defaultdict(list))
    episode_level = Counter()
    ep_samples = defaultdict(list)
    control_stats = defaultdict(Counter)
    control_fail = defaultdict(list)
    ingestion = {}
    rule_hits = Counter()
    unmapped_codes = Counter()
    mapped_codes = {c for r in rules for c in r["match"].get("finding_codes", [])}
    scored_eps = 0

    for subset in subsets:
        if subset not in runs:
            continue
        run_dir = runs[subset]
        recs = {m: module_records(run_dir, m) for m in modules}
        verdicts = funnel_verdicts(run_dir)
        supported, availability = preflight(run_dir)
        keys = sorted(k for k in idx if k[0] == subset)
        if any(ingestion_items & set(idx[k]) for k in keys):
            ingestion[subset] = {"handled": bool(supported), "preflight_supported": supported}
        for key in keys:
            ep = key[1]
            scored_eps += 1
            w = weight[key]
            hits = defaultdict(list)                 # item -> cameras
            state = {}                               # module -> "ok" | "error" | None
            flagged_any = False
            for m in modules:
                rec = recs[m].get(ep)
                if rec is None:
                    # not run on this episode: the module cannot read the dataset (preflight), or the funnel / the
                    # module selection left the episode out
                    state[m] = "unsupported" if availability.get(m) in UNAVAILABLE else None
                    continue
                if rec.get("verdict") == "error":
                    state[m] = "error"
                    continue
                state[m] = "ok"
                for r in rules:
                    if r["module"] != m:
                        continue
                    h = apply_rule(r, rec)
                    if h:
                        rule_hits[r["id"]] += 1
                        for i in r["items"]:
                            hits[i].extend(h)
                if m == "data_integrity":
                    for f in _details(rec).get("findings") or []:
                        if f.get("code") not in mapped_codes:
                            unmapped_codes[f.get("code")] += 1
            v = verdicts.get(ep) or {}
            dropped = v.get("verdict") == "drop"
            flagged_any = dropped or any(hits.values())
            # episode level
            ev = meta[key]["verdict"]
            if ev in ("defective", "clean_on_checked_items") and v:
                if v.get("verdict") == "held":
                    episode_level["held_" + ("defective" if ev == "defective" else "clean")] += w
                else:
                    cell = {("defective", True): "tp", ("defective", False): "fn",
                            ("clean_on_checked_items", True): "fp", ("clean_on_checked_items", False): "tn"}[(ev, dropped)]
                    episode_level[cell] += w
                    if cell in ("fp", "fn") and len(ep_samples[cell]) < sample_cap:
                        ep_samples[cell].append(meta[key]["episode_id"])
            # items
            for item, sides in idx[key].items():
                if item in controls and sides["present"]:
                    c = controls[item]
                    bad = [i for i in c.get("not_items", []) if hits.get(i)]
                    failed = bool(bad) or (c.get("not_drop") and dropped)
                    control_stats[item]["fail" if failed else "pass"] += w
                    if failed and len(control_fail[item]) < sample_cap:
                        control_fail[item].append(meta[key]["episode_id"])
                    continue
                if item in ingestion_items:
                    continue
                s = stats[item]
                mods = item_modules.get(item)
                st = [state.get(m) for m in mods] if mods else []
                for side in ("present", "absent"):
                    if not sides[side]:
                        continue
                    s[side] += w
                    if not mods:
                        s["no_check_" + side] += w
                        continue
                    if all(x in (None, "unsupported") for x in st):
                        s["not_assessed_" + side] += w
                        if all(x == "unsupported" for x in st):
                            s["unsupported_" + side] += w
                        continue
                    if all(x in (None, "unsupported", "error") for x in st):
                        s["error_" + side] += w
                        continue
                    found = hits.get(item, [])
                    if side == "absent" and sides["present"]:
                        # the same item is expected on another camera of the episode: only a finding on one of
                        # the clean cameras is a false alarm (an unattributed finding belongs to the present side)
                        found = [h for h in found if h is not None]
                    reported = _matches(found, sides[side])
                    cell = {("present", True): "tp", ("present", False): "fn", ("absent", True): "fp", ("absent", False): "tn"}[(side, reported)]
                    s[cell] += w
                    if side == "present" and flagged_any:
                        s["flagged_any"] += w
                    if cell in ("fp", "fn") and len(samples[item][cell]) < sample_cap:
                        samples[item][cell].append(meta[key]["episode_id"])

    items_out = {}
    for it in taxonomy["items"]:
        iid = it["id"]
        s = stats.get(iid, Counter())
        mods = sorted(item_modules.get(iid, []))
        row = {"name": it["name"], "kind": it["kind"], "level": it["level"], "mapped": bool(mods), "modules": mods,
               "present": _r(s["present"]), "absent": _r(s["absent"])}
        for k in ("tp", "fn", "fp", "tn"):
            row[k] = _r(s[k])
        row["not_assessed"] = {"present": _r(s["not_assessed_present"]), "absent": _r(s["not_assessed_absent"]),
                               "of_which_unsupported": _r(s["unsupported_present"] + s["unsupported_absent"])}
        row["error"] = {"present": _r(s["error_present"]), "absent": _r(s["error_absent"])}
        row["precision"] = _ratio(s["tp"], s["tp"] + s["fp"])
        row["recall"] = _ratio(s["tp"], s["tp"] + s["fn"])
        row["recall_any"] = _ratio(s["flagged_any"], s["tp"] + s["fn"])
        if samples[iid]:
            row["samples"] = {k: v for k, v in samples[iid].items()}
        if iid in controls:
            cs = control_stats.get(iid, Counter())
            row["control"] = {"pass": _r(cs["pass"]), "fail": _r(cs["fail"]), "pass_rate": _ratio(cs["pass"], cs["pass"] + cs["fail"]),
                              "failed": control_fail.get(iid, [])}
        items_out[iid] = row
    el = {k: _r(episode_level[k]) for k in ("tp", "fn", "fp", "tn", "held_defective", "held_clean")}
    el["precision"] = _ratio(episode_level["tp"], episode_level["tp"] + episode_level["fp"])
    el["recall"] = _ratio(episode_level["tp"], episode_level["tp"] + episode_level["fn"])
    if ep_samples:
        el["samples"] = dict(ep_samples)
    return {
        "schema_version": SCHEMA_VERSION,
        "set": expectation.get("set"), "set_version": expectation.get("set_version"),
        "taxonomy_version": taxonomy.get("taxonomy_version"), "map_version": fmap.get("schema_version"),
        "by_lineage": by_lineage,
        "episodes": {"expected": len(idx), "scored": scored_eps, "subsets": len(subsets), "runs": len([s for s in subsets if s in runs]),
                     "missing_runs": missing},
        "episode_level": el,
        "items": items_out,
        "ingestion": ingestion,
        "rule_hits": dict(sorted(rule_hits.items())),
        "unmapped_integrity_codes": dict(unmapped_codes),
    }


def _r(x):
    return round(x, 4) if isinstance(x, float) and not float(x).is_integer() else int(x)


# ---------------------------------------------------------------- baseline

def compare(current, baseline, max_drop, min_support):
    """Metrics that fell by more than max_drop (items with at least min_support in the current run)."""
    out = []

    def check(name, cur, base, support):
        if cur is None or base is None or support < min_support:
            return
        if base - cur > max_drop:
            out.append({"metric": name, "baseline": base, "current": cur, "drop": round(base - cur, 4), "support": support})

    el, bl = current["episode_level"], baseline.get("episode_level") or {}
    check("episode.precision", el.get("precision"), bl.get("precision"), el["tp"] + el["fp"])
    check("episode.recall", el.get("recall"), bl.get("recall"), el["tp"] + el["fn"])
    for iid, row in current["items"].items():
        b = (baseline.get("items") or {}).get(iid)
        if not b:
            continue
        check(f"{iid}.precision", row.get("precision"), b.get("precision"), row["tp"] + row["fp"])
        check(f"{iid}.recall", row.get("recall"), b.get("recall"), row["tp"] + row["fn"])
        if "control" in row and "control" in b:
            c = row["control"]
            check(f"{iid}.control_pass_rate", c.get("pass_rate"), b["control"].get("pass_rate"), c["pass"] + c["fail"])
    return out


# ---------------------------------------------------------------- report

def markdown(doc, regressions=None):
    L = []
    L.append(f"# Score: {doc.get('set')} {doc.get('set_version')} (taxonomy {doc.get('taxonomy_version')}, map {doc.get('map_version')})")
    e = doc["episodes"]
    L.append(f"\n{e['scored']} of {e['expected']} episodes scored, {e['runs']} of {e['subsets']} subsets have a run"
             + (f"; no run for: {', '.join(e['missing_runs'])}" if e["missing_runs"] else "") + (" (weighted by lineage)" if doc["by_lineage"] else "") + ".")
    el = doc["episode_level"]
    L.append(f"\n**Episode level** (platform drop vs expected defective): precision {_fmt(el['precision'])}, recall {_fmt(el['recall'])} "
             f"(TP {el['tp']}, FP {el['fp']}, FN {el['fn']}, TN {el['tn']}; held {el['held_defective']} defective / {el['held_clean']} clean)")
    L.append("\n| item | name | kind | present | absent | TP | FP | FN | TN | precision | recall | recall (any flag) | not assessed (module unsupported) | errors |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    gaps = []
    for iid, r in doc["items"].items():
        if not (r["present"] or r["absent"]) or "control" in r:
            continue
        if not r["mapped"]:
            gaps.append(f"{iid} {r['name']} (present {r['present']}, absent {r['absent']})")
            continue
        na = f"{r['not_assessed']['present'] + r['not_assessed']['absent']} ({r['not_assessed']['of_which_unsupported']})"
        er = r["error"]["present"] + r["error"]["absent"]
        L.append(f"| {iid} | {r['name']} | {r['kind']} | {r['present']} | {r['absent']} | {r['tp']} | {r['fp']} | {r['fn']} | {r['tn']} | "
                 f"{_fmt(r['precision'])} | {_fmt(r['recall'])} | {_fmt(r['recall_any'])} | {na} | {er} |")
    ctl = [(iid, r) for iid, r in doc["items"].items() if "control" in r and (r["control"]["pass"] or r["control"]["fail"])]
    if ctl:
        L.append("\n**Controls** (must not be reported)")
        for iid, r in ctl:
            c = r["control"]
            L.append(f"- {iid} {r['name']}: pass {c['pass']}, fail {c['fail']}" + (f" ({', '.join(c['failed'])})" if c["failed"] else ""))
    if doc["ingestion"]:
        bad = [s for s, v in doc["ingestion"].items() if not v["handled"]]
        L.append(f"\n**Ingestion** (SET-4): {len(doc['ingestion']) - len(bad)} of {len(doc['ingestion'])} subsets read" + (f"; not read: {', '.join(bad)}" if bad else ""))
    if gaps:
        L.append("\n**No check on the platform** (expectations exist, no rule maps to them):")
        L.extend(f"- {g}" for g in gaps)
    if regressions is not None:
        L.append("\n**Against the baseline**: " + ("no regression" if not regressions else f"{len(regressions)} regression(s)"))
        for g in regressions:
            L.append(f"- {g['metric']}: {g['baseline']} → {g['current']} (−{g['drop']}, support {g['support']})")
    return "\n".join(L) + "\n"


def _fmt(v):
    return "–" if v is None else f"{v:.2f}"


# ---------------------------------------------------------------- cli

def resolve_runs(args, set_name):
    runs = {}
    if args.runs_map:
        root = args.runs_root or os.path.dirname(os.path.abspath(args.runs_map))
        for name, v in load_json(args.runs_map).items():
            if isinstance(v, str):
                v = {"subset": v}
            if v.get("set") and set_name and v["set"] != set_name:
                continue
            d = os.path.join(root, name)
            if os.path.isdir(d):
                runs[v["subset"]] = d
    for spec in args.run or []:
        if "=" not in spec:
            raise InputError(f"--run takes SUBSET=RUN_DIR, got {spec!r}")
        subset, d = spec.split("=", 1)
        if not os.path.isdir(d):
            raise InputError(f"run directory not found: {d}")
        runs[subset] = d
    return runs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--expectation", required=True, help="the set's expectation.json")
    ap.add_argument("--taxonomy", help="taxonomy.json (default: next to the expectation, else the one in this directory)")
    ap.add_argument("--map", default=os.path.join(HERE, "finding_map.json"), help="finding_map.json")
    ap.add_argument("--runs-map", help="JSON {run_name: {set, subset}} (baseline/<commit>/runs.json)")
    ap.add_argument("--runs-root", help="directory holding the run directories named in --runs-map")
    ap.add_argument("--run", action="append", help="SUBSET=RUN_DIR, repeatable")
    ap.add_argument("--by-lineage", action="store_true", help="weight each episode by 1/(episodes sharing its lineage)")
    ap.add_argument("--out", help="write the score JSON here")
    ap.add_argument("--markdown", help="write a Markdown summary here")
    ap.add_argument("--baseline", help="an earlier score JSON to compare against")
    ap.add_argument("--max-drop", type=float, default=0.05, help="tolerated fall of a precision / recall / pass rate (default 0.05)")
    ap.add_argument("--min-support", type=int, default=5, help="compare a metric only with at least this many counted episodes (default 5)")
    ap.add_argument("--require-all-runs", action="store_true", help="exit 2 when a subset of the expectation has no run")
    a = ap.parse_args(argv)
    try:
        exp = load_json(a.expectation)
        tax_path = a.taxonomy or next((p for p in (os.path.join(os.path.dirname(os.path.abspath(a.expectation)), "taxonomy.json"),
                                                   os.path.join(HERE, "taxonomy.json")) if os.path.exists(p)), None)
        if not tax_path:
            raise InputError("no taxonomy.json found; pass --taxonomy")
        tax = load_json(tax_path)
        fmap = load_json(a.map)
        if fmap.get("taxonomy_version") != tax.get("taxonomy_version"):
            raise InputError(f"finding map is for taxonomy {fmap.get('taxonomy_version')}, the taxonomy is {tax.get('taxonomy_version')}")
        runs = resolve_runs(a, exp.get("set"))
        if not runs:
            raise InputError("no run directories: pass --runs-map / --run")
        doc = score(exp, tax, fmap, runs, by_lineage=a.by_lineage)
    except InputError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2
    regressions = None
    if a.baseline:
        try:
            regressions = compare(doc, load_json(a.baseline), a.max_drop, a.min_support)
        except InputError as ex:
            print(f"error: {ex}", file=sys.stderr)
            return 2
        doc["regressions"] = regressions
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
    md = markdown(doc, regressions)
    if a.markdown:
        with open(a.markdown, "w", encoding="utf-8") as fh:
            fh.write(md)
    if not a.out and not a.markdown:
        print(md)
    if a.require_all_runs and doc["episodes"]["missing_runs"]:
        print(f"error: no run for {len(doc['episodes']['missing_runs'])} subset(s)", file=sys.stderr)
        return 2
    if regressions:
        for g in regressions:
            print(f"regression: {g['metric']} {g['baseline']} -> {g['current']} (support {g['support']})", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
