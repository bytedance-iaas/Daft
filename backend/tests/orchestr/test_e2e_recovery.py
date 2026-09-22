"""Crash, restart and graceful shutdown (slow; D26, 01 §3.2, 09 §2.3).

* The Daemon is killed (SIGKILL) while a task runs; the CLI child it left behind is
  reaped by the next Daemon, which recovers the task from its checkpoints and runs
  it to the end by itself. A user-paused task stays paused, a user-stopped one stays
  stopped.
* A graceful shutdown whose command does not wind down in time (SIGKILL after the
  grace period) leaves the task paused by the system - never failed - and the next
  start resumes it.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

from .conftest import JSON, TERMINAL, make_settings, read_jsonl
from ..secrets.fakes import AK2, SK2

pytestmark = pytest.mark.slow

BACKEND = pathlib.Path(__file__).resolve().parents[2]
MASTER = bytes(range(32))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _prepare_volume(data: pathlib.Path, vlm_url: str) -> None:
    """Keys and the model service written straight into the database (no network at all)."""
    from daemon.masterkey import MasterKey
    from daemon.repo import protocol as P
    from daemon.repo.sqlite import SqliteRepository
    from daemon.secrets.sealing import Sealer
    from daemon.secrets.service import tos_payload
    from daemon.util import new_id

    data.mkdir(parents=True, exist_ok=True)
    repo = SqliteRepository(data / "curator.db")
    try:
        sealer = Sealer(MasterKey(MASTER))
        cred_id = new_id("cred")
        blob, version = sealer.seal(cred_id, tos_payload(AK2, SK2))
        repo.create_credential(P.Credential(id=cred_id, name="out-key", kind="tos",
                                            payload_enc=blob, key_version=version,
                                            payload_meta={"region": "cn-beijing"}))
        backend = repo.create_vlm_backend(P.VlmBackend(id="", name="fake", kind="custom",
                                                       endpoint=vlm_url, credential_id=None), None)
        repo.upsert_vlm_model(P.VlmModel(id="", backend_id=backend.id, model_name="fake-vlm"))
    finally:
        repo.close()


class DaemonProcess:
    def __init__(self, tmp: pathlib.Path, inputs: pathlib.Path, log_name: str):
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}/api/v1"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CURATOR_", "TOS_"))}
        env.update({
            "CURATOR_MASTER_KEY": base64.b64encode(MASTER).decode(),
            "CURATOR_DATA_DIR": str(tmp / "data"), "CURATOR_SCRATCH_DIR": str(tmp / "scratch"),
            "CURATOR_AUTH_MODE": "none", "CURATOR_LOCAL_DATA_ROOT": str(inputs),
            "CURATOR_LOCAL_DELIVERY_ROOT": str(tmp / "tos"), "CURATOR_VERIFY_VISIBILITY_S": "0",
            "CURATOR_MEMORY_ADMISSION": "0", "CURATOR_TERM_GRACE_S": "20",
            "CURATOR_LOG_FORMAT": "text", "PYTHONPATH": str(BACKEND)})
        self.log = open(tmp / log_name, "wb")
        self.proc = subprocess.Popen([sys.executable, "-m", "daemon", "--host", "127.0.0.1",
                                      "--port", str(self.port)], cwd=BACKEND, env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        self.http = httpx.Client(timeout=120)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if self.http.get(f"http://127.0.0.1:{self.port}/readyz").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if self.proc.poll() is not None:
                break
            time.sleep(0.2)
        raise AssertionError(f"the Daemon did not become ready (see {self.log.name})")

    def call(self, method: str, path: str, **kw):
        if method != "GET":
            kw.setdefault("headers", JSON)
        return self.http.request(method, self.base + path, **kw)

    def task(self, task_id: str) -> dict:
        return self.call("GET", f"/tasks/{task_id}").json()

    def wait(self, task_id: str, predicate, timeout: float = 240.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            t = self.task(task_id)
            if predicate(t):
                return t
            time.sleep(0.2)
        raise AssertionError(f"{task_id}: {json.dumps(self.task(task_id))[:1500]}")

    def create(self, dataset: str) -> str:
        pf = self.call("POST", "/preflight", json={"input": {"source": "local", "uri": dataset},
                                                   "vlm_backend": "fake"}).json()
        r = self.call("POST", "/tasks", json={
            "name": "crash drill", "input": {"source": "local", "uri": dataset},
            "output": {"uri": "tos://deliveries/mini", "credential": "out-key"},
            "preflight_id": pf["preflight_id"], "episodes": {"mode": "all"},
            "modules": ["timestamp_check", "kinematic_limits", "motion_quality",
                        "visual_quality", "video_action_sync", "task_success", "dedup",
                        "skill_profile"],
            "vlm": {"backend": "fake", "model": "fake-vlm"},
            "params": {"vlm_hedge": False}})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=30)
        self.http.close()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=150)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.http.close()
        self.log.close()


def _stage(task: dict, stage: str) -> dict:
    return {s["id"]: s for s in task["progress"]["stages"]}.get(stage, {})


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_a_killed_daemon_recovers_its_task_and_keeps_user_intent(tmp_path, mini_source, fake_vlm):
    inputs = tmp_path / "inputs"
    shutil.copytree(mini_source, inputs / "mini")
    dataset = str(inputs / "mini")
    _prepare_volume(tmp_path / "data", fake_vlm.url)
    first = DaemonProcess(tmp_path, inputs, "daemon-1.log")
    try:
        fake_vlm.delay_s = 0.15
        paused = first.create(dataset)
        first.wait(paused, lambda t: t["state"] == "running")
        assert first.call("POST", f"/tasks/{paused}/actions/pause").status_code == 200
        first.wait(paused, lambda t: t["state"] == "paused")
        stopped = first.create(dataset)
        first.wait(stopped, lambda t: t["state"] == "running")
        assert first.call("POST", f"/tasks/{stopped}/actions/stop").status_code == 200
        first.wait(stopped, lambda t: t["state"] == "stopped")
        victim = first.create(dataset)
        run_dir = tmp_path / "data" / "runs" / victim
        part = run_dir / "checks" / "task_success" / "parts" / "0001.jsonl"
        first.wait(victim, lambda t: _stage(t, "vlm").get("state") == "running"
                   and part.is_file() and part.read_text().count("\n") >= 1)
        child = json.loads((run_dir / ".orchestr" / "proc.json").read_text())["pid"]
    finally:
        first.kill()                                    # no shutdown hooks, no transitions
    assert _alive(child), "the CLI child should outlive a killed Daemon (its own session)"

    fake_vlm.delay_s = 0.0
    second = DaemonProcess(tmp_path, inputs, "daemon-2.log")
    try:
        assert not _alive(child) or second.wait(victim, lambda t: not _alive(child), 60)
        done = second.wait(victim, lambda t: t["state"] in TERMINAL and not t["active_subtask"])
        assert done["state"] == "succeeded", json.dumps(done)[:2000]
        assert done["summary"]["total"] == 8 and done["summary"]["passed"] == 5
        kinds = [e["kind"] for e in second.call("GET", f"/tasks/{victim}/timeline").json()["items"]]
        assert "system_pause" in kinds and "system_resume" in kinds
        assert second.task(paused)["state"] == "paused"
        assert second.task(paused)["pause_reason"] == "user"
        assert second.task(stopped)["state"] == "stopped"
        system = read_jsonl(str(run_dir / "logs" / "system.jsonl"))
        assert any("daemon restarted" in line["msg"] for line in system)
    finally:
        second.stop()


def test_a_graceful_shutdown_that_times_out_pauses_and_the_next_start_resumes(
        tmp_path, mini_source, fake_vlm, monkeypatch, clean_env):
    from fastapi.testclient import TestClient

    from daemon.app import create_app
    from daemon.util import now_ms

    from .conftest import Daemon, _seal_keys
    from ..secrets.fakes import AK, SK, FakeTos

    inputs = tmp_path / "inputs"
    shutil.copytree(mini_source, inputs / "mini")
    monkeypatch.setenv("CURATOR_LOCAL_DELIVERY_ROOT", str(tmp_path / "tos"))
    monkeypatch.setenv("CURATOR_VERIFY_VISIBILITY_S", "0")
    monkeypatch.setenv("CURATOR_MEMORY_ADMISSION", "0")
    monkeypatch.setenv("CURATOR_TERM_GRACE_S", "1")         # the episode in flight takes longer
    settings = make_settings(tmp_path / "vol", local_data_root=inputs)

    c1 = TestClient(create_app(settings, clock=now_ms), raise_server_exceptions=False)
    c1.__enter__()
    fake = FakeTos()
    fake.add_key(AK, SK, read={"datasets"})
    fake.add_key(AK2, SK2, read={"deliveries"}, write={"deliveries"})
    _seal_keys(c1, fake)
    r = c1.post("/api/v1/vlm-backends", headers=JSON,
                json={"name": "fake", "kind": "custom", "endpoint": fake_vlm.url})
    assert r.status_code == 201
    d = Daemon(c1, str(inputs), str(tmp_path / "tos"), str(inputs / "mini"), fake_vlm.url)
    fake_vlm.delay_s = 0.6
    task_id = d.create()["id"]
    part = pathlib.Path(d.run_dir(task_id)) / "checks" / "task_success" / "parts" / "0001.jsonl"
    d.wait_for(lambda: _stage(d.get(task_id), "vlm").get("state") == "running"
               and part.is_file() and part.read_text().count("\n") >= 1, timeout=180,
               what="the VLM stage to write a result")
    c1.__exit__(None, None, None)                       # SIGTERM -> 1 s -> SIGKILL

    fake_vlm.delay_s = 0.0
    c2 = TestClient(create_app(settings, clock=now_ms), raise_server_exceptions=False)
    c2.__enter__()
    try:
        from daemon.secrets import service_of

        service_of(c2.app.state.runtime).tos_factory = fake.factory
        d2 = Daemon(c2, str(inputs), str(tmp_path / "tos"), str(inputs / "mini"), fake_vlm.url)
        done = d2.wait(task_id)
        assert done["state"] == "succeeded", json.dumps(done)[:2000]
        timeline = d2.api("GET", f"/tasks/{task_id}/timeline").json()["items"]
        kinds = [e["kind"] for e in timeline]
        assert "system_pause" in kinds and "system_resume" in kinds
        assert "failed" not in kinds
        pause = next(e for e in timeline if e["kind"] == "system_pause")
        assert pause["text"] == "任务被系统暂停：Daemon 停机，将自动恢复"
    finally:
        c2.__exit__(None, None, None)
