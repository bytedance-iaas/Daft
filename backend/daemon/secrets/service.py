"""``SecretsService``: stored secrets, the calls made with them, and the lookups W5 needs.

One per Daemon: :func:`service_of` creates it on first use and keeps it on the runtime
(``runtime.secrets``). Tests swap its connectors: ``tos_factory`` (the TOS SDK client
factory) and ``vlm`` (the OpenAI-compatible HTTP connector).

What W5 calls (also through :mod:`.prechecks` and :mod:`.cli_env`):

* ``tos_key(cred_id)`` -> :class:`~.tos.TosKey` (decrypted); ``with tos(key, region) as
  (client, endpoints)`` for a TOS SDK client that is closed afterwards;
* ``vlm_target_for_task(task)`` -> :class:`VlmTarget` (endpoint, model, effective reasoning
  effort, parallelism and the live API key); ``VlmTarget.snapshot()`` is what start freezes
  into ``task.vlm_snapshot`` (P17) - everything but the key.

Lookups that cannot be served raise :class:`Unavailable` with a Chinese reason (the key was
deleted, the backend is gone, the stored secret does not open).
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping

from ..masterkey import MasterKey
from ..repo import protocol as P
from ..util import now_ms
from . import tos as T
from .effort import EffortTable, load_table
from .sealing import Sealer, SealError
from .vlm import VlmConnector

log = logging.getLogger("daemon.secrets")

#: Secret fields of the request bodies (never echoed, never kept in the clear).
TOS_SECRET_FIELDS = ("access_key_id", "secret_access_key")
VLM_SECRET_FIELDS = ("api_key",)

#: The name of the credential row that holds a VLM backend's API key. Users cannot create
#: access keys with this prefix, so the (owner, name) uniqueness never collides.
BACKEND_KEY_PREFIX = "vlm-backend/"

DEPLOYMENT_ENDPOINT_ENV = "TOS_ENDPOINT"


class Unavailable(Exception):
    """A key, backend or model a task needs is gone or does not open.

    ``code``: ``credential_missing`` | ``backend_missing`` | ``model_missing`` |
    ``secret_unreadable``; ``message_zh`` is shown to people as is.
    """

    def __init__(self, code: str, message_zh: str):
        super().__init__(f"{code}: {message_zh}")
        self.code = code
        self.message_zh = message_zh


@dataclass(frozen=True)
class VlmTarget:
    """Everything a VLM call needs. ``repr`` hides the key; ``snapshot()`` leaves it out."""

    backend_id: str
    backend: str
    kind: str
    endpoint: str
    model_id: str | None
    model: str
    reasoning_effort: str | None         # effective: task override > model setting > None
    max_concurrency: int
    api_key: str | None = field(default=None, repr=False)

    def snapshot(self) -> dict:
        """The effective settings start freezes into ``task.vlm_snapshot`` (P17); no key."""
        return {"backend_id": self.backend_id, "backend": self.backend, "kind": self.kind,
                "endpoint": self.endpoint, "model_id": self.model_id, "model": self.model,
                "reasoning_effort": self.reasoning_effort,
                "max_concurrency": self.max_concurrency}

    @property
    def label(self) -> str:
        return f"模型服务「{self.backend}」的模型 {self.model}"


def tos_payload(access_key_id: str, secret_access_key: str,
                session_token: str | None = None) -> dict:
    out = {"access_key_id": access_key_id, "secret_access_key": secret_access_key}
    if session_token:
        out["session_token"] = session_token
    return out


def tos_meta(key: T.TosKey) -> dict:
    """``payload_meta`` of a TOS key: what the list page shows, never decrypted to show it."""
    meta: dict[str, Any] = {"region": key.region or T.DEFAULT_REGION}
    if key.endpoint:
        meta["endpoint"] = key.endpoint
    if key.test_bucket:
        meta["test_bucket"] = key.test_bucket
    if key.access_key_id:
        meta["access_key_id_hint"] = key.access_key_id[-4:]
    return meta


class SecretsService:
    def __init__(self, repo: P.Repository, master_key: MasterKey, *,
                 clock: Callable[[], int] = now_ms, local_data_root=None,
                 environ: Mapping[str, str] | None = None,
                 tos_factory: T.ClientFactory | None = None, vlm: VlmConnector | None = None,
                 effort: EffortTable | None = None):
        env = os.environ if environ is None else environ
        self.repo = repo
        self.sealer = Sealer(master_key)
        self.clock = clock
        self.local_data_root = local_data_root
        self.tos_factory: T.ClientFactory = tos_factory or T.sdk_client
        self.vlm = vlm or VlmConnector()
        self.effort = effort or load_table(env)
        self.deployment_endpoint = self._deployment_endpoint(env)

    @staticmethod
    def _deployment_endpoint(env: Mapping[str, str]) -> str | None:
        raw = str(env.get(DEPLOYMENT_ENDPOINT_ENV, "") or "").strip()
        if not raw:
            return None
        try:
            return T.normalize_endpoint(raw)
        except ValueError:
            log.warning("%s=%r is not a TOS endpoint; ignored", DEPLOYMENT_ENDPOINT_ENV, raw)
            return None

    # -- sealing helpers ----------------------------------------------------------
    def open(self, cred: P.Credential) -> dict:
        try:
            return self.sealer.open_credential(cred)
        except SealError as err:
            log.error("stored secret does not open: %s", err)
            raise Unavailable("secret_unreadable", err.message_zh) from None

    def fingerprint_body(self, body: Any, secret_fields: Iterable[str]) -> Any:
        """``body`` with each secret string replaced by its keyed hash (for Idempotency-Key)."""
        if not isinstance(body, dict):
            return body
        fields = set(secret_fields)
        return {k: (self.sealer.fingerprint(v) if k in fields and isinstance(v, str) and v else v)
                for k, v in body.items()}

    # -- TOS ----------------------------------------------------------------------
    def tos_key_from(self, cred: P.Credential) -> T.TosKey:
        if cred.kind != "tos":
            raise Unavailable("credential_missing", f"「{cred.name}」不是 TOS 访问密钥")
        payload = self.open(cred)
        meta = cred.payload_meta or {}
        return T.TosKey(
            access_key_id=str(payload.get("access_key_id") or ""),
            secret_access_key=str(payload.get("secret_access_key") or ""),
            session_token=payload.get("session_token") or None,
            credential_id=cred.id, name=cred.name, region=meta.get("region"),
            endpoint=meta.get("endpoint"), test_bucket=meta.get("test_bucket"))

    def tos_key(self, cred_id: str | None, *, owner: str = P.DEFAULT_OWNER,
                role: str = "") -> T.TosKey:
        """The decrypted key; :class:`Unavailable` when it was deleted (``cred_id`` None)."""
        what = {"input": "输入数据集的", "output": "交付目录的"}.get(role, "")
        gone = f"{what}访问密钥已被删除，请给任务重新指定一个访问密钥"
        if not cred_id:
            raise Unavailable("credential_missing", gone)
        try:
            cred = self.repo.get_credential(cred_id, owner=owner)
        except P.NotFound:
            raise Unavailable("credential_missing", gone) from None
        return self.tos_key_from(cred)

    def tos_endpoints(self, key: T.TosKey | None, region: str | None = None) -> T.Endpoints:
        want = region or (key.region if key is not None else None)
        custom = key.endpoint if key is not None else None
        return T.endpoints(want, custom, self.deployment_endpoint)

    def tos_client(self, key: T.TosKey | None, region: str | None = None):
        """(SDK client for the Daemon's own calls, endpoints); ``key=None`` = anonymous.
        The caller closes the client; :meth:`tos` does that for a ``with`` block."""
        ends = self.tos_endpoints(key, region)
        return self.tos_factory(ends.server, ends.region, key), ends

    @contextlib.contextmanager
    def tos(self, key: T.TosKey | None, region: str | None = None, *,
            browser: bool = False) -> Iterator[tuple[Any, T.Endpoints]]:
        """``with svc.tos(key, region) as (client, ends):`` - closed afterwards. ``browser``:
        a client on the public endpoint, for presigned URLs (08 §6.3)."""
        ends = self.tos_endpoints(key, region)
        client = self.tos_factory(ends.browser if browser else ends.server, ends.region, key)
        try:
            yield client, ends
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - closing an idle session must not mask a result
                    log.debug("closing a TOS client failed", exc_info=False)

    def verify_tos(self, key: T.TosKey) -> T.Verification:
        with self.tos(key) as (client, ends):
            result = T.verify_identity(client, key, region=ends.region, endpoint=ends.server,
                                       now=self.clock())
        if result.state != "ok":
            log.info("access key %s: identity check %s: %s", key.name, result.state, result.error)
        return result

    # -- VLM backends -------------------------------------------------------------------
    def backend_credential(self, backend: P.VlmBackend) -> P.Credential | None:
        if not backend.credential_id:
            return None
        try:
            return self.repo.get_credential(backend.credential_id, owner=backend.owner_id)
        except P.NotFound:
            return None

    def backend_api_key(self, backend: P.VlmBackend) -> str | None:
        cred = self.backend_credential(backend)
        if cred is None:
            return None
        key = str(self.open(cred).get("api_key") or "")
        return key or None

    def vlm_target(self, model_id: str | None, *, reasoning_effort: str | None = None,
                   owner: str = P.DEFAULT_OWNER) -> VlmTarget:
        """The target of a configured (not yet started) task: model row + task override."""
        if not model_id:
            raise Unavailable("model_missing", "任务选的模型已被删除：请重新选择 VLM 模型")
        for backend in self.repo.list_vlm_backends(owner=owner):
            model = next((m for m in backend.models if m.id == model_id), None)
            if model is None:
                continue
            effort = reasoning_effort if reasoning_effort is not None else model.reasoning_effort
            return VlmTarget(
                backend_id=backend.id, backend=backend.name, kind=backend.kind,
                endpoint=backend.endpoint, model_id=model.id, model=model.model_name,
                reasoning_effort=effort,
                max_concurrency=int(model.max_concurrency or backend.max_concurrency),
                api_key=self.backend_api_key(backend))
        raise Unavailable("model_missing", "任务选的模型已被删除：请重新选择 VLM 模型")

    def vlm_target_from_snapshot(self, snapshot: Mapping[str, Any], *,
                                 owner: str = P.DEFAULT_OWNER) -> VlmTarget:
        """A started task: the frozen settings (P17) with the key read live."""
        backend_id = snapshot.get("backend_id")
        name = snapshot.get("backend") or backend_id
        try:
            backend = self.repo.get_vlm_backend(str(backend_id), owner=owner)
        except P.NotFound:
            raise Unavailable("backend_missing",
                              f"模型服务「{name}」已被删除：这个任务的模型调用没法继续") from None
        return VlmTarget(
            backend_id=backend.id, backend=str(snapshot.get("backend") or backend.name),
            kind=str(snapshot.get("kind") or backend.kind),
            endpoint=str(snapshot.get("endpoint") or backend.endpoint),
            model_id=snapshot.get("model_id"), model=str(snapshot.get("model") or ""),
            reasoning_effort=snapshot.get("reasoning_effort"),
            max_concurrency=int(snapshot.get("max_concurrency") or backend.max_concurrency),
            api_key=self.backend_api_key(backend))

    def vlm_target_for_task(self, task: P.Task) -> VlmTarget:
        snap = task.vlm_snapshot
        if isinstance(snap, dict) and snap.get("backend_id"):
            return self.vlm_target_from_snapshot(snap, owner=task.owner_id)
        return self.vlm_target(task.vlm_model_id, reasoning_effort=task.vlm_reasoning_effort,
                               owner=task.owner_id)

    def backend_references(self, backend: P.VlmBackend, *, owner: str = P.DEFAULT_OWNER,
                           count_finished: bool = True) -> tuple[int, int]:
        """(unfinished, finished or deleted) tasks that use one of the backend's models or were
        started on it (their snapshot names it). C5 has no count query for this, so it scans
        the tasks; ``count_finished=False`` skips the (large) set of finished ones."""
        model_ids = {m.id for m in backend.models}
        states = set(P.TASK_TRANSITIONS)
        if not count_finished:
            states -= P.TERMINAL_STATES
        active = historical = 0
        for task in self.repo.tasks_in_states(states):
            if task.owner_id != owner:
                continue
            snap = task.vlm_snapshot if isinstance(task.vlm_snapshot, dict) else {}
            if task.vlm_model_id not in model_ids and snap.get("backend_id") != backend.id:
                continue
            if task.state in P.TERMINAL_STATES or task.deleted_at is not None:
                historical += 1
            else:
                active += 1
        return active, historical

    def levels_for(self, model: P.VlmModel) -> tuple[str, ...]:
        caps = model.capabilities or {}
        return self.effort.levels_for(caps.get("served_model") or model.model_name)

    def check_task_effort(self, model_id: str | None, effort: str | None, *,
                          owner: str = P.DEFAULT_OWNER) -> None:
        """A task-level ``reasoning_effort`` override must be one of the model's levels too;
        raises :class:`~.effort.EffortNotAllowed` (``message_zh``) - for W5's create/PATCH."""
        if effort is None or not model_id:
            return
        for backend in self.repo.list_vlm_backends(owner=owner):
            for model in backend.models:
                if model.id == model_id:
                    caps = model.capabilities or {}
                    self.effort.check(caps.get("served_model") or model.model_name, effort)
                    return


_CREATE_LOCK = threading.Lock()


def service_of(runtime) -> SecretsService:
    """The runtime's service, created on first use (``runtime.secrets``)."""
    svc = getattr(runtime, "secrets", None)
    if svc is None:
        with _CREATE_LOCK:
            svc = getattr(runtime, "secrets", None)
            if svc is None:
                svc = SecretsService(runtime.repo, runtime.master_key, clock=runtime.clock,
                                     local_data_root=getattr(runtime.settings, "local_data_root",
                                                             None))
                runtime.secrets = svc
    return svc
