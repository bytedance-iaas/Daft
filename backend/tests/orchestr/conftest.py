"""Fixtures for W5a: a Daemon that really runs the CLI on the synthetic LeRobot fixture.

``daemon()`` starts an app (W4's fixtures, imported) whose input is a local copy of
``tools/parity``'s 8-episode dataset under the local data root, whose delivery is the
experimental local stand-in (``CURATOR_LOCAL_DELIVERY_ROOT``) and whose model is the
CLI tests' fake OpenAI-compatible server. Access keys are sealed for real (W8) against
a fake TOS, so the whole start procedure - prechecks, D37, freezing - runs as in
production. Tests marked ``slow`` run CLI subprocesses end to end.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field

import pytest

from ..daemon.conftest import (  # noqa: F401 - fixtures are registered by importing them
    T0,
    FakeClock,
    assert_error,
    assert_schema,
    clean_env,
    client_for,
    clock,
    make_app,
    make_settings,
    seed_dataset,
    seed_task,
)
from ..secrets.fakes import AK, AK2, SK, SK2, FakeTos

JSON = {"Content-Type": "application/json"}
API = "/api/v1"
ALL_MODULES = ["timestamp_check", "kinematic_limits", "motion_quality", "visual_quality",
               "video_action_sync", "task_success", "dedup", "skill_profile"]
TERMINAL = ("succeeded", "completed_with_errors", "failed", "stopped")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: runs the real CLI end to end (tens of seconds)")


@pytest.fixture(scope="session")
def mini_source(tmp_path_factory) -> str:
    """tools/parity's 8-episode LeRobot v2.1 dataset (franka, two cameras)."""
    from parity.fixtures import make_mini_lerobot

    return make_mini_lerobot(str(tmp_path_factory.mktemp("mini-src") / "mini"))


@pytest.fixture
def fake_vlm():
    from ..cli.fakevlm_server import FakeVlmServer

    with FakeVlmServer() as vlm:
        yield vlm


