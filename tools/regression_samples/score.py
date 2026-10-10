#!/usr/bin/env python
"""Score platform runs against a regression sample set's expectation.json (design doc 16 §8, design doc 17 §6.1).

One run directory per subset (what ``curation check`` / the Daemon write: ``checks/<module>/``, ``revisions/rNNNN/``,
``preflight.json``). The expectation says which items each episode should (present) or should not (absent) show.
Two record formats, told apart per run directory (a record 2.0 has ``status``; a run with nothing to read, because the
preflight refused the dataset, takes the format of the runs scored with it):

* **2.0** (C2 2.0, findings): the records name the items themselves - ``findings[].item`` with its ``scope``, the items a
  module ``assessed`` and the ones it found ``unassessable``, ``status: error`` when it could not judge the episode. Which
  modules cover an item is the registry's (``docs/contracts/modules.json``, ``covers``). Several modules on one item: the
  union counts in the item's metrics, and every module also gets its own (``by_module``). Dataset-level findings come
  from the revision's report (``dataset_findings``) and count once per subset, for the items scored per subset only: an
  episode-level item is read from the episodes' own records. The episode level reads the final lists.
* **1.0** (tasks made before, D59): ``finding_map.json`` turns each module's record into check items, as score 1.1 did.

The map's ``controls`` and ``ingestion`` apply to both formats. Per item:

    TP = expected present, reported      FN = expected present, not reported
    FP = expected absent, reported       TN = expected absent, not reported

An episode whose mapped modules did not run on it (funnel short circuit, module not selected; 2.0: no module that ran
assessed the item, or it said why it could not) is ``not_assessed``; one whose mapped modules all failed to execute is
``error``; neither counts in precision or recall, but both stay in the denominator of ``recall_end_to_end``. Items
without a rule (2.0: no module covers them) are ``no_check`` - the platform's gaps. A camera-scoped expectation only
matches a finding on the same camera. A dataset-level item (taxonomy level ``dataset``, or an entry with ``unit:
subset``) counts once per subset, not once per episode it is repeated on.

    PYTHONPATH=tools python -m regression_samples.score --expectation <set>/expectation.json \\
        --runs-root <runs> --runs-map <runs>/runs.json --out score.json --markdown score.md \\
        [--baseline previous.json --max-drop 0.05 --min-support 5]

An expectation exported with an older taxonomy (sample set v1 on TOS is still 1.3; 2.0 numbered every dimension
again) is scored with the taxonomy in this directory: its item ids go through that taxonomy's ``renumbered`` table.

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
SCHEMA_VERSION = "2.0"
#: the registry export (C1) whose ``covers`` say which modules assess an item on a run of findings
REGISTRY = os.path.join(REPO, "docs", "contracts", "modules.json")
TIMESTAMP_FAIL_KINDS = ("out_of_order", "gap", "fragment", "jitter")
#: preflight availabilities under which a module never runs on the subset
UNAVAILABLE = ("unsupported", "needs_input", "unavailable")
#: streams a scope may name that are not cameras
NON_CAMERA_STREAMS = ("action", "timestamp", "observation.state")


class InputError(Exception):
    pass


# ---------------------------------------------------------------- scopes

def short_camera(name) -> str:
    return str(name).split("observation.images.")[-1].lower()


def scope_cameras(scope):
    """The cameras an expectation entry is about, or None when it is about the whole episode.

    A scope is a dict ({"stream": ...} / {"streams": [...]}); clean entries of schema 1.0 carry it as text,
    "k=v k=v" with lists written "streams=a,b"."""
    if not scope:
        return None
    if isinstance(scope, str):
        scope = dict(p.split("=", 1) for p in scope.split() if "=" in p)
    cams = scope.get("streams") or scope.get("stream") or scope.get("cameras") or scope.get("camera") or []
    if isinstance(cams, str):
        cams = cams.split(",")
    cams = [short_camera(c) for c in cams if c and str(c) not in NON_CAMERA_STREAMS]
    return frozenset(cams) or None


# ---------------------------------------------------------------- inputs

def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as ex:
        raise InputError(f"cannot read {path}: {ex}") from None


def _version_key(v):
    return tuple(int(x) for x in str(v).split("."))


def expectation_taxonomy(exp, tax):
    """The taxonomy an expectation was exported with: its header names it (``taxonomy.json v1.3 (...)``), else the
    taxonomy file read with it."""
    m = re.search(r"taxonomy\.json v(\d+\.\d+)", str(exp.get("taxonomy") or ""))
    return m.group(1) if m else tax.get("taxonomy_version")


def upgrade_expectation(exp, tax, current):
    """Score a set exported with an older taxonomy against the current one: sample set v1 on TOS is still 1.3,
    taxonomy 2.0 numbered every dimension again (design doc 16 §3). Its item ids go through the current
    taxonomy's ``renumbered`` table (exact ids only, dict keys too) and the current taxonomy is used."""
    have, want = expectation_taxonomy(exp, tax), current.get("taxonomy_version")
    if not have or have == want:
        return exp, tax
    ren = current.get("renumbered") or {}
    if not ren or _version_key(have) > _version_key(ren.get("from_version", "0")):
        raise InputError(f"the expectation is for taxonomy {have}; taxonomy {want} has no table to map it")
    ids = ren["ids"]

    def walk(o):
        if isinstance(o, str):
            return ids.get(o, o)
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, dict):
            return {ids.get(k, k): walk(v) for k, v in o.items()}
        return o
    return {**exp, "episodes": [walk(e) for e in exp.get("episodes") or []]}, current


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


