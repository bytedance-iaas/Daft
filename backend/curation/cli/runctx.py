"""What the pipeline commands share: the run directory, the source, the VLM, the gates.

``autolabel``, ``check``, ``aggregate``, ``export``, ``report`` and
``adjudicate-apply`` all work on a local run directory (design doc 00 §4.2);
the ones that read source data take ``--input`` (read with the input key set)
and ``--source-manifest`` (every object they read is checked against it,
exit 6 on a difference, D27); the ones that call a model take the VLM options
and the three behaviour switches, all off by default (doc 02 §1):
``--concurrency N``, ``--retry N``, ``--hedge``.
"""
from __future__ import annotations

import argparse
import copy
import json
import os

from .errors import CliError, InputUnreachable, ModuleFailed, UsageError
from .framework import Context

GATE_CONFIG_KEYS = {
    "episode": "pipeline.vlm_episode_concurrency",
    "probe": "checks.task_success.vlm.max_concurrency",
    "caption": "skill_profile.caption_concurrency",
    "llm": "skill_profile.llm_concurrency",
    "audit": "skill_profile.audit_concurrency",
}


# ---------------------------------------------------------------- arguments

def add_run_dir(p: argparse.ArgumentParser) -> None:
    p.add_argument("--run-dir", required=True, metavar="DIR",
                   help="the task's local work directory")


def add_source(p: argparse.ArgumentParser, *, required: bool = True) -> None:
    p.add_argument("--input", required=required, metavar="URI",
                   help="dataset: tos://bucket/prefix, a local directory, or a public name "
                        "with --source public")
    p.add_argument("--source", choices=("tos", "public", "local"), default=None,
                   help="where the input lives (default: tos for tos:// URIs, else local)")
    p.add_argument("--source-manifest", metavar="FILE",
                   help="source_manifest.json from snapshot; a changed object exits 6")
    p.add_argument("--embodiment-id", metavar="ID",
                   help="robot model, overrides robot_type of info.json")
    p.add_argument("--max-episodes", type=int, metavar="N",
                   help="v1's head-N selection: the first N episodes (also the sample the "
                        "dataset semantics are resolved on)")
    p.add_argument("--selection", metavar="EXPR",
                   help="mcap / lance: the task's whole episode selection when --episodes is "
                        "one stage's part of it; v1 resolves these formats' semantics on the "
                        "first 100 episodes of the selection (default: --episodes)")


def add_episodes(p: argparse.ArgumentParser, *, required: bool = True) -> None:
    p.add_argument("--episodes", required=required, metavar="EXPR",
                   help="episode indices: 34, 10-20, 3,10-12 or @file")