@dataclass
class Daemon:
    client: object
    root: str                         # local data root (inputs)
    delivery_root: str                # the local stand-in of TOS
    dataset: str                      # the input dataset (a copy per test)
    vlm_url: str
    names: dict = field(default_factory=dict)

    @property
    def rt(self):
        return self.client.app.state.runtime

    @property
    def orch(self):
        return self.rt.orchestrator

    def api(self, method: str, path: str, **kw):
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            kw.setdefault("headers", JSON)
        return self.client.request(method, API + path, **kw)

    def preflight(self, **extra) -> dict:
        r = self.api("POST", "/preflight", json={"input": {"source": "local", "uri": self.dataset},
                                                 "vlm_backend": "fake", **extra})
        assert r.status_code == 200, r.text
        assert_schema("PreflightResponse", r.json())
        return r.json()

    def task_body(self, *, modules=None, start_now=True, episodes=None, export=True, **extra):
        pf = self.preflight()
        body = {"name": "fixture 8 episodes", "input": {"source": "local", "uri": self.dataset},
                "output": {"uri": "tos://deliveries/mini", "credential": "out-key"},
                "preflight_id": pf["preflight_id"], "episodes": episodes or {"mode": "all"},
                "modules": modules or list(ALL_MODULES),
                "vlm": {"backend": "fake", "model": "fake-vlm"},
                "params": {"start_now": start_now, "export": export, "vlm_hedge": False}}
        body.update(extra)
        return body

    def create(self, **kw) -> dict:
        r = self.api("POST", "/tasks", json=self.task_body(**kw))
        assert r.status_code == 201, r.text
        assert_schema("TaskCreated", r.json())
        return r.json()

    def get(self, task_id: str) -> dict:
        r = self.api("GET", f"/tasks/{task_id}")
        assert r.status_code == 200, r.text
        assert_schema("Task", r.json())
        return r.json()

    def action(self, task_id: str, action: str):
        return self.api("POST", f"/tasks/{task_id}/actions/{action}")

    def wait(self, task_id: str, states=TERMINAL, *, timeout: float = 240.0,
             subtask_done: bool = True) -> dict:
        deadline = time.monotonic() + timeout
        body = None
        while time.monotonic() < deadline:
            body = self.get(task_id)
            idle = body["active_subtask"] is None or not subtask_done
            if body["state"] in states and idle:
                return body
            time.sleep(0.2)
        raise AssertionError(f"task {task_id} did not reach {states}: {json.dumps(body)[:2000]}")

    def wait_for(self, predicate, *, timeout: float = 120.0, what: str = "a condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    def run_dir(self, task_id: str) -> str:
        return os.path.join(self.rt.settings.work_dir, task_id)

    def delivery(self, run_id: str = "") -> str:
        base = os.path.join(self.delivery_root, "deliveries", "mini")
        return os.path.join(base, run_id) if run_id else base


def _seal_keys(client, fake_tos: FakeTos) -> None:
    from daemon.secrets import service_of

    svc = service_of(client.app.state.runtime)
    svc.tos_factory = fake_tos.factory
    for name, ak, sk in (("out-key", AK2, SK2), ("in-key", AK, SK)):
        r = client.post(f"{API}/credentials", headers=JSON, json={
            "name": name, "access_key_id": ak, "secret_access_key": sk, "region": "cn-beijing"})
        assert r.status_code == 201, r.text


@pytest.fixture
def daemon(client_for, tmp_path, mini_source, fake_vlm, monkeypatch):
    """``daemon(**settings)`` -> a started :class:`Daemon`; the environment may be tuned
    first with ``monkeypatch.setenv`` (CURATOR_MAX_RUNNING_TASKS, CURATOR_TERM_GRACE_S...)."""
    import pathlib

    from daemon.util import now_ms

    def build(*, vlm=None, **settings) -> Daemon:
        root = tmp_path / "inputs"
        dataset = root / "mini"
        if not dataset.exists():
            shutil.copytree(mini_source, dataset)
        delivery_root = tmp_path / "tos"
        monkeypatch.setenv("CURATOR_LOCAL_DELIVERY_ROOT", str(delivery_root))
        monkeypatch.setenv("CURATOR_VERIFY_VISIBILITY_S", "0")
        monkeypatch.setenv("CURATOR_MEMORY_ADMISSION", "0")
        settings.setdefault("local_data_root", pathlib.Path(root))
        c = client_for(clock_fn=now_ms, **settings)
        fake_tos = FakeTos()
        fake_tos.add_key(AK, SK, read={"datasets"}, write=set())
        fake_tos.add_key(AK2, SK2, read={"deliveries"}, write={"deliveries"})
        _seal_keys(c, fake_tos)
        url = (vlm or fake_vlm).url
        r = c.post(f"{API}/vlm-backends", headers=JSON,
                   json={"name": "fake", "kind": "custom", "endpoint": url})
        assert r.status_code == 201, r.text
        assert any(m["model_name"] == "fake-vlm" for m in r.json()["models"]), r.text
        return Daemon(c, str(root), str(delivery_root), str(dataset), url)

    return build


class _Listing:
    def __init__(self, contents, prefixes):
        self.contents = contents
        self.common_prefixes = prefixes
        self.is_truncated = False
        self.next_continuation_token = None


class _Obj:
    def __init__(self, key: str, size: int):
        self.key, self.size, self.etag = key, size, f'"{abs(hash(key)) % 10**8}"'


class _Prefix:
    def __init__(self, prefix: str):
        self.prefix = prefix


class ListingTos(FakeTos):
    """W8's fake TOS plus ``list_objects_type2`` (with ``delimiter``) and ``put_object_from_file``."""

    def factory(self, endpoint, region, key):
        client = super().factory(endpoint, region, key)
        tos = self

        def list_objects_type2(bucket, prefix="", delimiter=None, continuation_token=None,
                               max_keys=1000):
            ak = client._call("list_objects", bucket, prefix)
            client._bucket(bucket)
            client._allowed(ak, bucket, "read")
            keys = sorted(k for (b, k) in tos.objects if b == bucket and k.startswith(prefix))
            if delimiter:
                subs, files = set(), []
                for k in keys:
                    rest = k[len(prefix):]
                    if delimiter in rest:
                        subs.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
                    else:
                        files.append(k)
                return _Listing([_Obj(k, len(tos.objects[(bucket, k)])) for k in files],
                                [_Prefix(p) for p in sorted(subs)])
            return _Listing([_Obj(k, len(tos.objects[(bucket, k)])) for k in keys[:max_keys]], [])

        def put_object_from_file(bucket, key, path):
            with open(path, "rb") as fh:
                client.put_object(bucket, key, content=fh.read())

        client.list_objects_type2 = list_objects_type2
        client.put_object_from_file = put_object_from_file
        return client


def read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def results(run_dir: str, module: str) -> dict[int, dict]:
    return {r["episode_index"]: r
            for r in read_jsonl(os.path.join(run_dir, "checks", module, "results.jsonl"))}


def comparable(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k not in ("elapsed_s", "evidence")}
