"""C4: the OpenAPI document is valid and covers every endpoint of design doc 03."""
from __future__ import annotations

import re

import pytest

from openapi_spec_validator import validate
from openapi_spec_validator.readers import read_from_filename

from curation.contracts import schemas

METHODS = ("get", "post", "put", "patch", "delete")


def _spec():
    return read_from_filename(str(schemas.contracts_dir() / schemas.OPENAPI))


def test_openapi_is_valid():
    spec, base_uri = _spec()
    validate(spec, base_uri=base_uri)


def _operations():
    spec, _ = _spec()
    return [(m.upper(), path, op) for path, item in spec["paths"].items()
            for m, op in item.items() if m in METHODS]


def test_no_yaml_flow_mapping_accidents():
    """An unquoted comma in ``{description: a, b}`` silently adds a key ``b``."""
    spec, _ = _spec()
    bad = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if not isinstance(key, str) or " " in key:
                    bad.append("/".join(path + [str(key)]))
                walk(value, path + [str(key)])
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, path + [str(i)])

    walk(spec, [])
    assert not bad, f"keys that look like parse accidents: {bad}"


def test_operation_ids_are_unique_and_errors_declared():
    ops = _operations()
    ids = [op["operationId"] for _, _, op in ops]
    assert len(ids) == len(set(ids))
    for method, path, op in ops:
        if path in ("/healthz", "/readyz"):
            continue
        assert "default" in op["responses"], f"{method} {path} lacks the Error response"


def _normalize(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _doc_endpoints() -> set[tuple[str, str]]:
    """(METHOD, path) pairs from the endpoint tables of design doc 03, section 2."""
    doc = (schemas.contracts_dir().parent / "design" / "03-rest-api.md").read_text(encoding="utf-8")
    section = doc.split("## 2. 端点总表", 1)[1].split("## 3.", 1)[0]
    out = set()
    for line in section.splitlines():
        m = re.match(r"^\|\s*([A-Z /]+?)\s*\|\s*(.+?)\s*\|", line)
        if not m or m.group(1) in ("方法",):
            continue
        methods = [x.strip() for x in m.group(1).split("/")]
        for path in re.findall(r"`([^`]+)`", m.group(2)):
            path = path.replace("/api/v1", "")
            for method in methods:
                out.add((method, _normalize(path)))
    return out


def test_every_documented_endpoint_exists():
    have = {(method, _normalize(path)) for method, path, _ in _operations()}
    missing = sorted(_doc_endpoints() - have)
    assert not missing, f"documented but not in openapi.yaml: {missing}"


def test_no_plan_submission_anywhere():
    """D5 / D31: callers can bound concurrency but never submit a plan."""
    spec, _ = _spec()
    params = spec["components"]["schemas"]["TaskParams"]["properties"]
    assert set(params["limits"]["properties"]) == {"cpu_concurrency", "vlm_parallelism"}
    assert "plan" not in params and "plan_overrides" not in params


def _pointer(*parts: str) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _examples():
    """(pointer of the object holding a schema, example name, value) for every example next to a
    schema: request and response media types, parameters, shared responses. ``$ref`` to
    components/examples is followed. The API reference page shows these, so they must be right."""
    spec, _ = _spec()
    shared = spec.get("components", {}).get("examples", {})
    found = []

    def walk(node, ptr):
        if isinstance(node, dict):
            if isinstance(node.get("schema"), dict):
                for name, example in (node.get("examples") or {}).items():
                    if "$ref" in example:
                        example = shared[example["$ref"].rsplit("/", 1)[-1]]
                    found.append((ptr, name, example["value"]))
                if "example" in node:
                    found.append((ptr, "example", node["example"]))
            for key, value in node.items():
                walk(value, ptr + _pointer(key))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, ptr + _pointer(i))

    walk(spec["paths"], "#/paths")
    for section in ("responses", "requestBodies", "parameters", "headers"):
        walk(spec.get("components", {}).get(section, {}), "#/components" + _pointer(section))
    return found


EXAMPLES = _examples()


@pytest.mark.parametrize("ptr,name,value", EXAMPLES, ids=[f"{p} {n}" for p, n, _ in EXAMPLES])
def test_every_example_fits_its_schema(ptr, name, value):
    assert schemas.errors(f"{schemas.OPENAPI}{ptr}/schema", value) == []


