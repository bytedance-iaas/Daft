"""Repository rows -> C4 bodies for keys, backends and models. Nothing here decrypts.

``Credential`` shows ``payload_meta`` only (region, endpoint, test bucket, the last four
characters of the access key id); ``VlmBackend.has_api_key`` and ``models_listed`` come from
the key row's ``payload_meta`` too. The tests validate every body against ``openapi.yaml``.
"""
from __future__ import annotations

from ..repo import protocol as P
from .tos import DEFAULT_REGION

_META_KEYS = ("endpoint", "test_bucket", "access_key_id_hint")


def credential(cred: P.Credential, refs: tuple[int, int]) -> dict:
    meta = cred.payload_meta or {}
    out_meta = {"region": meta.get("region") or DEFAULT_REGION}
    for key in _META_KEYS:
        if isinstance(meta.get(key), str) and meta[key]:
            out_meta[key] = meta[key]
    return {
        "id": cred.id, "name": cred.name, "kind": "tos", "meta": out_meta,
        "verify_state": cred.verify_state, "last_verified_at": cred.last_verified_at,
        "last_verify_error": cred.last_verify_error,
        "references": {"active_tasks": int(refs[0]), "historical_tasks": int(refs[1])},
        "created_at": cred.created_at, "updated_at": cred.updated_at,
    }


def verify_result(state: str, at: int, error: str | None) -> dict:
    return {"verify_state": state, "last_verified_at": at, "error": error}


def model(m: P.VlmModel, levels: tuple[str, ...]) -> dict:
    caps = m.capabilities or {}
    vision = caps.get("vision")
    return {
        "id": m.id, "model_name": m.model_name, "reasoning_effort": m.reasoning_effort,
        "max_concurrency": m.max_concurrency,
        "capabilities": {"vision": vision if isinstance(vision, bool) else None,
                         "reasoning_effort_levels": list(levels)},
        "source": m.source, "is_default": bool(m.is_default),
    }


def backend(b: P.VlmBackend, key_meta: dict | None, levels_for) -> dict:
    meta = key_meta or {}
    out = {
        "id": b.id, "name": b.name, "kind": b.kind, "endpoint": b.endpoint,
        "max_concurrency": int(b.max_concurrency), "has_api_key": bool(meta.get("has_api_key")),
        "verify_state": b.verify_state, "last_verified_at": b.last_verified_at,
        "last_verify_error": b.last_verify_error,
        "models": [model(m, levels_for(m)) for m in b.models],
        "created_at": b.created_at, "updated_at": b.updated_at,
    }
    if isinstance(meta.get("models_listed"), bool):
        out["models_listed"] = meta["models_listed"]
    return out
