"""C2 / C3: every JSON Schema is valid, resolvable and exercised by examples."""
from __future__ import annotations

import json
import pathlib

import pytest
from jsonschema import Draft202012Validator

from curation.contracts import schemas

EXAMPLES = sorted((schemas.contracts_dir() / "examples").glob("*.json"))


@pytest.mark.parametrize("rel", schemas.schema_files())
def test_schema_is_valid_draft_2020_12(rel):
    Draft202012Validator.check_schema(schemas.load(rel))


def test_every_schema_file_has_examples():
    covered = {json.loads(p.read_text())["schema"].split("#")[0] for p in EXAMPLES}
    missing = [rel for rel in schemas.schema_files()
               if rel != "cli/common.schema.json" and rel not in covered
               and not rel.startswith("parity/")]          # the tape is exercised by tools/parity
    assert not missing, f"schemas without examples: {missing}"


@pytest.mark.parametrize("path", EXAMPLES, ids=[p.name for p in EXAMPLES])
def test_examples(path: pathlib.Path):
    ex = json.loads(path.read_text(encoding="utf-8"))
    assert ex["valid"], "each example file needs at least one valid instance"
    for i, inst in enumerate(ex["valid"]):
        assert schemas.errors(ex["schema"], inst) == [], f"valid[{i}] rejected"
    for i, inst in enumerate(ex["invalid"]):
        assert schemas.errors(ex["schema"], inst), f"invalid[{i}] accepted"


#: the documents C2 2.0 changed (design doc 17 §7): written as 2.0, still read as 1.0 (D59)
CHANGED_IN_2_0 = ("cli/result-record.schema.json", "cli/check.schema.json", "cli/verdict-line.schema.json",
                  "cli/final-list.schema.json", "cli/report.schema.json", "cli/plan.schema.json")


def _is_2_0(rel: str, inst: dict) -> bool:
    if "schema_version" in inst:
        return inst["schema_version"] == "2.0"
    return "status" in inst if rel.endswith("result-record.schema.json") else "blocking" in inst


def test_schema_versions_are_pinned():
    """A breaking change bumps schema_version. C2 2.0 changed six documents: they take 2.0 and 1.0;
    every other CLI document is unchanged and stays 1.0."""
    for rel in schemas.schema_files():
        if not rel.startswith("cli/") or rel == "cli/common.schema.json":
            continue
        doc = schemas.load(rel)
        version = (doc.get("properties") or {}).get("schema_version")
        if rel in CHANGED_IN_2_0:
            assert "(schema 2.0; 1.0 still read)" in doc["title"], rel
            assert version in (None, {"$ref": "common.schema.json#/$defs/schema_version_2"}), rel
        else:
            assert "2.0" not in doc.get("title", ""), rel
            assert version is None or json.dumps(version) in ('{"$ref": "common.schema.json#/$defs/schema_version"}',
                                                             '{"const": "1.0"}'), rel


def test_the_result_record_stands_alone():
    """The parity tool validates records with this one file (no registry), so it refers to nothing outside
    itself; the definitions it carries are common's."""
    text = (schemas.contracts_dir() / "cli" / "result-record.schema.json").read_text(encoding="utf-8")
    assert "common.schema.json#" not in text
    own = schemas.load("cli/result-record.schema.json")["$defs"]
    shared = schemas.load("cli/common.schema.json")["$defs"]
    for name, definition in own.items():
        if name in shared:
            assert definition == shared[name], name


@pytest.mark.parametrize("rel", CHANGED_IN_2_0)
def test_changed_documents_have_examples_of_both_versions(rel):
    ex = json.loads((schemas.contracts_dir() / "examples" / f"{rel[4:-12]}.json").read_text(encoding="utf-8"))
    assert ex["schema"] == rel
    versions = {_is_2_0(rel, inst) for inst in ex["valid"]}
    assert versions == {True, False}, "2.0 and 1.0 (D59) are both valid"
    assert any(_is_2_0(rel, inst) for inst in ex["invalid"]), "and 2.0 has invalid examples too"


def test_parity_records_match_the_result_record_contract():
    """tools/parity writes the same record rows v2's check will write."""
    from parity import records as R

    rec = R.make_record("task_success", "ep000031", passed=True, score=None,
                        details={"rules": ["vlm_call_failed"]},
                        incidents=[{"step": "probe", "cause": "timeout"}])
    schemas.validate("cli/result-record.schema.json", rec)
