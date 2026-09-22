"""The real Daemon, started the way the chart starts it (design doc 09 §2.2-2.4).

``python -m daemon`` runs with the environment the chart renders for the container: its
volumes become local directories (the ConfigMap's files and an htpasswd table written into
them), its secret references get test values. Then the kubelet's view: the probes the
StatefulSet declares answer without credentials, everything else under the mount prefix
asks for them, and SIGTERM ends the process well inside the grace period. Needs helm.
"""
from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from daemon.auth import REALM, apr1

from .support import (BACKEND, container, daemon_environ, dummy_master_key, mounts, only,
                      pod_volumes, render)

VALUES = {"server": {"basePath": "/curation"},
          "reasoningEffortTable": [{"prefix": "glm-4.5", "levels": ["low", "medium", "high"]}]}
USER, PASSWORD = "alice", "wonderland-test-only"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str, auth: tuple[str, str] | None = None) -> tuple[int, dict, str]:
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(":".join(auth).encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read().decode()


def _local(path: str, where: dict[str, str]) -> str:
    """A path inside the pod -> the directory standing in for its volume here."""
    for mount_path in sorted(where, key=len, reverse=True):
        if path == mount_path or path.startswith(mount_path + "/"):
            return where[mount_path] + path[len(mount_path):]
    return path


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_the_daemon_as_the_chart_configures_it(tmp_path):
    docs = render(VALUES)
    sts = only(docs, "StatefulSet")
    c = container(docs)

    # the pod's volumes, on local directories
    where = {}
    for name, mount in mounts(docs).items():
        local = tmp_path / name
        local.mkdir()
        where[mount["mountPath"]] = str(local)
    site_dir = where[mounts(docs)["site-config"]["mountPath"]]
    for fname, content in only(docs, "ConfigMap")["data"].items():
        with open(os.path.join(site_dir, fname), "w", encoding="utf-8") as fh:
            fh.write(content)
    [item] = pod_volumes(docs)["auth"]["secret"]["items"]
    auth_dir = where[mounts(docs)["auth"]["mountPath"]]
    with open(os.path.join(auth_dir, item["path"]), "w", encoding="utf-8") as fh:
        fh.write(f"{USER}:{apr1(PASSWORD, 'w11salt0')}\n")

    master_key = dummy_master_key()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CURATOR_", "CURATION_")) and k != "TOS_ENDPOINT"}
    for name, value in daemon_environ(docs, {"curator-master-key/masterKey": master_key}).items():
        env[name] = _local(value, where) if value.startswith("/") else value
    port = _free_port()             # the chart's 8080 may be taken on this machine
    env.update({"CURATOR_HOST": "127.0.0.1", "CURATOR_PORT": str(port)})

    proc = subprocess.Popen([sys.executable, "-m", "daemon"], cwd=BACKEND, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if _get(base + "/healthz")[0] == 200:
                    break
            except OSError:
                pass
            assert proc.poll() is None, proc.stdout.read()
            assert time.monotonic() < deadline, "the Daemon did not come up"
            time.sleep(0.1)

        # the kubelet's probes, exactly as declared, without credentials
        for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
            status, _, body = _get(base + c[probe]["httpGet"]["path"])
            assert status == 200, (probe, body)
        status, _, body = _get(base + c["readinessProbe"]["httpGet"]["path"])
        assert json.loads(body)["status"] == "ok" and all(json.loads(body)["checks"].values())
        # the gateway's health checks under the prefix
        for path in ("/curation/healthz", "/curation/readyz"):
            assert _get(base + path)[0] == 200, path

        # everything else under the prefix needs an account from the mounted table
        status, headers, _ = _get(base + "/curation/api/v1/tasks")
        assert status == 401
        assert headers.get("www-authenticate", headers.get("WWW-Authenticate")) == f'Basic realm="{REALM}"'
        assert _get(base + "/curation/api/v1/tasks", (USER, "wrong"))[0] == 401
        status, _, body = _get(base + "/curation/api/v1/tasks", (USER, PASSWORD))
        assert status == 200 and json.loads(body)["total"] == 0
        # the routes live under the prefix, not at the root (the gateway does not strip it)
        assert _get(base + "/api/v1/tasks", (USER, PASSWORD))[0] == 404
        # the secrets service loads the reasoning-effort table from the mounted ConfigMap
        assert _get(base + "/curation/api/v1/vlm-backends", (USER, PASSWORD))[0] == 200

        # SIGTERM (what tini forwards) ends it well inside grace period minus preStop
        budget = sts["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] - int(
            c["lifecycle"]["preStop"]["exec"]["command"][1])
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=budget) in (0, -signal.SIGTERM)
        assert time.monotonic() - started < 30
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    output = proc.stdout.read()
    assert master_key not in output and PASSWORD not in output
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    assert records, "the chart asks for JSON logs"
    messages = " ".join(r.get("msg", "") for r in records)
    assert "authentication: htpasswd, 1 account(s)" in messages
    assert "CURATOR_REASONING_EFFORT_TABLE ignored" not in messages
