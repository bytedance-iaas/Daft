"""Sealing stored secrets with the master key: AES-256-GCM (design doc 08, section 2).

``credential.payload_enc`` is ``nonce (12 bytes) || ciphertext || tag (16 bytes)`` with a
fresh random nonce per seal. The plaintext is the canonical JSON of the secret fields -
``{"access_key_id", "secret_access_key"[, "session_token"]}`` for a TOS key,
``{"api_key"}`` for a VLM backend. The row id is bound in as associated data, so a
ciphertext copied onto another row does not open there.

Every row records the master-key version that sealed it (``key_version``). The loaded
:class:`~daemon.masterkey.MasterKey` opens rows of its own version and, while a rotation is
in progress (``CURATOR_MASTER_KEY_NEXT`` set), rows of the next version too; new rows are
then sealed with the next key, so nothing written during the rotation window is left
behind once the next key becomes the only one.

Rotation (08 §2.1) is :func:`rotate`: re-seal every row below the next version, one row per
transaction, a batch at a time. It can stop anywhere and simply be run again; rows it cannot
open are reported, not retried forever. ``python -m daemon.secrets.rotate`` runs it in the
pod (``kubectl exec``), next to the running Daemon.
"""
from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..masterkey import MasterKey
from ..repo import protocol as P

NONCE_BYTES = 12
TAG_BYTES = 16
AAD_PREFIX = "curator/credential/v1:"
_FINGERPRINT_LABEL = b"curator/fingerprint/v1"


class SealError(RuntimeError):
    """A stored secret does not open: unknown key version, wrong master key or altered data.

    The message names the row and the versions only - never key material or plaintext.
    ``message_zh`` is what people see.
    """

    def __init__(self, message: str, message_zh: str):
        super().__init__(message)
        self.message_zh = message_zh


def _aad(cred_id: str) -> bytes:
    return (AAD_PREFIX + cred_id).encode("utf-8")


class Sealer:
    """Seal and open credential payloads; one per loaded master key."""

    def __init__(self, master_key: MasterKey):
        if not isinstance(master_key, MasterKey):
            raise TypeError("a Sealer needs the loaded MasterKey")
        self._aead: dict[int, AESGCM] = {master_key.version: AESGCM(master_key.key)}
        if master_key.next_key is not None:
            self._aead[master_key.version + 1] = AESGCM(master_key.next_key)
        #: the version new rows get (the next key's while a rotation is in progress)
        self.seal_version = max(self._aead)
        self.versions = tuple(sorted(self._aead))
        # a key of its own for fingerprints: never use the encryption key for two jobs
        self._fp_key = hmac.new(master_key.key, _FINGERPRINT_LABEL, "sha256").digest()

    def seal(self, cred_id: str, payload: Mapping[str, Any]) -> tuple[bytes, int]:
        """-> (``payload_enc``, ``key_version``) for the row ``cred_id``."""
        if not cred_id:
            raise ValueError("a sealed payload is bound to its row id; generate the id first")
        data = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
        nonce = os.urandom(NONCE_BYTES)
        blob = nonce + self._aead[self.seal_version].encrypt(nonce, data, _aad(cred_id))
        return blob, self.seal_version

    def open(self, cred_id: str, blob: bytes, version: int) -> dict:
        aead = self._aead.get(int(version))
        if aead is None:
            loaded = "、".join(str(v) for v in self.versions)
            raise SealError(
                f"credential {cred_id} is sealed with master key version {version}; "
                f"loaded versions: {list(self.versions)}",
                f"这条密钥是用第 {version} 版主密钥加密的，当前加载的是第 {loaded} 版："
                "主密钥轮换没有做完，或者 CURATOR_MASTER_KEY_VERSION 配错了")
        raw = bytes(blob or b"")
        if len(raw) < NONCE_BYTES + TAG_BYTES:
            raise SealError(f"credential {cred_id}: sealed payload is truncated",
                            "保存的密钥数据不完整，无法解密，请重新填写这条密钥")
        try:
            data = aead.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], _aad(cred_id))
        except InvalidTag:
            raise SealError(
                f"credential {cred_id} does not open with master key version {version} "
                "(wrong master key, or the stored data was altered)",
                "保存的密钥解不开：当前的主密钥不是加密时用的那一把，或者数据被改动过") from None
        try:
            value = json.loads(data.decode("utf-8"))
        except ValueError:
            raise SealError(f"credential {cred_id}: sealed payload is not JSON",
                            "保存的密钥数据格式不对，请重新填写这条密钥") from None
        if not isinstance(value, dict):
            raise SealError(f"credential {cred_id}: sealed payload is not an object",
                            "保存的密钥数据格式不对，请重新填写这条密钥")
        return value

    def open_credential(self, cred: P.Credential) -> dict:
        return self.open(cred.id, cred.payload_enc, cred.key_version)

    def fingerprint(self, value: str) -> str:
        """A keyed hash of a secret: equal for equal values, useless without the master key.

        Used where "is this the same request" must be answered without keeping the secret
        (the ``Idempotency-Key`` fingerprint of a body that carries one).
        """
        digest = hmac.new(self._fp_key, str(value).encode("utf-8"), "sha256").hexdigest()
        return f"hmac-sha256:{digest[:32]}"


# ---------------------------------------------------------------------------
# master-key rotation (design doc 08, section 2.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RotationReport:
    target_version: int
    rotated: int
    remaining: int                   # rows still below the target (at most a batch more than failed)
    failed: tuple[str, ...]          # ids of rows that did not open with either loaded key

    @property
    def complete(self) -> bool:
        return self.remaining == 0 and not self.failed

    def to_json(self) -> dict:
        return {"target_version": self.target_version, "rotated": self.rotated,
                "remaining": self.remaining, "failed": list(self.failed),
                "complete": self.complete}


def rotate(repo: P.Repository, master_key: MasterKey, *, batch: int = 100,
           on_row: Callable[[str], None] | None = None) -> RotationReport:
    """Re-seal every credential below version ``master_key.version + 1`` with the next key.

    Resumable: each row is re-read, re-sealed and written in its own transaction (under the
    writer lock, so an edit made by the running Daemon at the same moment is not lost), and
    ``credentials_below_key_version`` finds whatever is left on the next run.
    """
    if master_key.next_key is None:
        raise ValueError("CURATOR_MASTER_KEY_NEXT is not set: there is nothing to rotate to")
    sealer = Sealer(master_key)
    target = master_key.version + 1
    batch = max(1, int(batch))
    rotated = 0
    failed: set[str] = set()
    while True:
        rows = repo.credentials_below_key_version(target, limit=batch + len(failed))
        todo = [r for r in rows if r.id not in failed]
        if not todo:
            break
        for row in todo:
            with repo.transaction():
                cur = repo.get_credential(row.id, owner=row.owner_id)
                if cur.key_version >= target:
                    continue
                try:
                    payload = sealer.open_credential(cur)
                except SealError:
                    failed.add(cur.id)
                    continue
                blob, version = sealer.seal(cur.id, payload)
                repo.update_credential(cur.id, owner=cur.owner_id, payload_enc=blob,
                                       key_version=version)
            rotated += 1
            if on_row is not None:
                on_row(row.id)
    remaining = len(repo.credentials_below_key_version(target, limit=len(failed) + batch))
    return RotationReport(target_version=target, rotated=rotated, remaining=remaining,
                          failed=tuple(sorted(failed)))
