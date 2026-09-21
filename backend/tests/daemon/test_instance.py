"""One Daemon per data volume: a second one on the same database refuses to start."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from daemon.app import create_app
from daemon.instance import AlreadyRunning, InstanceLock

from .conftest import make_settings


def test_second_daemon_on_the_same_volume_is_refused(tmp_path, clean_env):
    settings = make_settings(tmp_path)
    first = create_app(settings)
    with TestClient(first) as c:
        assert c.get("/healthz").status_code == 200
        with pytest.raises(AlreadyRunning, match="只能有一个写者"):
            create_app(settings, lock_wait_s=0.3)
    again = create_app(settings, lock_wait_s=0.3)            # the lock went with the shutdown
    with TestClient(again) as c:
        assert c.get("/readyz").status_code == 200


def test_lock_file_records_the_owner(tmp_path):
    lock = InstanceLock(tmp_path / "curator.db.lock").acquire(wait_s=0)
    try:
        assert (tmp_path / "curator.db.lock").read_text().strip().isdigit()
        with pytest.raises(AlreadyRunning):
            InstanceLock(tmp_path / "curator.db.lock").acquire(wait_s=0)
    finally:
        lock.release()
    InstanceLock(tmp_path / "curator.db.lock").acquire(wait_s=0).release()
