"""W8 - secrets and resources (design doc 08; F2.6).

* :mod:`.sealing` - AES-256-GCM sealing of stored secrets with the master key, key versions,
  and the resumable master-key rotation (08 §2, §2.1; ``python -m daemon.secrets.rotate``).
* :mod:`.scrub` - known secret values out of any text that came back from a provider.
* :mod:`.tos` / :mod:`.vlm` - the calls made with decrypted keys: TOS identity check, read,
  write probe and presigned URLs; OpenAI-compatible model listing and the minimal call.
* :mod:`.effort` - the reasoning-effort mapping table (08 §4.1).
* :mod:`.service` - :class:`SecretsService`, one per Daemon (:func:`service_of`).
* :mod:`.prechecks` - the three checks before a task starts (D30).
* :mod:`.cli_env` - the environment of a CLI subprocess (02 §2, 08 §3).
* :mod:`.presign` - browser URLs under a prefix, with the path checks of 08 §6.

The rule for all of it: a secret leaves sealed storage only into a subprocess environment
or an outgoing request. It is never returned, logged, written into an audit event or an
exception message, and provider text is scrubbed before it is kept or shown.
"""
from .cli_env import CliEnvironment, build_env, cli_environment
from .prechecks import (
    CheckResult,
    InputTarget,
    OutputTarget,
    PrecheckReport,
    VlmSelection,
    prechecks_for_task,
    run_prechecks,
    task_needs_vlm,
)
from .presign import BadPath, browser_url, relative_key
from .sealing import RotationReport, Sealer, SealError, rotate
from .service import SecretsService, Unavailable, VlmTarget, service_of
from .tos import TosKey

__all__ = [
    "BadPath", "CheckResult", "CliEnvironment", "InputTarget", "OutputTarget", "PrecheckReport",
    "RotationReport", "SealError", "Sealer", "SecretsService", "TosKey", "Unavailable",
    "VlmSelection", "VlmTarget", "browser_url", "build_env", "cli_environment",
    "prechecks_for_task", "relative_key", "rotate", "run_prechecks", "service_of",
    "task_needs_vlm",
]
