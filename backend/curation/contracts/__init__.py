"""Frozen contracts shared by the CLI, the Daemon and the frontend (work package W2).

* C1 - module registry: :mod:`curation.contracts.modules` (exported to
  ``docs/contracts/modules.json`` for the frontend and the contract tests).
* C2 - CLI ``--json`` outputs and the files commands exchange:
  ``docs/contracts/cli/*.schema.json``.
* C3 - progress protocol on stderr: ``docs/contracts/progress.schema.json``.
* C4 - REST API: ``docs/contracts/openapi.yaml``.
* C5 - persistence: ``daemon.repo.protocol`` (the Daemon package).

Changing any of them goes through review: update the file, bump its version,
refresh ``docs/contracts/CONTRACTS.lock`` (``python -m curation.contracts lock``)
and keep ``backend/tests/contracts`` green. This package lives outside the
v1 ``registry/`` directory on purpose: that directory is A-class code copied
verbatim from v1 and guarded file by file.
"""
