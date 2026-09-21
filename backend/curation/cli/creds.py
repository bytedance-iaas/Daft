"""Access keys come from environment variables only - never from argv.

Design doc 02, section 2 and doc 08, section 3: argv is visible to everyone
through ``ps``, inside and outside the container, so no option of this command
line takes a secret. The input dataset and the delivery directory use two
independent key sets, because the source is often someone else's read-only
bucket while the delivery goes into one's own:

============  ===================================================================
role          variables (``*_SESSION_TOKEN`` optional)
============  ===================================================================
``input``     ``CURATION_INPUT_TOS_ACCESS_KEY`` / ``CURATION_INPUT_TOS_SECRET_KEY``
``output``    ``CURATION_OUTPUT_TOS_ACCESS_KEY`` / ``CURATION_OUTPUT_TOS_SECRET_KEY``
fallback      ``TOS_ACCESS_KEY`` / ``TOS_SECRET_KEY`` (v1's names) when a role's pair is unset
============  ===================================================================

A pair that is only half set is refused rather than silently falling back:
half-configured credentials are worse than none (doc 08, section 5.1).
The public HuggingFace cache bucket is read anonymously and needs no key.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import UsageError

ROLE_ENV = {
    "input": ("CURATION_INPUT_TOS_ACCESS_KEY", "CURATION_INPUT_TOS_SECRET_KEY",
              "CURATION_INPUT_TOS_SESSION_TOKEN"),
    "output": ("CURATION_OUTPUT_TOS_ACCESS_KEY", "CURATION_OUTPUT_TOS_SECRET_KEY",
               "CURATION_OUTPUT_TOS_SESSION_TOKEN"),
}
SHARED_ENV = ("TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_SESSION_TOKEN")


@dataclass(frozen=True)
class TosCredentials:
    access_key: str
    secret_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)
    source: str = ""                     # the variable names they came from, for messages

    @property
    def anonymous(self) -> bool:
        return not self.access_key


ANONYMOUS = TosCredentials("", "", None, "anonymous")


def _pair(names: tuple[str, str, str], env) -> TosCredentials | None:
    ak = (env.get(names[0]) or "").strip()
    sk = (env.get(names[1]) or "").strip()
    if ak and sk:
        token = (env.get(names[2]) or "").strip() or None
        return TosCredentials(ak, sk, token, f"{names[0]} / {names[1]}")
    if ak or sk:
        missing = names[1] if ak else names[0]
        raise UsageError(f"{missing} is not set while its partner is; set both or neither",
                         {"variables": [names[0], names[1]]})
    return None


def tos_credentials(role: str, env=None) -> TosCredentials | None:
    """The key set for ``role`` (``input`` / ``output``), or None when none is configured."""
    env = os.environ if env is None else env
    if role not in ROLE_ENV:
        raise ValueError(f"unknown credential role {role!r}")
    return _pair(ROLE_ENV[role], env) or _pair(SHARED_ENV, env)


def require_tos_credentials(role: str, env=None) -> TosCredentials:
    creds = tos_credentials(role, env)
    if creds is None:
        ak, sk, _ = ROLE_ENV[role]
        raise UsageError(f"no TOS access key for the {role}: set {ak} and {sk} "
                         f"(or TOS_ACCESS_KEY and TOS_SECRET_KEY) in the environment",
                         {"variables": [ak, sk]})
    return creds


# ---------------------------------------------------------------- TOS clients


def _sdk_client(creds: TosCredentials, endpoint: str, region: str):
    import tos  # lazy: tests inject a fake instead

    return tos.TosClientV2(creds.access_key, creds.secret_key, endpoint, region,
                           security_token=creds.session_token)


#: Builds the TOS SDK client; tests replace it to run offline and to see which key set
#: each role used.
CLIENT_FACTORY: Callable[[TosCredentials, str, str], object] = _sdk_client


def resolve_region(region: str | None) -> tuple[str, str]:
    """(region, endpoint) with v1's rules (``tos_store.make_store``): the region asked
    for, else ``$TOS_REGION``, else the one in ``$TOS_ENDPOINT``, else cn-beijing; the
    internal endpoint is kept when the region matches the deployment's."""
    from .. import tos_store

    dep_endpoint = os.environ.get("TOS_ENDPOINT", "").strip()
    dep_region = (os.environ.get("TOS_REGION", "").strip()
                  or tos_store.region_from_endpoint(dep_endpoint) or tos_store.DEFAULT_REGION)
    want = (region or "").strip() or dep_region
    return want, tos_store.endpoint_for_region(want, dep_endpoint)


def tos_client(role: str, region: str | None, *, anonymous: bool = False):
    """(client, region) for one role. The secret never leaves this function's callees."""
    creds = ANONYMOUS if anonymous else require_tos_credentials(role)
    want, endpoint = resolve_region(region)
    return CLIENT_FACTORY(creds, endpoint, want), want
