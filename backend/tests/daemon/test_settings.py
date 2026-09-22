"""Configuration: mount prefix, paths, and the master key that must be there (fail closed)."""
from __future__ import annotations

import base64
import os
import pathlib
import subprocess
import sys

import pytest

from daemon import masterkey
from daemon.app import create_app
from daemon.masterkey import MasterKey, MasterKeyError
from daemon.settings import ConfigError, Settings, normalize_base_path, parse_tz_offset

BACKEND = pathlib.Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("raw, want", [
    ("", ""), (None, ""), ("/", ""), ("curation", "/curation"), ("/curation/", "/curation"),
    ("  /curation ", "/curation"), ("a/b", "/a/b")])
def test_base_path_normalization(raw, want):
    assert normalize_base_path(raw) == want


@pytest.mark.parametrize("raw", ["../x", "/a//b", "/a b", "/a?x", "/a#b", "/./x", "/%2e"])
def test_bad_base_paths(raw):
    with pytest.raises(ConfigError):
        normalize_base_path(raw)


def test_from_env(clean_env, tmp_path, master_key_b64):
    clean_env.setenv("CURATOR_MASTER_KEY", master_key_b64)
    clean_env.setenv("CURATION_UI_ROOT_PATH", "curation/")          # the old name still works
    clean_env.setenv("CURATOR_DATA_DIR", str(tmp_path))
    clean_env.setenv("CURATOR_PUBLIC_BASE_URL", "https://kit.example.com/")
    clean_env.setenv("CURATOR_PORT", "9000")
    s = Settings.from_env()
    assert s.base_path == "/curation" and s.port == 9000
    assert (s.db_path, s.work_dir) == (tmp_path / "curator.db", tmp_path / "runs")
    assert s.public_base_url == "https://kit.example.com"
    assert s.master_key.key == bytes(range(32)) and s.master_key.version == 1
    clean_env.setenv("CURATOR_BASE_PATH", "/other")                  # the new name wins
    assert Settings.from_env().base_path == "/other"


@pytest.mark.parametrize("name, value", [
    ("CURATOR_PORT", "http"), ("CURATOR_SSE_HEARTBEAT_S", "0"), ("CURATOR_LOG_FORMAT", "xml"),
    ("CURATOR_PUBLIC_BASE_URL", "kit.example.com"), ("CURATOR_AUTH_MODE", "ldap")])
def test_config_errors(clean_env, master_key_b64, name, value):
    clean_env.setenv("CURATOR_MASTER_KEY", master_key_b64)
    clean_env.setenv(name, value)
    with pytest.raises(ConfigError):
        Settings.from_env()


@pytest.mark.parametrize("raw, minutes", [
    ("+08:00", 480), ("+8", 480), ("-05:30", -330), ("+0545", 345), ("Z", 0), ("utc", 0),
    ("-00:00", 0), ("+14:00", 840), ("-12:00", -720)])
def test_tz_offset(raw, minutes):
    assert parse_tz_offset(raw) == minutes


@pytest.mark.parametrize("raw", ["8", "+15:00", "-12:30", "+08:60", "Asia/Shanghai", "", "+",
                                 "+05:07"])
def test_bad_tz_offsets(raw):
    with pytest.raises(ConfigError, match="CURATOR_TZ_OFFSET"):
        parse_tz_offset(raw)


