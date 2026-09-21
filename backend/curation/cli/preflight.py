"""``curation preflight`` - what can run on this dataset (design doc 02 §3.1, doc 05).

Reads metadata only - the object listing and the small files under ``meta/`` -
and returns in seconds. Output: ``docs/contracts/cli/preflight.schema.json``.

Rules (F2.7, doc 05 §2-§4, D6, D34):

* Anything but LeRobot v2/v3 (``.rrd``, mcap, Lance, other LeRobot versions,
  unknown layouts) greys out every module with the reason.
* ``info.json`` problems go to ``validation`` verbatim (v1's ``validate_info``)
  and make the dataset unsupported.
* Kinematic limits: robot type read and in the embodiment registry ->
  available; read but not in it -> unsupported (only that module is skipped,
  the task does not fail); not read (missing, empty or ``unknown``) ->
  needs_input with the registry's models as options. ``--embodiment-id``
  overrides the robot type, as v1's ``--embodiment-id`` did.
* VLM modules: no backend chosen -> needs_input (field ``vlm``); episodes
  without a task text never grey them out - autolabel captions them first,
  which a note says.
* ``--modules`` narrows the report to the selected modules: nothing is asked
  about a module that is not selected.
"""
from __future__ import annotations

import argparse

from ..contracts import modules as registry_modules
from . import inputs, lerobot_meta, source_manifest
from .errors import InputUnreachable, UsageError
from .framework import Context, Result
from .lerobot_meta import Format, MetaError

SCHEMA_VERSION = "1.0"
STAGE = "preflight"

_CAP_REASON = {
    "timestamps": "the dataset has no timestamp feature",
    "action": "the dataset has no action feature",
    "state": "the dataset has no observation.state feature",
}


def add_parser(sub, parents) -> None:
    p = sub.add_parser(
        "preflight", parents=parents,
        help="read dataset metadata and report which modules can run",
        description="Read the dataset's metadata (never its samples) and report the format, "
                    "the episode count, and for every module whether it is available, needs "
                    "input, or is unsupported - with the reason.")
    inputs.add_arguments(p)
    p.add_argument("--vlm-backend", metavar="NAME",
                   help="the VLM backend the task will use; without it VLM modules need input")
    p.add_argument("--embodiment-id", metavar="ID",
                   help="robot model to use instead of info.json's robot_type")
    p.add_argument("--modules", metavar="IDS",
                   help="comma-separated modules the task selects (default: all); nothing is "
                        "asked about the others")
    p.add_argument("--source-manifest", metavar="FILE",
                   help="source_manifest.json from snapshot; exit 6 if the metadata changed")
    p.set_defaults(func=run)


def parse_modules(value: str | None) -> list:
    specs = list(registry_modules.MODULES)
    if not value:
        return specs
    wanted = [x.strip() for x in str(value).split(",") if x.strip()]
    unknown = [x for x in wanted if x not in registry_modules.ids()]
    if unknown:
        raise UsageError(f"--modules: unknown module {unknown[0]!r}; known: "
                         f"{', '.join(registry_modules.ids())}")
    if not wanted:
        raise UsageError("--modules lists no module")
    return [m for m in specs if m.id in set(wanted)]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _unsupported(specs, reason: str, code: str, args: dict | None = None) -> list[dict]:
    """Every module greyed out for the same reason; ``code`` and ``args`` are for UIs (C2 1.1)."""
    out = []
    for m in specs:
        entry = {"id": m.id, "availability": "unsupported", "reason": reason, "reason_code": code}
        if args:
            entry["reason_args"] = dict(args)
        out.append(entry)
    return out


def _describe_kind(fmt: Format) -> str:
    return {"rrd": ".rrd (rerun) files", "mcap": "mcap files",
            "lancedb": "LanceDB (Lance) tables"}.get(fmt.kind, fmt.kind)


