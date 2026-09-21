"""Run the v1 pipeline once and export normalized records for parity checks.

Usage (``--`` separates our options from the v1 command line):

    python -m parity dump-v1 --out DIR [options] -- run --input ... --output ...

The v1 command is executed in this process by ``curation.cli.main`` exactly as
``curation run`` would run it. Nothing in v1 is edited; this module only wraps
a few functions for the duration of the run:

* ``daft.DataFrame.collect``        - per-episode check results *before* the hard
                                      gates filter rows out (v1's own report keeps
                                      only the failing check for dropped episodes);
* ``requests`` / ``hedged_request`` - record or replay every model call
                                      (``vlm_tape``);
* ``run_pipeline``, ``save_report``, ``_skill_profile_stage``,
  ``caption_episodes``, ``action_hash``, ``episode_fingerprint``,
  ``decode_window``                 - verdicts, report, profile, autolabel
                                      captions, dedup traversal and decode failures.

Before importing v1 the source tree is checked file by file against
``v1_manifest.json`` (git blob hashes at the freeze commit).
"""
from __future__ import annotations

import argparse
import collections
import datetime as _dt
import hashlib
import json
import os
import platform
import sys
import threading
import time
import traceback

from . import records as R
from . import vlm_tape as T

DUMP_SCHEMA_VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_V1_SRC = os.path.normpath(os.path.join(HERE, "..", ".."))
DEFAULT_MANIFEST = os.path.join(HERE, "v1_manifest.json")

EXIT_OK, EXIT_USAGE, EXIT_DIRTY, EXIT_V1_FAILED = 0, 2, 3, 4


# ---------------------------------------------------------------------------
# v1 source check
# ---------------------------------------------------------------------------

def git_blob_sha1(path: str) -> str:
    with open(path, "rb") as fh:
        data = fh.read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def verify_v1_source(v1_src: str, manifest_path: str) -> dict:
    """Compare ``<v1_src>/curation`` with the manifest; report every mismatch."""
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    root = os.path.join(os.path.abspath(v1_src), "curation")
    expected = manifest["files"]
    seen, modified, missing = set(), [], []
    for rel, sha in sorted(expected.items()):
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            missing.append(rel)
            continue
        seen.add(rel)
        if git_blob_sha1(p) != sha:
            modified.append(rel)
    extra = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__" and not d.startswith(".")]
        for name in filenames:
            if name.endswith((".pyc", ".pyo")) or name.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if rel not in expected:
                extra.append(rel)
    return {"commit": manifest.get("commit"), "root": root,
            "ok": not (modified or missing or extra),
            "modified": modified, "missing": missing, "extra": sorted(extra)}


# ---------------------------------------------------------------------------
# Taps
# ---------------------------------------------------------------------------

