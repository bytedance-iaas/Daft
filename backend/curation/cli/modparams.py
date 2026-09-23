"""``--param MODULE.KEY=VALUE``: task-level module parameters on the command line (registry 1.4).

The values are the same ``modules[].params`` the Daemon validates against each module's
``param_schema`` (C1); here they arrive as strings and are converted by that schema (number,
integer, boolean, choice). Repeatable. Unknown modules or keys are usage errors; whether a
required parameter is present is the command's business (preflight reports it, check needs it).
"""
from __future__ import annotations

import copy

from ..contracts import modules as registry
from .errors import UsageError


def add_argument(parser) -> None:
    parser.add_argument("--param", action="append", default=[], metavar="MODULE.KEY=VALUE",
                        help="a module parameter, e.g. eef_video_consistency.trajectory_json=/data/t.json "
                             "(repeatable; values are converted by the module's param_schema)")


def _convert(module: str, key: str, raw: str, prop: dict):
    choices = [o.get("const") for o in (prop.get("oneOf") or prop.get("anyOf") or []) if "const" in o]
    kind = prop.get("type")
    try:
        if kind == "integer":
            return int(raw)
        if kind == "number":
            return float(raw)
        if kind == "boolean":
            low = raw.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
            raise ValueError(raw)
    except ValueError:
        raise UsageError(f"--param {module}.{key}={raw}: expected a{'n' if kind == 'integer' else ''} "
                         f"{kind}") from None
    if choices and raw not in [str(c) for c in choices]:
        raise UsageError(f"--param {module}.{key}={raw}: one of {', '.join(str(c) for c in choices)}")
    if choices:
        return next(c for c in choices if str(c) == raw)
    return raw


def parse(values: list[str] | None) -> dict[str, dict]:
    """``["m.k=v", ...]`` -> ``{module: {key: value}}``, converted and type-checked (not ``required``)."""
    import jsonschema

    out: dict[str, dict] = {}
    for item in values or []:
        name, sep, raw = str(item).partition("=")
        module, dot, key = name.partition(".")
        if not sep or not dot or not module or not key:
            raise UsageError(f"--param {item!r}: expected MODULE.KEY=VALUE")
        if module not in registry.ids():
            raise UsageError(f"--param {item!r}: unknown module {module!r}; known: {', '.join(registry.ids())}")
        props = registry.get(module).param_schema.get("properties") or {}
        if key not in props:
            known = ", ".join(props) or "none"
            raise UsageError(f"--param {item!r}: {module} has no parameter {key!r} (parameters: {known})")
        out.setdefault(module, {})[key] = _convert(module, key, raw, props[key])
    for module, params in out.items():
        schema = copy.deepcopy(registry.get(module).param_schema)
        schema.pop("required", None)
        try:
            jsonschema.validate(params, schema)
        except jsonschema.ValidationError as e:
            raise UsageError(f"--param {module}: {e.message}") from None
    return out


def with_defaults(module: str, given: dict | None) -> dict:
    """The module's parameters with every schema default filled in."""
    props = registry.get(module).param_schema.get("properties") or {}
    params = {k: p["default"] for k, p in props.items() if "default" in p}
    params.update(given or {})
    return params
