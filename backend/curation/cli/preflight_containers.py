"""``curation preflight`` for mcap and lance datasets (D44; v1's PR #155).

Still metadata only, still seconds:

* **mcap** - the files are numbered by v1's rule; of each, only the summary section is
  read (channels, message counts, metadata records: a few ranged reads on TOS, never the
  messages). v1's topic rules decide what an episode offers (``containers.mcap_facts``):
  the action (by default ``/action``; UMI's ``/robotN/vio/eef_pose`` recognised; the site's
  ``ingest.mcap_mapping`` first), the state, the cameras, a task text (a ``/task`` topic
  with messages, or a metadata record), the robot type (a metadata record). The time axis
  is the action topic's ``log_time``, so there is no fps to configure (``fps`` is null).
  A dataset whose episodes all lack the action or every camera cannot be read by v1:
  ``metadata_invalid``. Files without a summary section are read in full by the checks;
  preflight says so and counts them as unlabelled (autolabel then decides per episode).
* **lance** - lerobot-lance-convert (>= 0.3.0) writes LeRobot v3.0 metadata into ``meta/``
  (mirrored in ``meta.lance``) with ``storage_format: "lance"``; preflight reads it like a
  LeRobot v3 dataset's, v1's ``validate_info`` included. Videos live in ``videos.lance``,
  not as mp4 objects, so no per-episode file check applies. A root with the tables but
  without the stamp is an old single-table-era conversion: invalid, as in v1.

Module availability then follows the LeRobot rules (``preflight._fill_supported``): the
robot type read from the data or given as ``--embodiment-id``, the VLM backend, the
declared features. EEF-video consistency reads LeRobot videos: unsupported here.
Either format switched off by the site (``ingest.mcap_enabled`` / ``ingest.lance_enabled``)
greys out every module with v1's message (``format_disabled``).
"""
from __future__ import annotations

from . import containers, lerobot_meta
from .lerobot_meta import DatasetMeta, Episode, Format


def verify_manifest(manifest, listing, kind: str) -> None:
    """``--source-manifest``: the objects standing for the metadata must be unchanged
    (none added, none gone, none rewritten)."""
    now = set(lerobot_meta.fingerprint_keys(listing, kind))
    then = set(lerobot_meta.fingerprint_keys(manifest.objects, kind))
    for key in sorted(now - then):
        raise manifest._changed(key, "added", None, listing[key])
    manifest.verify(listing, keys=sorted(then))


def fill(ctx, args, storage, listing, fmt: Format, specs, doc: dict) -> None:
    from .preflight import _unsupported

    on, why = containers.enabled(fmt.kind, ctx.config())
    if not on:
        doc["format"] = {"kind": fmt.kind, "version": None, "supported": False, "detail": why}
        doc["modules"] = _unsupported(specs, why, "format_disabled", {"format": fmt.kind})
        return
    if fmt.kind == "mcap":
        _mcap(ctx, args, storage, listing, fmt, specs, doc)
    else:
        _lance(ctx, args, storage, listing, fmt, specs, doc)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _preview(eps) -> str:
    from .episodes import preview

    return preview(sorted(eps))


# ---------------------------------------------------------------- mcap