def run(ctx: Context, args: argparse.Namespace) -> Result:
    specs = parse_modules(args.modules)
    storage = inputs.open_input(ctx, args)
    ctx.progress(STAGE, 0, 3)
    listing = storage.list()
    if not listing:
        raise InputUnreachable(f"nothing found at {storage.uri}", {"uri": storage.uri})
    ctx.log("info", f"listed {len(listing)} objects under {storage.uri}")
    manifest = source_manifest.guard(args.source_manifest, storage.uri)
    if manifest is not None:
        manifest.verify(listing, prefix="meta/")
    ctx.progress(STAGE, 1, 3)
    ctx.check_stop("after listing the input")

    meta_objs = [listing[k] for k in lerobot_meta.meta_keys(listing)] or list(listing.values())
    doc = {"schema_version": SCHEMA_VERSION, "format": None, "validation": [], "dataset": None,
           "modules": [], "meta_fingerprint": lerobot_meta.fingerprint(meta_objs),
           "warnings": []}

    fmt = lerobot_meta.detect_format(listing)
    if fmt.kind != "lerobot":
        reason = (f"only LeRobot v2/v3 is supported in this version; detected "
                  f"{_describe_kind(fmt)}")
        if fmt.kind == "unknown":
            reason = (f"only LeRobot v2/v3 is supported in this version; the input is not a "
                      f"recognised dataset ({fmt.note})")
        doc["format"] = {"kind": fmt.kind, "version": None, "supported": False,
                         "detail": reason}
        detected = {"detected": fmt.kind}
        if fmt.kind == "unknown" and fmt.note:
            detected["note"] = fmt.note
        doc["modules"] = _unsupported(specs, reason, "format_unsupported", detected)
        return _done(ctx, doc)

    try:
        info = lerobot_meta.load_info(storage)
    except MetaError as e:
        return _invalid(ctx, doc, specs, Format("lerobot"), [str(e)])
    fmt.codebase_version = str(info.get("codebase_version") or "") or None
    fmt.version = lerobot_meta.version_of(fmt.codebase_version or "")
    if fmt.codebase_version and fmt.version is None:
        reason = (f"only LeRobot v2/v3 is supported in this version; detected LeRobot "
                  f"{fmt.codebase_version}")
        doc["format"] = {"kind": "lerobot", "version": None, "supported": False,
                         "detail": reason}
        doc["modules"] = _unsupported(specs, reason, "format_unsupported",
                                      {"detected": f"lerobot {fmt.codebase_version}"})
        return _done(ctx, doc)

    problems = _validate(info, storage.uri, listing, fmt)
    meta = None
    if not problems:
        try:
            meta = lerobot_meta.read_dataset(storage, listing, info, fmt)
        except MetaError as e:
            problems = [str(e)]
    if problems:
        return _invalid(ctx, doc, specs, fmt, problems)
    ctx.progress(STAGE, 2, 3)
    ctx.check_stop("after reading the metadata")

    _fill_supported(doc, specs, meta, listing, args, storage.uri)
    return _done(ctx, doc)


def _validate(info: dict, uri: str, listing, fmt: Format) -> list[str]:
    from ..ingest.validate import IngestValidationError, validate_info

    problems = []
    try:
        validate_info(info, uri)                    # v1's check, message verbatim
    except IngestValidationError as e:
        problems.append(str(e))
    except Exception as e:  # noqa: BLE001 - malformed shapes (features not a dict, ...)
        problems.append(f"meta/info.json is malformed: {e!r}")
    if not problems and fmt.codebase_version:
        problems += lerobot_meta.check_layout(listing, fmt.codebase_version)
    return problems


def _invalid(ctx: Context, doc: dict, specs, fmt: Format, problems: list[str]) -> Result:
    doc["format"] = {"kind": "lerobot", "version": fmt.version, "supported": False,
                     "detail": "LeRobot dataset with invalid metadata: " + problems[0]}
    doc["validation"] = problems
    doc["modules"] = _unsupported(specs, "the dataset metadata is invalid (see validation): "
                                  + problems[0], "metadata_invalid", {"problem": problems[0]})
    return _done(ctx, doc)


def _profile(info: dict, dataset_name: str) -> tuple[dict | None, str]:
    """(profile field, suggested embodiment) from the dataset semantics profiles."""
    from ..ingest import dataset_semantics as ds

    sem = ds.resolve_semantics(info, None, dataset_name)
    if sem.source != "profile" or not sem.profile_name:
        return None, ""
    prof = next((p for p in ds._load_profiles() if p.get("_file") == sem.profile_name), {})
    name = sem.profile_name[:-5] if sem.profile_name.endswith(".yaml") else sem.profile_name
    by = "+".join(str(k) for k in (prof.get("match") or {})) or "profile"
    return {"matched": name, "by": by}, str(prof.get("suggest_embodiment") or "").strip()


