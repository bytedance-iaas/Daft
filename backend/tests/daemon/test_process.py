"""``python -m daemon`` as a real process: serves under the prefix, stops cleanly on SIGTERM."""
from __future__ import annotations

import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from daemon.logconfig import JsonFormatter, redact

BACKEND = pathlib.Path(__file__).resolve().parents[2]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url, auth=None):
    req = urllib.request.Request(url)
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_serves_under_the_prefix_and_stops_on_sigterm(tmp_path, master_key_b64):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CURATOR_", "CURATION_"))}
    env.update({"CURATOR_MASTER_KEY": master_key_b64, "CURATOR_DATA_DIR": str(tmp_path),
                "CURATOR_SCRATCH_DIR": str(tmp_path / "scratch"), "CURATOR_BASE_PATH": "/curation",
                "CURATOR_PORT": str(port), "CURATOR_HOST": "127.0.0.1",
                "CURATOR_AUTH_USER": "demo", "CURATOR_AUTH_PASSWORD": "s3cret"})
    proc = subprocess.Popen([sys.executable, "-m", "daemon"], cwd=BACKEND, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            try:
                status, _ = _get(base + "/healthz")
                if status == 200:
                    break
            except OSError:
                pass
            assert proc.poll() is None, proc.stdout.read()
            assert time.monotonic() < deadline, "daemon did not come up"
            time.sleep(0.1)
        status, body = _get(base + "/curation/readyz")
        assert status == 200 and json.loads(body)["status"] == "ok"
        assert _get(base + "/curation/api/v1/tasks")[0] == 401
        status, body = _get(base + "/curation/api/v1/tasks", auth="demo:s3cret")
        assert status == 200 and json.loads(body)["total"] == 0
        proc.send_signal(signal.SIGTERM)
        # uvicorn shuts down gracefully, then re-raises the signal it caught
        assert proc.wait(timeout=30) in (0, -signal.SIGTERM)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    output = proc.stdout.read()
    assert master_key_b64 not in output and "s3cret" not in output
    messages = [json.loads(line).get("msg", "") for line in output.splitlines()
                if line.startswith("{")]
    assert any(m.startswith("daemon ready to start") for m in messages)
    assert "Application shutdown complete." in messages, output


def test_secrets_are_masked_in_logs():
    assert redact("Authorization: Basic ZGVtbzpzM2NyZXQ=") == "Authorization: Basic ***"
    assert redact('{"secret_access_key": "AKxyz", "region": "cn"}') == \
        '{"secret_access_key": "***", "region": "cn"}'
    assert redact("ARK_API_KEY=abc123 TOS_SECRET_KEY=def password=hunter2") == \
        "ARK_API_KEY=*** TOS_SECRET_KEY=*** password=***"
    assert redact("nothing to hide here") == "nothing to hide here"
    assert redact("secret_key=abc access_key: def ak=g1 sk=h2") == \
        "secret_key=*** access_key: *** ak=*** sk=***"
    import logging
    try:
        raise RuntimeError("api_key=leak")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed with %s",
                                   ("password=hunter2",), sys.exc_info())
    line = JsonFormatter().format(record)
    assert "hunter2" not in line and "leak" not in line
    assert json.loads(line)["msg"] == "failed with password=***"
