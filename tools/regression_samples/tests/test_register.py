"""register.py against a stub Daemon: the login, the C4 write headers, the request bodies, re-runs, mcap mappings."""
from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from regression_samples import register as R

AUTH = "Basic " + base64.b64encode(b"qa:secret").decode()


class Daemon:
    """Just enough of C4 for the script: credentials, browse, register (get or create), patch, mcap probe, mapping."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.datasets: dict[str, dict] = {}
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, doc):
                payload = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _handle(self, method):
                body = None
                if "Content-Length" in self.headers:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"null")
                daemon.calls.append((method, self.path, dict(self.headers), body))
                if self.headers.get("Authorization") != AUTH:
                    return self._send(401, {"error": {"code": "unauthorized", "message": "要登录"}})
                path = urllib.parse.urlsplit(self.path).path.removeprefix("/curation/api/v1")
                if method == "GET" and path == "/credentials":
                    return self._send(200, {"items": [{"name": "other", "is_default": False, "meta": {"region": "cn-beijing"}},
                                                      {"name": "anchor-key", "is_default": True, "meta": {"region": "cn-beijing"}}]})
                if method == "GET" and path == "/datasets/browse":
                    return self._send(200, {"items": [], "next_cursor": None})
                if method == "POST" and path == "/datasets":
                    uri = body["input"]["uri"].rstrip("/")
                    if uri in daemon.datasets:
                        return self._send(200, daemon.datasets[uri])
                    mcap = "mcap/" in uri
                    ds = {"id": f"ds-{'abcdefghi'[len(daemon.datasets)] * 9}", "name": body.get("name") or uri.rsplit("/", 1)[-1],
                          "note": body.get("note"), "format": "mcap" if mcap else "lerobot_v3", "episode_count": 3,
                          "robot_type": None, "listing": {"objects": 9, "bytes": 1, "digest": "x"},
                          "preflight": {"format": {"kind": "mcap" if mcap else "lerobot", "supported": True, "detail": ""},
                                        "validation": []},
                          "viz_mapping": {"state": "none", "version": None, "name": None} if mcap else None}
                    daemon.datasets[uri] = ds
                    return self._send(201, ds)
                if method == "PATCH" and path.startswith("/datasets/"):
                    ds = next(d for d in daemon.datasets.values() if d["id"] == path.split("/")[2])
                    ds.update(body)
                    return self._send(200, ds)
                if method == "POST" and path == "/viz/mcap-probe":
                    return self._send(200, {"draft": {"name": "UMI"}, "matched": {"name": "UMI 手持夹爪（内置）"}})
                if method == "PUT" and path.endswith("/mapping"):
                    ds = next(d for d in daemon.datasets.values() if d["id"] == path.split("/")[2])
                    ds["viz_mapping"] = {"state": "confirmed", "version": 1, "name": "UMI"}
                    return self._send(200, {"version": 1})
                return self._send(404, {"error": {"code": "not_found", "message": path}})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PATCH(self):
                self._handle("PATCH")

            def do_PUT(self):
                self._handle("PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.api = f"http://127.0.0.1:{self.server.server_address[1]}/curation/api/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def writes(self, method, suffix=""):
        return [c for c in self.calls if c[0] == method and urllib.parse.urlsplit(c[1]).path.endswith(suffix)]


@pytest.fixture()
def daemon():
    d = Daemon()
    yield d
    d.server.shutdown()


@pytest.fixture()
def picks(tmp_path):
    doc = {"region": "cn-beijing", "picks": [
        {"uri": "tos://curation-robo-anchor/anchor/v1/lerobot_v30/svla_so101_pickplace/", "name": "anchor-v1/svla", "note": "SO-101"},
        {"uri": "tos://curation-robo-anchor/anchor/v1/mcap/genrobot_clutter_tidyup_stage2/", "name": "anchor-v1/genrobot"}]}
    path = tmp_path / "picks.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


@pytest.fixture()
def login(monkeypatch):
    monkeypatch.setenv("CURATOR_USER", "qa")
    monkeypatch.setenv("CURATOR_PASSWORD", "secret")


def test_registers_with_the_default_key_and_confirms_mcap_mappings(daemon, picks, login, tmp_path, capsys):
    out = tmp_path / "registered.jsonl"
    assert R.main(["--api", daemon.api, "--picks", str(picks), "--out", str(out)]) == 0
    posts = daemon.writes("POST", "/datasets")
    assert [b["input"] for _, _, _, b in posts] == [
        {"source": "tos", "uri": "tos://curation-robo-anchor/anchor/v1/lerobot_v30/svla_so101_pickplace/",
         "region": "cn-beijing", "credential": "anchor-key"},
        {"source": "tos", "uri": "tos://curation-robo-anchor/anchor/v1/mcap/genrobot_clutter_tidyup_stage2/",
         "region": "cn-beijing", "credential": "anchor-key"}]
    assert posts[0][3]["name"] == "anchor-v1/svla" and posts[0][3]["note"] == "SO-101"
    for _, _, headers, _ in daemon.calls:            # every call logs in; every write is JSON with an idempotency key
        assert headers["Authorization"] == AUTH
    for method, _, headers, _ in daemon.calls:
        if method != "GET":
            assert headers["Content-Type"] == "application/json" and len(headers["Idempotency-Key"]) >= 8
    browse = [urllib.parse.parse_qs(urllib.parse.urlsplit(c[1]).query) for c in daemon.calls if "/datasets/browse" in c[1]]
    assert browse == [{"source": ["tos"], "uri": ["tos://curation-robo-anchor/anchor/v1/lerobot_v30/"],
                       "region": ["cn-beijing"], "credential": ["anchor-key"], "limit": ["1"]}]
    assert len(daemon.writes("POST", "/viz/mcap-probe")) == 1 and len(daemon.writes("PUT", "/mapping")) == 1
    recs = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [(r["ok"], r["created"], r["format"]) for r in recs] == [(True, True, "lerobot_v3"), (True, True, "mcap")]
    assert "secret" not in capsys.readouterr().out + out.read_text(encoding="utf-8")


def test_a_second_run_registers_nothing_new_and_patches_a_changed_note(daemon, picks, login, tmp_path):
    assert R.main(["--api", daemon.api, "--picks", str(picks)]) == 0
    doc = json.loads(picks.read_text(encoding="utf-8"))
    doc["picks"][0]["note"] = "SO-101, new note"
    picks.write_text(json.dumps(doc), encoding="utf-8")
    assert R.main(["--api", daemon.api, "--picks", str(picks), "--credential", "other"]) == 0
    assert len(daemon.datasets) == 2
    assert [b for _, _, _, b in daemon.writes("PATCH")] == [{"note": "SO-101, new note"}]
    assert len(daemon.writes("PUT", "/mapping")) == 1  # confirmed once; the second run sees state confirmed


def test_a_refused_login_or_an_unknown_key_stops_before_registering(daemon, picks, monkeypatch):
    monkeypatch.setenv("CURATOR_USER", "qa")
    monkeypatch.setenv("CURATOR_PASSWORD", "wrong")
    assert R.main(["--api", daemon.api, "--picks", str(picks)]) == 2
    monkeypatch.setenv("CURATOR_PASSWORD", "secret")
    assert R.main(["--api", daemon.api, "--picks", str(picks), "--credential", "nope"]) == 2
    assert daemon.writes("POST") == []


def test_dry_run_calls_nothing(picks, capsys):
    assert R.main(["--api", "http://127.0.0.1:9/curation/api/v1", "--picks", str(picks), "--dry-run"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["input"]["source"] for line in lines] == ["tos", "tos"]


def test_the_console_picks_file_is_well_formed():
    with open(R.__file__.replace("register.py", "console_picks.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    uris = [p["uri"] for p in doc["picks"]]
    assert len(uris) == len(set(uris)) == 20
    assert all(u.startswith("tos://curation-robo-anchor/anchor") for u in uris)
    assert all(0 < len(p["name"]) <= 128 and len(p["note"]) <= 2000 for p in doc["picks"])
