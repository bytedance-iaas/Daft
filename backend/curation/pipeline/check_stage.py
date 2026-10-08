"""One funnel stage over a set of episodes: the engine of ``curation check`` (doc 02 §3.5).

The stages and their order are a block's segments (design doc 17 §3): ``numeric``
(timestamp_check, kinematic_limits, motion_quality), ``frame`` (visual_quality and
video_action_sync on one shared decode per camera), ``vlm`` (task_success), ``dedup``
(exact duplicates, streaming since D70 - see :meth:`StageRun._dedup`). Each
episode goes through the module bodies of ``pipeline.funnel`` - the very
functions v1's DataFrame chain wraps in UDFs - with the row v1's lazy scan would
build (``pipeline.rows``). What the shell adds, per episode:

* the record lines, appended to this call's part and fsync'ed one by one;
* ``inflight.json`` (which episodes are in the process's hands right now);
* execution incidents (D33): decode failures and model calls that failed for
  good, recorded by wrappers around what the algorithm is given, never by
  changing it; an episode with incidents is ``verdict = error``;
* ``--resume``: episodes whose current record is not an error are skipped; an
  episode found twice in a crashed process's ``inflight.json`` is recorded as an
  error and skipped (P14);
* SIGTERM: nothing new starts, the episodes in flight finish and are written;
* the module circuit breaker (P15): when the first 20 episodes of a VLM stage
  all fail on the same infrastructure cause, the module fails as a whole.

Concurrency is a self-built pool of ``concurrency`` workers (default 1); the
VLM gates live in the clients, sized from the plan (doc 04 §2.2).
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ..contracts import modules as registry_mod
from . import funnel
from .incidents import (IncidentLog, camera_names, wrap_arbitration, wrap_call, wrap_decode,
                        wrap_voter)
from .records import (CRASHES_NAME, Inflight, PartWriter, check_counts, compact, is_error,
                      latest_results, passes_funnel,
                      module_dir, pid_alive, read_inflight, record_from_struct, two_blocks,
                      write_json_atomic)
from .rows import EpisodeMissingSource, EpisodeReadError, RowSource, column, open_row_source

FUNNEL_STAGES = ("integrity", "numeric", "frame", "vlm", "dedup")
NUMERIC_ORDER = ("timestamp_check", "kinematic_limits", "motion_quality")
#: P15: this many consecutive failures on one infrastructure cause at the start
#: of a VLM stage fail the module instead of timing out episode by episode.
BREAKER_LIMIT = 20
INFRA_CAUSES = frozenset({"connect_error", "timeout", "server_error", "rate_limited",
                          "http_error", "tls_error"})
CRASH_LIMIT = 2


def stage_of(module: str) -> str:
    return registry_mod.get(module).stage


def action_semantics(row: dict) -> dict:
    """What the reader settled the action's meaning on (the semantics profile, an inference, the run-time
    preflight - or nothing): v2 stores it nowhere else, and ACT-6's dataset-level finding is drawn from it."""
    try:
        extras = json.loads(column(row, "semantics_extras", "{}") or "{}")
    except (TypeError, ValueError):
        extras = {}
    pre = (extras or {}).get("semantics_preflight") if isinstance(extras, dict) else None
    out = {"source": str(column(row, "semantics_source", "") or ""),
           "action_space": str(column(row, "action_space", "") or ""),
           "control_mode": str(column(row, "control_mode", "") or "")}
    if isinstance(pre, dict) and pre.get("status"):
        out["preflight"] = str(pre["status"])
    out["undetermined"] = out["action_space"] in ("", "unknown") or out["source"] == "preflight_unknown"
    return out


def input_digest(episodes) -> str:
    text = "".join(f"{int(e)}\n" for e in sorted(set(int(x) for x in episodes)))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


@dataclass
class TaskClients:
    """The VLM stage's model clients, built once per process (inside the policy)."""

    vlm_completion: Callable
    cam_voter: Callable | None
    arb_deps: dict | None


@dataclass
class StageOptions:
    run_dir: str
    input_dir: str
    modules: list[str]
    episodes: list[int]
    part: str
    cfg: dict
    resume: bool = False
    concurrency: int = 1
    embodiment_id: str | None = None
    max_episodes: int | None = None
    task_clients: TaskClients | None = None
    task_text: object | None = None                     # tasktext.TaskText (vlm stage)
    evidence_mode: str = "off"
    verify_source: Callable[[list[int]], None] | None = None
    pipeline_state: str | None = None
    pipeline_next: str = "done"
    episode_stream: object | None = None
    #: mcap / lance (D44): the task's selection (the semantics sample) and the hook that
    #: makes an episode's source objects local before it is read (a remote dataset)
    selection: list[int] | None = None
    fetch: Callable[[list[int]], None] | None = None
    row_instructions: bool = False
    fmt: str | None = None                              # mcap | lance; None: v1's sniffing
    #: the EEF module's per-episode judge (D49, ``cli/eef_check.EefJudge``) and, per module, the
    #: episodes whose existing line was made from another input and must be redone on --resume
    eef: object | None = None
    stale: dict[str, set[int]] | None = None
    #: the data integrity module's judge (design doc 14, ``extensions.integrity.IntegrityJudge``)
    integrity: object | None = None
    #: the modules' parameters (``--param``, defaults filled in by the record writer): the judgement
    #: lines a record 2.0's findings are drawn with (design doc 17 §1.3)
    params: dict[str, dict] = field(default_factory=dict)
    stage: str = field(init=False, default="")

    def __post_init__(self) -> None:
        stages = {stage_of(m) for m in self.modules}
        if len(stages) != 1 or not stages <= set(FUNNEL_STAGES):
            raise ValueError(f"modules {self.modules} are not one funnel stage")
        self.stage = stages.pop()


