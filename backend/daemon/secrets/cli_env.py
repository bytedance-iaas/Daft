"""The environment of a ``curation`` subprocess (design doc 02, section 2; 08, section 3).

Keys reach the CLI through environment variables only - never argv, which ``ps`` shows to
everyone in and outside the container. Two independent key sets, because the source is
often someone else's read-only bucket and the delivery one's own:

=========  =====================================================================
role       variables (``*_SESSION_TOKEN`` only for temporary keys)
=========  =====================================================================
input      ``CURATION_INPUT_TOS_ACCESS_KEY`` / ``_SECRET_KEY`` / ``_SESSION_TOKEN``
output     ``CURATION_OUTPUT_TOS_ACCESS_KEY`` / ``_SECRET_KEY`` / ``_SESSION_TOKEN``
VLM        ``ARK_API_KEY`` - the CLI's default for ``--vlm-api-key-env`` (02 §2)
=========  =====================================================================

The CLI falls back to ``TOS_ACCESS_KEY`` / ``TOS_SECRET_KEY`` when a role's pair is unset.
A key the Daemon itself inherited under those names must therefore never reach a child:
every inherited ``TOS_*`` variable is dropped, as are inherited role variables, VLM key
variables and the Daemon's own secrets. Then exactly the task's keys are set - a public
input gets no input key (anonymous read), a task without VLM modules gets no VLM key. The
one ``TOS_*`` variable put back is ``TOS_ENDPOINT``, deliberately and only when the
Daemon is configured with one: it carries no identity, only "use the internal endpoint of
this region" (v1 ``tos_store.endpoint_for_region``), and without it a pod with no public
egress could not reach TOS at all. Regions go through argv (``--input-region`` /
``--output-region``); :class:`CliEnvironment` says which.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from ..repo import protocol as P
from ..settings import SECRET_ENVS
from .prechecks import task_needs_vlm
from .service import SecretsService
from .tos import TosKey

INPUT_ENV = ("CURATION_INPUT_TOS_ACCESS_KEY", "CURATION_INPUT_TOS_SECRET_KEY",
             "CURATION_INPUT_TOS_SESSION_TOKEN")
OUTPUT_ENV = ("CURATION_OUTPUT_TOS_ACCESS_KEY", "CURATION_OUTPUT_TOS_SECRET_KEY",
              "CURATION_OUTPUT_TOS_SESSION_TOKEN")
#: The CLI's default ``--vlm-api-key-env`` (02 §2); W5 may still pass it explicitly.
VLM_API_KEY_ENV = "ARK_API_KEY"

#: Inherited names that are always dropped (besides every ``TOS_*`` and role variable).
_DROPPED = frozenset({VLM_API_KEY_ENV, "CURATION_VLM_API_KEY", "CURATION_VLM_API_KEY_ENV",
                      "OPENAI_API_KEY", *SECRET_ENVS})
_DROPPED_PREFIXES = ("TOS_", "CURATION_INPUT_TOS_", "CURATION_OUTPUT_TOS_",
                     "CURATOR_MASTER_KEY")


def _dropped(name: str) -> bool:
    return name in _DROPPED or name.startswith(_DROPPED_PREFIXES)


def _set_role(env: dict, names: tuple[str, str, str], key: TosKey) -> None:
    if not (key.access_key_id and key.secret_access_key):
        raise ValueError(f"the key {key.name!r} is missing its access key id or secret")
    env[names[0]] = key.access_key_id
    env[names[1]] = key.secret_access_key
    if key.session_token:
        env[names[2]] = key.session_token


def build_env(base: Mapping[str, str] | None = None, *, input_key: TosKey | None = None,
              output_key: TosKey | None = None, vlm_api_key: str | None = None,
              tos_endpoint: str | None = None) -> dict[str, str]:
    """``base`` (default ``os.environ``) without inherited secrets, plus exactly these keys."""
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if not _dropped(k)}
    if input_key is not None:
        _set_role(env, INPUT_ENV, input_key)
    if output_key is not None:
        _set_role(env, OUTPUT_ENV, output_key)
    if vlm_api_key:
        env[VLM_API_KEY_ENV] = vlm_api_key
    if tos_endpoint:
        env["TOS_ENDPOINT"] = tos_endpoint
    return env


@dataclass(frozen=True)
class CliEnvironment:
    """What W5 needs to start one CLI process. ``repr`` shows variable names, never values."""

    env: dict = field(repr=False)
    input_region: str | None             # -> --input-region (None for a local input)
    output_region: str | None            # -> --output-region
    vlm_api_key_env: str | None          # -> --vlm-api-key-env (None: no key was set)
    secret_names: tuple[str, ...]        # the variables that carry secrets (never log them)

    def __repr__(self) -> str:
        return (f"CliEnvironment(input_region={self.input_region!r}, "
                f"output_region={self.output_region!r}, vlm_api_key_env={self.vlm_api_key_env!r},"
                f" secret_names={self.secret_names!r}, env=<{len(self.env)} variables>)")


def cli_environment(svc: SecretsService, task: P.Task, *, need_input: bool = True,
                    need_output: bool = True, need_vlm: bool | None = None,
                    base: Mapping[str, str] | None = None) -> CliEnvironment:
    """The environment for a CLI command of ``task``.

    Keys are looked up and decrypted now (a key edited since the task started is used in
    its new form; P17 keeps only the VLM *settings* frozen). Raises
    :class:`~daemon.secrets.service.Unavailable` when a key or the backend the command needs
    was deleted - W5 fails the step with its ``message_zh``.
    """
    owner = task.owner_id
    input_key = output_key = None
    input_region = output_region = None
    if need_input and task.input_source == "tos":
        input_key = svc.tos_key(task.input_cred_id, owner=owner, role="input")
        input_region = task.input_region or input_key.region
    elif task.input_source == "public":
        input_region = task.input_region
    if need_output:
        output_key = svc.tos_key(task.output_cred_id, owner=owner, role="output")
        output_region = task.output_region or output_key.region
    if need_vlm is None:
        need_vlm = task_needs_vlm(svc.repo, task.id)
    api_key = svc.vlm_target_for_task(task).api_key if need_vlm else None
    env = build_env(base, input_key=input_key, output_key=output_key, vlm_api_key=api_key,
                    tos_endpoint=svc.deployment_endpoint)
    secret_names = tuple(n for n in (*INPUT_ENV, *OUTPUT_ENV, VLM_API_KEY_ENV) if n in env)
    return CliEnvironment(env=env, input_region=input_region, output_region=output_region,
                          vlm_api_key_env=VLM_API_KEY_ENV if api_key else None,
                          secret_names=secret_names)
