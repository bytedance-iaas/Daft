"""One funnel stage over a set of episodes: the engine of ``curation check`` (doc 02 §3.5).

The stages and their order are v1's funnel (D18): ``numeric`` (timestamp_check,
kinematic_limits, motion_quality), ``frame`` (visual_quality and
video_action_sync on one shared decode per camera), ``vlm`` (task_success). Each
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
from collections.abc import Callable
from dataclasses import dataclass, field

from ..contracts import modules as registry_mod
from . import funnel
from .incidents import (IncidentLog, camera_names, wrap_arbitration, wrap_call, wrap_decode,
                        wrap_voter)
from .records import (CRASHES_NAME, Inflight, PartWriter, compact, latest_results,
                      module_dir, pid_alive, read_inflight, record_from_struct,
                      write_json_atomic)
from .rows import EpisodeMissingSource, EpisodeReadError, RowSource, column, open_row_source

FUNNEL_STAGES = ("numeric", "frame", "vlm")
NUMERIC_ORDER = ("timestamp_check", "kinematic_limits", "motion_quality")
#: P15: this many consecutive failures on one infrastructure cause at the start
#: of a VLM stage fail the module instead of timing out episode by episode.
BREAKER_LIMIT = 20
INFRA_CAUSES = frozenset({"connect_error", "timeout", "server_error", "rate_limited",
                          "http_error", "tls_error"})
CRASH_LIMIT = 2


def stage_of(module: str) -> str:
    return registry_mod.get(module).stage


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
    #: mcap / lance (D44): the task's selection (the semantics sample) and the hook that
    #: makes an episode's source objects local before it is read (a remote dataset)
    selection: list[int] | None = None
    fetch: Callable[[list[int]], None] | None = None
    row_instructions: bool = False
    fmt: str | None = None                              # mcap | lance; None: v1's sniffing
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

    def __init__(self, limit: int = BREAKER_LIMIT):
        self.limit, self.count, self.cause, self.armed = limit, 0, None, True

    def observe(self, records: dict[str, dict]) -> None:
        if not self.armed:
            return
        causes = set()
        for rec in records.values():
            if rec["verdict"] != "error":
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


class StageRun:
    """Runs one stage call; :meth:`run` returns the ``check --json`` document."""

    def __init__(self, ctx, opts: StageOptions, registry):
        self.ctx, self.o, self.registry = ctx, opts, registry
        self.label = "check:" + "+".join(opts.modules)
        self._lock = threading.Lock()
        self.done = 0
        #: episodes found without their source files (D40): no result line
        self.missing: dict[int, list[str]] = {}

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
        current = {m: latest_results(self.o.run_dir, m) for m in self.o.modules}
        done = {e for e in eps
                if all(e in current[m] and current[m][e]["verdict"] != "error"
                       for m in self.o.modules)}
        crashes = self._stale_inflight()
        crashed = {e for e in eps if crashes.get(e, 0) >= CRASH_LIMIT and e not in done}
        return [e for e in eps if e not in done and e not in crashed], len(done), crashed

    # ------------------------------------------------------------ per episode
    def _source(self, todo: list[int]) -> RowSource:
        """v1's data source for these episodes. It reads the dataset's first episodes to
        resolve its semantics; when that fails no episode can be judged (exit 4)."""
        from ..cli.errors import CliError, ModuleFailed

        o = self.o
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
                 elapsed: float, evidence: dict[str, list] | None = None) -> dict[str, dict]:
        return {m: record_from_struct(m, ep, structs.get(m), incidents=logs[m].items(),
                                      evidence=(evidence or {}).get(m), elapsed_s=elapsed)
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
        log = logs["task_success"]
        if self.o.row_instructions:
            # mcap / lance: the annotation comes with the row (v1's meta and data reads of
            # these formats build it the same way), no separate metadata pass
            self.o.task_text.instructions[int(ep)] = str(row.get("instruction") or "")
        text, src, problem = self.o.task_text.resolve(ep)
        if problem is not None:
            log.add("autolabel", cause=problem)
            return {"task_success": None}, {}
        clients = self.o.task_clients
        deps = funnel.TaskDeps(
            vlm_completion=wrap_call(clients.vlm_completion, log, step="probe",
                                     call_kind="probe"),
            cam_voter=wrap_voter(clients.cam_voter, log),
            arb_deps=wrap_arbitration(clients.arb_deps, log),
            decode=wrap_decode(funnel._default_decode, log, camera_names(row.get("video"))))
        # D39: a human relabel is judged again the way adjudicate-apply recorded -
        # "v1" (default) is v1's rejudge itself, multi-view scoring and the
        # per-camera vote; "full" is the first run's whole flow with the relabel as
        # the annotation (label guard, arbitration intent)
        rerun = self.o.task_text.relabel_rerun(ep) if src == "人工改标" else None
        try:
            if rerun == "v1":
                from .rejudge import rerun_task_success

                struct = funnel.result_to_struct(rerun_task_success(
                    self.o.cfg, row["video"], text, deps.vlm_completion, deps.cam_voter,
                    decode=deps.decode))
            else:
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

    def _work(self, source: RowSource, ep: int) -> dict[str, dict] | None:
        """The records of one episode; None when its source files are missing (D40)."""
        t0 = time.monotonic()
        logs = {m: IncidentLog() for m in self.o.modules}
        evidence = None
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
        try:
            if self.o.stage == "numeric":
                structs = self._numeric(ep, row, logs)
            elif self.o.stage == "frame":
                structs = self._frame(ep, row, logs)
            else:
                structs, evidence = self._vlm(ep, row, logs)
        finally:
            release = getattr(source, "release", None)
            if release is not None:                    # mcap: this episode's muxed videos
                release(row)
        return self._records(ep, structs, logs, time.monotonic() - t0, evidence)

    # ------------------------------------------------------------ the run
    def run(self) -> dict:
        o, ctx = self.o, self.ctx
        todo, skipped, crashed = self._todo()
        total = len(o.episodes)
        writer = PartWriter(o.run_dir, o.modules, o.part)
        inflight = Inflight(o.run_dir, o.modules, o.part)
        breaker = _Breaker() if o.stage == "vlm" else None
        self.done = skipped
        drained = False
        try:
            for ep in sorted(crashed):
                log = IncidentLog()
                log.add("crash", cause="the process died twice while working on this episode")
                for rec in self._records(ep, {}, {m: log for m in o.modules}, 0.0).values():
                    writer.write(rec)
                self.done += 1
            if crashed:
                ctx.log("warn", f"{len(crashed)} episode(s) crashed the process twice; "
                                f"recorded as errors and skipped: {sorted(crashed)}")
            ctx.progress(self.label, self.done, total)
            if todo:
                if o.verify_source is not None:
                    o.verify_source(todo)
                source = self._source(todo)
                self._drive(source, todo, writer, inflight, breaker, total)
            drained = True
        finally:
            writer.close()
            if drained:
                inflight.clear()        # SIGINT / a crash leave it for the next --resume
            for m in o.modules:
                compact(o.run_dir, m)
            if self.missing:
                from .skipped import record

                record(o.run_dir, self.missing)
                ctx.log("warn", f"{len(self.missing)} episode(s) have missing source files and "
                                f"are left out like v1 does: {sorted(self.missing)[:10]}")
        ctx.check_stop(f"{self.label}: {self.done}/{total} episodes done")
        return self.summary(skipped)

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
                        inflight.remove(ep)
                        self.done += 1
                        ctx.progress(self.label, self.done, total, episode_index=ep)
                        continue
                    for rec in records.values():
                        writer.write(rec)
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
        for m in o.modules:
            cur = latest_results(o.run_dir, m)
            counts = {"total": len(judged), "pass": 0, "fail": 0, "abstain": 0,
                      "scored": 0, "error": 0}
            errors = []
            for e in judged:
                rec = cur.get(e)
                verdict = rec["verdict"] if rec else "error"
                counts[verdict] += 1
                if verdict == "error":
                    errors.append(e)
            entry = {"part": o.part, "input_digest": digest, "episodes": counts,
                     "error_episodes": errors}
            if o.resume:
                entry["skipped_existing"] = skipped
            if self.missing:
                entry["skipped_missing_source"] = as_list(self.missing)
            out[m] = entry
        return {"schema_version": "1.0", "modules": out}

    def survivors(self) -> list[int]:
        """Episodes of this call that go on to the next stage (no error, no hard fail)."""
        cur = {m: latest_results(self.o.run_dir, m) for m in self.o.modules}
        out = []
        for e in self.o.episodes:
            recs = [cur[m].get(e) for m in self.o.modules]
            if any(r is None or r["verdict"] in ("error", "fail") for r in recs):
                continue
            out.append(e)
        return out