def test_tz_offset_from_env(clean_env, master_key_b64):
    clean_env.setenv("CURATOR_MASTER_KEY", master_key_b64)
    assert Settings.from_env().tz_offset_minutes == 480                 # China, no DST
    clean_env.setenv("CURATOR_TZ_OFFSET", "")                           # an empty Helm value
    assert Settings.from_env().tz_offset_minutes == 480
    clean_env.setenv("CURATOR_TZ_OFFSET", "-03:00")
    assert Settings.from_env().tz_offset_minutes == -180
    clean_env.setenv("CURATOR_TZ_OFFSET", "Beijing")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_master_key_is_required_and_validated(master_key_b64):
    with pytest.raises(MasterKeyError, match="拒绝启动"):
        masterkey.load({})
    for bad in ("", "   ", "not base64!", base64.b64encode(b"short").decode(),
                base64.b64encode(bytes(33)).decode()):
        with pytest.raises(MasterKeyError) as err:
            masterkey.load({"CURATOR_MASTER_KEY": bad})
        assert bad.strip() == "" or bad not in str(err.value)            # never echo the value
    urlsafe = base64.urlsafe_b64encode(bytes([251] * 32)).decode().rstrip("=")
    assert masterkey.load({"CURATOR_MASTER_KEY": urlsafe}).key == bytes([251] * 32)
    with pytest.raises(MasterKeyError):
        masterkey.load({"CURATOR_MASTER_KEY": master_key_b64, "CURATOR_MASTER_KEY_VERSION": "0"})
    with pytest.raises(MasterKeyError, match="相同"):
        masterkey.load({"CURATOR_MASTER_KEY": master_key_b64, "CURATOR_MASTER_KEY_NEXT": master_key_b64})
    nxt = base64.b64encode(bytes(32)).decode()
    key = masterkey.load({"CURATOR_MASTER_KEY": master_key_b64, "CURATOR_MASTER_KEY_NEXT": nxt,
                          "CURATOR_MASTER_KEY_VERSION": "3"})
    assert (key.version, key.next_key) == (3, bytes(32))
    assert master_key_b64 not in repr(key) and "***" in repr(key)
    assert len(key.fingerprint()) == 12 and key.fingerprint() != master_key_b64[:12]


def test_create_app_refuses_without_a_master_key(tmp_path):
    settings = Settings.__new__(Settings)
    object.__setattr__(settings, "master_key", None)
    with pytest.raises(MasterKeyError):
        create_app(settings)


def test_scrub_environment():
    from daemon.settings import scrub_secrets

    env = {"CURATOR_MASTER_KEY": "x", "CURATOR_MASTER_KEY_NEXT": "y", "PATH": "/bin"}
    masterkey.scrub_environment(env)
    assert env == {"PATH": "/bin"}
    env = {"CURATOR_MASTER_KEY": "x", "CURATOR_AUTH_PASSWORD": "p", "CURATION_UI_PASSWORD": "q",
           "CURATOR_AUTH_USER": "demo", "PATH": "/bin"}
    scrub_secrets(env)                                  # CLI children inherit no password either
    assert env == {"CURATOR_AUTH_USER": "demo", "PATH": "/bin"}


def _run_main(env, *args, timeout=60):
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("CURATOR_", "CURATION_"))}
    base.update(env)
    return subprocess.run([sys.executable, "-m", "daemon", *args], cwd=BACKEND, env=base,
                          capture_output=True, text=True, timeout=timeout)


def test_python_m_daemon_refuses_to_start_without_the_master_key(tmp_path):
    r = _run_main({"CURATOR_DATA_DIR": str(tmp_path)})
    assert r.returncode == 2
    assert "CURATOR_MASTER_KEY" in r.stderr and "拒绝启动" in r.stderr
    assert not (tmp_path / "curator.db").exists()                 # nothing was touched
    r = _run_main({"CURATOR_DATA_DIR": str(tmp_path), "CURATOR_MASTER_KEY": "dG9vIHNob3J0"})
    assert r.returncode == 2 and "字节" in r.stderr and "dG9vIHNob3J0" not in r.stderr


def test_python_m_daemon_check_config(tmp_path, master_key_b64):
    r = _run_main({"CURATOR_DATA_DIR": str(tmp_path), "CURATOR_MASTER_KEY": master_key_b64,
                   "CURATOR_LOG_FORMAT": "json"}, "--check-config")
    assert r.returncode == 0, r.stderr
    assert '"msg": "configuration ok' in r.stdout