def _embodiment(info: dict, override: str | None):
    """(state, subject, value) - v1's rule: ``--embodiment-id`` wins over ``robot_type``.

    state ``ok``: value is the registry's embodiment id; ``unsupported`` (named but not
    in the registry) and ``needs_input`` (not named, or ``unknown``): value is the
    registry's ids, to list as options. Lookup is case-insensitive and follows aliases.
    """
    from ..registry.registry import EmbodimentRegistry, UnknownEmbodimentError

    rt = info.get("robot_type")
    robot_type = str(rt).strip() if isinstance(rt, str) else ""
    wanted = (override or "").strip() or robot_type
    registry = EmbodimentRegistry()
    if wanted in ("", "unknown"):
        return "needs_input", robot_type, registry.ids()
    try:
        return "ok", wanted, registry.get(wanted).embodiment_id
    except UnknownEmbodimentError:
        return "unsupported", wanted, registry.ids()


def _fill_supported(doc: dict, specs, meta, listing, args, uri: str) -> None:
    info, episodes = meta.info, meta.episodes
    n = len(episodes)
    warnings = list(meta.warnings)

    # files behind every episode (listing only)
    missing_eps, cams_with_files = [], set()
    for ep in episodes:
        present = [cam for cam, key in ep.video_keys.items() if key in listing]
        cams_with_files.update(present)
        if any(k not in listing for k in ep.data_keys) or len(present) < len(ep.video_keys):
            missing_eps.append(ep.index)
    if missing_eps:
        from .episodes import preview

        warnings.append(f"{_plural(len(missing_eps), 'episode')} miss data or video files "
                        f"({preview(missing_eps)}); they are skipped at run time")
    for cam in meta.cameras:
        if cam not in cams_with_files and n:
            warnings.append(f"camera {lerobot_meta.short_camera(cam)} has no video files")

    declared = info.get("total_episodes")
    if isinstance(declared, int) and declared != n:
        warnings.append(f"info.json total_episodes is {declared} but the episode table lists {n}")
    indices = [ep.index for ep in episodes]
    if indices != list(range(n)):
        warnings.append(f"episode indices are not 0..{max(n - 1, 0)}; selections use the "
                        f"indices as listed")

    total_frames = info.get("total_frames")
    if not isinstance(total_frames, int) or isinstance(total_frames, bool):
        total_frames = sum(ep.length for ep in episodes)
    fps = info.get("fps")
    with_task = sum(1 for ep in episodes if ep.task.strip())
    without_task = n - with_task
    rt = info.get("robot_type")
    dataset_name = uri.rstrip("/").rsplit("/", 1)[-1]
    profile, suggested = _profile(info, dataset_name)

    doc["format"] = {"kind": "lerobot", "version": meta.fmt.version, "supported": True,
                     "detail": f"LeRobot {meta.fmt.codebase_version}, "
                               f"{_plural(n, 'episode')}, {_plural(len(meta.cameras), 'camera')}"}
    doc["dataset"] = {
        "episode_count": n,
        "cameras": [lerobot_meta.short_camera(c) for c in meta.cameras],
        "fps": float(fps) if isinstance(fps, (int, float)) and not isinstance(fps, bool) else None,
        "robot_type": rt.strip() if isinstance(rt, str) and rt.strip() else None,
        "total_frames": total_frames,
        "labels": {"with_task": with_task, "without_task": without_task},
        "profile": profile,
    }

    feats = info.get("features") or {}
    caps = {"timestamps": "timestamp" in feats, "action": "action" in feats,
            "state": "observation.state" in feats, "video": bool(cams_with_files),
            "raw_bytes": True}
    if not meta.cameras:
        video_reason = "the dataset declares no video camera"
        video_cause = "none_declared"
    else:
        video_reason = "no video files were found for the declared cameras"
        video_cause = "files_missing"
    emb_state, emb_subject, emb_value = _embodiment(info, args.embodiment_id)
    override = (args.embodiment_id or "").strip()
    vlm_backend = (args.vlm_backend or "").strip()
    caption_note = (f"{_plural(without_task, 'episode')} "
                    f"{'has' if without_task == 1 else 'have'} no task text; "
                    f"the model will caption them first") if without_task else ""

    modules = []
    for spec in specs:
        entry: dict = {"id": spec.id}
        lacking = [c for c in ("timestamps", "action", "state", "video")
                   if c in spec.needs and not caps[c]]
        notes: list[str] = []
        if lacking:
            args: dict = {"missing": lacking}
            if "video" in lacking:
                args["video_cause"] = video_cause
            entry.update(availability="unsupported", reason="; ".join(
                video_reason if c == "video" else _CAP_REASON[c] for c in lacking),
                reason_code="missing_input", reason_args=args)
        elif "embodiment_profile" in spec.needs and emb_state != "ok":
            if emb_state == "unsupported":
                who = "embodiment" if override else "robot_type"
                entry.update(availability="unsupported",
                             reason=f"{who} '{emb_subject}' is not in the embodiment registry "
                                    f"(supported: {', '.join(emb_value)})",
                             reason_code="embodiment_unsupported",
                             reason_args={"subject": emb_subject,
                                          "given_by": "embodiment_id" if override else "robot_type",
                                          "supported": list(emb_value)})
            else:
                said = (f"robot_type is '{emb_subject}' in info.json" if emb_subject
                        else "robot_type not found in info.json")
                entry.update(availability="needs_input",
                             reason=f"{said}; pick a model or skip this module",
                             reason_code="robot_type_unknown",
                             reason_args={"robot_type": emb_subject or None},
                             input_hint={"field": "embodiment_id", "options": list(emb_value)})
                if suggested:
                    notes.append(f"the dataset profile {profile['matched']} suggests "
                                 f"{suggested}")
        elif "vlm" in spec.needs and not vlm_backend:
            entry.update(availability="needs_input",
                         reason="no VLM backend chosen; pick one (add one first if there is "
                                "none)",
                         reason_code="vlm_backend_missing",
                         input_hint={"field": "vlm"})
        else:
            entry["availability"] = "available"
            if "embodiment_profile" in spec.needs and override \
                    and override.lower() != str(rt or "").strip().lower():
                notes.append(f"embodiment {emb_value} given by the caller "
                             f"(info.json robot_type: {rt!r})")
        if "vlm" in spec.needs and caption_note and entry["availability"] != "unsupported":
            notes.append(caption_note)
        if notes:
            entry["notes"] = notes
        modules.append(entry)
    doc["modules"] = modules
    doc["warnings"] = warnings


