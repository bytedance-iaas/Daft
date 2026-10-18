#!/usr/bin/env python
"""Score platform runs against a regression sample set's expectation.json (design doc 16 §8, design doc 17 §6.1).

One run directory per subset (what ``curation check`` / the Daemon write: ``checks/<module>/``, ``revisions/rNNNN/``,
``preflight.json``). The expectation says which items each episode should (present) or should not (absent) show.
The records (C2 2.0) name the items themselves - ``findings[].item`` with its ``scope``, the items a module ``assessed``
and the ones it found ``unassessable``, ``status: error`` when it could not judge the episode. Which modules cover an
item is the registry's (``docs/contracts/modules.json``, ``covers``). Several modules on one item: the union counts in
the item's metrics, and every module also gets its own (``by_module``). Dataset-level findings come from the revision's
report (``dataset_findings``) and count once per subset, for the items scored per subset only: an episode-level item is
read from the episodes' own records. The episode level reads the final lists. A run made before findings (C2 1.0, D59)
is refused: run the subset again.

Phenomena whose expectation says *where* (``intervals`` of steps, a ``task_end`` with its ``time_s`` / ``frames``, an
``at_s``) are also compared by position (``boundaries``; a defect's position stays a note): every start and end of an expected interval is a boundary
(the episode's start is nobody's, and neither is its end: the last step ends where the recording stops, which no
detector can find - a boundary within half a frame of the episode's length, read from the records' ``n_frames`` /
``duration_s``, is left out on both sides and counted as ``at_episode_end``), a platform finding's ``time_s`` (else
``frames`` over the run's fps, read from ``preflight.json``) gives the platform's, and the two sets are matched
nearest-first within a tolerance - the entry's
``boundary_tolerance_s``, else ``--boundary-tolerance`` (default 1.0 s: a hand-marked step boundary wobbles that much).
The platform's boundaries are kept apart by where they come from: ``readings.source = declared`` (the dataset's own
labels surfaced, e.g. ``declared_steps``) against everything else (``detected``), so surfacing the answer key never
reads as detection. Each source gets expected / matched / extra, the hits at 0.25 / 0.5 / 1.0 s, and the error's
median and p90.

``finding_map.json`` holds what the records do not say: the ``controls`` (legitimate situations the platform must not
report) and the ``ingestion`` items (input formats it must read). Per item:

    TP = expected present, reported      FN = expected present, not reported
    FP = expected absent, reported       TN = expected absent, not reported

An episode on which no module that ran assessed the item (module not selected, or it said why it could not) is
``not_assessed``; one whose covering modules all failed to execute is ``error``; neither counts in precision or recall,
but both stay in the denominator of ``recall_end_to_end``. Items no module covers are ``no_check`` - the platform's
gaps. A camera-scoped expectation only
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
SCHEMA_VERSION = "2.1"
#: a platform boundary counts as the expected one within this many seconds (the entry's ``boundary_tolerance_s`` wins)
BOUNDARY_TOLERANCE_S = 1.0
#: the fixed tolerances every boundary comparison is also reported at
BOUNDARY_LADDER = (0.25, 0.5, 1.0)
#: the sources a platform boundary is scored under: the dataset's own labels, or a measurement
BOUNDARY_SOURCES = ("declared", "detected")
#: the registry export (C1) whose ``covers`` say which modules assess an item on a run of findings
REGISTRY = os.path.join(REPO, "docs", "contracts", "modules.json")
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
    if _version_key(have) > _version_key(want):
        raise InputError(f"the expectation is for taxonomy {have}, newer than this tool's {want}")
    ren = current.get("renumbered") or {}
    if ren and _version_key(have) > _version_key(ren.get("from_version", "0")):
        # 2.1 (2026-10-16): since 2.0 the ids are frozen and a release only appends items, so an expectation
        # exported with 2.0 (the sample set v2 as published) scores as it is, against the current taxonomy
        return exp, current
    if not ren:
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


def made_before_findings(run_dir) -> bool:
    """Whether the run was made before findings (C2 1.0, D59): its report or its first record says so."""
    reports = sorted(glob.glob(os.path.join(run_dir, "revisions", "r*", "report.json")))
    if reports:
        try:
            return str(load_json(reports[-1]).get("schema_version")) != "2.0"
        except InputError:
            pass
    for path in sorted(glob.glob(os.path.join(run_dir, "checks", "*", "parts", "*.jsonl"))) + \
            sorted(glob.glob(os.path.join(run_dir, "checks", "*", "results.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    return "status" not in json.loads(line)
    return False


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


def preflight(run_dir):
    """(format supported or None, {module: availability})."""
    path = os.path.join(run_dir, "preflight.json")
    if not os.path.exists(path):
        return None, {}
    p = load_json(path)
    avail = {m.get("id"): m.get("availability") for m in p.get("modules") or [] if isinstance(m, dict)}
    return bool((p.get("format") or {}).get("supported")), avail


def preflight_fps(run_dir):
    """The dataset's frame rate the preflight recorded (``dataset.fps``), or None."""
    path = os.path.join(run_dir, "preflight.json")
    if not os.path.exists(path):
        return None
    fps = ((load_json(path).get("dataset") or {}).get("fps"))
    return float(fps) if isinstance(fps, (int, float)) and not isinstance(fps, bool) and fps > 0 else None