def run_format(run_dir):
    """``2.0`` for a run of findings (C2 2.0), ``1.0`` for one made before (D59): the report says it, else a record;
    None when there is nothing to tell it by (the preflight refused the dataset: no record, no report)."""
    reports = sorted(glob.glob(os.path.join(run_dir, "revisions", "r*", "report.json")))
    if reports:
        try:
            if str(load_json(reports[-1]).get("schema_version")) == "2.0":
                return "2.0"
            return "1.0"
        except InputError:
            pass
    for path in sorted(glob.glob(os.path.join(run_dir, "checks", "*", "parts", "*.jsonl"))) + \
            sorted(glob.glob(os.path.join(run_dir, "checks", "*", "results.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    return "2.0" if "status" in json.loads(line) else "1.0"
    return None


def run_modules(run_dir):
    """The modules with records in ``run_dir``."""
    return sorted(os.path.basename(d) for d in glob.glob(os.path.join(run_dir, "checks", "*")) if os.path.isdir(d))


def final_lists(run_dir):
    """episode -> (passed | reject | held, its entry) of the latest revision (C2 final-list)."""
    revs = sorted(glob.glob(os.path.join(run_dir, "revisions", "r*")))
    out = {}
    if not revs:
        return out
    for name in ("passed", "reject", "held"):
        path = os.path.join(revs[-1], f"{name}.json")
        if os.path.exists(path):
            for e in load_json(path).get("episodes") or []:
                out[int(e["episode_index"])] = (name, e)
    return out


def dataset_findings(run_dir):
    """The dataset-level findings of the latest revision's report: [(module, finding)]."""
    reports = sorted(glob.glob(os.path.join(run_dir, "revisions", "r*", "report.json")))
    if not reports:
        return []
    out = []
    for sec in load_json(reports[-1]).get("modules") or []:
        for f in ((sec or {}).get("summary") or {}).get("dataset_findings") or []:
            if isinstance(f, dict):
                out.append((sec.get("id"), f))
    return out


def finding_cameras(f) -> list:
    """The cameras a finding is about (short names), or [None] for the whole episode."""
    cams = scope_cameras(f.get("scope"))
    return sorted(cams) if cams else [None]


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
    d = rec.get("details") or rec.get("detail") or {}
    if isinstance(d, str):                           # v1-shaped structs keep the detail as a JSON string
        try:
            d = json.loads(d)
        except ValueError:
            d = {}
    return d if isinstance(d, dict) else {}


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


def unassessed(rule, rec):
    """Whether the record says it could not judge the rule's items on this episode (e.g. the model did not answer)."""
    u = rule.get("unknown_when")
    if not u:
        return False
    return _dig(_details(rec), u["detail"]) in u["values"]


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
    if "camera_levels" in m:
        key, levels = m["camera_levels"]["key"], m["camera_levels"]["levels"]
        for cam, cd in (d.get("per_camera") or {}).items():
            if isinstance(cd, dict) and isinstance(cd.get(key), dict) and cd[key].get("level") in levels:
                hits.append(short_camera(cam))
    if "detail_outcomes" in m and d.get("outcome") in m["detail_outcomes"]:
        hits.append(None)
    if "passed" in m and rec.get("passed") in m["passed"] and rec.get("verdict") != "error":
        hits.append(None)
    return hits


# ---------------------------------------------------------------- scoring

def expectation_index(exp, taxonomy=None):
    """(dataset, episode) -> {item: {"present": [scopes], "absent": [scopes], "unit": "episode" | "subset"}}, plus per-episode
    facts: lineage, verdict, the items it was checked on, whether it carries an episode-level defect."""
    level = {i["id"]: i.get("level") for i in (taxonomy or {}).get("items", [])}
    idx, meta = {}, {}
    for e in exp["episodes"]:
        key = (e["dataset"], int(e["episode_index"]))
        items = defaultdict(lambda: {"present": [], "absent": [], "unit": "episode"})
        has_defect = False
        for group, side in (("problems", "present"), ("phenomena", "present"), ("clean", "absent")):
            for entry in e.get(group) or []:
                it = items[entry["item"]]
                it[side].append(scope_cameras(entry.get("scope")))
                if entry.get("unit") == "subset" or level.get(entry["item"]) == "dataset":
                    it["unit"] = "subset"
                if group == "problems" and (entry.get("level") or level.get(entry["item"]) or "episode") == "episode":
                    has_defect = True
        idx[key] = dict(items)
        meta[key] = {"episode_id": e.get("episode_id") or f"{key[0]}:{key[1]}", "lineage": e.get("lineage") or f"{key[0]}:{key[1]}",
                     "verdict": e.get("verdict"), "checked": set(e.get("checked_items") or []) or set(items), "has_defect": has_defect}
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


#: how the per-episode cells of a subset-level item reduce to the subset's one cell, strongest first
_SUBSET_ORDER = {"present": ("tp", "fn", "error", "not_assessed", "no_check"), "absent": ("fp", "tn", "error", "not_assessed", "no_check")}


def _count(s, side, cell, w, unsupported=False, flagged_any=False):
    s[side] += w
    if cell in ("no_check", "not_assessed", "error"):
        s[f"{cell}_{side}"] += w
        if cell == "not_assessed" and unsupported:
            s[f"unsupported_{side}"] += w
        return
    s[cell] += w
    if side == "present" and flagged_any:
        s["flagged_any"] += w


def _cell(side, found, sides):
    """tp / fn / fp / tn of one item on one episode from the cameras it was reported on."""
    if side == "absent" and sides["present"]:
        # the same item is expected on another camera of the episode: only a finding on one of the clean cameras is
        # a false alarm (an unattributed finding belongs to the present side)
        found = [h for h in found if h is not None]
    reported = _matches(found, sides[side])
    return {("present", True): "tp", ("present", False): "fn", ("absent", True): "fp", ("absent", False): "tn"}[(side, reported)]


def registry_covers(registry) -> dict:
    """item -> the modules that assess it (the registry's ``covers`` and its codes' items, C1 2.x)."""
    out = defaultdict(set)
    for m in (registry or {}).get("modules") or []:
        for i in list(m.get("covers") or []) + [c.get("item") for c in m.get("codes") or []]:
            if i:
                out[i].add(m["id"])
    return out


def _v1_episode(ep, rules, modules, recs, verdicts, availability, mapped_codes, rule_hits, unmapped_codes):
    """The evidence of one episode of a run made before (C2 1.0): what the finding map's rules read from its records."""
    hits = defaultdict(list)                 # item -> cameras
    state = {}                               # module -> "ok" | "error" | "unsupported" | None
    unknown = defaultdict(set)               # module -> items it ran for but could not judge
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
            if unassessed(r, rec):
                unknown[m].update(r["items"])
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
    by = set(v.get("hard_fails") or []) or {m for m in modules if (recs[m].get(ep) or {}).get("verdict") == "scored"}
    return {"hits": hits, "states": lambda mods, item: [None if (state.get(m) == "ok" and item in unknown[m]) else state.get(m) for m in mods],
            "verdict": v.get("verdict") if v else None, "by": by, "per_module": None}


#: the EEF module's opinions (registry 5.0, design doc 25 §7): a finding of it counts as a detection from this
#: inconsistency confidence on - the demo profile's high band, "inconsistent" (a conflict reaches it by definition)
EEF_MODULE, EEF_MIN_P = "eef_video_consistency", 0.7


def _eef_below(f: dict, min_p: float) -> bool:
    """An EEF opinion under the bar: a "possibly inconsistent" one does not count as found (an older finding has no p)."""
    p = (f.get("readings") or {}).get("p")
    return isinstance(p, (int, float)) and not isinstance(p, bool) and p < min_p


def _v2_episode(ep, recs, lists, availability, covers, dataset_hits, intervals, eef_min_p=EEF_MIN_P):
    """The evidence of one episode of a run of findings (C2 2.0): its records' findings, what they assessed, what failed.
    The EEF module's opinions count from ``eef_min_p`` on (label + p, design doc 25 §7)."""
    hits = defaultdict(list)
    per_module = defaultdict(lambda: defaultdict(list))      # item -> module -> cameras
    assessed = defaultdict(set)                              # item -> modules that assessed it
    errored = set()
    for m, mrecs in recs.items():
        rec = mrecs.get(ep)
        if rec is None:
            continue
        if rec.get("status") == "error":
            errored.add(m)
            continue
        for i in rec.get("assessed") or []:
            assessed[i].add(m)
        for f in rec.get("findings") or []:
            item = f.get("item")
            if not item or f.get("unit") == "dataset":
                continue
            if m == EEF_MODULE and _eef_below(f, eef_min_p):
                continue
            cams = finding_cameras(f)
            hits[item].extend(cams)
            per_module[item][m].extend(cams)
            intervals[item]["findings"] += 1
            if f.get("frames") or f.get("time_s"):
                intervals[item]["with_interval"] += 1

    def states(mods, item):
        # judged by any module that assessed it (the union); else an error of a covering module; else not assessed
        if assessed.get(item):
            return ["ok"]
        if any(m in errored for m in mods):
            return ["error"]
        return ["unsupported" if availability.get(m) in UNAVAILABLE else None for m in mods]

    name, entry = lists.get(ep, (None, {}))
    verdict = {"reject": "drop", "held": "held", "passed": "keep"}.get(name)
    by = {r.get("module") for r in entry.get("reasons") or [] if isinstance(r, dict) and r.get("kind") != "execution_error"}
    return {"hits": hits, "dataset_hits": dataset_hits, "states": states, "verdict": verdict, "by": by,
            "per_module": {"items": per_module, "assessed": assessed, "errored": errored}}


def score(expectation, taxonomy, fmap, runs, by_lineage=False, sample_cap=20, registry=None, eef_min_p=EEF_MIN_P):
    """runs: {subset: run_dir}; registry: the C1 export (modules.json) for the runs of findings. Returns the score document."""
    idx, meta = expectation_index(expectation, taxonomy)
    items_meta = {i["id"]: i for i in taxonomy["items"]}
    rules = fmap["rules"]
    for r in rules:
        unknown = [i for i in r["items"] if i not in items_meta]
        if unknown:
            raise InputError(f"rule {r['id']} names items not in the taxonomy: {unknown}")
    rule_modules = defaultdict(set)
    for r in rules:
        for i in r["items"]:
            rule_modules[i].add(r["module"])
    modules = sorted({r["module"] for r in rules})
    covers = registry_covers(registry)
    item_modules = defaultdict(set)              # the modules that judge an item on the runs scored (either format)
    controls = (fmap.get("controls") or {}).get("items") or {}
    ingestion_items = set((fmap.get("ingestion") or {}).get("items") or [])

    # weights: an episode counts 1 / (episodes of its lineage that carry the same item on the same side), so versions of one
    # recording are one piece of evidence for an item; the episode level weighs by the lineage's size
    carriers = Counter()
    for key, items in idx.items():
        for item, sides in items.items():
            for side in ("present", "absent"):
                if sides[side]:
                    carriers[(meta[key]["lineage"], item, side)] += 1
    lineage_size = Counter(m["lineage"] for m in meta.values())

    def weight(key, item, side):
        return 1.0 / carriers[(meta[key]["lineage"], item, side)] if by_lineage else 1.0

    subsets = sorted({k[0] for k in idx})
    missing = [s for s in subsets if s not in runs]
    stats = defaultdict(lambda: Counter())
    module_stats = defaultdict(lambda: defaultdict(Counter))  # item -> module -> cells (runs of findings)
    intervals = defaultdict(Counter)
    samples = defaultdict(lambda: defaultdict(list))
    units = defaultdict(set)
    episode_level = Counter()
    ep_samples = defaultdict(list)
    control_stats = defaultdict(Counter)
    control_fail = defaultdict(list)
    ingestion = {}
    formats = {}
    rule_hits = Counter()
    unmapped_codes = Counter()
    mapped_codes = {c for r in rules for c in r["match"].get("finding_codes", [])}
    scored_eps = 0

    def sample(item, cell, ep_id):
        if cell in ("fp", "fn") and len(samples[item][cell]) < sample_cap:
            samples[item][cell].append(ep_id)

    found = {s: run_format(runs[s]) for s in subsets if s in runs}
    # a run with nothing to read takes the format of the runs scored with it: its items are judged by the same
    # modules (the registry's, or the map's), none of which could run on it
    nothing_to_read = "2.0" if {f for f in found.values() if f} == {"2.0"} else "1.0"
    for subset in subsets:
        if subset not in runs:
            continue
        run_dir = runs[subset]
        fmt = found[subset] or nothing_to_read
        formats[subset] = fmt
        supported, availability = preflight(run_dir)
        if fmt == "2.0":
            if registry is None:
                raise InputError(f"{run_dir} holds findings (C2 2.0): the registry (modules.json) is needed to score it")
            recs = {m: module_records(run_dir, m) for m in run_modules(run_dir)}
            lists = final_lists(run_dir)
            # the subset's own findings: they count for the items scored per subset (every episode carries them, one
            # cell per subset), never as a finding on each episode of an episode-level item
            dataset_hits = defaultdict(list)
            for m, f in dataset_findings(run_dir):
                if f.get("item"):
                    dataset_hits[f["item"]].extend(finding_cameras(f))
            mods_of = covers
        else:
            recs = {m: module_records(run_dir, m) for m in modules}
            verdicts = funnel_verdicts(run_dir)
            mods_of = rule_modules
        keys = sorted(k for k in idx if k[0] == subset)
        if any(ingestion_items & set(idx[k]) for k in keys):
            ingestion[subset] = {"handled": bool(supported), "preflight_supported": supported}
        subset_cells = defaultdict(list)            # (item, side) -> [(cell, unsupported, flagged_any, episode_id)]
        for key in keys:
            ep = key[1]
            scored_eps += 1
            if fmt == "2.0":
                ev = _v2_episode(ep, recs, lists, availability, covers, dataset_hits, intervals, eef_min_p)
            else:
                ev = _v1_episode(ep, rules, modules, recs, verdicts, availability, mapped_codes, rule_hits, unmapped_codes)
            hits = ev["hits"]
            subset_hits = ev.get("dataset_hits") or {}
            dropped = ev["verdict"] == "drop"
            flagged_any = dropped or any(hits.values())
            # episode level: a drop of an episode clean on its checked items is a false alarm only when a module that
            # judges one of those items caused it; a drop for something nobody checked is unknown, not wrong
            ev_exp = meta[key]["verdict"]
            w_ep = 1.0 / lineage_size[meta[key]["lineage"]] if by_lineage else 1.0
            if ev_exp in ("defective", "clean_on_checked_items") and ev["verdict"]:
                if ev["verdict"] == "held":
                    episode_level["held_" + ("defective" if ev_exp == "defective" else "clean")] += w_ep
                else:
                    if ev_exp == "defective":
                        cell = "tp" if dropped else "fn"
                    elif not dropped:
                        cell = "tn"
                    else:
                        relevant = set().union(*[mods_of.get(i, set()) for i in meta[key]["checked"]])
                        cell = "fp" if ev["by"] & relevant else "dropped_outside_checked"
                    episode_level[cell] += w_ep
                    if cell in ("fp", "fn") and len(ep_samples[cell]) < sample_cap:
                        ep_samples[cell].append(meta[key]["episode_id"])
            # items
            for item, sides in idx[key].items():
                if item in controls and sides["present"]:
                    c = controls[item]
                    bad = [i for i in c.get("not_items", []) if hits.get(i)]
                    # an episode that is expected to be dropped for a defect of its own may be dropped: the control is
                    # about its guarded items
                    failed = bool(bad) or bool(c.get("not_drop") and dropped and not meta[key]["has_defect"])
                    control_stats[item]["fail" if failed else "pass"] += weight(key, item, "present")
                    if failed and len(control_fail[item]) < sample_cap:
                        control_fail[item].append(meta[key]["episode_id"])
                    continue
                if item in ingestion_items:
                    continue
                mods = mods_of.get(item)
                if mods:
                    item_modules[item].update(mods)
                st = ev["states"](mods, item) if mods else []
                for side in ("present", "absent"):
                    if not sides[side]:
                        continue
                    unsupported = False
                    if not mods:
                        cell = "no_check"
                    elif all(x in (None, "unsupported") for x in st):
                        cell, unsupported = "not_assessed", all(x == "unsupported" for x in st)
                    elif all(x in (None, "unsupported", "error") for x in st):
                        cell = "error"
                    elif sides["unit"] == "subset":
                        cell = _cell(side, hits.get(item, []) + subset_hits.get(item, []), sides)
                    else:
                        cell = _cell(side, hits.get(item, []), sides)
                    units[item].add(sides["unit"])
                    if sides["unit"] == "subset":
                        subset_cells[(item, side)].append((cell, unsupported, flagged_any or any(subset_hits.values()),
                                                           meta[key]["episode_id"]))
                        continue
                    _count(stats[item], side, cell, weight(key, item, side), unsupported, flagged_any)
                    sample(item, cell, meta[key]["episode_id"])
                    # each module on its own (runs of findings): the episodes it assessed the item on
                    pm = ev["per_module"]
                    if pm is not None and mods:
                        for m in sorted(mods):
                            if m in pm["errored"] or m not in pm["assessed"].get(item, set()):
                                continue
                            module_stats[item][m][_cell(side, pm["items"][item].get(m, []), sides)] += weight(key, item, side)
        # subset-level items: one cell per subset and side, the strongest outcome among its episodes
        for (item, side), cells in subset_cells.items():
            kind = next(k for k in _SUBSET_ORDER[side] if any(c[0] == k for c in cells))
            picked = [c for c in cells if c[0] == kind]
            _count(stats[item], side, kind, 1.0, all(c[1] for c in picked), any(c[2] for c in cells))
            sample(item, kind, f"{subset.split('/')[-1]} (subset)")

    items_out = {}
    v2 = "2.0" in formats.values()
    v1 = "1.0" in formats.values() or not formats
    for it in taxonomy["items"]:
        iid = it["id"]
        s = stats.get(iid, Counter())
        # the modules that judge the item: the ones met on the runs, else the map's (1.0) and the registry's (2.0)
        mods = sorted(item_modules.get(iid) or ((rule_modules.get(iid, set()) if v1 else set()) | (covers.get(iid, set()) if v2 else set())))
        row = {"name": it["name"], "kind": it["kind"], "level": it["level"], "mapped": bool(mods), "modules": mods,
               "unit": "/".join(sorted(units.get(iid) or {"subset" if it["level"] == "dataset" else "episode"})),
               "present": _r(s["present"]), "absent": _r(s["absent"])}
        for k in ("tp", "fn", "fp", "tn"):
            row[k] = _r(s[k])
        row["not_assessed"] = {"present": _r(s["not_assessed_present"]), "absent": _r(s["not_assessed_absent"]),
                               "of_which_unsupported": _r(s["unsupported_present"] + s["unsupported_absent"])}
        row["error"] = {"present": _r(s["error_present"]), "absent": _r(s["error_absent"])}
        row["precision"] = _ratio(s["tp"], s["tp"] + s["fp"])
        row["recall"] = _ratio(s["tp"], s["tp"] + s["fn"])
        row["recall_any"] = _ratio(s["flagged_any"], s["tp"] + s["fn"])
        # end to end: every expected-present unit in the denominator, also the ones the platform did not assess, could not
        # execute on, or has no check for
        row["recall_end_to_end"] = _ratio(s["tp"], s["present"])
        row["assessed_share"] = _ratio(s["tp"] + s["fn"], s["present"])
        if samples[iid]:
            row["samples"] = {k: v for k, v in samples[iid].items()}
        if module_stats.get(iid):
            # runs of findings: every module on its own, over the episodes it assessed the item on (design doc 17 §6.1)
            row["by_module"] = {m: {"tp": _r(c["tp"]), "fp": _r(c["fp"]), "fn": _r(c["fn"]), "tn": _r(c["tn"]),
                                    "precision": _ratio(c["tp"], c["tp"] + c["fp"]), "recall": _ratio(c["tp"], c["tp"] + c["fn"])}
                                for m, c in sorted(module_stats[iid].items())}
        if intervals.get(iid):
            # P19: how many of the item's findings say where in the episode (design doc 17 §6.3)
            n, w = intervals[iid]["findings"], intervals[iid]["with_interval"]
            row["intervals"] = {"findings": n, "with_interval": w, "share": _ratio(w, n)}
        if iid in controls:
            cs = control_stats.get(iid, Counter())
            row["control"] = {"pass": _r(cs["pass"]), "fail": _r(cs["fail"]), "pass_rate": _ratio(cs["pass"], cs["pass"] + cs["fail"]),
                              "failed": control_fail.get(iid, [])}
        items_out[iid] = row
    el = {k: _r(episode_level[k]) for k in ("tp", "fn", "fp", "tn", "held_defective", "held_clean", "dropped_outside_checked")}
    el["precision"] = _ratio(episode_level["tp"], episode_level["tp"] + episode_level["fp"])
    el["recall"] = _ratio(episode_level["tp"], episode_level["tp"] + episode_level["fn"])
    if ep_samples:
        el["samples"] = dict(ep_samples)
    return {
        "schema_version": SCHEMA_VERSION,
        "set": expectation.get("set"), "set_version": expectation.get("set_version"),
        "expectation_schema": expectation.get("schema_version"),
        "taxonomy_version": taxonomy.get("taxonomy_version"), "map_version": fmap.get("version") or fmap.get("schema_version"),
        "by_lineage": by_lineage,
        "episodes": {"expected": len(idx), "scored": scored_eps, "subsets": len(subsets), "runs": len([s for s in subsets if s in runs]),
                     "missing_runs": missing},
        "formats": dict(sorted(formats.items())),
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
             f"(TP {el['tp']}, FP {el['fp']}, FN {el['fn']}, TN {el['tn']}; held {el['held_defective']} defective / {el['held_clean']} clean; "
             f"{el.get('dropped_outside_checked', 0)} clean episodes dropped by a module none of their checked items maps to, not counted)")
    L.append("\n| item | name | kind | unit | present | absent | TP | FP | FN | TN | precision | recall | recall end to end | recall (any flag) | not assessed (module unsupported) | errors |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    gaps = []
    for iid, r in doc["items"].items():
        if not (r["present"] or r["absent"]) or "control" in r:
            continue
        if not r["mapped"]:
            gaps.append(f"{iid} {r['name']} (present {r['present']}, absent {r['absent']})")
            continue
        na = f"{r['not_assessed']['present'] + r['not_assessed']['absent']} ({r['not_assessed']['of_which_unsupported']})"
        er = r["error"]["present"] + r["error"]["absent"]
        L.append(f"| {iid} | {r['name']} | {r['kind']} | {r.get('unit', 'episode')} | {r['present']} | {r['absent']} | {r['tp']} | {r['fp']} | {r['fn']} | {r['tn']} | "
                 f"{_fmt(r['precision'])} | {_fmt(r['recall'])} | {_fmt(r.get('recall_end_to_end'))} | {_fmt(r['recall_any'])} | {na} | {er} |")
    ctl = [(iid, r) for iid, r in doc["items"].items() if "control" in r and (r["control"]["pass"] or r["control"]["fail"])]
    if ctl:
        L.append("\n**Controls** (must not be reported)")
        for iid, r in ctl:
            c = r["control"]
            L.append(f"- {iid} {r['name']}: pass {c['pass']}, fail {c['fail']}" + (f" ({', '.join(c['failed'])})" if c["failed"] else ""))
    if doc["ingestion"]:
        bad = [s for s, v in doc["ingestion"].items() if not v["handled"]]
        L.append(f"\n**Ingestion** (SET-2): {len(doc['ingestion']) - len(bad)} of {len(doc['ingestion'])} subsets read" + (f"; not read: {', '.join(bad)}" if bad else ""))
    if gaps:
        why = {"1.0": "no rule maps to them", "2.0": "no module covers them"}
        fmts = sorted(set((doc.get("formats") or {}).values())) or ["1.0"]
        L.append(f"\n**No check on the platform** (expectations exist, {' / '.join(why[f] for f in fmts if f in why)}):")
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
    ap.add_argument("--map", default=os.path.join(HERE, "finding_map.json"), help="finding_map.json (rules for runs made before; controls and ingestion for all)")
    ap.add_argument("--registry", default=REGISTRY, help="the platform's registry export, modules.json (which modules cover an item, for runs of findings)")
    ap.add_argument("--runs-map", help="JSON {run_name: {set, subset}} (baseline/<commit>/runs.json)")
    ap.add_argument("--runs-root", help="directory holding the run directories named in --runs-map")
    ap.add_argument("--run", action="append", help="SUBSET=RUN_DIR, repeatable")
    ap.add_argument("--by-lineage", action="store_true", help="weight each episode by 1/(episodes of its lineage that carry the same item on the same side)")
    ap.add_argument("--out", help="write the score JSON here")
    ap.add_argument("--markdown", help="write a Markdown summary here")
    ap.add_argument("--baseline", help="an earlier score JSON to compare against")
    ap.add_argument("--max-drop", type=float, default=0.05, help="tolerated fall of a precision / recall / pass rate (default 0.05)")
    ap.add_argument("--min-support", type=int, default=5, help="compare a metric only with at least this many counted episodes (default 5)")
    ap.add_argument("--require-all-runs", action="store_true", help="exit 2 when a subset of the expectation has no run")
    ap.add_argument("--eef-min-p", type=float, default=EEF_MIN_P,
                    help="the EEF module's opinions count as found from this inconsistency confidence on (default 0.7)")
    a = ap.parse_args(argv)
    try:
        exp = load_json(a.expectation)
        tax_path = a.taxonomy or next((p for p in (os.path.join(os.path.dirname(os.path.abspath(a.expectation)), "taxonomy.json"),
                                                   os.path.join(HERE, "taxonomy.json")) if os.path.exists(p)), None)
        if not tax_path:
            raise InputError("no taxonomy.json found; pass --taxonomy")
        tax = load_json(tax_path)
        exp, tax = upgrade_expectation(exp, tax, load_json(os.path.join(HERE, "taxonomy.json")))
        fmap = load_json(a.map)
        if fmap.get("taxonomy_version") != tax.get("taxonomy_version"):
            raise InputError(f"finding map is for taxonomy {fmap.get('taxonomy_version')}, the taxonomy is {tax.get('taxonomy_version')}")
        runs = resolve_runs(a, exp.get("set"))
        if not runs:
            raise InputError("no run directories: pass --runs-map / --run")
        registry = load_json(a.registry) if a.registry and os.path.exists(a.registry) else None
        doc = score(exp, tax, fmap, runs, by_lineage=a.by_lineage, registry=registry, eef_min_p=a.eef_min_p)
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
