"""``python -m curation.contracts <export-modules | lock | check>``.

* ``export-modules`` writes the registry (C1) to ``docs/contracts/modules.json``.
* ``lock`` records the sha256 of every contract file in ``docs/contracts/CONTRACTS.lock``.
* ``check`` fails when a contract file differs from the lock or modules.json is stale.

Changing a contract is a reviewed act: edit it, bump its version, run ``lock``,
and the diff of CONTRACTS.lock shows reviewers exactly which contracts moved.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys

from . import modules, schemas

#: Contract files outside docs/contracts, relative to the repo root.
CODE_CONTRACTS = ("backend/curation/contracts/modules.py", "backend/daemon/repo/protocol.py")


def _repo_root() -> pathlib.Path:
    return schemas.contracts_dir().parent.parent


def _modules_json() -> str:
    return json.dumps(modules.export(), ensure_ascii=False, indent=1) + "\n"


def contract_files() -> list[str]:
    root = _repo_root()
    base = schemas.contracts_dir().relative_to(root)
    files = [str(base / rel).replace("\\", "/") for rel in schemas.schema_files()]
    files += [str(base / schemas.OPENAPI), str(base / schemas.MODULES_JSON)]
    files += list(CODE_CONTRACTS)
    return sorted(files)


def compute_lock() -> dict:
    root = _repo_root()
    return {"note": "sha256 of every frozen contract; refresh with `python -m curation.contracts lock` "
                    "after a reviewed change",
            "files": {rel: "sha256:" + hashlib.sha256((root / rel).read_bytes()).hexdigest()
                      for rel in contract_files()}}


def lock_path() -> pathlib.Path:
    return schemas.contracts_dir() / "CONTRACTS.lock"


def check() -> list[str]:
    problems = []
    mj = schemas.contracts_dir() / schemas.MODULES_JSON
    if not mj.is_file() or mj.read_text(encoding="utf-8") != _modules_json():
        problems.append("docs/contracts/modules.json is stale: run `python -m curation.contracts "
                        "export-modules`")
    if not lock_path().is_file():
        return problems + ["docs/contracts/CONTRACTS.lock is missing"]
    locked = json.loads(lock_path().read_text(encoding="utf-8"))["files"]
    current = compute_lock()["files"]
    for rel in sorted(set(locked) | set(current)):
        if locked.get(rel) != current.get(rel):
            state = ("new" if rel not in locked else "removed" if rel not in current
                     else "changed")
            problems.append(f"{rel}: {state} since CONTRACTS.lock")
    return problems


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "check"
    if cmd == "export-modules":
        (schemas.contracts_dir() / schemas.MODULES_JSON).write_text(_modules_json(),
                                                                   encoding="utf-8")
        print("wrote docs/contracts/modules.json")
        return 0
    if cmd == "lock":
        lock_path().write_text(json.dumps(compute_lock(), indent=1, sort_keys=True) + "\n",
                               encoding="utf-8")
        print(f"wrote {lock_path()}")
        return 0
    if cmd == "check":
        problems = check()
        for p in problems:
            print(p, file=sys.stderr)
        return 1 if problems else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