class Taps:
    """Collects what v1 computed, without changing what it computes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.check_rows: dict[tuple[str, str], dict] = {}
        self.summary: dict | None = None
        self.report: dict | None = None
        self.profile_stage: tuple | None = None
        self.phase = "autolabel"
        self.caption_calls: list[dict] = []
        self.dedup_order: list[str] = []
        self.action_hashes: dict[str, str] = {}
        self.fingerprints: dict[str, str] = {}
        self.decode_failures: list[dict] = []
        self.tap_errors: list[str] = []
        self._undo: list[tuple] = []

    def _patch(self, obj, name, new):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, new)

    def uninstall(self):
        while self._undo:
            obj, name, old = self._undo.pop()
            setattr(obj, name, old)

    def install(self):
        import daft

        import curation.adapters.decode as decode_mod
        import curation.dataset_level.caption as caption_mod
        import curation.dataset_level.dedup as dedup_mod
        import curation.export.report as report_mod
        import curation.pipeline.funnel as funnel_mod
        import curation.pipeline.run as run_mod

        taps = self
        orig_collect = daft.DataFrame.collect
        reentry = threading.local()

        def collect(df, *a, **kw):
            out = orig_collect(df, *a, **kw)
            # to_pydict() calls collect() itself; the guard stops the tap from
            # tapping its own read-back
            if getattr(reentry, "active", False):
                return out
            reentry.active = True
            try:
                res = out if out is not None else df
                cols = [c for c in res.column_names if c.startswith("check_")]
                if cols and "episode_id" in res.column_names:
                    d = res.select("episode_id", *cols).to_pydict()
                    with taps.lock:
                        for i, eid in enumerate(d["episode_id"]):
                            for c in cols:
                                v = d[c][i]
                                if v is not None:
                                    taps.check_rows[(c[len("check_"):], str(eid))] = dict(v)
            except Exception as e:  # noqa: BLE001 - a tap must never break the run
                taps.tap_errors.append(f"collect: {type(e).__name__}: {e}")
            finally:
                reentry.active = False
            return out

        self._patch(daft.DataFrame, "collect", collect)

        orig_run = run_mod.run_pipeline

        def run_pipeline(*a, **kw):
            out = orig_run(*a, **kw)
            taps.summary = out
            return out

        self._patch(run_mod, "run_pipeline", run_pipeline)

        orig_stage = run_mod._skill_profile_stage

        def skill_profile_stage(*a, **kw):
            taps.phase = "profile"
            out = orig_stage(*a, **kw)
            taps.profile_stage = out
            return out

        self._patch(run_mod, "_skill_profile_stage", skill_profile_stage)

        orig_save = report_mod.save_report

        def save_report(report, *a, **kw):
            taps.report = report
            return orig_save(report, *a, **kw)

        self._patch(report_mod, "save_report", save_report)

        orig_funnel = funnel_mod.run_funnel

        def run_funnel(*a, **kw):
            taps.phase = "funnel"
            try:
                return orig_funnel(*a, **kw)
            finally:
                taps.phase = "post_funnel"

        self._patch(funnel_mod, "run_funnel", run_funnel)

        orig_caps = caption_mod.caption_episodes

        def caption_episodes(rows, *a, **kw):
            out = orig_caps(rows, *a, **kw)
            try:
                with taps.lock:
                    taps.caption_calls.append({
                        "phase": taps.phase,
                        "episodes": [str(r.get("episode_id")) for r in rows],
                        "captions": [str(c or "") for c in out]})
            except Exception as e:  # noqa: BLE001
                taps.tap_errors.append(f"caption_episodes: {type(e).__name__}: {e}")
            return out

        self._patch(caption_mod, "caption_episodes", caption_episodes)

        orig_ahash = dedup_mod.action_hash

        def action_hash(row, *a, **kw):
            h = orig_ahash(row, *a, **kw)
            with taps.lock:
                eid = str(row.get("episode_id"))
                taps.dedup_order.append(eid)
                taps.action_hashes[eid] = str(h)
            return h

        self._patch(dedup_mod, "action_hash", action_hash)

        orig_fp = dedup_mod.episode_fingerprint

        def episode_fingerprint(row, *a, **kw):
            fp = orig_fp(row, *a, **kw)
            with taps.lock:
                taps.fingerprints[str(row.get("episode_id"))] = str(fp)
            return fp

        self._patch(dedup_mod, "episode_fingerprint", episode_fingerprint)

        orig_decode = decode_mod.decode_window

        def decode_window(path, from_ts, to_ts, *a, **kw):
            try:
                return orig_decode(path, from_ts, to_ts, *a, **kw)
            except Exception as e:
                with taps.lock:
                    taps.decode_failures.append({
                        "path": str(path), "from_ts": from_ts, "to_ts": to_ts,
                        "phase": taps.phase, "error": f"{type(e).__name__}: {e}"[:300]})
                raise

        self._patch(decode_mod, "decode_window", decode_window)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _idx(eid) -> int:
    return R.episode_index(eid)


def build_records(taps: Taps) -> dict[str, list[dict]]:
    by_module: dict[str, list[dict]] = collections.defaultdict(list)
    for (module, eid), struct in taps.check_rows.items():
        by_module[module].append(R.record_from_v1_struct(module, eid, struct))
    return {m: R.sort_records(rows) for m, rows in by_module.items()}


def build_verdicts(summary: dict) -> list[dict]:
    rows = []
    for eid, v in (summary.get("verdicts") or {}).items():
        rows.append({"episode_index": _idx(eid), "verdict": v.get("verdict"),
                     "reason": v.get("reason", ""),
                     "hard_fails": list(v.get("hard_fails") or []),
                     "soft_score": v.get("soft_score"),
                     "undecidable": list(v.get("undecidable") or [])})
    return sorted(rows, key=lambda r: r["episode_index"])


def build_autolabel(taps: Taps) -> list[dict]:
    rows = []
    for call in taps.caption_calls:
        if call["phase"] != "autolabel":
            continue
        for eid, cap in zip(call["episodes"], call["captions"]):
            rows.append({"episode_index": _idx(eid), "caption": cap, "has_caption": bool(cap)})
    return sorted(rows, key=lambda r: r["episode_index"])


def build_dedup(taps: Taps, report: dict) -> dict:
    dups = (report.get("episodes") or {}).get("duplicates") or []
    groups = collections.defaultdict(list)
    for eid in taps.dedup_order:
        groups[taps.action_hashes.get(eid)].append(_idx(eid))
    return {
        "enabled": not (report.get("dataset") or {}).get("dedup_note"),
        "order": [_idx(e) for e in taps.dedup_order],
        "action_collisions": sorted(sorted(g) for g in groups.values() if len(g) > 1),
        "fingerprints": {str(_idx(e)): fp for e, fp in sorted(taps.fingerprints.items())},
        "dropped": sorted(({"episode_index": _idx(d["episode_id"]),
                            "duplicate_of": _idx(d["duplicate_of"])} for d in dups),
                          key=lambda d: d["episode_index"]),
    }


_AUDIT_TIERS = ("high", "mid_for_review", "low_caption_unstable")


def _normalize_audit(audit: dict | None) -> list[dict]:
    rows = []
    for tier in _AUDIT_TIERS:
        for e in (audit or {}).get(tier) or []:
            row = {"episode_index": _idx(e.get("id")), "tier": tier,
                   "reason": e.get("reason"), "priority": e.get("priority"),
                   "caption_stable": e.get("caption_stable")}
            if isinstance(e.get("recaptions"), list):
                # identical re-caption requests may come back in any order under
                # concurrency; the stability verdict does not depend on the order
                row["recaptions_sorted"] = sorted(str(c) for c in e["recaptions"])
            rows.append(row)
    return sorted(rows, key=lambda r: (r["episode_index"], r["tier"]))


def build_skill_profile(taps: Taps, report: dict) -> dict:
    from curation.dataset_level.profile import skill_assignment_rows

    profile = report.get("skills") or {}
    caption_of, gtext_of, gsrc_of = {}, {}, {}
    if taps.profile_stage:
        _p, caption_of, gtext_of, gsrc_of, _audit = taps.profile_stage
    assignments = []
    for row in skill_assignment_rows(profile, caption_of, gtext_of, gsrc_of):
        assignments.append({"episode_index": _idx(row["episode_id"]),
                            "family": row.get("family", ""),
                            "subskill": row.get("subskill", ""),
                            "caption": row.get("caption", ""),
                            "grouping_text": row.get("grouping_text", ""),
                            "grouping_text_source": row.get("grouping_text_source", "")})
    return {"ran": taps.profile_stage is not None,
            "assignments": sorted(assignments, key=lambda r: r["episode_index"]),
            "label_audit_queue": _normalize_audit(report.get("label_audit")),
            "raw": {"profile": profile, "label_audit": report.get("label_audit")}}


def build_final(summary: dict, report: dict, skill: dict) -> dict:
    """Final lists under v2 semantics, plus v1's own file views for reference.

    v1 quirk: ``passed.json`` is built from the funnel verdicts, so an episode
    that dedup removed still appears there (and also in ``reject.json``); the
    delivered dataset does not contain it. The normalized lists follow the
    delivered set: passed = funnel keep - duplicates.
    """
    verdicts = summary.get("verdicts") or {}
    results = (report.get("episodes") or {}).get("results") or {}
    dups = {d["episode_id"] for d in (report.get("episodes") or {}).get("duplicates") or []}
    keep = {e for e, v in verdicts.items() if v.get("verdict") == "keep"}
    drop = {e for e, v in verdicts.items() if v.get("verdict") == "drop"}
    undecidable = {e for e, pe in results.items() if pe.get("undecidable")}
    queue = {r["episode_index"] for r in skill["label_audit_queue"]}
    ids = lambda s: sorted(_idx(e) for e in s)  # noqa: E731
    return {
        "passed": ids(keep - dups),
        "reject": ids(drop | dups),
        "held": [],
        "review": sorted(set(ids(undecidable)) | queue),
        "v1_views": {"passed_json": ids(keep), "reject_json": ids(drop | dups),
                     "review_json": ids(undecidable)},
        "n_delivered": summary.get("n_delivered"),
    }


def suspect_probe_hashes(tape_path: str) -> list[str]:
    """Probe answers v1 could not parse; drop them to re-ask in replay-record."""
    from curation.adapters.vlm_client import parse_completion, strip_reasoning

    bad = []
    _, entries = T.read_tape(tape_path)
    for e in entries:
        if e.get("kind") != "logical" or e.get("tag") != "probe" or e.get("outcome") != "response":
            continue
        try:
            body = json.loads(e.get("body") or "{}")
            parse_completion(strip_reasoning(body["choices"][0]["message"]["content"]))
        except Exception:  # noqa: BLE001
            bad.append(e["hash"])
    return sorted(set(bad))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m parity dump-v1",
        description="Run v1 once (as `curation <args>`) and export normalized records.")
    p.add_argument("--out", required=True, help="dump directory (created, must be empty)")
    p.add_argument("--v1-src", default=DEFAULT_V1_SRC,
                   help="directory holding the v1 `curation/` package (default: this repo)")
    p.add_argument("--manifest", default=DEFAULT_MANIFEST,
                   help="v1 source manifest (git blob hashes at the freeze commit)")
    p.add_argument("--allow-source-drift", action="store_true",
                   help="run even if the v1 source differs from the manifest (not a golden run)")
    p.add_argument("--replay", metavar="TAPE", help="serve model calls from this tape")
    p.add_argument("--record-missing", action="store_true",
                   help="with --replay: calls not on the tape (or failed there) go live "
                        "and a complete new tape is written")
    p.add_argument("--drop-hashes", metavar="FILE",
                   help="with --record-missing: request hashes to re-ask live, one per line")
    p.add_argument("--tape-out", metavar="FILE",
                   help="where to write the tape (default: <out>/vlm_tape.jsonl.gz)")
    p.add_argument("--fake-vlm", action="store_true",
                   help="live calls go to the built-in deterministic fake model (tests)")
    p.add_argument("--fake-vlm-model", default="fake-vlm")
    p.add_argument("--allow-failures", action="store_true",
                   help="exit 0 even when the run had execution failures")
    p.add_argument("--label", default="", help="free-form label stored in dump.json")
    return p


def _versions() -> dict:
    out = {"python": sys.version.split()[0], "platform": platform.platform(),
           "machine": platform.machine()}
    for mod in ("daft", "numpy", "scipy", "cv2", "av", "pyarrow", "pandas"):
        try:
            out[mod] = getattr(__import__(mod), "__version__", "?")
        except Exception:  # noqa: BLE001
            out[mod] = None
    return out


def _write_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True, allow_nan=True)


def main(argv: list[str]) -> int:
    if "--" in argv:
        cut = argv.index("--")
        ours, v1_args = argv[:cut], argv[cut + 1:]
    else:
        ours, v1_args = argv, []
    args = build_parser().parse_args(ours)
    if not v1_args:
        print("dump-v1: give the v1 command after `--`, e.g. `-- run --input ... --output ...`",
              file=sys.stderr)
        return EXIT_USAGE
    if args.record_missing and not args.replay:
        print("dump-v1: --record-missing needs --replay", file=sys.stderr)
        return EXIT_USAGE
    if os.path.exists(args.out) and os.listdir(args.out):
        print(f"dump-v1: {args.out} is not empty", file=sys.stderr)
        return EXIT_USAGE
    os.makedirs(args.out, exist_ok=True)

    src = verify_v1_source(args.v1_src, args.manifest)
    if not src["ok"] and not args.allow_source_drift:
        print("dump-v1: v1 source differs from the freeze manifest "
              f"(commit {src['commit']}): modified={src['modified'][:10]} "
              f"missing={src['missing'][:10]} extra={src['extra'][:10]}", file=sys.stderr)
        if not os.path.isdir(os.path.join(args.v1_src, "curation")):
            print("dump-v1: no v1 package there; in the repo, extract it first with "
                  "`python -m parity v1-src --out DIR` and pass `--v1-src <printed path>`",
                  file=sys.stderr)
        _write_json(os.path.join(args.out, "dump.json"),
                    {"dump_schema_version": DUMP_SCHEMA_VERSION, "status": "refused",
                     "v1_source": src})
        return EXIT_USAGE

    sys.path.insert(0, os.path.abspath(args.v1_src))
    import curation  # noqa: E402
    import curation.adapters.vlm_client as vlm_client  # noqa: E402
    import curation.cli as v1_cli  # noqa: E402

    loaded_from = os.path.dirname(os.path.abspath(curation.__file__))
    if os.path.normpath(loaded_from) != os.path.normpath(src["root"]):
        print(f"dump-v1: imported curation from {loaded_from}, expected {src['root']}",
              file=sys.stderr)
        return EXIT_USAGE

    mode = "record"
    replay_entries = None
    if args.replay:
        _, replay_entries = T.read_tape(args.replay)
        mode = "replay-record" if args.record_missing else "replay"
    tape_out = None if mode == "replay" else (args.tape_out
                                              or os.path.join(args.out, "vlm_tape.jsonl.gz"))
    drop = []
    if args.drop_hashes:
        with open(args.drop_hashes, encoding="utf-8") as fh:
            drop = [ln.strip() for ln in fh if ln.strip()]
    transport = None
    fake = None
    if args.fake_vlm:
        from .fakevlm import FakeVlm
        fake = FakeVlm(args.fake_vlm_model)
        transport = fake.transport()
    hooks = T.TapeHooks(mode, tape_out=tape_out, replay_entries=replay_entries,
                        transport=transport, drop_hashes=drop,
                        tape_meta={"v1_commit": src["commit"], "v1_args": v1_args,
                                   "label": args.label})
    taps = Taps()
    started = time.time()
    exit_code, crash = None, None
    hooks.install(vlm_client)
    taps.install()
    try:
        try:
            exit_code = v1_cli.main(list(v1_args))
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except Exception:  # noqa: BLE001
            crash = traceback.format_exc()
            exit_code = 1
    finally:
        taps.uninstall()
        hooks.uninstall()
    finished = time.time()

    summary, report = taps.summary or {}, taps.report or {}
    recs = build_records(taps)
    os.makedirs(os.path.join(args.out, "records"), exist_ok=True)
    for module, rows in recs.items():
        R.write_jsonl(os.path.join(args.out, "records", f"{module}.jsonl"), rows)
    R.write_jsonl(os.path.join(args.out, "verdicts.jsonl"), build_verdicts(summary))
    R.write_jsonl(os.path.join(args.out, "autolabel.jsonl"), build_autolabel(taps))
    dedup = build_dedup(taps, report)
    _write_json(os.path.join(args.out, "dedup.json"), dedup)
    skill = build_skill_profile(taps, report) if report else {
        "ran": False, "assignments": [], "label_audit_queue": [], "raw": {}}
    _write_json(os.path.join(args.out, "skill_profile.json"), skill)
    final = build_final(summary, report, skill)
    _write_json(os.path.join(args.out, "final.json"), final)
    if hooks.store is not None:
        R.write_jsonl(os.path.join(args.out, "replay_misses.jsonl"), hooks.store.misses)

    # -- cleanliness (D33: any failed call or decode makes the run unusable) --
    problems = []
    tape_summary = None
    if tape_out and os.path.exists(tape_out):
        tape_summary = T.summarize_tape(tape_out)
        if tape_summary["failures"]:
            problems.append({"kind": "model_call_failed", "count": len(tape_summary["failures"]),
                             "examples": tape_summary["failures"][:5]})
        suspects = suspect_probe_hashes(tape_out)
        if suspects:
            with open(os.path.join(args.out, "suspect_hashes.txt"), "w") as fh:
                fh.write("\n".join(suspects) + "\n")
            problems.append({"kind": "probe_answer_unparseable", "count": len(suspects)})
    incidents = [{"module": m, "episode_index": r["episode_index"], **inc}
                 for m, rows in recs.items() for r in rows
                 for inc in ((r.get("error") or {}).get("incidents") or [])]
    if incidents:
        problems.append({"kind": "degraded_results", "count": len(incidents),
                         "examples": incidents[:10]})
    if taps.decode_failures:
        problems.append({"kind": "decode_failed", "count": len(taps.decode_failures),
                         "examples": taps.decode_failures[:5]})
    if mode == "replay" and hooks.store.misses:
        # in replay-record mode a miss is expected: it went live and is on the new tape
        problems.append({"kind": "replay_miss", "count": len(hooks.store.misses)})
    note = (report.get("dataset") or {}).get("task_success_note")
    if note:
        problems.append({"kind": "task_success_not_run", "note": note})
    if exit_code not in (0, None) or crash:
        problems.append({"kind": "v1_run_failed", "exit_code": exit_code,
                         "crash": (crash or "")[-2000:]})
    if taps.tap_errors:
        problems.append({"kind": "tap_error", "examples": taps.tap_errors[:5]})

    status = "clean" if not problems else "dirty"
    config = report.get("config_effective")
    config_sha = (hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False)
                                 .encode("utf-8")).hexdigest() if config is not None else None)
    meta = {
        "config_effective": config, "config_sha256": config_sha,
        "config_path": os.environ.get("CURATION_CONFIG") or None,
        "dump_schema_version": DUMP_SCHEMA_VERSION,
        "record_schema_version": R.RECORD_SCHEMA_VERSION,
        "label": args.label, "status": status, "problems": problems,
        "v1_source": src, "v1_args": v1_args, "v1_exit_code": exit_code,
        "v1_run_dir": summary.get("run_dir"), "v1_delivery_dir": summary.get("delivery_dir"),
        "dataset": summary.get("dataset_name"), "robot": summary.get("robot"),
        "tape": {"mode": mode, "path": tape_out, "replayed_from": args.replay,
                 "hooks": hooks.stats(), "summary": (
                     {k: v for k, v in tape_summary.items() if k != "failures"}
                     if tape_summary else None),
                 "fake_vlm": bool(fake)},
        "counts": {"records": {m: len(r) for m, r in recs.items()},
                   "verdicts": len(summary.get("verdicts") or {}),
                   "passed": len(final["passed"]), "reject": len(final["reject"]),
                   "review": len(final["review"])},
        "versions": _versions(),
        "started_at": _dt.datetime.fromtimestamp(started).isoformat(timespec="seconds"),
        "finished_at": _dt.datetime.fromtimestamp(finished).isoformat(timespec="seconds"),
        "wall_s": round(finished - started, 1),
    }
    _write_json(os.path.join(args.out, "dump.json"), meta)
    print(f"[dump-v1] {status}: {meta['counts']} -> {args.out}", file=sys.stderr)
    for p in problems:
        print(f"[dump-v1] problem: {p['kind']} {p.get('count', '')}", file=sys.stderr)
    if exit_code not in (0, None) or crash:
        return EXIT_V1_FAILED
    if problems and not args.allow_failures:
        return EXIT_DIRTY
    return EXIT_OK