# ---------------------------------------------------------------- boundaries (design doc 17 §6.3)

def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def episode_end_s(recs, ep, fps):
    """Where the episode's recording stops, in seconds, from what its records say: a module's ``details.n_frames``
    over the fps (``declared_steps``, ``step_detection``), else a ``duration_s`` (the timestamp check's reading).
    None when no record says."""
    frames, duration = [], []
    for mrecs in recs.values():
        rec = mrecs.get(ep)
        if not isinstance(rec, dict) or rec.get("status") == "error":
            continue
        for part in (rec.get("details") or {}, rec.get("readings") or {}):
            n = _num(part.get("n_frames"))
            if n is not None and n > 0:
                frames.append(n)
            d = _num(part.get("duration_s"))
            if d is not None and d > 0:
                duration.append(d)
    if frames and fps:
        return max(frames) / fps
    if duration:
        return max(duration)
    return None


def _range_seconds(obj, fps, time_key="time_s", frame_key="frames"):
    """(start, end) in seconds of an object's ``time_s`` pair (else its ``frames`` pair over fps, the end frame
    inclusive), None without a usable position, False when it is in frames and there is no fps."""
    t = obj.get(time_key)
    if isinstance(t, (list, tuple)) and len(t) == 2 and _num(t[0]) is not None and _num(t[1]) is not None:
        return float(t[0]), float(t[1])
    if _num(t) is not None:
        return float(t), float(t)
    fr = obj.get(frame_key)
    if isinstance(fr, (list, tuple)) and len(fr) == 2 and _num(fr[0]) is not None and _num(fr[1]) is not None:
        if not fps:
            return False
        return float(fr[0]) / fps, (float(fr[1]) + 1.0) / fps
    if _num(fr) is not None:
        if not fps:
            return False
        return float(fr) / fps, float(fr) / fps
    return None


def positions_of(obj, fps):
    """The boundaries (seconds, sorted, the episode's start left out) an expectation entry or a finding places in
    the episode: every start and end of its ``intervals`` (``start_s`` / ``end_s``, or ``start_frame`` /
    ``end_frame`` with the end frame inclusive), the start of a task end (``value: task_end``), both ends of a
    ``time_s`` / ``frames`` range (one boundary when the range is a point), an ``at_s``. Returns (boundaries,
    in_frames_without_fps): the second is True when a position is in frames and the run has no fps."""
    out, no_clock = [], False
    ivs = obj.get("intervals")
    if isinstance(ivs, list) and ivs:
        for iv in ivs:
            if not isinstance(iv, dict):
                continue
            a, b = _num(iv.get("start_s")), _num(iv.get("end_s"))
            if a is None and b is None:
                fa, fb = _num(iv.get("start_frame")), _num(iv.get("end_frame"))
                if fa is None and fb is None:
                    continue
                if not fps:
                    no_clock = True
                    continue
                a = fa / fps if fa is not None else None
                b = (fb + 1.0) / fps if fb is not None else None
            out.extend(x for x in (a, b) if x is not None)
    else:
        rng = _range_seconds(obj, fps)
        if rng is False:
            no_clock = True
        elif rng is not None:
            out.append(rng[0])
            if obj.get("value") != "task_end":
                out.append(rng[1])
        at = _num(obj.get("at_s"))
        if at is not None:
            out.append(at)
    return _dedup(out, fps), no_clock