def _mcap(ctx, args, storage, listing, fmt: Format, specs, doc: dict) -> None:
    from .preflight import STAGE, _fill_supported, mark_invalid

    num = containers.mcap_episodes(listing)
    mapping = containers.mapping_of(ctx.config())
    summaries = containers.mcap_summaries(storage, listing, num.values())
    ctx.progress(STAGE, 2, 3)
    ctx.check_stop("after reading the mcap summaries")
    if num and not any(s.indexed for s in summaries.values()):
        # no file has a summary section: read the first one through to see its topics
        key = num[min(num)]
        summaries[key] = containers.mcap_summary(storage, key, int(listing[key].size), scan=True)
    facts, unindexed = {}, []
    for ep, key in sorted(num.items()):
        s = summaries[key]
        if s.indexed:
            facts[ep] = containers.mcap_facts(s, mapping)
        else:
            unindexed.append(ep)
    warnings: list[str] = []
    strays = [k for k in containers.mcap_keys(listing) if k not in set(num.values())]
    if strays:
        warnings.append(f"{_plural(len(strays), 'file')} do not follow episode_<N>.mcap and "
                        f"are not read, as in v1 ({', '.join(strays[:5])})")
    if unindexed:
        warnings.append(f"{_plural(len(unindexed), 'episode')} ({_preview(unindexed)}) have "
                        f"no mcap summary section: preflight cannot see their topics; the "
                        f"checks read those files in full")
    if not facts:
        base = None
    else:
        base = facts.get(min(num)) or facts[min(facts)]
    no_action = sorted(ep for ep, f in facts.items() if f.missing_action)
    no_video = sorted(ep for ep, f in facts.items() if not f.cameras)
    problems = []
    if facts and len(no_action) == len(facts):
        seen = sorted(summaries[num[min(facts)]].topics)
        problems.append(f"mcap 里找不到必需的动作 topic（{'、'.join(base.missing_action)}）。"
                        f"实际见到的 topic：{', '.join(seen[:20])}；topic 命名不同时，"
                        f"在站点配置的 ingest.mcap_mapping 里指认")
    if facts and len(no_video) == len(facts):
        problems.append(f"mcap 里找不到视频 topic（期望 {base.mapping['video_prefix']}<相机名>，"
                        f"或 UMI 的 /robotN/sensor/cameraN/compressed）")
    if problems:
        mark_invalid(doc, specs, fmt, problems)
        return
    if no_action:
        warnings.append(f"{_plural(len(no_action), 'episode')} ({_preview(no_action)}) lack the "
                        f"action topic: their checks will fail to read them")
    if no_video:
        warnings.append(f"{_plural(len(no_video), 'episode')} ({_preview(no_video)}) have no "
                        f"camera topic: their checks will fail to read them")
    if base is not None:
        odd = sorted(ep for ep, f in facts.items()
                     if (f.action_topics, f.cameras) != (base.action_topics, base.cameras))
        if odd:
            warnings.append(f"{_plural(len(odd), 'episode')} ({_preview(odd)}) have other topics "
                            f"than the first one (a missing source or camera); v1 expects one "
                            f"layout per dataset")
    cams = list(base.cameras) if base else []
    feats: dict = {"action": {"dtype": "float32",
                              "names": list(base.mapping.get("action_names") or [])
                              if base else []},
                   "timestamp": {"dtype": "float64"}}                # log_time, always there
    if base is not None and base.state_present:
        feats["observation.state"] = {"dtype": "float32", "names": []}
    for cam in cams:
        feats[cam] = {"dtype": "video"}
    robot = (base.robot_type if base else "") or None
    info = {"codebase_version": "mcap", "fps": None, "robot_type": robot, "features": feats}
    episodes = [Episode(index=ep, length=int((facts[ep].frames or 0) if ep in facts else 0),
                        task=(facts[ep].task or "(task topic)") if ep in facts
                        and facts[ep].has_task else "")
                for ep in sorted(num)]
    known = [facts[ep].frames for ep in sorted(num) if ep in facts]
    total = sum(known) if not unindexed and all(f is not None for f in known) else None
    how = []
    if base is not None and base.profile == "umi_das":
        how.append("UMI (das_gripper) topics recognised")
    if mapping:
        how.append("topics mapped by ingest.mcap_mapping")
    detail = (f"mcap, {_plural(len(num), 'episode')}, {_plural(len(cams), 'camera')}; time axis "
              f"from the action topic's log_time" + (f"; {', '.join(how)}" if how else ""))
    meta = DatasetMeta(info=info, fmt=Format("mcap"), cameras=cams, episodes=episodes,
                       warnings=warnings)
    _fill_supported(doc, specs, meta, listing, args, storage.uri,
                    container={"kind": "mcap", "detail": detail, "cameras_present": cams,
                               "profile_name": "", "total_frames": total,
                               "robot_where": "the mcap metadata records"})


