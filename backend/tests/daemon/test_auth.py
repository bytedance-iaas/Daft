"""Basic auth, ported from v1 ``curation/ui/auth.py`` and its retired tests (docs/v1/retired-ui-tests.md).

v1 cases kept: 401 then 200 on every route, probes exempt, half-configured single
user = not enabled, htpasswd with several users, bcrypt ``$2y$``, htpasswd wins
over the single-user variables, fail closed on a bad file, WebSocket scopes are
covered too. New: the probes under the mount prefix, the JSON ``Error`` body of
a 401, ``CURATOR_AUTH_MODE`` pinning, ``CURATOR_*`` aliases.
"""
from __future__ import annotations

import asyncio

import pytest

from daemon import auth
from daemon.auth import AuthConfig, AuthMiddleware, build_provider

from .conftest import assert_error, seed_task

# Real lines from Apache's tools: `htpasswd -nbB alice pwd123`, `htpasswd -nbm bob pwd456`.
ALICE = "alice:$2y$05$ox42VAHHnYObmiyIbCQJY.zvrdFxTqE6Q/mmkLjQRdjFPLd937yke"
BOB = "bob:$apr1$48nU0unb$/IN1/8ZMcjP8/o5Jzgemq."
BASE = "/curation"


def _htpasswd(tmp_path, *lines) -> str:
    f = tmp_path / "htpasswd"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(f)


def _protected_paths(task_id):
    return [f"{BASE}/api/v1/tasks", f"{BASE}/api/v1/modules", f"{BASE}/api/v1/tasks/{task_id}",
            f"{BASE}/events/tasks/{task_id}", f"{BASE}/", f"{BASE}/tasks/new",
            f"{BASE}/?dataset=tos://b/x", f"{BASE}/api/v1/unknown"]


def test_apr1_matches_openssl():
    # `openssl passwd -apr1 -salt saltsalt pwd123` and `-salt 'x.y/z12' 'p@ss w0rd'`
    assert auth.apr1("pwd123", "saltsalt") == "$apr1$saltsalt$owCIubpwdHE3knGL/4Yl51"
    assert auth.apr1("p@ss w0rd", "x.y/z12") == "$apr1$x.y/z12$LBi4lWN8fsp8TmZOjtjPY1"


def test_single_user_401_then_200(client_for):
    c = client_for(base_path=BASE, auth=AuthConfig(user="demo", password="s3cret"))
    t = seed_task(c.app.state.runtime.repo)
    for path in _protected_paths(t.id):
        r = c.get(path, follow_redirects=False)
        body = assert_error(r, "unauthorized")
        assert r.headers["www-authenticate"] == 'Basic realm="Robot Data Curation"'
        assert "登录" in body["error"]["message"]
    for path in ("/healthz", "/readyz", f"{BASE}/healthz", f"{BASE}/readyz"):
        assert c.get(path).status_code == 200, path                   # probes never 401
    assert c.get(f"{BASE}/api/v1/tasks", auth=("demo", "wrong")).status_code == 401
    assert c.get(f"{BASE}/api/v1/tasks", auth=("nobody", "s3cret")).status_code == 401
    assert c.get(f"{BASE}/api/v1/tasks", auth=("demo", "s3cret")).status_code == 200
    r = c.get(f"{BASE}/?dataset=tos://b/x", auth=("demo", "s3cret"), follow_redirects=False)
    assert r.status_code == 302


def test_half_configured_single_user_is_not_enabled(client_for):
    c = client_for(auth=AuthConfig(user="demo"))
    assert c.get("/api/v1/tasks").status_code == 200


def test_htpasswd_multiuser_bcrypt_and_apr1(client_for, tmp_path):
    c = client_for(base_path=BASE, auth=AuthConfig(htpasswd_file=_htpasswd(
        tmp_path, "# accounts shared with the rerun viewer", ALICE, BOB, "broken line",
        "carol:{SHA}unsupported=")))
    url = f"{BASE}/api/v1/tasks"
    assert c.get(url).status_code == 401
    assert c.get(f"{BASE}/healthz").status_code == 200
    assert c.get(url, auth=("alice", "pwd123")).status_code == 200      # bcrypt $2y$
    assert c.get(url, auth=("bob", "pwd456")).status_code == 200        # apr1
    assert c.get(url, auth=("alice", "pwd456")).status_code == 401      # crossed passwords
    assert c.get(url, auth=("carol", "anything")).status_code == 401    # unsupported hash skipped
    assert c.get(url, auth=("dave", "pwd123")).status_code == 401       # not in the table


def test_bcrypt_2b_hashes_work_too(client_for, tmp_path):
    bcrypt = pytest.importorskip("bcrypt")
    hashed = bcrypt.hashpw(b"pwd123", bcrypt.gensalt(rounds=4)).decode()
    assert hashed.startswith("$2b$")
    c = client_for(auth=AuthConfig(htpasswd_file=_htpasswd(tmp_path, f"erin:{hashed}")))
    assert c.get("/api/v1/tasks", auth=("erin", "pwd123")).status_code == 200
    assert c.get("/api/v1/tasks", auth=("erin", "nope")).status_code == 401


def test_htpasswd_wins_over_single_user(client_for, tmp_path):
    c = client_for(auth=AuthConfig(htpasswd_file=_htpasswd(tmp_path, BOB), user="demo",
                                   password="s3cret"))
    assert c.get("/api/v1/tasks", auth=("demo", "s3cret")).status_code == 401
    assert c.get("/api/v1/tasks", auth=("bob", "pwd456")).status_code == 200