def add_vlm(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("VLM")
    g.add_argument("--vlm-backend", metavar="NAME",
                   help="a VLM backend preset of the site configuration (vlm_backends)")
    g.add_argument("--vlm-endpoint", metavar="URL",
                   default=os.environ.get("CURATION_VLM_ENDPOINT") or None,
                   help="OpenAI-compatible endpoint (default: $CURATION_VLM_ENDPOINT)")
    g.add_argument("--vlm-model", metavar="NAME",
                   default=os.environ.get("CURATION_VLM_MODEL") or None,
                   help="model name (default: $CURATION_VLM_MODEL)")
    g.add_argument("--vlm-api-key-env", metavar="VAR",
                   default=os.environ.get("CURATION_VLM_API_KEY_ENV") or None,
                   help="the environment variable holding the API key (default ARK_API_KEY)")
    g.add_argument("--vlm-reasoning-effort", metavar="LEVEL",
                   help="send reasoning_effort=LEVEL with every model request (default: "
                        "none sent, as v1); the level is not checked here")


def add_behaviour(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("behaviour (all off by default)")
    g.add_argument("--concurrency", type=int, metavar="N",
                   help="CPU modules: episodes at a time; VLM modules: parallelism N the "
                        "gates are derived from (default 1: nothing concurrent)")
    g.add_argument("--retry", type=int, default=0, metavar="N",
                   help="outer retry of a failed model call: 1s/2s/4s, at most N times "
                        "(default 0)")
    g.add_argument("--hedge", action="store_true",
                   help="v1's timeout hedging (a second shot at the timeout line)")


# ---------------------------------------------------------------- run directory

def run_dir_of(args, *, create: bool = False) -> str:
    path = os.path.abspath(os.path.expanduser(args.run_dir))
    if create:
        os.makedirs(path, exist_ok=True)
    elif not os.path.isdir(path):
        raise UsageError(f"--run-dir {path} is not a directory")
    return path


def read_json(path: str, what: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as e:
        raise UsageError(f"{what} {path}: cannot read it: {e}") from None
    except ValueError as e:
        raise UsageError(f"{what} {path}: not valid JSON: {e}") from None


def selected_modules(args, run_dir: str) -> list[str]:
    """``--modules``, else the modules of ``<run-dir>/plan.json``, else what ran."""
    from ..contracts import modules as registry
    from ..pipeline.records import CHECKS_DIR, PLAN_NAME

    raw = getattr(args, "modules", None)
    if raw:
        mods = [m.strip() for m in str(raw).split(",") if m.strip()]
    else:
        plan = os.path.join(run_dir, PLAN_NAME)
        mods = []
        if os.path.isfile(plan):
            for st in read_json(plan, "plan").get("stages") or []:
                mods += [m for m in st.get("modules") or [] if m not in mods]
        if not mods:
            base = os.path.join(run_dir, CHECKS_DIR)
            mods = sorted(os.listdir(base)) if os.path.isdir(base) else []
    unknown = [m for m in mods if m not in registry.ids()]
    if unknown:
        raise UsageError(f"unknown module(s) {unknown}; known: {', '.join(registry.ids())}")
    if not mods:
        raise UsageError("no modules: pass --modules or put plan.json into the run directory")
    return [m for m in registry.ids() if m in mods]


# ---------------------------------------------------------------- configuration

def stage_config(ctx: Context, modules, *, gates: dict | None = None,
                 args: argparse.Namespace | None = None) -> dict:
    """The pipeline config for this call: exactly ``modules`` enabled (v1's ``--only``),
    the VLM settings from the arguments and the gate sizes as v1 config keys.

    Advisory modules (registry 1.4) are not v1 checks and never enter v1's config: they are
    left out here, and a call that selects nothing else runs with every v1 check off."""
    from ..contracts import modules as registry
    from ..pipeline.config import apply_check_selection, apply_overrides, validate_config

    cfg = copy.deepcopy(ctx.config())
    v1 = [m for m in modules if m not in registry.advisory_ids()]
    try:
        if v1:
            cfg = apply_check_selection(cfg, only=",".join(v1))
        else:
            for entry in cfg["checks"].values():
                entry["enable"] = False
            for extra in ("skill_profile", "dedup"):
                cfg.setdefault(extra, {})["enable"] = False
    except ValueError as e:
        raise UsageError(str(e)) from None
    if args is not None and hasattr(args, "vlm_endpoint"):
        apply_vlm_args(cfg, args)
    if gates:
        sets = [f"{GATE_CONFIG_KEYS[g]}={int(v)}" for g, v in gates.items()
                if g in GATE_CONFIG_KEYS]
        cfg = apply_overrides(cfg, sets)
    validate_config(cfg, "stage config")
    return cfg


def apply_vlm_args(cfg: dict, args) -> None:
    from ..pipeline.config import ConfigError, apply_vlm_backend, apply_vlm_direct

    try:
        if getattr(args, "vlm_backend", None):
            apply_vlm_backend(cfg, args.vlm_backend)
        if args.vlm_endpoint or args.vlm_model or args.vlm_api_key_env:
            apply_vlm_direct(cfg, endpoint=args.vlm_endpoint, model=args.vlm_model,
                             api_key_env=args.vlm_api_key_env)
    except ConfigError as e:
        raise UsageError(str(e)) from None


def vlm_gates(args, plan_stage: dict | None) -> dict:
    """The VLM gates of this call: the plan stage's, else derived from ``--concurrency``.

    With neither, every gate is 1: one model request in flight at a time (doc 02
    §1: no concurrency unless asked for). ``derive_gates(1)`` would still allow two
    review votes at once (the endstate gate never drops below 2 there).
    """
    from ..planner.gates import derive_gates

    if args.concurrency is not None and int(args.concurrency) < 1:
        raise UsageError("--concurrency must be at least 1")
    if plan_stage and plan_stage.get("gates"):
        n = int(args.concurrency) if args.concurrency else None
        gates = derive_gates(n) if n else {k: 1 for k in derive_gates(1)}
        gates.update({k: int(v) for k, v in plan_stage["gates"].items()})
        return gates
    if args.concurrency is None:
        return {k: 1 for k in derive_gates(1)}
    return derive_gates(int(args.concurrency))


def cpu_workers(args, plan_stage: dict | None) -> int:
    if args.concurrency is not None:
        if args.concurrency < 1:
            raise UsageError("--concurrency must be at least 1")
        return int(args.concurrency)
    if plan_stage and plan_stage.get("concurrency"):
        return int(plan_stage["concurrency"])
    return 1


def load_plan_stage(path: str | None, modules, *, stage_id: str | None = None) -> dict | None:
    """``--plan-stage``: one stage of ``plan.json`` (or a whole plan: the stage with these
    modules, or with ``stage_id``, is taken). It carries the gates, the concurrency and
    the merge proposal."""
    if not path:
        return None
    doc = read_json(path, "--plan-stage")
    if isinstance(doc, dict) and "stages" in doc:
        if stage_id is not None:
            stages = [s for s in doc["stages"] if s.get("id") == stage_id]
        else:
            stages = [s for s in doc["stages"]
                      if s.get("modules") and set(s["modules"]) == set(modules)]
        if not stages:
            raise UsageError(f"--plan-stage {path}: no stage "
                             f"{stage_id or 'runs ' + str(sorted(modules))}")
        doc = stages[0]
    if not isinstance(doc, dict) or not doc.get("id") or not doc.get("kind"):
        raise UsageError(f"--plan-stage {path}: not a plan stage (needs id and kind)")
    if doc.get("modules") and set(doc["modules"]) != set(modules):
        raise UsageError(f"--plan-stage {path} is stage {doc['id']} for {doc['modules']}, "
                         f"not for {sorted(modules)}")
    return doc


# ---------------------------------------------------------------- source

def open_input(ctx: Context, args):
    """The input as a Storage, and v1's readers pointed at it with the input key set."""
    from . import inputs
    from .storage import is_remote

    storage = inputs.open_input(ctx, args)
    uri = storage.uri
    if is_remote(uri):
        from ..ingest import dsfs
        from .creds import tos_credentials

        creds = tos_credentials("input")
        # v1's readers (ingest/dsfs, tos_store) take TOS_* from the environment; this
        # process is the command's own, so the input key set is bound for its lifetime
        if creds is not None:
            os.environ["TOS_ACCESS_KEY"] = creds.access_key
            os.environ["TOS_SECRET_KEY"] = creds.secret_key
            if creds.session_token:
                os.environ["TOS_SESSION_TOKEN"] = creds.session_token
            else:
                os.environ.pop("TOS_SESSION_TOKEN", None)
        dsfs.configure(getattr(storage, "region", None) or ctx.input_region)
    if getattr(args, "source", None) == "public" or getattr(storage, "anonymous", False):
        from ..ingest import public_catalog

        public_catalog.apply_config(ctx.config())
    return storage


class Source:
    """A source-reading command's input: the storage, its format and the directory v1's
    readers are given - the dataset itself (a local path), its ``tos://`` URI (LeRobot,
    which v1 reads through dsfs) or, for mcap / lance on TOS (D44), a local copy of what
    the command reads (``containers.SourceCache``: :meth:`fetch` before reading).

    One listing per command: format detection, the episode numbering, the source guard
    and the cache all work on it. Temporary files of the container readers (v1's muxed
    and extracted videos) and a command's own cache go when the command ends.
    """

    def __init__(self, ctx: Context, args, storage, *, listing=None, cache: bool = True):
        """``listing``: one the caller already has; ``cache=False``: a command that reads
        metadata only (snapshot) needs no local copy."""
        from ..pipeline import rows
        from . import containers, lerobot_meta

        self.ctx, self.args, self.storage = ctx, args, storage
        self.uri, self.remote = storage.uri, bool(storage.remote)
        self.listing = listing if listing is not None else storage.list()
        if not self.listing:
            raise InputUnreachable(f"nothing found at {storage.uri}", {"uri": storage.uri})
        self.format = lerobot_meta.detect_format(self.listing)
        self.kind = self.format.kind
        self.cache = None
        self._numbering: dict[int, str] | None = None
        self.input_dir = storage.root if not storage.remote else storage.uri
        if self.kind not in containers.FORMATS:
            return
        cfg = ctx.config()
        rows.configure_ingest(cfg)
        on, why = containers.enabled(self.kind, cfg)
        if not on:
            raise UsageError(why, {"format": self.kind})
        ctx.on_exit(self.close)
        if self.remote and cache:
            self.cache = containers.SourceCache(storage, self.listing, self.kind)
            self.input_dir = self.cache.data

    @property
    def container(self) -> bool:
        return self.kind in ("mcap", "lance")

    def numbering(self) -> dict[int, str]:
        """mcap: ``{episode: key}`` by v1's rule."""
        from . import containers

        if self._numbering is None:
            self._numbering = containers.mcap_episodes(self.listing)
        return self._numbering

    def episode_keys(self, episodes) -> list[str]:
        """The objects reading ``episodes`` touches: one file per mcap episode, the whole
        layout for lance, nothing to add for LeRobot (its guard knows its own keys)."""
        from . import containers

        if self.kind == "mcap":
            num = self.numbering()
            return sorted({num[int(e)] for e in episodes if int(e) in num})
        if self.kind == "lance":
            return containers.lance_keys(self.listing)
        return []

    def fetch(self, episodes) -> None:
        """Make the source objects of ``episodes`` local (a remote mcap / lance dataset)."""
        if self.cache is None:
            return
        n = self.cache.fetch(self.episode_keys(episodes))
        if n:
            self.ctx.log("info", f"source cache: {n} more object(s); {self.cache.describe()}")

    def close(self) -> None:
        from ..pipeline import rows

        rows.cleanup(self.input_dir)
        if self.cache is not None:
            self.cache.close()


def open_source(ctx: Context, args) -> Source:
    """:func:`open_input` plus what the readers need: format, listing, local directory."""
    return Source(ctx, args, open_input(ctx, args))


def selection_of(args) -> list[int] | None:
    """``--selection`` (the task's whole selection, for mcap / lance semantics)."""
    return read_episode_file(getattr(args, "selection", None))


def source_guard(ctx: Context, args, storage):
    """A callable(episodes) that checks the source objects of those episodes against
    ``--source-manifest`` (meta files included); a no-op without one.

    v1's readers (``read_lerobot_meta``, ``LeRobotDataSource``) resolve the dataset's
    semantics from the data files of its first ``min(100, --max-episodes)`` episodes
    whatever the selection; those files are checked too, since they shape every
    selected episode's judgement. ``storage`` may be a :class:`Source`; an mcap / lance
    source is checked by :func:`_container_guard`.
    """
    from . import lerobot_meta, source_manifest

    src = storage if isinstance(storage, Source) else None
    storage = src.storage if src is not None else storage
    manifest = source_manifest.guard(getattr(args, "source_manifest", None), storage.uri)
    if manifest is None:
        return None
    if src is not None and src.container:
        return _container_guard(ctx, args, src, manifest)
    state: dict = {}

    def check(episodes) -> None:
        if not state:                    # one listing per command: it is what the call reads
            listing = storage.list()
            info = lerobot_meta.load_info(storage)
            fmt = lerobot_meta.detect_format(listing)
            fmt.codebase_version = str(info.get("codebase_version") or "")
            fmt.version = lerobot_meta.version_of(fmt.codebase_version)
            keys = set(lerobot_meta.meta_keys(listing)) & set(manifest.objects)
            try:
                meta = lerobot_meta.read_dataset(storage, listing, info, fmt)
            except lerobot_meta.MetaError as e:
                raise InputUnreachable(f"{storage.uri}: {e}") from None
            for ep in lerobot_meta.semantics_sample(meta, getattr(args, "max_episodes", None)):
                if ep.index in manifest.skipped:
                    continue
                # v1's LeRobot v2 sample drops an incomplete episode: its parquet counts
                # only if the snapshot recorded it or the episode is complete now
                if fmt.version == "v2" and not lerobot_meta.complete(ep, listing) \
                        and not any(k in manifest.objects for k in ep.data_keys):
                    continue
                keys.update(ep.data_keys)
            state.update(listing=listing, verified=set(),
                         episodes={ep.index: ep for ep in meta.episodes}, first=keys)
        keys = set(state.pop("first", ()))
        for e in episodes:
            ep = state["episodes"].get(int(e))
            if ep is not None:
                keys.update(ep.data_keys)
                keys.update(ep.video_keys.values())
        # a file missing at snapshot time and still missing is unchanged (that episode is
        # an error at run time); one that appeared since is a change
        keys = {k for k in keys - state["verified"]
                if k in manifest.objects or k in state["listing"]}
        if keys:
            manifest.verify(state["listing"], keys=sorted(keys))
            state["verified"] |= keys
        ctx.log("info", f"source manifest: {len(state['verified'])} objects unchanged")

    return check


def _container_guard(ctx: Context, args, src: Source, manifest):
    """The guard of an mcap / lance source (D44), on the command's one listing.

    What stands for a LeRobot dataset's ``meta/`` must be unchanged: an mcap dataset's
    set of episode files (v1 numbers them by what is there), a lance dataset's whole
    layout (``meta/`` and the three tables are read as a whole). Then, per call, the
    files of the episodes about to be read - the semantics sample (the first episodes of
    ``--selection``) the first time. The cache then checks each copy against this listing.
    """
    from . import containers

    listing = src.listing
    state: dict = {"verified": set()}

    def first() -> set[str]:
        if src.kind == "lance":
            then = {k for k in manifest.objects if k.startswith(containers.LANCE_PREFIXES)}
            now = set(containers.lance_keys(listing))
        else:
            then = {k for k in manifest.objects if "/" not in k and k.endswith(".mcap")}
            now = set(containers.mcap_keys(listing))
        for key in sorted(now - then):
            raise manifest._changed(key, "added", None, listing[key])
        for key in sorted(then - now):
            raise manifest._changed(key, "missing", manifest.objects[key], None)
        if src.kind == "lance":
            return then
        chosen = selection_of(args) or read_episode_file(getattr(args, "episodes", None)) or []
        n = getattr(args, "max_episodes", None) or 100
        return set(src.episode_keys(sorted(chosen)[:min(100, int(n))]))

    def check(episodes) -> None:
        keys = set(src.episode_keys(episodes))
        if "first" not in state:
            state["first"] = True
            keys |= first()
        keys -= state["verified"]
        if keys:
            manifest.verify(listing, keys=sorted(keys))
            state["verified"] |= keys
        ctx.log("info", f"source manifest: {len(state['verified'])} objects unchanged")

    return check


def leave_out_skipped(ctx: Context, args, episodes: list[int]) -> list[int]:
    """``episodes`` without the ones ``--source-manifest`` left out for missing source
    files (D40): no command given the manifest reads them."""
    from . import source_manifest

    path = getattr(args, "source_manifest", None)
    if not path:
        return list(episodes)
    skipped = source_manifest.SourceManifest.load(path).skipped
    kept = [e for e in episodes if e not in skipped]
    if len(kept) < len(episodes):
        ctx.log("info", f"{len(episodes) - len(kept)} episode(s) the source manifest left out "
                        f"for missing source files are not read")
    return kept


def meta_rows(source, episodes, args, *, what: str) -> list[dict]:
    """v1's metadata rows of ``episodes`` in index order (``pipeline.rows.meta_rows``).

    v1 reads the semantics sample here as well; when that fails nothing can be
    judged: exit 4 with the reader's reason instead of an internal error. ``source`` is
    a :class:`Source` (a remote mcap / lance dataset's episodes are made local first) or
    the readers' input directory.
    """
    from ..pipeline.rows import index_of
    from ..pipeline.rows import meta_rows as read

    input_dir = source
    if isinstance(source, Source):
        source.fetch(sorted(episodes))
        input_dir = source.input_dir
    try:
        rows = read(input_dir, episodes, embodiment_id=getattr(args, "embodiment_id", None),
                    max_episodes=getattr(args, "max_episodes", None))
    except CliError:
        raise
    except Exception as e:  # noqa: BLE001 - reader errors are many
        raise ModuleFailed(f"{what}: the dataset cannot be read: {type(e).__name__}: {e}"[:600],
                           {"exception": type(e).__name__}) from None
    return sorted(rows, key=lambda r: index_of(r["episode_id"]))


def resolve_episodes(args, available, *, what: str = "the dataset") -> tuple[list[int], str]:
    """(``--episodes`` within ``available``, a warning when some did not exist)."""
    from . import episodes as episode_sel

    requested = episode_sel.parse(getattr(args, "episodes", None))
    selected, warning = episode_sel.reconcile(requested, available, what)
    return sorted(available if selected is None else selected), warning


def dataset_episodes(ctx: Context, storage) -> tuple[list[int], dict]:
    """The episode indices of the input dataset (metadata only) and its info.json.

    ``storage`` may be a :class:`Source`: its listing is used, and an mcap / lance
    dataset answers too (:func:`container_episodes`)."""
    from . import lerobot_meta

    if isinstance(storage, Source):
        src, storage = storage, storage.storage
        if src.container:
            return container_episodes(src)
        listing = src.listing
    else:
        listing = storage.list()
    if not listing:
        raise InputUnreachable(f"nothing found at {storage.uri}", {"uri": storage.uri})
    fmt = lerobot_meta.detect_format(listing)
    if fmt.kind != "lerobot":
        raise UsageError(f"{storage.uri} is not a LeRobot dataset ({fmt.kind}: {fmt.note}); "
                         f"run preflight first")
    try:
        info = lerobot_meta.load_info(storage)
        fmt.codebase_version = str(info.get("codebase_version") or "") or None
        fmt.version = lerobot_meta.version_of(fmt.codebase_version or "")
        if fmt.version is None:
            raise lerobot_meta.MetaError(
                f"codebase_version {fmt.codebase_version!r} is not LeRobot v2/v3")
        meta = lerobot_meta.read_dataset(storage, listing, info, fmt)
    except lerobot_meta.MetaError as e:
        raise UsageError(f"{storage.uri}: {e}; run preflight for the full list") from None
    return [ep.index for ep in meta.episodes], info


def container_episodes(src: Source) -> tuple[list[int], dict]:
    """(episode indices, dataset info) of an mcap / lance dataset without reading samples.

    mcap: the files numbered by v1's rule; the info carries the robot type v1 reads
    from the first episode's metadata records (``mcap_dataset_info``). lance: ``meta/``
    (LeRobot v3.0: ``info.json`` and the episode table)."""
    from . import containers

    if src.kind == "mcap":
        num = src.numbering()
        if not num:
            raise UsageError(f"{src.uri}: no episode_<N>.mcap files")
        first = num[min(num)]
        summary = containers.mcap_summary(src.storage, first, int(src.listing[first].size),
                                          scan=True)
        facts = containers.mcap_facts(summary, containers.mapping_of(src.ctx.config()))
        return sorted(num), {"robot_type": facts.robot_type or "unknown", "fps": None,
                             "codebase_version": "mcap"}
    try:
        meta = containers.lance_meta(src.storage, src.listing)
    except CliError:
        raise
    except Exception as e:  # noqa: BLE001 - a broken meta/ or meta.lance
        raise UsageError(f"{src.uri}: the lance dataset's meta/ cannot be read: "
                         f"{type(e).__name__}: {e}; run preflight for the full list") from None
    return sorted(int(r["episode_index"]) for r in meta.episodes), meta.info


def read_episode_file(expr: str | None) -> list[int] | None:
    from . import episodes as episode_sel

    got = episode_sel.parse(expr)
    return None if got is None else sorted(got)


# ---------------------------------------------------------------- VLM session

class VlmSession:
    """``with VlmSession(ctx, args, cfg, module): ...`` - probe the endpoint, install the
    transport policy and the usage booker for the command's lifetime."""

    def __init__(self, ctx: Context, args, cfg: dict, module: str, run_dir: str):
        self.ctx, self.args, self.cfg, self.module, self.run_dir = ctx, args, cfg, module, run_dir
        self._installed = None
        self._usage_log = None
        self.booker = None

    def _vlm(self) -> dict:
        return self.cfg["checks"]["task_success"]["vlm"]

    def probe(self) -> None:
        from ..adapters.vlm_client import probe_endpoint, resolve_single_model

        v = self._vlm()
        if not v.get("endpoint"):
            raise UsageError("no VLM endpoint: pass --vlm-endpoint / --vlm-backend or set "
                             "checks.task_success.vlm.endpoint")
        if not v.get("model"):
            try:
                v["model"] = resolve_single_model(v["endpoint"], v.get("api_key_env"))
            except ValueError as e:
                raise ModuleFailed(str(e), {"endpoint": v["endpoint"]}) from None
        ok, why = probe_endpoint(v["endpoint"], v["model"], api_key_env=v.get("api_key_env"))
        if not ok:
            raise ModuleFailed(f"the VLM endpoint cannot be used: {why}",
                               {"endpoint": v["endpoint"], "model": v["model"]})
        self.ctx.log("info", f"VLM {v['model']} @ {v['endpoint']}")

    def __enter__(self):
        from ..pipeline import vlm_policy
        from ..pipeline.records import USAGE_FILE, AppendLog
        from ..planner.retry import RetryPolicy

        if self.args.retry is not None and self.args.retry < 0:
            raise UsageError("--retry must not be negative")
        self.probe()
        self._usage_log = AppendLog(os.path.join(self.run_dir, USAGE_FILE))
        self.booker = vlm_policy.UsageBooker(self.module, str(self._vlm()["model"]),
                                             emit=self.ctx.emitter.emit,
                                             persist=self._usage_log.write)
        policy = vlm_policy.TransportPolicy(
            hedge=bool(self.args.hedge), retry=RetryPolicy(max_retries=int(self.args.retry or 0)),
            reasoning_effort=getattr(self.args, "vlm_reasoning_effort", None) or None)
        self._installed = vlm_policy.installed(policy, usage=self.booker)
        self._installed.__enter__()
        self._mark = latency_mark()
        return self

    def __exit__(self, *exc):
        try:
            append_latency(self.run_dir, self._mark)
        finally:
            if self._installed is not None:
                self._installed.__exit__(*exc)
            if self._usage_log is not None:
                self._usage_log.close()
        return False


def latency_mark() -> int:
    from ..adapters.vlm_client import latency_rows

    return len(latency_rows())


def append_latency(run_dir: str, mark: int) -> None:
    """Add this command's latency rows to ``details/vlm_latency.csv`` (whole-file write)."""
    import csv
    import io

    from ..adapters.vlm_client import LATENCY_CSV_HEADER, latency_rows, read_latency_csv
    from ..pipeline.records import LATENCY_FILE, write_text_atomic

    delta = latency_rows()[mark:]
    if not delta:
        return
    path = os.path.join(run_dir, LATENCY_FILE)
    rows = read_latency_csv(path) if os.path.isfile(path) else []
    rows.extend(delta)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(LATENCY_CSV_HEADER)
    for t, s, ok, st, cid, att, fk in rows:
        w.writerow([t, round(float(s), 3), int(ok), "" if st is None else round(float(st), 3),
                    cid or "", att, fk or ""])
    write_text_atomic(path, buf.getvalue())