# ---------------------------------------------------------------- lance


def _lance(ctx, args, storage, listing, fmt: Format, specs, doc: dict) -> None:
    from ..ingest.validate import IngestValidationError, validate_info
    from .preflight import STAGE, _fill_supported, mark_invalid

    try:
        meta = containers.lance_meta(storage, listing)
    except Exception as e:  # noqa: BLE001 - a broken meta/ or meta.lance
        mark_invalid(doc, specs, fmt, [f"the lance dataset's meta/ cannot be read: "
                                       f"{type(e).__name__}: {e}"[:600]])
        return
    ctx.progress(STAGE, 2, 3)
    info = meta.info if isinstance(meta.info, dict) else {}
    fmt.codebase_version = str(info.get("codebase_version") or "") or None
    fmt.version = lerobot_meta.version_of(fmt.codebase_version or "")
    problems: list[str] = []
    if str(info.get("storage_format") or "") != "lance":
        # v1's lance_reader._build_rows, verbatim
        problems.append(
            f"'{storage.uri}': 三表齐但 info.json 没有 storage_format=\"lance\" 标记 "
            f"(实际: {info.get('storage_format')!r})—— 不是 lerobot-lance-convert "
            "≥0.3.0 的产出,疑似旧插件布局(官方已废弃,请用 lerobot-lance-convert 重转)")
    try:
        validate_info(info, storage.uri)
    except IngestValidationError as e:
        problems.append(str(e))
    except Exception as e:  # noqa: BLE001 - malformed shapes
        problems.append(f"meta/info.json is malformed: {e!r}")
    if not problems and fmt.version != "v3":
        problems.append(f"lerobot-lance-convert writes LeRobot v3.0 metadata; info.json says "
                        f"codebase_version {fmt.codebase_version!r}")
    cameras = lerobot_meta.cameras_of(info)
    episodes: list[Episode] = []
    warnings: list[str] = []
    if not problems:
        from ..ingest.lerobot_reader import _v3_instruction

        needed = ["episode_index", "length", "dataset_from_index", "dataset_to_index"]
        for vk in cameras:
            needed += [f"videos/{vk}/chunk_index", f"videos/{vk}/file_index",
                       f"videos/{vk}/from_timestamp", f"videos/{vk}/to_timestamp"]
        cols = set(meta.episodes[0].index) if meta.episodes else set()
        lacking = [c for c in needed if c not in cols]
        if not meta.episodes:
            problems.append("the episode table (meta/episodes/*.parquet) lists no episode")
        elif lacking:
            problems.append(f"meta/episodes parquet lacks columns {lacking}")
        else:
            if "tasks" not in cols:
                warnings.append("meta/episodes has no tasks column; task texts are read from "
                                "the frames table at run time, so the labelled count here is a "
                                "lower bound")
            for row in meta.episodes:
                episodes.append(Episode(index=int(row["episode_index"]),
                                        length=int(row["length"]),
                                        task=_v3_instruction(row)))
            episodes.sort(key=lambda e: e.index)
            dup = sorted({a.index for a, b in zip(episodes, episodes[1:]) if a.index == b.index})
            if dup:
                problems.append(f"episode_index repeats in the episode table: {dup[:8]}")
    if problems:
        mark_invalid(doc, specs, fmt, problems)
        return
    if meta.source == containers.LANCE_META_TABLE:
        warnings.append("meta/ is missing; the metadata was read from the meta.lance mirror")
    detail = (f"LeRobot {fmt.codebase_version} in lance tables (lerobot-lance-convert), "
              f"{_plural(len(episodes), 'episode')}, {_plural(len(cameras), 'camera')}")
    name = storage.uri.rstrip("/").rsplit("/", 1)[-1]
    dm = DatasetMeta(info=info, fmt=fmt, cameras=cameras, episodes=episodes, warnings=warnings)
    _fill_supported(doc, specs, dm, listing, args, storage.uri,
                    container={"kind": "lance", "detail": detail, "cameras_present": cameras,
                               "profile_name": name, "robot_where": "meta/info.json"})
