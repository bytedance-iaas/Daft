"""``python faultycli.py <curation arguments>``: the real CLI with a fault injected.

The Daemon runs it through ``CURATOR_CLI``. Faults (environment, ``<module>:<episode>``),
active while the file named by ``FAKE_SWITCH`` exists (always, when it is unset):

* ``FAKE_CRASH`` - the process kills itself (SIGKILL) while working on that episode in
  a check that includes that module: a native crash the CLI cannot catch;
* ``FAKE_ERROR`` - that module records an execution error for that episode (a model
  call that failed after every retry, D33), the other episodes are judged normally;
* ``FAKE_FAIL_MODULE`` (``<module>``) - a check including that module fails as a
  whole (exit 4, as when its VLM endpoint is unreachable).
"""
from __future__ import annotations

import os
import signal
import sys


def _active() -> bool:
    switch = os.environ.get("FAKE_SWITCH")
    return not switch or os.path.exists(switch)


def _target(name: str):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    module, _, ep = raw.partition(":")
    return module, (int(ep) if ep else None)


def install() -> None:
    from curation.pipeline import check_stage
    from curation.pipeline.incidents import IncidentLog

    crash, error, fail = _target("FAKE_CRASH"), _target("FAKE_ERROR"), _target("FAKE_FAIL_MODULE")
    original_work = check_stage.StageRun._work

    def work(self, source, ep):
        if _active():
            if crash and crash[0] in self.o.modules and ep == crash[1]:
                os.kill(os.getpid(), signal.SIGKILL)
            if error and error[0] in self.o.modules and ep == error[1]:
                logs = {m: IncidentLog() for m in self.o.modules}
                logs[error[0]].add("probe", call_kind="probe", cause="timeout", attempts=4)
                return self._records(ep, {}, logs, 0.0)
        return original_work(self, source, ep)

    check_stage.StageRun._work = work
    if fail:
        original_run = check_stage.StageRun.run

        def run(self):
            if _active() and fail[0] in self.o.modules:
                from curation.cli.errors import ModuleFailed

                raise ModuleFailed(f"{fail[0]}: the VLM endpoint cannot be used (injected)")
            return original_run(self)

        check_stage.StageRun.run = run


if __name__ == "__main__":
    install()
    from curation.cli.app import main

    sys.exit(main(sys.argv[1:]))
