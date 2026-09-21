"""The master key that encrypts stored secrets (design doc 08, section 2).

W4 only loads and validates it; encryption itself is W8's. The key comes from
the environment (``CURATOR_MASTER_KEY``, 32 bytes, base64), never from disk,
the database or the logs. A missing or malformed key stops the Daemon from
starting: half-configured encryption is worse than none (fail closed).

``CURATOR_MASTER_KEY_NEXT`` is the optional key being rotated to
(``curator-admin rotate-master-key``); ``CURATOR_MASTER_KEY_VERSION`` numbers
the current key (default 1) so every stored row can say which key sealed it.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import os
from dataclasses import dataclass
from typing import Mapping

KEY_ENV = "CURATOR_MASTER_KEY"
NEXT_KEY_ENV = "CURATOR_MASTER_KEY_NEXT"
VERSION_ENV = "CURATOR_MASTER_KEY_VERSION"
KEY_BYTES = 32


class MasterKeyError(RuntimeError):
    """The master key is missing or malformed; the message never contains key material."""


def _decode(name: str, raw: str) -> bytes:
    text = "".join(raw.split())
    if not text:
        raise MasterKeyError(f"{name} 为空：主密钥缺失，Daemon 拒绝启动（fail-closed）")
    padded = text + "=" * (-len(text) % 4)
    try:
        if "-" in text or "_" in text:
            key = base64.urlsafe_b64decode(padded)
        else:
            key = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        raise MasterKeyError(f"{name} 不是合法的 base64 文本") from None
    if len(key) != KEY_BYTES:
        raise MasterKeyError(f"{name} 解码后是 {len(key)} 字节，应为 {KEY_BYTES} 字节"
                             "（例：openssl rand -base64 32）")
    return key


@dataclass(frozen=True)
class MasterKey:
    """Key material kept in memory only; ``repr`` never shows it."""

    key: bytes
    version: int = 1
    next_key: bytes | None = None

    def __repr__(self) -> str:
        return (f"MasterKey(version={self.version}, "
                f"next={'set' if self.next_key else 'none'}, key=***)")

    __str__ = __repr__

    def fingerprint(self) -> str:
        """A short non-reversible tag for logs ("which key is loaded"), never the key."""
        return hmac.new(self.key, b"curator-master-key-fingerprint", "sha256").hexdigest()[:12]


def load(environ: Mapping[str, str] | None = None) -> MasterKey:
    """Read and validate the master key; raises :class:`MasterKeyError`."""
    env = os.environ if environ is None else environ
    if KEY_ENV not in env:
        raise MasterKeyError(f"没有配置 {KEY_ENV}：主密钥缺失，Daemon 拒绝启动（fail-closed）。"
                             "生成方法：openssl rand -base64 32，经 K8s Secret 注入")
    key = _decode(KEY_ENV, env[KEY_ENV])
    raw_version = str(env.get(VERSION_ENV, "") or "1").strip()
    if not raw_version.isdigit() or int(raw_version) < 1:
        raise MasterKeyError(f"{VERSION_ENV} 必须是正整数")
    next_key = None
    if str(env.get(NEXT_KEY_ENV, "") or "").strip():
        next_key = _decode(NEXT_KEY_ENV, env[NEXT_KEY_ENV])
        if hmac.compare_digest(next_key, key):
            raise MasterKeyError(f"{NEXT_KEY_ENV} 与 {KEY_ENV} 相同，轮换没有意义")
    return MasterKey(key=key, version=int(raw_version), next_key=next_key)


def scrub_environment(environ: dict | None = None) -> None:
    """Drop key material from ``os.environ`` once loaded, so CLI children never inherit it."""
    env = os.environ if environ is None else environ
    for name in (KEY_ENV, NEXT_KEY_ENV):
        env.pop(name, None)