@pytest.mark.parametrize("config", [
    AuthConfig(htpasswd_file="/no/such/file"),                   # configured but unreadable
    AuthConfig(mode="htpasswd"),                                  # pinned, file not configured
    AuthConfig(mode="basic", user="demo"),                        # pinned, half configured
])
def test_fail_closed(client_for, tmp_path, config):
    c = client_for(base_path=BASE, auth=config)
    assert c.get(f"{BASE}/api/v1/tasks").status_code == 401
    assert c.get(f"{BASE}/api/v1/tasks", auth=("demo", "s3cret")).status_code == 401
    assert c.get("/healthz").status_code == 200 and c.get(f"{BASE}/readyz").status_code == 200


def test_empty_htpasswd_file_fails_closed(client_for, tmp_path):
    c = client_for(auth=AuthConfig(htpasswd_file=_htpasswd(tmp_path, "# nobody yet")))
    assert c.get("/api/v1/tasks", auth=("bob", "pwd456")).status_code == 401


def test_mode_none_is_explicit(client_for):
    c = client_for(auth=AuthConfig(mode="none", user="demo", password="s3cret"))
    assert c.get("/api/v1/tasks").status_code == 200


def test_config_from_env_old_and_new_names(clean_env):
    clean_env.setenv("CURATION_UI_USER", "old-user")
    clean_env.setenv("CURATION_UI_PASSWORD", "old-pass")
    clean_env.setenv("CURATION_UI_HTPASSWD_FILE", "/old/htpasswd")
    cfg = AuthConfig.from_env()
    assert (cfg.user, cfg.password, cfg.htpasswd_file, cfg.mode) == \
        ("old-user", "old-pass", "/old/htpasswd", "")
    clean_env.setenv("CURATOR_AUTH_USER", "new-user")
    clean_env.setenv("CURATOR_HTPASSWD_FILE", "/new/htpasswd")
    clean_env.setenv("CURATOR_AUTH_MODE", "HTPASSWD")
    cfg = AuthConfig.from_env()
    assert (cfg.user, cfg.htpasswd_file, cfg.mode) == ("new-user", "/new/htpasswd", "htpasswd")
    assert "old-pass" not in repr(cfg)
    clean_env.setenv("CURATOR_AUTH_MODE", "ldap")
    from daemon.settings import ConfigError
    with pytest.raises(ConfigError):
        AuthConfig.from_env()


def test_websocket_scopes_are_covered():
    """A future WebSocket route must not slip past (v1: BaseHTTPMiddleware would miss it)."""
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["type"])

    mw = AuthMiddleware(app, build_provider(AuthConfig(user="demo", password="s3cret")),
                        exempt=frozenset({"/healthz"}))
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "websocket.connect"}

    asyncio.run(mw({"type": "websocket", "path": "/ws", "headers": []}, receive, send))
    assert reached == [] and sent == [{"type": "websocket.close", "code": 1008}]
    sent.clear()
    scope = {"type": "websocket", "path": "/ws", "headers": [],
             "extensions": {"websocket.http.response": {}}}
    asyncio.run(mw(scope, receive, send))
    assert sent[0]["type"] == "websocket.http.response.start" and sent[0]["status"] == 401
    ok = {"type": "websocket", "path": "/ws",
          "headers": [(b"authorization", b"Basic ZGVtbzpzM2NyZXQ=")]}
    asyncio.run(mw(ok, receive, send))
    assert reached == ["websocket"]


def test_audit_events_name_the_account(client_for, tmp_path):
    c = client_for(auth=AuthConfig(htpasswd_file=_htpasswd(tmp_path, BOB)))
    rt = c.app.state.runtime
    t = seed_task(rt.repo, state="created")
    assert c.delete(f"/api/v1/tasks/{t.id}", auth=("bob", "pwd456"),
                    headers={"Content-Type": "application/json"}).status_code == 204
    event = rt.repo.list_events(resource=t.id).items[0]
    assert (event.action, event.actor) == ("task.delete", "bob")


def test_malformed_authorization_headers(client_for):
    c = client_for(auth=AuthConfig(user="demo", password="s3cret"))
    for value in ("Bearer abc", "Basic !!!notbase64", "Basic ZGVtbw==", "basic"):
        assert c.get("/api/v1/tasks", headers={"Authorization": value}).status_code == 401, value
    assert c.get("/api/v1/tasks", headers={"Authorization": "basic ZGVtbzpzM2NyZXQ="}).status_code == 200


def test_verified_credentials_are_cached_without_the_password():
    calls = []

    def check(u, p):
        calls.append(u)
        return (u, p) == ("demo", "s3cret")

    provider = auth.BasicAuthProvider(check, description="test")
    scope = {"headers": [(b"authorization", b"Basic ZGVtbzpzM2NyZXQ=")]}
    assert provider.authenticate(scope).display_name == "demo"
    assert provider.authenticate(scope).display_name == "demo"
    assert calls == ["demo"]                                   # the second hit skipped bcrypt
    bad = {"headers": [(b"authorization", b"Basic ZGVtbzp3cm9uZw==")]}
    assert provider.authenticate(bad) is None and provider.authenticate(bad) is None
    assert calls == ["demo", "demo", "demo"]                   # failures are never cached
    assert not any(b"s3cret" in k for k in provider._cache._entries)