class BreakerTripped(Exception):
    pass


class _Breaker:
    """P15 on the first episodes of a VLM stage (in completion order)."""

    def __init__(self, limit: int = BREAKER_LIMIT, watched: set[str] | None = None):
        self.limit, self.count, self.cause, self.armed = limit, 0, None, True
        self.watched = watched            # None: every module; else only these (the verdict's)

    def observe(self, records: dict[str, dict]) -> None:
        if not self.armed:
            return
        causes = set()
        for module, rec in records.items():
            if self.watched is not None and module not in self.watched:
                continue                  # an advisory rider's record says nothing about the model
            if not is_error(rec):
                self.armed = False
                return
            for inc in rec["error"]["incidents"]:
                causes.add(inc.get("cause") if inc.get("call_kind") else "data")
        if len(causes) != 1 or not causes <= INFRA_CAUSES \
                or (self.cause is not None and causes != {self.cause}):
            self.armed = False
            return
        self.cause = causes.pop()
        self.count += 1
        if self.count >= self.limit:
            raise BreakerTripped(f"the first {self.count} episodes all failed with "
                                 f"{self.cause}; the module cannot run")


class _NoRows:
    """The row source of a vlm stage without task_success: the EEF module reads its own media."""

    def get(self, ep: int) -> dict:
        return {}