def _clock_eps(fps):
    return 0.5 / fps if fps else 0.01


def _dedup(values, fps, end_s=None):
    """Sorted boundaries with the episode's start left out (nobody's boundary), its end too when ``end_s`` says where
    it is (the recording stopping is not a boundary anyone detects), and the ones within half a frame of each other
    merged (the end of one step is the start of the next, also across findings)."""
    eps = _clock_eps(fps)
    out = []
    for x in sorted(values):
        if x <= eps:
            continue
        if end_s is not None and x >= end_s - eps:
            continue
        if out and x - out[-1] <= eps:
            continue
        out.append(x)
    return out


def finding_source(f) -> str:
    """Where a finding's position comes from: the dataset's own labels (``readings.source = declared``), else a
    measurement."""
    return "declared" if (f.get("readings") or {}).get("source") == "declared" else "detected"


def match_boundaries(expected, got, tolerance):
    """The errors (seconds) of the pairs matched nearest-first within ``tolerance``, one boundary to one."""
    pairs = sorted((abs(e - g), i, j) for i, e in enumerate(expected) for j, g in enumerate(got) if abs(e - g) <= tolerance)
    used_e, used_g, errs = set(), set(), []
    for d, i, j in pairs:
        if i in used_e or j in used_g:
            continue
        used_e.add(i)
        used_g.add(j)
        errs.append(round(d, 3))
    return errs


def _score_boundaries(acc, entries, got_by_source, fps, default_tolerance, assessed, end_s=None):
    """One episode's boundaries of one item into ``acc[source]`` (a Counter + the matched errors). ``end_s``: where
    the recording stops; an expected or reported boundary there is left out (``at_episode_end`` counts the expected
    ones)."""
    expected, no_clock, tol = [], False, None
    for entry in entries:
        b, nc = positions_of(entry, fps)
        expected.extend(b)
        no_clock = no_clock or nc
        t = _num(entry.get("boundary_tolerance_s"))
        if t is not None:
            tol = max(tol or 0.0, t)
    all_expected = _dedup(expected, fps)
    expected = _dedup(all_expected, fps, end_s)
    at_end = len(all_expected) - len(expected)
    tolerance = tol if tol is not None else default_tolerance
    for source in BOUNDARY_SOURCES:
        a = acc[source]
        n = a["n"]
        got = _dedup(got_by_source.get(source, []), fps, end_s)
        n["episodes"] += 1
        n["expected"] += len(expected)
        n["at_episode_end"] += at_end
        if no_clock:
            n["episodes_without_clock"] += 1
        if not assessed:
            n["expected_not_assessed"] += len(expected)
        if got:
            n["episodes_reported"] += 1
        errs = match_boundaries(expected, got, tolerance)
        n["matched"] += len(errs)
        n["extra"] += len(got) - len(errs)
        for lvl in BOUNDARY_LADDER:
            n[f"at_{lvl}"] += sum(1 for d in errs if d <= lvl)
        a["errs"].extend(errs)


