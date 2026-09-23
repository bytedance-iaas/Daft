"""Load and validate against the contract files in ``docs/contracts``.

    from curation.contracts import schemas
    schemas.validate("cli/preflight.schema.json", payload)
    schemas.validate("openapi.yaml#/components/schemas/TaskCreate", body)

References between files are relative (``common.schema.json#/$defs/digest``,
``./cli/plan.schema.json``) and resolve against the file that contains them.
"""
from __future__ import annotations

import functools
import json
import os
import pathlib
from typing import Any
from urllib.parse import unquote, urlparse

CONTRACTS_ENV = "CURATOR_CONTRACTS_DIR"

#: Every contract file, relative to the contracts directory, grouped by kind.
JSON_SCHEMA_GLOBS = ("cli/*.schema.json", "parity/*.schema.json", "progress.schema.json",
                     "eef/*.schema.json")
OPENAPI = "openapi.yaml"
MODULES_JSON = "modules.json"


def contracts_dir() -> pathlib.Path:
    """``$CURATOR_CONTRACTS_DIR``, else ``docs/contracts`` above this package."""
    env = os.environ.get(CONTRACTS_ENV)
    if env:
        return pathlib.Path(env).resolve()
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "docs" / "contracts"
        if (cand / OPENAPI).is_file():
            return cand
    raise FileNotFoundError("docs/contracts not found; set CURATOR_CONTRACTS_DIR")


def _read(path: pathlib.Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        import yaml

        return yaml.safe_load(text)
    return json.loads(text)


def load(rel: str) -> Any:
    return _read(contracts_dir() / rel)


def schema_files() -> list[str]:
    root = contracts_dir()
    out: list[str] = []
    for pattern in JSON_SCHEMA_GLOBS:
        out += sorted(str(p.relative_to(root)).replace(os.sep, "/") for p in root.glob(pattern))
    return out


@functools.lru_cache(maxsize=1)
def _registry():
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    def retrieve(uri: str):
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise LookupError(f"only local contract files can be referenced, got {uri}")
        return Resource.from_contents(_read(pathlib.Path(unquote(parsed.path))),
                                      default_specification=DRAFT202012)

    return Registry(retrieve=retrieve)


def _uri(rel: str) -> str:
    path, _, fragment = rel.partition("#")
    uri = (contracts_dir() / path).resolve().as_uri()
    return f"{uri}#{fragment}" if fragment else uri


def validator(ref: str):
    """A Draft 2020-12 validator for ``file`` or ``file#/json/pointer``."""
    from jsonschema import Draft202012Validator

    return Draft202012Validator({"$ref": _uri(ref)}, registry=_registry())


def validate(ref: str, instance: Any) -> None:
    """Raise ``jsonschema.ValidationError`` if ``instance`` does not fit ``ref``."""
    validator(ref).validate(instance)


def errors(ref: str, instance: Any) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in validator(ref).iter_errors(instance)]