class StageRun:
    """Runs one stage call; :meth:`run` returns the ``check --json`` document."""

    def __init__(self, ctx, opts: StageOptions, registry):
        self.ctx, self.o, self.registry = ctx, opts, registry
        self.label = "check:" + "+".join(opts.modules)
        self._lock = threading.Lock()
        self.done = 0
        #: what stops an episode in the funnel (``records.passes_funnel``): a finding that blocks under
        #: the task's policy (``run.json``; the default levels without one) - until the two blocks of F12.4
        from .policy import load as load_policy

        self.funnel_policy = load_policy(opts.run_dir)
        #: plan 2.0: the next stage takes every episode this one judged (design doc 17 §3, D57)
        self.two_blocks = two_blocks(opts.run_dir)
        if opts.pipeline_state:
            from .episode_state import EpisodeState

            self._store = EpisodeState(opts.pipeline_state, self.funnel_policy)
        else:
            self._store = None
        #: episodes found without their source files (D40): no result line
        self.missing: dict[int, list[str]] = {}
        #: dedup's streaming state (D70, design doc 17 §3.2): the action hash of every episode
        #: this call hashed, the first episode of each hash, and - only for hashes that collide -
        #: the content fingerprint of each member and the episode that owns it
        self._dd_hash: dict[int, str] = {}
        self._dd_first: dict[str, int] = {}
        self._dd_groups: dict[str, dict[str, int]] = {}
        self._dd_fp: dict[int, str] = {}
        self._dd_order: list[int] = []
        self._dd_seeded: list[int] = []
        self._dd_dropped: dict[int, int] = {}

    # ------------------------------------------------------------ bookkeeping
    def _stale_inflight(self) -> dict[int, int]:
        """Episodes a crashed call of these modules had in flight, with crash counts."""
        counts: dict[int, int] = {}
        path = os.path.join(module_dir(self.o.run_dir, self.o.modules[0]), CRASHES_NAME)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                counts = {int(k): int(v) for k, v in (json.load(fh) or {}).items()}
        found: set[int] = set()
        for m in self.o.modules:
            doc = read_inflight(self.o.run_dir, m)
            if doc and doc.get("episodes") and not (doc.get("pid") != os.getpid()
                                                   and pid_alive(doc.get("pid", 0))):
                found |= {int(e) for e in doc["episodes"]}
        for e in found:
            counts[e] = counts.get(e, 0) + 1
        if found:
            for m in self.o.modules:
                write_json_atomic(os.path.join(module_dir(self.o.run_dir, m), CRASHES_NAME),
                                  {str(k): v for k, v in sorted(counts.items())})
            self.ctx.log("warn", f"a previous call crashed with episode(s) "
                                 f"{sorted(found)} in flight")
        return counts

    def _todo(self) -> tuple[list[int], int, set[int]]:
        """(episodes to run, skipped because done, episodes to record as crashed)."""
        eps = list(self.o.episodes)
        if not self.o.resume:
            return eps, 0, set()
        current = {m: latest_results(self.o.run_dir, m, eps) for m in self.o.modules}
        stale = {m: set(ids) for m, ids in (self.o.stale or {}).items()}
        clients = self.o.task_clients
        if "task_success" in current and clients is not None and getattr(clients.vlm_completion, "media_input", None) == "video":
            from ..adapters.video_vlm import CAMERA_CHECK_PROTOCOL, PROTOCOL

            stale.setdefault("task_success", set()).update(
                e for e, rec in current["task_success"].items()
                if (rec.get("details") or {}).get("protocol") != PROTOCOL
                and not (rec.get("details") or {}).get("skipped"))     # not judged at all (D72)
            if "camera_defects" in current:
                stale.setdefault("camera_defects", set()).update(
                    e for e, rec in current["camera_defects"].items()
                    if (rec.get("details") or {}).get("protocol") != CAMERA_CHECK_PROTOCOL)
        done = {e for e in eps
                if all(e in current[m] and not is_error(current[m][e]) and e not in stale.get(m, ())
                       for m in self.o.modules)}
        crashes = self._stale_inflight()
        crashed = {e for e in eps if crashes.get(e, 0) >= CRASH_LIMIT and e not in done}
        return [e for e in eps if e not in done and e not in crashed], len(done), crashed

    # ------------------------------------------------------------ per episode
    def _source(self, todo: list[int]) -> RowSource:
        """v1's data source for these episodes. It reads the dataset's first episodes to
        resolve its semantics; when that fails no episode can be judged (exit 4). The EEF
        module alone reads its own media (D49): no v1 rows then."""
        from ..cli.errors import CliError, ModuleFailed

        o = self.o
        if o.stage == "vlm" and "task_success" not in o.modules:
            return _NoRows()
        if o.stage == "integrity":
            # its own rows: v1's readers without the semantics sample (design doc 14 §2.3)
            try:
                return o.integrity.rows(todo)
            except Exception as e:  # noqa: BLE001 - the episode table itself cannot be read
                from ..cli.errors import ModuleFailed

                raise ModuleFailed(f"{self.label}: the dataset cannot be read: "
                                   f"{type(e).__name__}: {e}"[:600],
                                   {"modules": list(o.modules),
                                    "exception": type(e).__name__}) from None
        try:
            return open_row_source(o.input_dir, todo, embodiment_id=o.embodiment_id,
                                   max_episodes=o.max_episodes, selection=o.selection,
                                   fetch=o.fetch, fmt=o.fmt)
        except CliError:
            raise                              # a changed source (exit 6), an unreachable one
        except Exception as e:  # noqa: BLE001 - reader errors are many
            raise ModuleFailed(f"{self.label}: the dataset cannot be read: "
                               f"{type(e).__name__}: {e}"[:600],
                               {"modules": list(o.modules),
                                "exception": type(e).__name__}) from None

    def _records(self, ep: int, structs: dict[str, dict | None], logs: dict[str, IncidentLog],
                 elapsed: float, evidence: dict[str, list] | None = None,
                 contexts: dict[str, dict] | None = None) -> dict[str, dict]:
        return {m: record_from_struct(m, ep, structs.get(m), incidents=logs[m].items(),
                                      evidence=(evidence or {}).get(m), elapsed_s=elapsed,
                                      params=self.o.params.get(m), context=(contexts or {}).get(m))
                for m in self.o.modules}

    def _numeric(self, ep: int, row: dict, logs) -> dict:
        cfg, reg = self.o.cfg, self.registry
        out: dict[str, dict | None] = {}
        for m in self.o.modules:
            try:
                if m == "timestamp_check":
                    out[m] = funnel.make_timestamp_check(cfg)(row["timestamps"], row["fps"])
                elif m == "kinematic_limits":
                    out[m] = funnel.make_kinematic_check(cfg, reg)(
                        row["action"], row["embodiment_id"], row["fps"],
                        column(row, "action_space", "joint"),
                        column(row, "control_mode", "unknown"), row["proprio_state"],
                        column(row, "proprio_space", "joint"))
                elif m == "motion_quality":
                    out[m] = funnel.make_motion_check(cfg, reg)(
                        row["action"], row["proprio_state"], row["fps"],
                        column(row, "action_space", "joint"),
                        column(row, "control_mode", "unknown"),
                        column(row, "proprio_space", "joint"), row["embodiment_id"],
                        column(row, "stuck_strategy", "auto"),
                        column(row, "semantics_extras", "{}"))
            except Exception as e:  # noqa: BLE001 - one episode's failure stays its own
                out[m] = None
                logs[m].add("internal", cause=f"{type(e).__name__}: {e}")
        return out

    def _frame(self, ep: int, row: dict, logs) -> dict:
        shared = IncidentLog()
        decode = wrap_decode(funnel._default_decode, shared, camera_names(row.get("video")))
        body = funnel.make_frame_checks(self.o.cfg, self.registry, decode=decode)
        try:
            out = body(row["video"], row["proprio_state"], row["timestamps"], row["fps"],
                       column(row, "proprio_space", "joint"), row["embodiment_id"])
        except Exception as e:  # noqa: BLE001
            shared.add("internal", cause=f"{type(e).__name__}: {e}")
            out = {}
        for m in self.o.modules:
            logs[m].extend(shared.items())
        if out.get("curves"):
            path = os.path.join(module_dir(self.o.run_dir, "video_action_sync"), "curves",
                                f"ep{ep:06d}.json")
            write_json_atomic(path, json.loads(out["curves"]), indent=None)
        structs = {}
        if "visual_quality" in self.o.modules:
            structs["visual_quality"] = out.get("visual")
        if "video_action_sync" in self.o.modules:
            structs["video_action_sync"] = out.get("sync")
        return structs

    def _vlm(self, ep: int, row: dict, logs) -> tuple[dict, dict]:
        """The vlm tier: task_success (v1) and the EEF module (D49), each when selected."""
        structs: dict[str, dict | None] = {}
        evidence: dict[str, list] = {}
        if "task_success" in self.o.modules:
            s, e = self._task_success(ep, row, logs)
            structs.update(s)
            evidence.update(e)
            if "camera_defects" in self.o.modules:
                # the rider: read back out of the per-camera reviews, no request of its own;
                # built here rather than in _task_success so an episode without task text
                # or with an internal error still gets its (all-unknown) record
                from ..extensions import camera_defects

                structs["camera_defects"] = camera_defects.struct_from_task(s.get("task_success"))
        if self.o.eef is not None:
            s, e = self.o.eef.judge(ep, logs[self.o.eef.module])
            structs[self.o.eef.module] = s
            evidence[self.o.eef.module] = e
        return structs, evidence

    def _task_success(self, ep: int, row: dict, logs) -> tuple[dict, dict]:
        log = logs["task_success"]
        if self.o.row_instructions:
            # mcap / lance: the annotation comes with the row (v1's meta and data reads of
            # these formats build it the same way), no separate metadata pass
            self.o.task_text.instructions[int(ep)] = str(row.get("instruction") or "")
        text, src, problem = self.o.task_text.resolve(ep)
        if problem is not None:
            # D72: no task text, no judgement - the record says so and the episode goes on;
            # the platform does not write a caption for it (the autolabel step is gone)
            from .tasktext import NO_TASK_TEXT, SRC_NONE

            detail = {"rules": [NO_TASK_TEXT], "skipped": NO_TASK_TEXT, "task_desc_source": SRC_NONE,
                      "reason": "没有任务标注，没有做任务成败判定"}
            return {"task_success": {"passed": None, "score": None,
                                     "detail": json.dumps(detail, ensure_ascii=False)}}, {}
        clients = self.o.task_clients
        deps = funnel.TaskDeps(
            vlm_completion=wrap_call(clients.vlm_completion, log, step="probe",
                                     call_kind="probe"),
            cam_voter=wrap_voter(clients.cam_voter, log),
            arb_deps=wrap_arbitration(clients.arb_deps, log),
            decode=wrap_decode(funnel._default_decode, log, camera_names(row.get("video"))))
        # D39 recorded how a human relabel is judged again ("v1": v1's rejudge, scoring plus
        # the per-camera vote; "full": the first run's flow). Under the one protocol (D71) there
        # is nothing left to choose between: both judge the relabel in the single request, with
        # the label guard. The recorded mode is kept on the record for aggregate.
        rerun = self.o.task_text.relabel_rerun(ep) if src == "人工改标" else None
        try:
            protocol_src = "原始标注" if src == "人工改标" else src
            struct = funnel.task_check_episode(
                self.o.cfg, self.registry, deps, row["video"], text, protocol_src,
                row["fps"], row["action"], row["timestamps"], row["embodiment_id"],
                column(row, "semantics_extras", "{}"))
        except Exception as e:  # noqa: BLE001 - v1 turns it into internal_error; so do we
            struct = funnel.internal_error_struct(e)
            log.add("internal", cause=f"{type(e).__name__}: {e}")
        if src == "人工改标":
            detail = json.loads(struct.get("detail") or "{}")
            # what aggregate checks the relabel was judged with, and how
            detail["task_desc"] = str(text)[:80]
            detail["task_desc_source"] = src
            detail["relabel_rerun"] = rerun
            struct = dict(struct, detail=json.dumps(detail, ensure_ascii=False, default=str))
        evidence = self._evidence(ep, row, struct)
        return {"task_success": struct}, {"task_success": evidence}

    # ------------------------------------------------------------ dedup (D70)
    def _eid(self, ep: int) -> str:
        return f"ep{int(ep):06d}"

    def _dedup_seed(self) -> None:
        """Rebuild the streaming state from the records this run directory already has, so that
        a resumed or retried call groups against the episodes it is not judging again."""
        existing = latest_results(self.o.run_dir, "dedup")
        for ep in sorted(existing):
            rec = existing[ep] or {}
            if is_error(rec):
                continue
            d = rec.get("details") or {}
            ah = d.get("action_hash")
            if not ah:
                continue
            ep = int(ep)
            self._dd_seeded.append(ep)
            self._dd_hash[ep] = str(ah)
            owner = d.get("duplicate_of")
            if owner is not None:
                self._dd_dropped[ep] = int(owner)
            self._dd_first.setdefault(str(ah), ep if owner is None else int(owner))
        # the content fingerprints and the traversal order are the segment's own bookkeeping
        doc = None
        path = os.path.join(module_dir(self.o.run_dir, "dedup"), "groups.json")
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):       # the segment's own bookkeeping: rebuild it instead
                doc = None
        if isinstance(doc, dict):
            for key, fp in (doc.get("fingerprints") or {}).items():
                try:
                    e = int(key)
                except (TypeError, ValueError):
                    continue
                self._dd_fp[e] = str(fp)
                ah = self._dd_hash.get(e)
                if ah:
                    self._dd_groups.setdefault(ah, {}).setdefault(str(fp), e)
            seen = set(self._dd_seeded)
            was = [int(e) for e in (doc.get("order") or []) if int(e) in seen]
            self._dd_seeded = was + [e for e in self._dd_seeded if e not in set(was)]

    def _fingerprint(self, source, ep: int, row: dict | None) -> str | None:
        """The content fingerprint of one episode (v1's ``episode_fingerprint``: the action bytes and
        every camera's content identity and time window). ``row`` None: the episode is not this
        call's, so its row is read now - that happens only for a collision."""
        from ..dataset_level.dedup import episode_fingerprint

        if row is not None:
            return str(episode_fingerprint(row))
        got = None
        try:
            got = source.get(int(ep))
        except Exception:  # noqa: BLE001 - not this call's episode: read it on its own
            from .rows import read_rows

            try:
                rows = read_rows(self.o.input_dir, episode_indices={int(ep)},
                                 embodiment_id=self.o.embodiment_id, validate=False,
                                 skip_missing=True)
                return str(episode_fingerprint(rows[0])) if rows else None
            except Exception:  # noqa: BLE001 - the collision cannot be confirmed
                return None
        try:
            return str(episode_fingerprint(got))
        except Exception:  # noqa: BLE001 - the collision cannot be confirmed
            return None
        finally:
            release = getattr(source, "release", None)
            if release is not None:
                release(got)

    def _dedup(self, source, ep: int, row: dict, logs) -> dict:
        """One episode's dedup record (D70, design doc 17 §3.2). v1's two passes, streaming: the
        action bytes are hashed as the episode arrives; the video content is read only when a hash
        collides, and then only for the two episodes involved. Byte-level duplicates (action and
        video both identical) get the ``duplicate`` finding; the member seen first owns the group,
        and ``aggregate`` picks which one the delivery keeps (§4.5)."""
        from ..dataset_level.dedup import action_hash

        m = "dedup"
        ep = int(ep)
        try:
            ah = str(action_hash(row))
        except Exception as e:  # noqa: BLE001 - this episode's action cannot be hashed
            logs[m].add("internal", cause=f"{type(e).__name__}: {e}")
            return {m: None}
        with self._lock:
            self._dd_hash[ep] = ah
            self._dd_order.append(ep)
            first = self._dd_first.get(ah)
            if first is None:
                self._dd_first[ah] = ep
        detail: dict = {"action_hash": ah}
        if first is None or first == ep:
            return {m: self._dd_struct(True, detail)}
        try:
            mine = self._fingerprint(source, ep, row)
        except Exception as e:  # noqa: BLE001 - this episode's video content cannot be read
            logs[m].add("read", cause=f"{type(e).__name__}: {e}")
            return {m: None}
        if mine is None:
            logs[m].add("read", cause=f"the action bytes collide with {self._eid(first)}, "
                                      f"whose video content could not be read")
            return {m: None}
        with self._lock:
            group = self._dd_groups.setdefault(ah, {})
            first_collision = not group
        if first_collision:                        # the member that owns the hash is fingerprinted now
            theirs = self._fingerprint(source, first, None)
            with self._lock:
                if theirs is not None:
                    self._dd_fp[first] = theirs
                    group.setdefault(theirs, first)
        with self._lock:
            self._dd_fp[ep] = mine
            owner = group.get(mine)
            if owner is None:
                group[mine] = ep
        if owner is None or owner == ep:
            return {m: self._dd_struct(True, detail)}
        detail["duplicate_of"] = owner
        detail["reason"] = f"与 {self._eid(owner)} 字节级完全重复"
        with self._lock:
            self._dd_dropped[ep] = owner
        return {m: self._dd_struct(False, detail)}

    @staticmethod
    def _dd_struct(passed: bool, detail: dict) -> dict:
        return {"passed": passed, "score": None,
                "detail": json.dumps(detail, ensure_ascii=False)}

    def _dedup_settle(self, writer) -> None:
        """The group keeps its lowest episode index (D70), whatever order the block handed them over.

        The segment hashes episodes as they arrive, so the member it met first is not always the
        lowest index - and a paused run can leave a group half judged. Every confirmed duplicate is a
        pointer (``duplicate_of``), so the groups are the connected components of those pointers,
        including the ones read back from earlier calls: the lowest index of a component keeps, and a
        member whose record says otherwise gets a corrected one appended. Byte-level copies are rare,
        so this normally writes nothing; the command line feeds episodes in ascending order and never
        needs it.
        """
        parent: dict[int, int] = {}

        def find(x: int) -> int:
            while parent.get(x, x) != x:
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        for ep, owner in self._dd_dropped.items():
            parent.setdefault(int(ep), int(ep))
            parent.setdefault(int(owner), int(owner))
            union(int(ep), int(owner))
        components: dict[int, list[int]] = {}
        for ep in parent:
            components.setdefault(find(ep), []).append(ep)
        mine = set(self.o.episodes)
        for members in components.values():
            canonical = min(members)
            for ep in sorted(members):
                want = None if ep == canonical else canonical
                if self._dd_dropped.get(ep) == want:
                    continue                                   # already recorded that way
                detail: dict = {}
                if self._dd_hash.get(ep):
                    detail["action_hash"] = self._dd_hash[ep]
                if want is None:
                    self._dd_dropped.pop(ep, None)
                else:
                    detail["duplicate_of"] = want
                    detail["reason"] = f"与 {self._eid(want)} 字节级完全重复"
                    self._dd_dropped[ep] = want
                records = self._records(ep, {"dedup": self._dd_struct(want is None, detail)},
                                        {"dedup": IncidentLog()}, 0.0)
                for rec in records.values():
                    writer.write(rec)
                if self._store is None:
                    continue
                if ep in mine:
                    self._store.finish(self.o.stage, ep, records, self.o.pipeline_next)
                else:                                          # an episode of an earlier call
                    for rec in records.values():
                        self._store.put_result(rec)

    def _dedup_groups_doc(self) -> dict:
        """``checks/dedup/groups.json``: the traversal order, the action-hash collisions, the
        fingerprints computed and what this run dropped (the report and the parity tool read it)."""
        order = self._dd_seeded + [e for e in self._dd_order if e not in set(self._dd_seeded)]
        hit = {ah for ah, group in self._dd_groups.items() if group}
        hit |= {self._dd_hash[e] for e in (set(self._dd_dropped) | set(self._dd_dropped.values()))
                if e in self._dd_hash}
        collisions = sorted(sorted(e for e, h in self._dd_hash.items() if h == ah) for ah in hit)
        dropped = [{"episode_index": e, "duplicate_of": self._dd_dropped[e]}
                   for e in sorted(self._dd_dropped)]
        return {"order": order, "action_collisions": collisions,
                "fingerprints": {str(e): fp for e, fp in sorted(self._dd_fp.items())},
                "dropped": dropped}

    def _evidence(self, ep: int, row: dict, struct: dict) -> list[str]:
        mode = self.o.evidence_mode
        if mode == "off" or struct is None:
            return []
        passed = struct.get("passed")
        if mode == "flagged" and passed is True:
            return []
        from ..export.evidence import render_task_evidence

        pcfg = self.o.cfg.get("pipeline", {})
        eid = f"ep{ep:06d}"
        entry = {"checks": {"task_success": struct},
                 "undecidable": ["task_success"] if passed is None else []}
        out_dir = os.path.join(self.o.run_dir, "details", "evidence")
        try:
            written = render_task_evidence({eid: entry}, {eid: row.get("video")}, out_dir,
                                           interval=pcfg.get("frame_sample_interval_s", 0.5),
                                           max_side=pcfg.get("frame_max_side", 448), mode=mode)
        except Exception:  # noqa: BLE001 - evidence is an attachment, never a verdict
            return []
        return [f"details/evidence/{r}".replace(os.sep, "/") for r in written.get(eid, [])]

    def _integrity(self, source, ep: int, t0: float, logs) -> dict[str, dict] | None:
        """The data integrity module: its files, v1's row validation (D51), the rest."""
        m = self.o.modules[0]
        row = row_error = None
        try:
            row = source.get(ep)
        except EpisodeMissingSource as e:          # D40 as everywhere: no result line
            with self._lock:
                self.missing[int(ep)] = list(e.missing)
            return None
        except EpisodeReadError as e:
            row_error = e
        try:
            struct = self.o.integrity.judge(ep, row, row_error, logs[m])
        except Exception as e:  # noqa: BLE001 - one episode's failure stays its own
            struct = None
            logs[m].add("internal", cause=f"{type(e).__name__}: {e}")
        finally:
            source.release(row)
        return self._records(ep, {m: struct}, logs, time.monotonic() - t0)

    def _work(self, source: RowSource, ep: int) -> dict[str, dict] | None:
        """The records of one episode; None when its source files are missing (D40)."""
        t0 = time.monotonic()
        logs = {m: IncidentLog() for m in self.o.modules}
        evidence = None
        if self.o.stage == "integrity":
            return self._integrity(source, ep, t0, logs)
        try:
            row = source.get(ep)
        except EpisodeMissingSource as e:          # v1 leaves it out: no result line
            with self._lock:
                self.missing[int(ep)] = list(e.missing)
            return None
        except EpisodeReadError as e:
            for m in self.o.modules:
                logs[m].add("read", cause=str(e))
            return self._records(ep, {}, logs, time.monotonic() - t0)
        contexts = None
        try:
            if self.o.stage == "numeric":
                structs = self._numeric(ep, row, logs)
                if "motion_quality" in self.o.modules:
                    contexts = {"motion_quality": {"action_semantics": action_semantics(row)}}
            elif self.o.stage == "frame":
                structs = self._frame(ep, row, logs)
            elif self.o.stage == "dedup":
                structs = self._dedup(source, ep, row, logs)
            else:
                structs, evidence = self._vlm(ep, row, logs)
        finally:
            release = getattr(source, "release", None)
            if release is not None:                    # mcap: this episode's muxed videos
                release(row)
        return self._records(ep, structs, logs, time.monotonic() - t0, evidence, contexts)

    # ------------------------------------------------------------ the run
    def run(self) -> dict:
        o, ctx = self.o, self.ctx
        if o.stage == "dedup":
            self._dedup_seed()            # group against what this run directory already judged
        todo, skipped, crashed = self._todo()
        total = len(o.episodes)
        writer = PartWriter(o.run_dir, o.modules, o.part, index=self._store is None)
        inflight = Inflight(o.run_dir, o.modules, o.part)
        breaker = (_Breaker(watched={m for m in o.modules if not registry_mod.is_rider(m)})
                   if o.stage == "vlm" else None)
        self.done = skipped if o.episode_stream is None else 0
        drained = False
        try:
            for ep in (sorted(crashed) if o.episode_stream is None else []):
                log = IncidentLog()
                log.add("crash", cause="the process died twice while working on this episode")
                records = self._records(ep, {}, {m: log for m in o.modules}, 0.0)
                for rec in records.values():
                    writer.write(rec)
                if self._store is not None:
                    self._store.finish(o.stage, ep, records, o.pipeline_next)
                self.done += 1
            if crashed and o.episode_stream is None:
                ctx.log("warn", f"{len(crashed)} episode(s) crashed the process twice; "
                                f"recorded as errors and skipped: {sorted(crashed)}")
            ctx.progress(self.label, self.done, total)
            if o.episode_stream is not None:
                self._drive_stream(todo, crashed, writer, inflight, breaker, total)
            elif todo:
                if o.verify_source is not None:
                    o.verify_source(todo)
                source = self._source(todo)
                self._drive(source, todo, writer, inflight, breaker, total)
            if o.stage == "dedup":
                # also when this call was paused or stopped: the groups it did confirm are settled
                # before it leaves, so a run that ends here is not left with a half-judged group
                self._dedup_settle(writer)
            drained = True
        finally:
            writer.close()
            if o.stage == "dedup":
                write_json_atomic(os.path.join(module_dir(o.run_dir, "dedup"), "groups.json"),
                                  self._dedup_groups_doc())
            if drained:
                inflight.clear()        # SIGINT / a crash leave it for the next --resume
            if self._store is None:
                for m in o.modules:
                    compact(o.run_dir, m)
            if self.missing:
                from .skipped import record

                record(o.run_dir, self.missing)
                ctx.log("warn", f"{len(self.missing)} episode(s) have missing source files and "
                                f"are left out like v1 does: {sorted(self.missing)[:10]}")
            if self._store is not None:
                self._store.close()
        ctx.check_stop(f"{self.label}: {self.done}/{total} episodes done")
        return self.summary(skipped)

    def _drive_stream(self, todo, crashed, writer, inflight, breaker, total) -> None:
        """Consume admitted episodes without a batch barrier.

        The parent limits admissions and CPU usage across stages. Completion is
        published only after the result and its next destination are durable.
        Input metadata and the check pool are reused for the entire layer.
        """
        o, ctx = self.o, self.ctx
        stream = o.episode_stream
        remaining = set(todo)
        current = {m: latest_results(o.run_dir, m, o.episodes) for m in o.modules}
        queue = deque()
        admitted = []
        pending = {}
        source = None
        closed = False
        tripped = None
        pool = cf.ThreadPoolExecutor(max_workers=max(1, o.concurrency),
                                     thread_name_prefix="check")

        def complete(ep, records):
            if records is None:
                self._store.missing(o.stage, ep)
            else:
                for rec in records.values():
                    writer.write(rec)
                self._store.finish(o.stage, ep, records, o.pipeline_next)
            inflight.remove(ep)
            self.done += 1
            stream.completed(ep, records is not None and (self._store.blocks is not None or all(
                passes_funnel(r, self.funnel_policy) for r in records.values())))
            ctx.progress(self.label, self.done, total, episode_index=ep)

        try:
            while pending or queue or not closed:
                if ctx.stop_requested or tripped is not None:
                    closed = True
                    queue.clear()
                if not closed and len(pending) + len(queue) < o.concurrency:
                    incoming = stream.receive(timeout=0 if pending or queue else 0.05)
                    if incoming is None:
                        closed = True
                    else:
                        queue.extend(incoming)
                while queue and len(pending) < o.concurrency and not ctx.stop_requested:
                    ep = queue.popleft()
                    admitted.append(ep)
                    if ep in crashed:
                        log = IncidentLog()
                        log.add("crash", cause="the process died twice while working on this episode")
                        complete(ep, self._records(ep, {}, {m: log for m in o.modules}, 0.0))
                        ctx.log("warn", f"episode {ep} crashed the process twice; recorded as error")
                        continue
                    if ep not in remaining:
                        records = {m: current[m][ep] for m in o.modules}
                        complete(ep, records)
                        continue
                    if o.verify_source is not None:
                        o.verify_source([ep])
                    if source is None:
                        source = self._source(todo)
                    inflight.add(ep)
                    pending[pool.submit(self._work, source, ep)] = ep
                if not pending:
                    if closed:
                        break
                    continue
                finished, _ = cf.wait(pending, timeout=0.02, return_when=cf.FIRST_COMPLETED)
                for fut in finished:
                    ep = pending.pop(fut)
                    records = fut.result()
                    complete(ep, records)
                    if breaker is not None and tripped is None and records is not None:
                        try:
                            breaker.observe(records)
                        except BreakerTripped as exc:
                            tripped = exc
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            o.episodes = admitted
        if tripped is not None:
            from ..cli.errors import ModuleFailed

            raise ModuleFailed(f"{', '.join(o.modules)}: {tripped}",
                               {"modules": list(o.modules), "episodes_done": self.done})

    def _drive(self, source, todo, writer, inflight, breaker, total) -> None:
        o, ctx = self.o, self.ctx
        workers = max(1, int(o.concurrency))
        pending: dict[cf.Future, int] = {}
        queue = list(todo)
        tripped: BreakerTripped | None = None
        pool = cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="check")
        started = time.monotonic()
        try:
            while queue or pending:
                while queue and len(pending) < workers and not ctx.stop_requested \
                        and tripped is None:
                    ep = queue.pop(0)
                    inflight.add(ep)
                    pending[pool.submit(self._work, source, ep)] = ep
                if not pending:
                    break
                finished, _ = cf.wait(list(pending), timeout=0.5,
                                      return_when=cf.FIRST_COMPLETED)
                for fut in finished:
                    ep = pending.pop(fut)
                    records = fut.result()
                    if records is None:                # left out: missing source (D40)
                        if self._store is not None:
                            self._store.missing(o.stage, ep)
                        inflight.remove(ep)
                        self.done += 1
                        ctx.progress(self.label, self.done, total, episode_index=ep)
                        continue
                    for rec in records.values():
                        writer.write(rec)
                    if self._store is not None:
                        self._store.finish(o.stage, ep, records, o.pipeline_next)
                    inflight.remove(ep)
                    self.done += 1
                    rate = (time.monotonic() - started) / max(1, self.done)
                    ctx.progress(self.label, self.done, total,
                                 eta_s=rate * (total - self.done), episode_index=ep)
                    if breaker is not None and tripped is None:
                        try:
                            breaker.observe(records)
                        except BreakerTripped as e:
                            tripped = e
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if tripped is not None:
            from ..cli.errors import ModuleFailed

            raise ModuleFailed(f"{', '.join(o.modules)}: {tripped}",
                               {"modules": list(o.modules), "episodes_done": self.done})

    def summary(self, skipped: int) -> dict:
        from .skipped import as_list

        o = self.o
        digest = input_digest(o.episodes)
        judged = [e for e in o.episodes if e not in self.missing]
        out: dict = {}
        version = "1.0"
        for m in o.modules:
            version, counts, errors, found = check_counts(latest_results(o.run_dir, m, judged), judged)
            entry = {"part": o.part, "input_digest": digest, "episodes": counts,
                     "error_episodes": errors}
            if version != "1.0":
                entry["findings"] = found
            if o.resume:
                entry["skipped_existing"] = skipped
            if self.missing:
                entry["skipped_missing_source"] = as_list(self.missing)
            out[m] = entry
        return {"schema_version": version, "modules": out}

    def survivors(self) -> list[int]:
        """Episodes of this call that go on to the next stage: plan 2.0 - every one judged (findings and errors
        stop nothing, D57); a funnel run - no error, nothing that blocks under the policy."""
        cur = {m: latest_results(self.o.run_dir, m, self.o.episodes)
               for m in self.o.modules}
        out = []
        for e in self.o.episodes:
            recs = [cur[m].get(e) for m in self.o.modules]
            if self.two_blocks:
                if all(r is not None for r in recs):
                    out.append(e)
                continue
            if not all(passes_funnel(r, self.funnel_policy) for r in recs):
                continue
            out.append(e)
        return out
