"""Running the pipeline commands in tests: in process, with the output discipline checked.

``run(argv)`` runs ``curation.cli.main`` like the ``cli`` fixture does (stdout is
exactly one JSON document, every stderr line is a C3 event, a non-zero exit
prints a valid error envelope) but without pytest's ``capsys``, so module-scoped
fixtures can build a whole run directory once. ``Chain`` runs the commands in
the Daemon's order (design doc 00 §4).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

from curation.contracts import schemas

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO = os.path.dirname(BACKEND)

#: the C2 schema of each command's --json
SCHEMA_OF = {"preflight": "cli/preflight.schema.json", "plan": "cli/plan.schema.json",
             "snapshot": "cli/source-manifest.schema.json",
             "autolabel": "cli/autolabel.schema.json", "check": "cli/check.schema.json",
             "aggregate": "cli/aggregate.schema.json", "export": "cli/export.schema.json",
             "report": "cli/report-output.schema.json",
             "adjudicate-apply": "cli/adjudicate-apply.schema.json",
             "verify": "cli/verify.schema.json"}


@dataclass
class Run:
    rc: int
    doc: object
    events: list = field(default_factory=list)
    err: str = ""


def run(*argv, check_schema: bool = True) -> Run:
    from curation.cli.app import main

    args = [str(a) for a in argv]
    if "--json" not in args:
        args.append("--json")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(args)
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1, f"stdout must be one JSON document, got {out.getvalue()!r}"
    doc = json.loads(lines[0])
    events = []
    for line in err.getvalue().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:                 # say which line, not "char 0"
            raise AssertionError(f"stderr is not one C3 event per line: {line!r}") from None
        problems = schemas.errors("progress.schema.json", event)
        assert not problems, f"stderr line breaks C3: {line} -> {problems}"
        events.append(event)
    if rc != 0:
        problems = schemas.errors("cli/error.schema.json", doc)
        assert not problems, f"bad error envelope {doc}: {problems}"
        assert doc["exit_code"] == rc
    elif check_schema and args[0] in SCHEMA_OF:
        problems = schemas.errors(SCHEMA_OF[args[0]], doc)
        assert not problems, f"{args[0]} --json breaks {SCHEMA_OF[args[0]]}: {problems}"
    return Run(rc, doc, events, err.getvalue())


def subprocess_cli(*argv, env=None, **kw) -> subprocess.Popen:
    """``python -m curation.cli <argv> --json`` as a child process (signals, SIGKILL)."""
    e = dict(os.environ if env is None else env)
    e["PYTHONPATH"] = os.pathsep.join([BACKEND, os.path.join(REPO, "tools"),
                                       e.get("PYTHONPATH", "")])
    return subprocess.Popen([sys.executable, "-m", "curation.cli", *map(str, argv), "--json"],
                            cwd=BACKEND, env=e, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, **kw)


def read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def results(run_dir: str, module: str) -> dict[int, dict]:
    return {r["episode_index"]: r
            for r in read_jsonl(os.path.join(run_dir, "checks", module, "results.jsonl"))}


def comparable(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k not in ("elapsed_s", "evidence")}


class Chain:
    """The commands in the Daemon's order on one run directory."""

    NUMERIC = "timestamp_check,kinematic_limits,motion_quality"
    FRAME = "visual_quality,video_action_sync"

    def __init__(self, dataset: str, run_dir: str, vlm_url: str, *, extra_vlm=(),
                 extra_source=()):
        """``extra_source``: more source arguments of every reading command (mcap / lance:
        ``--selection``, as the Daemon passes it)."""
        self.ds, self.rd = dataset, run_dir
        self.vlm = ["--vlm-endpoint", vlm_url, "--vlm-model", "fake-vlm", *extra_vlm]
        self.extra_source = list(extra_source)
        self.steps: dict[str, Run] = {}
        os.makedirs(run_dir, exist_ok=True)

    def path(self, *parts) -> str:
        return os.path.join(self.rd, *parts)

    def step(self, name: str, *argv, expect: int = 0) -> Run:
        res = run(*argv)
        assert res.rc == expect, f"{name}: exit {res.rc}: {res.doc}\n{res.err[-3000:]}"
        self.steps[name] = res
        return res

    def front(self, episodes: str = "0-7") -> None:
        """preflight, plan, snapshot."""
        pf = self.step("preflight", "preflight", "--input", self.ds, "--vlm-backend", "ark")
        with open(self.path("preflight.json"), "w", encoding="utf-8") as fh:
            json.dump(pf.doc, fh)
        self.step("plan", "plan", "--preflight", self.path("preflight.json"), "--modules",
                  "timestamp_check,kinematic_limits,motion_quality,visual_quality,"
                  "video_action_sync,task_success,dedup,skill_profile",
                  "--episodes", episodes, "--out", self.path("plan.json"))
        self.step("snapshot", "snapshot", "--input", self.ds, "--episodes", episodes,
                  "--out", self.path("source_manifest.json"))

    def common(self) -> list[str]:
        return ["--input", self.ds, "--run-dir", self.rd,
                "--source-manifest", self.path("source_manifest.json"), *self.extra_source]

    def before_vlm(self, episodes: str = "0-7") -> None:
        """autolabel, check numeric, check frame: everything the VLM stage reads."""
        os.makedirs(self.path("stages"), exist_ok=True)
        self.step("autolabel", "autolabel", *self.common(), "--episodes", episodes, *self.vlm)
        self.step("numeric", "check", "--modules", self.NUMERIC, *self.common(),
                  "--episodes", episodes, "--survivors-out", self.path("stages", "numeric.txt"))
        self.step("frame", "check", "--modules", self.FRAME, *self.common(),
                  "--episodes", "@" + self.path("stages", "numeric.txt"),
                  "--survivors-out", self.path("stages", "frame.txt"))

    def funnel(self, episodes: str = "0-7") -> None:
        self.before_vlm(episodes)
        self.step("vlm", "check", "--modules", "task_success", *self.common(),
                  "--episodes", "@" + self.path("stages", "frame.txt"), *self.vlm)

    def post(self, revision: int = 1, episodes: str = "0-7") -> None:
        r = str(revision)
        self.step("funnel", "aggregate", "--run-dir", self.rd, "--phase", "funnel",
                  "--revision", r, "--episodes", episodes)
        keep = self.path("revisions", f"r{revision:04d}", "keep.txt")
        self.step("dedup", "check", "--modules", "dedup", *self.common(), "--episodes",
                  "@" + keep, "--survivors-out", self.path("stages", "dedup.txt"))
        self.step("profile", "check", "--modules", "skill_profile", *self.common(),
                  "--episodes", "@" + self.path("stages", "dedup.txt"), *self.vlm)
        self.step("final", "aggregate", "--run-dir", self.rd, "--phase", "final",
                  "--revision", r, "--episodes", episodes, "--input", self.ds)
        self.step("report", "report", "--run-dir", self.rd, "--revision", r)

    def deliver(self, delivery: str, *extra) -> None:
        import shutil

        self.step("export", "export", "--run-dir", self.rd, "--input", self.ds,
                  "--output", delivery, *extra)
        skip = {os.path.join(self.rd, "export", name)            # export uploads them itself
                for name in ("lerobot_curated", "mcap_curated", "lance_episodes")}
        shutil.copytree(self.rd, delivery, dirs_exist_ok=True,
                        ignore=lambda d, names: [n for n in names
                                                 if os.path.join(d, n) in skip])
        self.step("verify", "verify", "--run-dir", self.rd, "--output", delivery,
                  "--visibility-timeout", "0")