def _boundary_row(acc):
    n, errs = acc["n"], sorted(acc["errs"])
    k = len(errs)
    return {"episodes": n["episodes"], "episodes_reported": n["episodes_reported"],
            "episodes_without_clock": n["episodes_without_clock"],
            "expected": n["expected"], "expected_not_assessed": n["expected_not_assessed"],
            "at_episode_end": n["at_episode_end"], "matched": n["matched"], "missed": n["expected"] - n["matched"], "extra": n["extra"],
            "precision": _ratio(n["matched"], n["matched"] + n["extra"]), "recall": _ratio(n["matched"], n["expected"]),
            "recall_assessed": _ratio(n["matched"], n["expected"] - n["expected_not_assessed"]),
            "at": {str(lvl): n[f"at_{lvl}"] for lvl in BOUNDARY_LADDER},
            "median_err_s": round(errs[k // 2], 3) if k else None,
            "p90_err_s": round(errs[min(k - 1, int(0.9 * k))], 3) if k else None}


# ---------------------------------------------------------------- scoring

def expectation_index(exp, taxonomy=None):
    """(dataset, episode) -> {item: {"present": [scopes], "absent": [scopes], "unit": "episode" | "subset", "positions":
    [the present entries that say where]}}, plus per-episode
    facts: lineage, verdict, the items it was checked on, whether it carries an episode-level defect."""
    level = {i["id"]: i.get("level") for i in (taxonomy or {}).get("items", [])}
    idx, meta = {}, {}
    for e in exp["episodes"]:
        key = (e["dataset"], int(e["episode_index"]))
        items = defaultdict(lambda: {"present": [], "absent": [], "unit": "episode", "positions": []})
        has_defect = False
        for group, side in (("problems", "present"), ("phenomena", "present"), ("clean", "absent")):
            for entry in e.get(group) or []:
                it = items[entry["item"]]
                it[side].append(scope_cameras(entry.get("scope")))
                if group == "phenomena" and any(k in entry for k in ("intervals", "time_s", "frames", "at_s")):
                    it["positions"].append(entry)        # a phenomenon that says where is compared by position too
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


#: the EEF module's opinions (registry 5.0, design doc 25 §7): a finding of it counts as a detection from this
#: inconsistency confidence on - the demo profile's high band, "inconsistent" (a conflict reaches it by definition)
EEF_MODULE, EEF_MIN_P = "eef_video_consistency", 0.7


def _eef_below(f: dict, min_p: float) -> bool:
    """An EEF opinion under the bar: a "possibly inconsistent" one does not count as found (an older finding has no p)."""
    p = (f.get("readings") or {}).get("p")
    return isinstance(p, (int, float)) and not isinstance(p, bool) and p < min_p


def _episode(ep, recs, lists, availability, dataset_hits, intervals, eef_min_p=EEF_MIN_P, fps=None):
    """The evidence of one episode: its records' findings, what they assessed, what failed, where the findings
    place themselves (``positions``: item -> source -> boundaries in seconds, see ``positions_of``).
    The EEF module's opinions count from ``eef_min_p`` on (label + p, design doc 25 §7)."""
    hits = defaultdict(list)
    positions = defaultdict(lambda: defaultdict(list))
    end_s = episode_end_s(recs, ep, fps)
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
            where, _ = positions_of(f, fps)
            if where:
                positions[item][finding_source(f)].extend(where)

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
    return {"hits": hits, "dataset_hits": dataset_hits, "states": states, "verdict": verdict, "by": by, "positions": positions,
            "end_s": end_s, "per_module": {"items": per_module, "assessed": assessed, "errored": errored}}


def score(expectation, taxonomy, fmap, runs, by_lineage=False, sample_cap=20, registry=None, eef_min_p=EEF_MIN_P,
          boundary_tolerance=BOUNDARY_TOLERANCE_S):
    """runs: {subset: run_dir}; registry: the C1 export (modules.json), which says the modules that cover an item;
    boundary_tolerance: how close (seconds) a platform boundary must land on an expected one when the entry sets no
    ``boundary_tolerance_s``. Returns the score document."""
    if registry is None:
        raise InputError("the registry (modules.json) is needed: it says which modules cover an item")
    idx, meta = expectation_index(expectation, taxonomy)
    covers = registry_covers(registry)
    item_modules = defaultdict(set)              # the modules that judge an item on the runs scored
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
    boundaries = defaultdict(lambda: defaultdict(lambda: {"n": Counter(), "errs": []}))   # item -> source
    samples = defaultdict(lambda: defaultdict(list))
    units = defaultdict(set)
    episode_level = Counter()
    ep_samples = defaultdict(list)
    control_stats = defaultdict(Counter)
    control_fail = defaultdict(list)
    ingestion = {}
    scored_eps = 0

    def sample(item, cell, ep_id):
        if cell in ("fp", "fn") and len(samples[item][cell]) < sample_cap:
            samples[item][cell].append(ep_id)

    for subset in subsets:
        if subset not in runs:
            continue
        run_dir = runs[subset]
        if made_before_findings(run_dir):
            raise InputError(f"{run_dir} was made before findings (C2 1.0, D59): run the subset again to score it")
        supported, availability = preflight(run_dir)
        fps = preflight_fps(run_dir)
        recs = {m: module_records(run_dir, m) for m in run_modules(run_dir)}
        lists = final_lists(run_dir)
        # the subset's own findings: they count for the items scored per subset (every episode carries them, one
        # cell per subset), never as a finding on each episode of an episode-level item
        dataset_hits = defaultdict(list)
        for m, f in dataset_findings(run_dir):
            if f.get("item"):
                dataset_hits[f["item"]].extend(finding_cameras(f))
        mods_of = covers
        keys = sorted(k for k in idx if k[0] == subset)
        if any(ingestion_items & set(idx[k]) for k in keys):
            ingestion[subset] = {"handled": bool(supported), "preflight_supported": supported}
        subset_cells = defaultdict(list)            # (item, side) -> [(cell, unsupported, flagged_any, episode_id)]
        for key in keys:
            ep = key[1]
            scored_eps += 1
            ev = _episode(ep, recs, lists, availability, dataset_hits, intervals, eef_min_p, fps=fps)
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
                present_cell = None
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
                    if side == "present":
                        present_cell = cell
                    if sides["unit"] == "subset":
                        subset_cells[(item, side)].append((cell, unsupported, flagged_any or any(subset_hits.values()),
                                                           meta[key]["episode_id"]))
                        continue
                    _count(stats[item], side, cell, weight(key, item, side), unsupported, flagged_any)
                    sample(item, cell, meta[key]["episode_id"])
                    # each module on its own: the episodes it assessed the item on
                    pm = ev["per_module"]
                    if mods:
                        for m in sorted(mods):
                            if m in pm["errored"] or m not in pm["assessed"].get(item, set()):
                                continue
                            module_stats[item][m][_cell(side, pm["items"][item].get(m, []), sides)] += weight(key, item, side)
                # by position: the entries that say where, against where the findings put themselves, per source
                if sides.get("positions") and sides["unit"] != "subset":
                    _score_boundaries(boundaries[item], sides["positions"], ev["positions"].get(item, {}), fps,
                                      boundary_tolerance, assessed=present_cell in ("tp", "fn"), end_s=ev["end_s"])
        # subset-level items: one cell per subset and side, the strongest outcome among its episodes
        for (item, side), cells in subset_cells.items():
            kind = next(k for k in _SUBSET_ORDER[side] if any(c[0] == k for c in cells))
            picked = [c for c in cells if c[0] == kind]
            _count(stats[item], side, kind, 1.0, all(c[1] for c in picked), any(c[2] for c in cells))
            sample(item, kind, f"{subset.split('/')[-1]} (subset)")

    items_out = {}
    for it in taxonomy["items"]:
        iid = it["id"]
        s = stats.get(iid, Counter())
        # the modules that judge the item: the ones met on the runs, else the registry's
        mods = sorted(item_modules.get(iid) or covers.get(iid, set()))
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
            # every module on its own, over the episodes it assessed the item on (design doc 17 §6.1)
            row["by_module"] = {m: {"tp": _r(c["tp"]), "fp": _r(c["fp"]), "fn": _r(c["fn"]), "tn": _r(c["tn"]),
                                    "precision": _ratio(c["tp"], c["tp"] + c["fp"]), "recall": _ratio(c["tp"], c["tp"] + c["fn"])}
                                for m, c in sorted(module_stats[iid].items())}
        if intervals.get(iid):
            # P19: how many of the item's findings say where in the episode (design doc 17 §6.3)
            n, w = intervals[iid]["findings"], intervals[iid]["with_interval"]
            row["intervals"] = {"findings": n, "with_interval": w, "share": _ratio(w, n)}
        if boundaries.get(iid):
            # by position, per source: the dataset's own labels surfaced are not a detection
            row["boundaries"] = {"tolerance_s": boundary_tolerance, "ladder_s": list(BOUNDARY_LADDER),
                                 **{src: _boundary_row(boundaries[iid][src]) for src in BOUNDARY_SOURCES}}
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
        "by_lineage": by_lineage, "boundary_tolerance_s": boundary_tolerance,
        "episodes": {"expected": len(idx), "scored": scored_eps, "subsets": len(subsets), "runs": len([s for s in subsets if s in runs]),
                     "missing_runs": missing},
        "episode_level": el,
        "items": items_out,
        "ingestion": ingestion,
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
    bnd = [(iid, r) for iid, r in doc["items"].items() if r.get("boundaries")]
    if bnd:
        lad = bnd[0][1]["boundaries"].get("ladder_s") or list(BOUNDARY_LADDER)
        L.append(f"\n**Boundaries** (by position, tolerance {doc.get('boundary_tolerance_s', BOUNDARY_TOLERANCE_S)} s unless the entry says; "
                 "declared = the dataset's own labels surfaced, detected = measured; recall over every expected boundary of the scored episodes, "
                 "recall (assessed) over the episodes the platform judged the item on; a boundary where the recording stops is nobody's "
                 "and is left out of both sides)")
        L.append("| item | source | episodes (reported) | expected | matched | " + " | ".join(f"≤{x} s" for x in lad)
                 + " | extra | precision | recall | recall (assessed) | median err | p90 err |")
        L.append("|---|---|---|---|---|" + "---|" * len(lad) + "---|---|---|---|---|---|")
        for iid, r in bnd:
            b = r["boundaries"]
            for src in BOUNDARY_SOURCES:
                s = b[src]
                at = " | ".join(str(s["at"].get(str(x), 0)) for x in lad)
                nc = f" ({s['episodes_without_clock']} without fps)" if s.get("episodes_without_clock") else ""
                ends = f" (+{s['at_episode_end']} at the end)" if s.get("at_episode_end") else ""
                L.append(f"| {iid} | {src} | {s['episodes']} ({s['episodes_reported']}){nc} | {s['expected']}{ends} | {s['matched']} | {at} | {s['extra']} | "
                         f"{_fmt(s['precision'])} | {_fmt(s['recall'])} | {_fmt(s['recall_assessed'])} | {_sec(s['median_err_s'])} | {_sec(s['p90_err_s'])} |")
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
        L.append("\n**No check on the platform** (expectations exist, no module covers them):")
        L.extend(f"- {g}" for g in gaps)
    if regressions is not None:
        L.append("\n**Against the baseline**: " + ("no regression" if not regressions else f"{len(regressions)} regression(s)"))
        for g in regressions:
            L.append(f"- {g['metric']}: {g['baseline']} → {g['current']} (−{g['drop']}, support {g['support']})")
    return "\n".join(L) + "\n"


def _fmt(v):
    return "–" if v is None else f"{v:.2f}"


def _sec(v):
    return "–" if v is None else f"{v:.2f} s"


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
    ap.add_argument("--map", default=os.path.join(HERE, "finding_map.json"), help="finding_map.json (the controls and the ingestion items)")
    ap.add_argument("--registry", default=REGISTRY, help="the platform's registry export, modules.json (which modules cover an item)")
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
    ap.add_argument("--boundary-tolerance", type=float, default=BOUNDARY_TOLERANCE_S,
                    help="a platform boundary counts as an expected one within this many seconds, unless the entry sets "
                         "boundary_tolerance_s (default 1.0)")
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
        doc = score(exp, tax, fmap, runs, by_lineage=a.by_lineage, registry=registry, eef_min_p=a.eef_min_p,
                    boundary_tolerance=a.boundary_tolerance)
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
