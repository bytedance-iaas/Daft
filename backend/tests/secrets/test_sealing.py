"""AES-GCM sealing, key versions, fingerprints and the resumable master-key rotation (08 §2)."""
from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys

import pytest

from daemon.masterkey import MasterKey
from daemon.repo import protocol as P
from daemon.repo.sqlite import SqliteRepository
from daemon.secrets.sealing import NONCE_BYTES, TAG_BYTES, RotationReport, Sealer, SealError, rotate

from .fakes import AK, SK

K1, K2, K3 = bytes(range(32)), bytes(range(1, 33)), bytes(range(2, 34))
BACKEND = pathlib.Path(__file__).resolve().parents[2]


def test_seal_and_open_round_trip_with_a_fresh_nonce_each_time():
    s = Sealer(MasterKey(K1))
    payload = {"access_key_id": AK, "secret_access_key": SK}
    a, va = s.seal("cred_1", payload)
    b, vb = s.seal("cred_1", payload)
    assert va == vb == 1
    assert a != b and a[:NONCE_BYTES] != b[:NONCE_BYTES]           # a random nonce per seal
    plain = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert len(a) == NONCE_BYTES + len(plain) + TAG_BYTES           # nonce || ciphertext || tag
    assert SK.encode() not in a and AK.encode() not in a
    assert s.open("cred_1", a, 1) == payload == s.open("cred_1", b, 1)


def test_a_sealed_payload_opens_only_on_its_own_row_with_its_own_key():
    s = Sealer(MasterKey(K1))
    blob, _ = s.seal("cred_1", {"api_key": SK})
    with pytest.raises(SealError) as moved:                         # copied onto another row
        s.open("cred_2", blob, 1)
    with pytest.raises(SealError):
        Sealer(MasterKey(K2)).open("cred_1", blob, 1)               # another master key
    tampered = bytearray(blob)
    tampered[-1] ^= 1
    with pytest.raises(SealError):
        s.open("cred_1", bytes(tampered), 1)
    with pytest.raises(SealError):
        s.open("cred_1", blob[:20], 1)                              # truncated
    with pytest.raises(SealError) as unknown:
        s.open("cred_1", blob, 7)
    assert "第 7 版" in unknown.value.message_zh
    for err in (moved.value, unknown.value):
        text = f"{err} {err.message_zh} {err!r}"
        assert SK not in text and K1.hex() not in text


def test_an_id_is_required_before_sealing():
    with pytest.raises(ValueError):
        Sealer(MasterKey(K1)).seal("", {"api_key": "x"})


def test_during_a_rotation_both_versions_open_and_new_rows_get_the_next_key():
    old = Sealer(MasterKey(K1, version=3))
    blob3, v3 = old.seal("cred_1", {"api_key": "a"})
    both = Sealer(MasterKey(K1, version=3, next_key=K2))
    assert both.versions == (3, 4) and both.seal_version == 4
    blob4, v4 = both.seal("cred_2", {"api_key": "b"})
    assert (v3, v4) == (3, 4)
    assert both.open("cred_1", blob3, 3) == {"api_key": "a"}
    assert both.open("cred_2", blob4, 4) == {"api_key": "b"}
    after = Sealer(MasterKey(K2, version=4))                         # the next key alone
    assert after.open("cred_2", blob4, 4) == {"api_key": "b"}


def test_fingerprints_are_keyed_and_stable():
    a, b = Sealer(MasterKey(K1)), Sealer(MasterKey(K2))
    assert a.fingerprint(SK) == a.fingerprint(SK) != a.fingerprint(SK + "x")
    assert a.fingerprint(SK) != b.fingerprint(SK)
    assert SK not in a.fingerprint(SK)


# ---------------------------------------------------------------------------
# rotation
# ---------------------------------------------------------------------------

