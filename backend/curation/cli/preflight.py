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
  without a task text never grey them out - task_success leaves those episodes
  unjudged (D72), which a note says.
* ``--modules`` narrows the report to the selected modules: nothing is asked
  about a module that is not selected.
* EEF-video consistency (registry 1.4, design doc 12): ``--param
  eef_video_consistency.trajectory_json=PATH`` is validated against the dataset and
  the entry carries the per sub-item capability table; without the file the module
  is unsupported (the console cannot take the file before F5.5).
* Warnings only, never a change of availability (D52, design doc 14 §1): data and video
  files that are empty or too small to be valid (the listing), mcap recordings cut off
  (no end marker) and mcap summary sections failing their CRC (the bytes the summary
  read fetches anyway). Checks that read the data are the data integrity module's.
"""
from __future__ import annotations

import argparse
import os

from ..contracts import modules as registry_modules
from . import inputs, lerobot_meta, modparams, source_manifest
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
    p.add_argument("--declaration", metavar="FILE",
                   help="the dataset declaration (C7 dataset-declaration/1.0, design doc 25 §3): what the records "
                        "mean and how they become pixels; without it one is drafted to say what is missing")
    modparams.add_argument(p)
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
            "lancedb": f"LanceDB (Lance) tables - {fmt.note}"}.get(fmt.kind, fmt.kind)


#: what this version reads (D44): LeRobot v2 / v3, mcap, lance (lerobot-lance-convert)
SUPPORTED = "LeRobot v2/v3, mcap and lance (lerobot-lance-convert >= 0.3.0)"


def load_declaration(args) -> dict | None:
    """``--declaration``: the dataset declaration, normalized; a file that is not one is a usage error."""
    path = getattr(args, "declaration", None)
    if not path:
        return None
    from .. import declaration as DCL

    try:
        return DCL.load(os.path.expanduser(path))
    except (OSError, ValueError) as e:
        raise UsageError(f"--declaration: {e}") from None


def run(ctx: Context, args: argparse.Namespace) -> Result:
    specs = parse_modules(args.modules)
    args.module_params = modparams.parse(getattr(args, "param", None))
    args.declaration_doc = load_declaration(args)
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
    if fmt.kind in ("lerobot", "mcap", "lance"):
        # a git clone without Git LFS: say so, not "Parquet magic bytes not found" further down
        pointers, candidates = lerobot_meta.lfs_pointers(storage, listing)
        if pointers:
            return _invalid(ctx, doc, specs, fmt, [lerobot_meta.lfs_problem(pointers, candidates)])
    if fmt.kind in ("mcap", "lance"):
        from . import preflight_containers

        doc["meta_fingerprint"] = lerobot_meta.fingerprint(
            [listing[k] for k in lerobot_meta.fingerprint_keys(listing, fmt.kind)])
        if manifest is not None:
            preflight_containers.verify_manifest(manifest, listing, fmt.kind)
        preflight_containers.fill(ctx, args, storage, listing, fmt, specs, doc)
        return _done(ctx, doc)
    if fmt.kind == "umi_session":
        doc["meta_fingerprint"] = lerobot_meta.fingerprint(
            [listing[k] for k in lerobot_meta.fingerprint_keys(listing, fmt.kind)])
        _umi_session(ctx, args, storage, listing, specs, doc)
        return _done(ctx, doc)
    if fmt.kind != "lerobot":
        reason = f"only {SUPPORTED} are supported; detected {_describe_kind(fmt)}"
        if fmt.kind == "unknown":
            reason = (f"only {SUPPORTED} are supported; the input is not a recognised dataset "
                      f"({fmt.note})")
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

    _fill_supported(doc, specs, meta, listing, args, storage.uri, storage=storage)
    _viz_descriptor(doc, info, listing, storage)
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
    mark_invalid(doc, specs, fmt, problems)
    return _done(ctx, doc)


def mark_invalid(doc: dict, specs, fmt: Format, problems: list[str]) -> None:
    what = {"mcap": "mcap dataset", "lance": "lance dataset"}.get(fmt.kind, "LeRobot dataset")
    doc["format"] = {"kind": fmt.kind, "version": fmt.version, "supported": False,
                     "detail": f"{what} with invalid metadata: " + problems[0]}
    doc["validation"] = problems
    doc["modules"] = _unsupported(specs, "the dataset metadata is invalid (see validation): "
                                  + problems[0], "metadata_invalid", {"problem": problems[0]})


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


def _viz_descriptor(doc: dict, info: dict, listing, storage) -> None:
    """What the data visualizer reads from a LeRobot dataset (design doc 18 §7; C2 optional fields):
    every feature, each camera's codec / size / fps and whether a browser cannot play it, and the
    segment annotations it recognises (or warns it cannot read). Metadata only: a few hundred bytes
    of each small ``meta/*.jsonl`` table at most."""
    from ..viz import annotations as viz_ann
    from ..viz import lerobot_info as viz_info

    ds = doc.get("dataset")
    if not isinstance(ds, dict):
        return
    ds["features"] = viz_info.features_of(info)
    ds["camera_info"] = viz_info.camera_info_of(info)
    meta_files = sorted(k for k in listing if k.startswith("meta/"))
    peeks: dict[str, dict | None] = {}

    def peek(rel: str) -> dict | None:
        if rel not in peeks:
            try:
                rows = viz_ann.parse_jsonl(storage.read_range(rel, 0, 16384))
                peeks[rel] = rows[0] if rows else None
            except Exception:  # noqa: BLE001 - an unreadable table: the name decides
                peeks[rel] = None
        return peeks[rel]

    first_episode = peek("meta/episodes.jsonl") if "meta/episodes.jsonl" in listing else None
    fields = set(first_episode) if isinstance(first_episode, dict) else set()
    ds["segment_sources"] = [s.as_preflight() for s in
                             viz_ann.detect_sources(info, meta_files, peek=peek, episode_fields=fields)]


def _fill_supported(doc: dict, specs, meta, listing, args, uri: str, *,
                    container: dict | None = None, storage=None) -> None:
    """``container`` (D44, ``preflight_containers``): an mcap / lance dataset described as
    LeRobot metadata - ``kind``, ``detail``, ``cameras_present`` (the cameras whose videos
    exist; they live in the files / tables, not as mp4 objects), ``profile_name`` (the
    name v1 matches semantics profiles with), ``robot_where`` (where the robot type is
    read) and ``total_frames``."""
    info, episodes = meta.info, meta.episodes
    n = len(episodes)
    warnings = list(meta.warnings)

    # files behind every episode (listing only)
    missing_eps, cams_with_files = [], set()
    for ep in episodes if container is None else ():
        present = [cam for cam, key in ep.video_keys.items() if key in listing]
        cams_with_files.update(present)
        if any(k not in listing for k in ep.data_keys) or len(present) < len(ep.video_keys):
            missing_eps.append(ep.index)
    if container is not None:
        cams_with_files = set(container["cameras_present"])
    if missing_eps:
        from .episodes import preview

        if meta.fmt.version == "v2":
            warnings.append(f"{_plural(len(missing_eps), 'episode')} miss their parquet or a "
                            f"camera's video ({preview(missing_eps)}); they are left out like "
                            f"v1 does: not checked, in no list, listed in the report")
        else:
            warnings.append(f"{_plural(len(missing_eps), 'episode')} miss data or video files "
                            f"({preview(missing_eps)}); LeRobot v3 episodes are not left out "
                            f"(v1 reads them): their checks will fail to read them")
    for cam in meta.cameras:
        if cam not in cams_with_files and n:
            warnings.append(f"camera {lerobot_meta.short_camera(cam)} has no video files")
    if container is None:
        warnings += empty_files(episodes, listing)

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
    if container is not None and "total_frames" in container:
        total_frames = container["total_frames"]
    fps = info.get("fps")
    with_task = sum(1 for ep in episodes if ep.task.strip())
    without_task = n - with_task
    rt = info.get("robot_type")
    dataset_name = uri.rstrip("/").rsplit("/", 1)[-1]
    if container is not None:
        dataset_name = container["profile_name"]
    profile, suggested = _profile(info, dataset_name)
    where = container["robot_where"] if container is not None else "info.json"

    doc["format"] = {"kind": "lerobot", "version": meta.fmt.version, "supported": True,
                     "detail": f"LeRobot {meta.fmt.codebase_version}, "
                               f"{_plural(n, 'episode')}, {_plural(len(meta.cameras), 'camera')}"}
    if container is not None:
        doc["format"].update(kind=container["kind"], detail=container["detail"])
    doc["dataset"] = {
        "episode_count": n,
        "cameras": [lerobot_meta.short_camera(c) for c in meta.cameras],
        "fps": float(fps) if isinstance(fps, (int, float)) and not isinstance(fps, bool) else None,
        "robot_type": rt.strip() if isinstance(rt, str) and rt.strip() else None,
        "total_frames": total_frames,
        "labels": {"with_task": with_task, "without_task": without_task},
        "profile": profile,
    }
    if indices != list(range(n)):
        # a subset that keeps its source's numbers, mcap files named episode_<N>.mcap: the plan, the
        # Daemon's selection and the console's form select from these, not from 0..n-1
        from .episodes import compact

        doc["dataset"]["episode_indices"] = compact(indices)

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
                    f"task_success will not judge them, their cameras are still checked "
                    f"for picture defects") if without_task else ""

    modules = []
    eef_base = None
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
        elif "eef_input" in spec.needs and container is not None and container["kind"] != "mcap":
            # mcap cameras are image topics the module reads itself (F5.13); lance is not read yet
            entry.update(availability="unsupported",
                         reason=f"EEF-video consistency reads LeRobot and mcap datasets only, not "
                                f"{container['kind']}",
                         reason_code="format_unsupported_by_module",
                         reason_args={"format": container["kind"]})
        elif "eef_input" in spec.needs:
            from ..extensions.eef_consistency import preflight as eef_preflight

            if eef_base is None:
                eef_base = _eef_entry(eef_preflight, storage, listing, uri, getattr(args, "module_params", {}),
                                      [ep.index for ep in episodes],
                                      handheld=container is not None and container.get("profile") == "umi_das",
                                      decl=getattr(args, "declaration_doc", None), info=info,
                                      kind=container["kind"] if container is not None else "lerobot")
            entry.update(eef_preflight.module_entry(eef_base, vlm_backend=bool(vlm_backend)))
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
                said = (f"robot_type is '{emb_subject}' in {where}" if emb_subject
                        else f"robot_type not found in {where}")
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
                             f"({where} robot_type: {rt!r})")
        if "vlm" in spec.needs and caption_note and entry["availability"] != "unsupported" \
                and "eef_input" not in spec.needs:
            notes.append(caption_note)
        if notes:
            entry["notes"] = entry.get("notes", []) + notes
        modules.append(entry)
    # A rider (registry 1.14) is answered inside its host's requests: it is exactly as available
    # as the host, reason and hint included, whatever its own needs would have said here.
    by_id = {m["id"]: m for m in modules}
    for i, entry in enumerate(modules):
        host = registry_modules.get(entry["id"]).rides_on
        if host and host in by_id:
            modules[i] = {**by_id[host], "id": entry["id"]}
    doc["modules"] = modules
    doc["warnings"] = warnings


#: below these sizes a file cannot hold its format's fixed bytes: parquet's two magics and
#: footer length, mcap's two magics and footer record, an mp4's ftyp and a moov with one
#: video track (a real clip of a few low-resolution frames is a few KB: no higher bound)
MIN_VALID_BYTES = {".parquet": 12, ".mcap": 45, ".mp4": 512}


def too_small(key: str, size: int) -> bool:
    """An empty file, or one too small to be a valid file of its format (D52)."""
    ext = os.path.splitext(key.lower())[1]
    return int(size) == 0 or int(size) < MIN_VALID_BYTES.get(ext, 1)


def empty_files(episodes, listing) -> list[str]:
    """D52: the episodes' data and video files that are empty or too small, as one warning
    (the listing alone; a LeRobot v3 file shared by several episodes is named once)."""
    from .episodes import preview

    bad: dict[str, list[int]] = {}
    for ep in episodes:
        for key in list(ep.data_keys) + list(ep.video_keys.values()):
            obj = listing.get(key)
            if obj is not None and too_small(key, obj.size):
                bad.setdefault(key, []).append(ep.index)
    if not bad:
        return []
    eps = sorted({e for idx in bad.values() for e in idx})
    shown = ", ".join(f"{k} {int(listing[k].size)} B" for k in sorted(bad)[:3])
    more = ", ..." if len(bad) > 3 else ""
    return [f"{_plural(len(bad), 'file')} {'is' if len(bad) == 1 else 'are'} empty or too small to be valid "
            f"({_plural(len(eps), 'episode')}: {preview(eps)}; {shown}{more})"]


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


def _eef_entry(eef_preflight, storage, listing, uri: str, module_params: dict, episodes: list[int], *,
               handheld: bool = False, decl: dict | None = None, info: dict | None = None,
               kind: str = "lerobot") -> dict:
    """The EEF module's entry: how its trajectory is had, in the order of design doc 25 §4.1 (``trajectory_source``) -
    the file a caller gave; generated (a raw UMI session, a handheld gripper's LeRobot export or raw mcap, a robot
    arm's pose record by the dataset declaration ``decl``); the dataset's own ``trajectory.json``. A dataset with
    none says what is missing: the declaration's parts (``needs_input`` declaration_incomplete - complete it on the
    dataset page, or upload a trajectory.json) or any pose record (trajectory_missing). Without ``decl`` one is
    drafted from the metadata, to say what is missing - never to generate from."""
    import os

    from ..extensions.eef_consistency import contracts as EC
    from ..extensions.eef_consistency import derive

    params = modparams.with_defaults(eef_preflight.MODULE_ID, module_params.get(eef_preflight.MODULE_ID))
    upload = bool((params.get("trajectory_json") or "").strip())
    drafted = False
    cameras = _cameras_of(info, decl, kind)
    if decl is None and not upload and kind == "lerobot" and isinstance(info, dict):
        from ..declaration import draft as DD

        decl = DD.draft_lerobot(info, listing=listing, read=lambda key: derive._read(storage, key))["declaration"]
        drafted = True
    plan = derive.plan_source(listing=listing, upload=upload, handheld=handheld, decl=decl, drafted=drafted,
                              cameras=cameras, kind=kind)
    source: dict = {"kind": plan["kind"]}
    if decl is not None and ("readiness" in plan or plan["kind"] == derive.MCAP_DERIVE):
        from .. import declaration as DCL

        source["declaration"] = {"drafted": drafted, "sha256": DCL.sha256(decl)}
    if plan.get("missing"):
        source["missing"] = plan["missing"]
    how = plan["kind"]
    if how == derive.MISSING_POSE:
        entry = eef_preflight.consistency_entry(params, episodes=episodes, media_exists=lambda key: key in listing,
                                                lerobot_root=None if "://" in uri else uri)
    elif how == derive.MISSING_DECLARATION:
        names = ", ".join(f"{m['field']} ({m['code']})" for m in plan["missing"][:6])
        entry = {"availability": EC.NEEDS_INPUT, "reason_code": "declaration_incomplete",
                 "reason": f"the dataset records the pose, but its declaration lacks {names}: complete the declaration "
                           f"(the dataset page) or upload a trajectory.json",
                 "reason_args": {"missing": plan["missing"]}, "input_hint": {"field": "trajectory_json"}}
    elif how == derive.MCAP_DERIVE:
        hh = (((decl or {}).get("calibration") or {}).get("handheld") or {}).get("calibration") if not drafted else None
        entry = eef_preflight.derived_entry(params, episodes=episodes, lerobot_root=None if "://" in uri else uri,
                                            calibration=hh)
    elif how == derive.GENERATE and "readiness" in plan:
        joints = isinstance(((decl or {}).get("semantics") or {}).get("joints"), dict)
        entry = eef_preflight.declared_entry(params, plan["readiness"], episodes=episodes, joints=joints)
    elif how == derive.UPLOAD:
        entry = eef_preflight.consistency_entry(params, episodes=episodes, media_exists=lambda key: key in listing,
                                                lerobot_root=None if "://" in uri else uri)
    else:                                          # session, a handheld LeRobot export, the dataset's own file
        try:
            temp = derive.to_temp(storage, listing)
        except Exception as e:  # noqa: BLE001 - the dataset's own data does not make a trajectory: asked for instead
            return {"availability": EC.NEEDS_INPUT, "reason_code": EC.TRAJECTORY_MISSING,
                    "reason": f"the trajectory could not be generated from the dataset ({e}"[:240]
                              + "): upload a trajectory.json (console) or pass "
                                "--param eef_video_consistency.trajectory_json=PATH",
                    "input_hint": {"field": "trajectory_json"}, "trajectory_source": {"kind": derive.MISSING_POSE}}
        try:
            entry = eef_preflight.consistency_entry({**params, "trajectory_json": temp}, episodes=episodes,
                                                    media_exists=lambda key: key in listing,
                                                    lerobot_root=None if "://" in uri else uri)
        finally:
            os.unlink(temp)
        entry["notes"] = entry.get("notes", []) + [
            {"generate": "trajectory generated from the dataset's state and camera calibration",
             "session": "trajectory computed from the raw UMI session (plan, SLAM camera poses, session calibration)"}
            .get(how, "trajectory from the dataset's own trajectory.json")]
    entry["trajectory_source"] = source
    return entry


def _cameras_of(info: dict | None, decl: dict | None, kind: str) -> list[str]:
    """The dataset's camera sources: LeRobot video / image keys, an mcap declaration's camera topics."""
    if kind == "mcap":
        return [c["topic"] for c in (decl or {}).get("cameras") or []]
    feats = (info or {}).get("features") or {}
    return [k for k, f in feats.items() if isinstance(f, dict) and f.get("dtype") in ("video", "image")
            and "depth" not in k.lower()]


def _umi_session(ctx: Context, args, storage, listing, specs, doc: dict) -> None:
    """A raw UMI / TRUMI session read in place (design doc 24 §6): the plan says how many episodes and cameras;
    the EEF module runs on it (its trajectory is computed from the session), nothing else does - there are
    no LeRobot columns and no task text."""
    import os
    import tempfile

    import numpy as np

    from ..extensions.eef_consistency.adapters import umi

    fd, tmp = tempfile.mkstemp(prefix="umi-plan-", suffix=".pkl")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(storage.read_bytes("dataset_plan.pkl") if storage.remote
                     else open(os.path.join(storage.root, "dataset_plan.pkl"), "rb").read())
        plans = umi.read_plan(tmp)
    except Exception as e:  # noqa: BLE001 - an unreadable plan is the dataset's problem
        return mark_invalid(doc, specs, Format("umi_session"), [f"dataset_plan.pkl: {e}"])
    finally:
        os.unlink(tmp)
    cams = len(plans[0]["cameras"])
    frames = [len(p["episode_timestamps"]) for p in plans]
    dt = np.median(np.diff(np.asarray(plans[0]["episode_timestamps"], float)))
    doc["format"] = {"kind": "umi_session", "version": None, "supported": True,
                     "detail": f"UMI raw session: {len(plans)} episode(s), {cams} wrist camera(s), "
                               "read in place (videos not transcoded)"}
    doc["dataset"] = {"episode_count": len(plans), "cameras": [f"camera{j}" for j in range(cams)],
                      "fps": round(1.0 / float(dt), 6) if dt > 0 else None,
                      "robot_type": "umi_dual_handheld_gripper" if cams == 2 else "umi_handheld_gripper",
                      "total_frames": int(sum(frames)), "labels": {"with_task": 0, "without_task": len(plans)},
                      "profile": None}
    from ..extensions.eef_consistency import preflight as eef_preflight

    vlm_backend = (args.vlm_backend or "").strip()
    reason = ("a raw UMI session only has the EEF-video consistency check: it has no LeRobot columns "
              "and no task text")
    modules = []
    for spec in specs:
        if "eef_input" in spec.needs:
            base = _eef_entry(eef_preflight, storage, listing, str(storage.uri), getattr(args, "module_params", {}),
                              list(range(len(plans))), decl=getattr(args, "declaration_doc", None), kind="umi_session")
            modules.append({"id": spec.id, **eef_preflight.module_entry(base, vlm_backend=bool(vlm_backend))})
        else:
            modules.append({"id": spec.id, "availability": "unsupported", "reason": reason,
                            "reason_code": "format_unsupported_by_module",
                            "reason_args": {"format": "umi_session"}})
    doc["modules"] = modules
