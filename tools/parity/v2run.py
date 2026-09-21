"""Run the v2 command chain on a dataset, with the model calls taped: ``python -m parity run-v2``.

The v2 side of the synthetic parity (design doc 11 §3, 2026-09-21): the atomic
commands run in this process in the order the Daemon runs them (doc 00 §4) -

    preflight -> plan -> snapshot -> autolabel -> check numeric -> check frame
    -> check vlm -> aggregate funnel -> check dedup -> check skill_profile
    -> aggregate final -> report -> export -> verify

- each stage reading the survivors of the one before, into one v2 run
directory, which ``python -m parity compare`` loads directly. Model calls go
through the same tape hooks as ``dump-v1``: ``--replay`` serves v1's recorded
answers and counts every request that is not on the tape (a different prompt,
frame or JPEG parameter gives a different hash), ``--fake-vlm`` answers live
from the deterministic fake model and records a tape. ``/models`` endpoint
probes are served from the tape without being counted: every v2 command probes
its endpoint once, v1 probed twice per run, and a probe is not a model call.

The VLM commands run with ``--hedge`` (v1 always hedges; the tape hooks replace
``hedged_request``) and ``--concurrency 64`` (the gates of v1's factory defaults).

Exit code 0 when every command succeeded, 1 otherwise; ``parity.json`` in the
run directory records the steps and the tape statistics.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time

from . import vlm_tape as T

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
BACKEND = os.path.join(REPO, "backend")
ALL_MODULES = ("timestamp_check", "kinematic_limits", "motion_quality", "visual_quality",
               "video_action_sync", "task_success", "dedup", "skill_profile")


class StepFailed(RuntimeError):
    pass


class Chain:
    def __init__(self, run_dir: str, dataset: str, *, delivery: str | None,
                 vlm_endpoint: str, vlm_model: str, log_path: str):
        self.run_dir, self.dataset, self.delivery = run_dir, dataset, delivery
        self.vlm = ["--vlm-endpoint", vlm_endpoint, "--vlm-model", vlm_model, "--hedge",
                    "--concurrency", "64"]
        self.log = open(log_path, "a", encoding="utf-8")
        self.steps: list[dict] = []

    def close(self) -> None:
        self.log.close()

    def run(self, name: str, *argv: str, ok=(0,)) -> dict:
        from curation.cli.app import main

        args = [str(a) for a in argv] + ["--json"]
        out = io.StringIO()
        self.log.write(f"\n### {name}: curation {' '.join(args)}\n")
        self.log.flush()
        t0 = time.time()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(self.log):
            rc = main(args)
        doc = json.loads(out.getvalue().strip().splitlines()[-1]) if out.getvalue().strip() \
            else None
        self.steps.append({"step": name, "argv": args, "exit_code": rc,
                           "seconds": round(time.time() - t0, 2), "output": doc})
        if rc not in ok:
            raise StepFailed(f"{name}: exit {rc}: {json.dumps(doc, ensure_ascii=False)[:600]}")
        return doc

    def stage_file(self, name: str) -> str:
        path = os.path.join(self.run_dir, "stages", f"{name}.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def all(self, episodes: str) -> None:
        rd, ds = self.run_dir, self.dataset
        sm = os.path.join(rd, "source_manifest.json")
        pf = os.path.join(rd, "preflight.json")
        doc = self.run("preflight", "preflight", "--input", ds, "--vlm-backend", "ark")
        with open(pf, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
        self.run("plan", "plan", "--preflight", pf, "--modules", ",".join(ALL_MODULES),
                 "--episodes", episodes, "--out", os.path.join(rd, "plan.json"))
        self.run("snapshot", "snapshot", "--input", ds, "--episodes", episodes, "--out", sm)
        common = ["--input", ds, "--run-dir", rd, "--source-manifest", sm]
        self.run("autolabel", "autolabel", *common, "--episodes", episodes, *self.vlm)
        num, frame = self.stage_file("numeric"), self.stage_file("frame")
        self.run("check numeric", "check", "--modules",
                 "timestamp_check,kinematic_limits,motion_quality", *common,
                 "--episodes", episodes, "--survivors-out", num)
        self.run("check frame", "check", "--modules", "visual_quality,video_action_sync",
                 *common, "--episodes", f"@{num}", "--survivors-out", frame)
        self.run("check vlm", "check", "--modules", "task_success", *common,
                 "--episodes", f"@{frame}", *self.vlm)
        self.run("aggregate funnel", "aggregate", "--run-dir", rd, "--phase", "funnel",
                 "--revision", "1", "--episodes", episodes)
        keep = os.path.join(rd, "revisions", "r0001", "keep.txt")
        dedup = self.stage_file("dedup")
        self.run("check dedup", "check", "--modules", "dedup", *common,
                 "--episodes", f"@{keep}", "--survivors-out", dedup)
        self.run("check skill_profile", "check", "--modules", "skill_profile", *common,
                 "--episodes", f"@{dedup}", *self.vlm)
        self.run("aggregate final", "aggregate", "--run-dir", rd, "--phase", "final",
                 "--revision", "1", "--episodes", episodes, "--input", ds)
        if _has_command("report"):
            self.run("report", "report", "--run-dir", rd, "--revision", "1")
            if _has_command("export"):
                out = ["--output", self.delivery] if self.delivery else []
                self.run("export", "export", "--run-dir", rd, "--input", ds, "--source-manifest",
                         sm, *out)
            if self.delivery:
                self.run("verify", "verify", "--run-dir", rd, "--output", self.delivery,
                         "--visibility-timeout", "0")


def _has_command(name: str) -> bool:
    from curation.cli.app import build_parser

    sub = [a for a in build_parser()._actions if a.dest == "command"][0]
    return name in sub.choices


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m parity run-v2", description=__doc__.split("\n")[0])
    p.add_argument("--out", required=True, help="the v2 run directory (created, must be empty)")
    p.add_argument("--input", required=True, help="the dataset")
    p.add_argument("--episodes", default=None,
                   help="episode expression (default: every episode of the dataset)")
    p.add_argument("--delivery", help="a local directory standing in for the delivery (export "
                                      "and verify run when given)")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", metavar="TAPE", help="serve model calls from this tape")
    mode.add_argument("--fake-vlm", action="store_true",
                      help="answer live from the built-in fake model and record a tape")
    p.add_argument("--vlm-endpoint", default="http://fake-vlm.local/v1")
    p.add_argument("--vlm-model", default="fake-vlm")
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if os.path.exists(args.out) and os.listdir(args.out):
        print(f"run-v2: {args.out} is not empty", file=sys.stderr)
        return 2
    os.makedirs(args.out, exist_ok=True)
    if BACKEND not in sys.path:
        sys.path.insert(0, BACKEND)
    import curation.adapters.vlm_client as vlm_client

    loaded = os.path.dirname(os.path.dirname(os.path.abspath(vlm_client.__file__)))
    if os.path.normpath(os.path.dirname(loaded)) != os.path.normpath(BACKEND):
        print(f"run-v2: curation comes from {loaded}, not from this repository's backend",
              file=sys.stderr)
        return 2
    episodes = args.episodes
    if episodes is None:
        with open(os.path.join(args.input, "meta", "info.json"), encoding="utf-8") as fh:
            n = int(json.load(fh)["total_episodes"])
        episodes = f"0-{n - 1}"
    if args.replay:
        _, entries = T.read_tape(args.replay)
        hooks = T.TapeHooks("replay", replay_entries=entries, sticky_tags=("models",))
        mode = "replay"
    else:
        from .fakevlm import FakeVlm

        hooks = T.TapeHooks("record", tape_out=os.path.join(args.out, "vlm_tape.jsonl.gz"),
                            transport=FakeVlm(args.vlm_model).transport(),
                            tape_meta={"label": "v2 chain"})
        mode = "record"
    chain = Chain(args.out, args.input, delivery=args.delivery, vlm_endpoint=args.vlm_endpoint,
                  vlm_model=args.vlm_model, log_path=os.path.join(args.out, "parity.log"))
    failure = None
    hooks.install(vlm_client)
    try:
        chain.all(episodes)
    except StepFailed as e:
        failure = str(e)
    finally:
        hooks.uninstall()
        chain.close()
    doc = {"kind": "v2-run", "input": args.input, "episodes": episodes,
           "tape": {"mode": mode, "replayed_from": args.replay, "hooks": hooks.stats()},
           "steps": chain.steps, "failure": failure}
    if hooks.store is not None:
        doc["replay_misses"] = hooks.store.misses[:50]
    with open(os.path.join(args.out, "parity.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1, default=str)
    stats = doc["tape"]["hooks"]
    print(f"[run-v2] {'FAILED ' + failure if failure else 'done'}; tape {mode}: "
          f"hits={sum((stats.get('hits') or {}).values())} misses={stats.get('misses')} "
          f"unused={stats.get('unused')}", file=sys.stderr)
    return 1 if failure else 0