#: Operations whose examples the API reference page leads with (C4 2.5.1): the typical run, the
#: registry and the shared error body. ``request`` is the request body, a number the response.
EXAMPLES_EXPECTED = {
    ("createTask", "request"), ("createTask", "201"), ("getTask", "200"), ("listTasks", "200"),
    ("getReport", "200"), ("listTaskEpisodes", "200"), ("getEpisodeSyncCurves", "200"),
    ("listAdjudication", "200"), ("submitAdjudication", "request"), ("submitAdjudication", "200"),
    ("applyAdjudication", "request"), ("applyAdjudication", "202"), ("preflight", "request"),
    ("preflight", "200"), ("createDataset", "request"), ("recheckDataset", "200"),
    ("signDataset", "request"), ("signDataset", "200"), ("getOverview", "200"),
}


def test_common_operations_have_examples():
    have = set()
    for method, path, op in _operations():
        prefix = "#/paths" + _pointer(path, method.lower())
        for ptr, _, _ in EXAMPLES:
            if ptr.startswith(prefix + "/requestBody/"):
                have.add((op["operationId"], "request"))
            elif ptr.startswith(prefix + "/responses/"):
                have.add((op["operationId"], ptr[len(prefix + "/responses/"):].split("/", 1)[0]))
    missing = sorted(EXAMPLES_EXPECTED - have)
    assert not missing, f"examples missing for {missing}"
    assert any(ptr == "#/components/responses/Error/content/application~1json" for ptr, _, _ in EXAMPLES)


#: Internal references - contracts (C4), frozen decisions (D36), features (F12.3), work packages (W5),
#: design docs (design doc 18 §4.0, doc 03 §5) and registry versions - mean nothing to the people the
#: API reference is written for. The API's own texts may carry them (the published copy drops them,
#: frontend/src/lib/publicText.ts); the reference's copy below must not.
INTERNAL_REF = re.compile(r"\b(?:C[1-7]|D\d{1,2}|P\d{1,2}|F\d{1,2}(?:\.\d+)*|W\d{1,2}[ab]?)\b"
                          r"|\b(?:design )?docs? \d{2}\b|registry \d+\.\d+|§\s?\d|frozen contract", re.IGNORECASE)


def _copy():
    """The reference's own copy, in both languages: the home page, every tag, every example title."""
    spec, _ = _spec()
    out = [("info", spec["info"].get("description"), spec["info"].get("x-description-zh"))]
    out += [(f"tag {t['name']}", t.get("description"), t.get("x-description-zh")) for t in spec["tags"]]
    out += [(f"{ptr} {name}", example.get("summary"), example.get("x-summary-zh"))
            for ptr, name, example in _example_objects()]
    return out


def _example_objects():
    spec, _ = _spec()
    found = []

    def walk(node, ptr):
        if isinstance(node, dict):
            if isinstance(node.get("schema"), dict):
                found.extend((ptr, name, example) for name, example in (node.get("examples") or {}).items())
            for key, value in node.items():
                walk(value, ptr + _pointer(key))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, ptr + _pointer(i))

    walk(spec["paths"], "#/paths")
    walk(spec.get("components", {}).get("responses", {}), "#/components/responses")
    return found


@pytest.mark.parametrize("where,en,zh", _copy(), ids=[w for w, _, _ in _copy()])
def test_the_reference_copy_has_both_languages_and_no_internal_references(where, en, zh):
    """The home page, the tags and the example titles come in English and Chinese (``x-description-zh``,
    ``x-summary-zh``; the page switches between them) and name nothing internal."""
    assert en and en.strip(), "English text missing"
    assert zh and zh.strip(), "Chinese text (x-description-zh / x-summary-zh) missing"
    assert re.search(r"[一-鿿]", zh), "the Chinese version has no Chinese in it"
    for text in (en, zh):
        assert not INTERNAL_REF.search(text), f"internal reference {INTERNAL_REF.search(text).group(0)!r}"


def test_the_changelog_starts_at_the_first_published_version():
    """The changelog is for the people the API is published to: it starts at 2.5.1, newest last."""
    spec, _ = _spec()
    for text, heading in ((spec["info"]["description"], "## Changelog"),
                          (spec["info"]["x-description-zh"], "## 变更记录")):
        log = text.split(heading, 1)[1]
        versions = re.findall(r"^\*\*(\d+\.\d+\.\d+)\*\*", log, re.MULTILINE)
        assert versions and versions[0] == "2.5.1", versions
        assert versions[-1] == spec["info"]["version"], "the current version needs an entry"
