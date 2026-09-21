"""Export and incremental re-export of the delivered LeRobot dataset.

The library behind ``curation export --run-dir <dir> [--incremental]`` (design doc
02, section 3.7; doc 06, section 4).  The CLI parses its arguments, calls
``export_run`` and prints ``outcome.result`` -- a ``cli/export.schema.json``
document -- as its ``--json`` output.

    from curation.export.incremental import export_run
    outcome = export_run("<run-dir>", "tos://bucket/datasets/x", incremental=True)
    print(json.dumps(outcome.result))

What gets delivered is ``passed.json`` of the result revision, in its order: the
episodes waiting for a human decision are in it (v1 delivers them and asks for a
review), the episodes waiting for a retry (``held.json``) are not (D24, D35).

``--incremental`` builds on the previous export in the same directory when it can
be trusted (``manifest.json`` + ``manifest.detail.json`` agree, nothing is missing,
same source format and export parameters, no interrupted export) and touches only
what changed; otherwise it exports everything again and says why.  Without it the
dataset is always exported in full.  Either way the directory ends up holding
exactly the new passed list.

Errors: ``ExportInputError`` (bad lists; exit 2), ``SourceChangedError`` (source
differs from ``source_manifest.json``; exit 6), ``ExportError`` (anything else the
export cannot do).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..ingest.lerobot_reader import _load_info
from .diff import Diff, EpisodeKey, diff_episodes
from .incremental_base import (LogFn, PreviousExport, ProgressFn, RunContext, _default_log,
                               load_previous, parse_passed)
from .incremental_v2 import V2Exporter
from .incremental_v3 import V3Exporter
from .manifest import (MANIFEST_NAME, SCHEMA_VERSION, EpisodeEntry, ExportState,
                       export_fingerprint, normalize_params)
from .source import ExportError, ExportInputError, SourceChangedError, SourceGuard
from .target import LocalTarget

__all__ = ["ExportError", "ExportInputError", "ExportOutcome", "SourceChangedError",
           "export_dataset", "export_fingerprint", "export_run", "resolve_revision_dir"]

EXPORT_DIR = "export"
COMPLETE_MARKER = "_COMPLETE"


@dataclass
class ExportOutcome:
    """``result`` is the ``--json`` output.  The rest tells a caller that mirrors the
    export directory to TOS what changed (paths relative to ``lerobot_curated/``):
    upload ``written``, copy or re-upload ``renamed`` (old -> new), delete ``deleted``
    and then ``manifest.detail.json`` and ``manifest.json``, the latter last."""

    result: dict                                   # cli/export.schema.json
    manifest_path: str                             # local path of manifest.json
    state: ExportState
    diff: Diff
    rebuild_reasons: list[str] = field(default_factory=list)   # why not incremental
    written: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


def _keys(entries) -> list[EpisodeKey]:
    return [EpisodeKey(e.episode_index, e.new_index, e.content_key, e.task_key,
                       e.index_from, e.task_index) for e in entries]


def _manifest_keys(manifest: dict) -> list[EpisodeKey]:
    return [EpisodeKey(int(e["episode_index"]), int(e["new_index"]), e["content_key"],
                       e["task_key"]) for e in manifest["episodes"]]


def export_dataset(source: str, passed, output_dir: str, *, incremental: bool = True,
                   source_manifest=None, camera_health: dict | None = None,
                   params: dict | None = None, scratch_dir: str | None = None,
                   concurrency: int = 1, log: LogFn | None = None,
                   progress: ProgressFn | None = None) -> ExportOutcome:
    """Export ``passed`` episodes of ``source`` into ``output_dir``.

    ``output_dir`` receives ``manifest.json``, ``manifest.detail.json`` and
    ``lerobot_curated/``.  ``passed``: ``passed.json`` (path or document) or its
    ``episodes`` list.  ``source_manifest``: ``source_manifest.json`` (path or
    document); every source object read is checked against it.  ``camera_health``:
    v1's camera-health payload for ``meta/curation_camera_health.json``; when None the
    previous export's sidecar is carried over.  ``params``: ``video_file_mb`` /
    ``data_file_mb`` (v3 file-size thresholds).  Scratch goes to ``scratch_dir``, else
    ``$CURATION_EXPORT_SCRATCH``, else the system temp dir (v1's encoders use
    ``$TMPDIR``).
    """
    wanted, raw_entries = parse_passed(passed)
    params_n = normalize_params(params)
    if isinstance(source_manifest, (str, os.PathLike)):
        with open(source_manifest, encoding="utf-8") as f:
            source_manifest = json.load(f)
    ctx = RunContext(log=log, progress=progress, scratch_dir=scratch_dir, concurrency=concurrency)
    try:
        with ctx.capture_stdout():
            guard = SourceGuard(source, source_manifest)
            info = _load_info(guard.root)
            version = str(info.get("codebase_version") or "")
            if version.startswith("v3"):
                cls = V3Exporter
            elif version.startswith("v2"):
                cls = V2Exporter
            else:
                raise ExportError(f"unsupported LeRobot codebase_version {version!r}")
            target = LocalTarget(output_dir)
            exporter = cls(ctx, guard, info, target, None, camera_health=camera_health,
                           params=params_n)
            prev = load_previous(target, source_format=cls.fmt, codebase_version=version,
                                 params=exporter.resolved_params())
            if not incremental and prev.state is not None:
                prev = PreviousExport(None, ["a full export was asked for"], prev.manifest)
            exporter.set_previous(prev.state)

            entries: list[EpisodeEntry] = exporter.plan(wanted)
            fingerprint = export_fingerprint(raw_entries, source_format=cls.fmt,
                                             source_digest=guard.digest(), params=params_n)
            new_keys = _keys(entries)
            work = diff_episodes(_keys(prev.state.episodes) if prev.state else None, new_keys)
            report = work
            if prev.state is None and prev.manifest is not None:
                report = diff_episodes(_manifest_keys(prev.manifest), new_keys)
            if prev.state is None:
                ctx.log("info", "exporting every episode: " + "; ".join(prev.reasons))
            else:
                ctx.log("info", f"incremental export on top of {prev.state.fingerprint}")
            state = exporter.run(entries, work, fingerprint)
    finally:
        ctx.close()

    c = exporter.counters
    counts = report.counts()
    ctx.log("info", f"exported {len(entries)} episode(s): " +
            ", ".join(f"{k} {v}" for k, v in counts.items()) +
            f"; videos copied {c.videos_copied}, re-encoded {c.videos_reencoded}, "
            f"renamed {c.renamed}; {len(exporter.deleted)} stale file(s) deleted")
    manifest_path = target.path(MANIFEST_NAME)
    result = {"schema_version": SCHEMA_VERSION, "format": cls.fmt,
              "incremental": prev.state is not None, "episodes": len(entries),
              "diff": counts, "fingerprint": fingerprint, "manifest": manifest_path,
              "videos_copied": c.videos_copied, "videos_reencoded": c.videos_reencoded}
    return ExportOutcome(result=result, manifest_path=manifest_path, state=state, diff=report,
                         rebuild_reasons=[] if prev.state is not None else list(prev.reasons),
                         written=list(exporter.placer.written), renamed=list(exporter.renamed),
                         deleted=list(exporter.deleted))


# ── run directory entry point ────────────────────────────────────────────────

def _read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_revision_dir(run_dir: str, revision: int | None = None) -> str:
    """``revisions/r<NNNN>/`` of ``run_dir``: the given one, else the newest committed one.

    Only revisions with ``commit.json`` are trusted (design doc 06, section 1).
    """
    base = os.path.join(run_dir, "revisions")
    if revision is not None:
        rev_dir = os.path.join(base, f"r{int(revision):04d}")
        if not os.path.isfile(os.path.join(rev_dir, "commit.json")):
            raise ExportInputError(f"revision {revision} of {run_dir} is not committed "
                                   "(no commit.json)")
        return rev_dir
    names = sorted(n for n in (os.listdir(base) if os.path.isdir(base) else [])
                   if len(n) == 5 and n[0] == "r" and n[1:].isdigit()
                   and os.path.isfile(os.path.join(base, n, "commit.json")))
    if not names:
        raise ExportInputError(f"{run_dir} has no committed result revision to export")
    return os.path.join(base, names[-1])


def export_run(run_dir: str, source: str, *, revision: int | None = None,
               output_dir: str | None = None, incremental: bool = True,
               source_manifest=None, camera_health: dict | None = None,
               params: dict | None = None, scratch_dir: str | None = None,
               concurrency: int = 1, log: LogFn | None = None,
               progress: ProgressFn | None = None) -> ExportOutcome:
    """Export the passed list of a run directory's result revision into ``<run-dir>/export``.

    Reads ``revisions/r<NNNN>/passed.json`` (and ``held.json`` to check the two are
    disjoint), uses ``<run-dir>/source_manifest.json`` when present, and deletes
    ``<run-dir>/_COMPLETE`` before touching anything: the batch is incomplete until
    ``curation verify`` writes it again (design doc 06, section 4.4).
    """
    rev_dir = resolve_revision_dir(run_dir, revision)
    passed_doc = _read_json(os.path.join(rev_dir, "passed.json"))
    if passed_doc.get("list") != "passed":
        raise ExportInputError(f"{rev_dir}/passed.json is not a passed list")
    held_path = os.path.join(rev_dir, "held.json")
    held = set()
    if os.path.isfile(held_path):
        held_doc = _read_json(held_path)
        if held_doc.get("list") != "held":
            raise ExportInputError(f"{held_path} is not a held list")
        held = {int(e["episode_index"]) for e in held_doc.get("episodes") or []}
    both = sorted(held & {int(e["episode_index"]) for e in passed_doc.get("episodes") or []})
    if both:
        raise ExportInputError(f"episodes both passed and held in {rev_dir}: {both[:8]}")
    if source_manifest is None and os.path.isfile(os.path.join(run_dir, "source_manifest.json")):
        source_manifest = os.path.join(run_dir, "source_manifest.json")
    complete = os.path.join(run_dir, COMPLETE_MARKER)
    if os.path.exists(complete):
        os.remove(complete)
    out_dir = output_dir or os.path.join(run_dir, EXPORT_DIR)
    outcome = export_dataset(source, passed_doc, out_dir, incremental=incremental,
                             source_manifest=source_manifest, camera_health=camera_health,
                             params=params, scratch_dir=scratch_dir, concurrency=concurrency,
                             log=log, progress=progress)
    review_path = os.path.join(rev_dir, "review.json")
    if os.path.isfile(review_path):
        exported = {e.episode_index for e in outcome.state.episodes}
        pending = [e["episode_index"] for e in _read_json(review_path).get("episodes") or []
                   if e.get("current_list") == "passed" and e["episode_index"] in exported]
        if pending:
            (log or _default_log)("info", f"{len(pending)} exported episode(s) are still "
                                          "waiting for a human decision")
    rel = os.path.relpath(outcome.manifest_path, os.path.abspath(run_dir))
    if not rel.startswith(".."):
        outcome.result["manifest"] = rel.replace(os.sep, "/")
    return outcome