def _done(ctx: Context, doc: dict) -> Result:
    ctx.progress(STAGE, 3, 3)
    return Result(doc, human=render(doc))


def render(doc: dict) -> str:
    fmt = doc["format"]
    lines = [f"{fmt['detail']} ({'supported' if fmt['supported'] else 'not supported'})"]
    ds = doc.get("dataset")
    if ds:
        prof = ds["profile"]["matched"] if ds["profile"] else "none"
        lines.append(f"  robot_type {ds['robot_type'] or '-'}, {ds['fps'] or '-'} fps, "
                     f"{ds['total_frames'] if ds['total_frames'] is not None else '-'} frames, "
                     f"task text: {ds['labels']['with_task']} with / "
                     f"{ds['labels']['without_task']} without, profile: {prof}")
    for problem in doc["validation"]:
        lines.append(f"  invalid: {problem}")
    lines.append("modules:")
    width = max([len(m["id"]) for m in doc["modules"]] + [8])
    for m in doc["modules"]:
        state = m["availability"].replace("_", " ")
        tail = f" - {m['reason']}" if m.get("reason") else ""
        lines.append(f"  {m['id']:<{width}}  {state}{tail}")
        hint = m.get("input_hint")
        if hint and hint.get("options"):
            lines.append(f"  {'':<{width}}  options: {', '.join(hint['options'])}")
        for note in m.get("notes", []):
            lines.append(f"  {'':<{width}}  note: {note}")
    if doc["warnings"]:
        lines.append("warnings:")
        lines += [f"  - {w}" for w in doc["warnings"]]
    lines.append(f"meta fingerprint: {doc['meta_fingerprint']}")
    return "\n".join(lines)