def _seed(repo, sealer: Sealer, n: int) -> dict[str, dict]:
    out = {}
    for i in range(n):
        cred_id = f"cred_{i:03d}"
        payload = {"access_key_id": f"AK{i:04d}", "secret_access_key": f"SK-{i:04d}-secret"}
        blob, version = sealer.seal(cred_id, payload)
        repo.create_credential(P.Credential(id=cred_id, name=f"k{i}", kind="tos",
                                            payload_enc=blob, key_version=version,
                                            payload_meta={"region": "cn-beijing"}))
        out[cred_id] = payload
    return out


class _Crash(Exception):
    pass


def test_rotation_is_resumable_and_leaves_every_row_on_the_next_key(tmp_path):
    repo = SqliteRepository(tmp_path / "curator.db")
    try:
        payloads = _seed(repo, Sealer(MasterKey(K1)), 7)
        mk = MasterKey(K1, version=1, next_key=K2)
        done = []

        def crash_after_three(cred_id):
            done.append(cred_id)
            if len(done) == 3:
                raise _Crash()

        with pytest.raises(_Crash):                                  # killed halfway through
            rotate(repo, mk, batch=2, on_row=crash_after_three)
        assert len(repo.credentials_below_key_version(2, limit=100)) == 4
        report = rotate(repo, mk, batch=2)                           # run again: picks up the rest
        assert report == RotationReport(target_version=2, rotated=4, remaining=0, failed=())
        assert report.complete and report.to_json()["complete"] is True
        assert rotate(repo, mk).rotated == 0                         # nothing left to do
        after = Sealer(MasterKey(K2, version=2))
        for cred in repo.list_credentials():
            assert cred.key_version == 2
            assert after.open_credential(cred) == payloads[cred.id]
    finally:
        repo.close()


def test_rows_that_do_not_open_are_reported_not_retried_forever(tmp_path):
    repo = SqliteRepository(tmp_path / "curator.db")
    try:
        _seed(repo, Sealer(MasterKey(K1)), 3)
        stray, _ = Sealer(MasterKey(K3)).seal("cred_x", {"api_key": "q"})   # a foreign key
        repo.create_credential(P.Credential(id="cred_x", name="stray", kind="tos",
                                            payload_enc=stray, key_version=1,
                                            payload_meta={"region": "cn-beijing"}))
        report = rotate(repo, MasterKey(K1, next_key=K2), batch=1)
        assert report.failed == ("cred_x",) and report.rotated == 3 and report.remaining == 1
        assert not report.complete
    finally:
        repo.close()


def test_rotation_needs_the_next_key():
    with pytest.raises(ValueError):
        rotate(None, MasterKey(K1))


def _run_rotate(env_extra: dict, data_dir: pathlib.Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CURATOR_", "CURATION_"))}
    env.update({"PYTHONPATH": str(BACKEND), "CURATOR_DATA_DIR": str(data_dir), **env_extra})
    return subprocess.run([sys.executable, "-m", "daemon.secrets.rotate", "--batch", "2"],
                          cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120)


def test_the_rotate_command(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    repo = SqliteRepository(data / "curator.db")
    _seed(repo, Sealer(MasterKey(K1)), 3)
    repo.close()
    b64 = {k: base64.b64encode(v).decode() for k, v in (("k1", K1), ("k2", K2))}

    missing_next = _run_rotate({"CURATOR_MASTER_KEY": b64["k1"]}, data)
    assert missing_next.returncode == 2 and "CURATOR_MASTER_KEY_NEXT" in missing_next.stderr

    done = _run_rotate({"CURATOR_MASTER_KEY": b64["k1"], "CURATOR_MASTER_KEY_NEXT": b64["k2"]},
                       data)
    assert done.returncode == 0, done.stderr
    summary = json.loads(done.stdout)
    assert summary == {"target_version": 2, "rotated": 3, "remaining": 0, "failed": [],
                       "complete": True}
    for secret in (b64["k1"], b64["k2"], "SK-0000-secret"):
        assert secret not in done.stdout + done.stderr
    repo = SqliteRepository(data / "curator.db")
    try:
        assert {c.key_version for c in repo.list_credentials()} == {2}
    finally:
        repo.close()
