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


def test_schema_versions_are_pinned():
    """A breaking change must bump schema_version; today every CLI contract is 1.0."""
    for rel in schemas.schema_files():
        doc = json.dumps(schemas.load(rel))
        assert '"schema_version"' not in doc or "1.0" in doc, rel


def test_parity_records_match_the_result_record_contract():
    """tools/parity writes the same record rows v2's check will write."""
    from parity import records as R

    rec = R.make_record("task_success", "ep000031", passed=True, score=None,
                        details={"rules": ["vlm_call_failed"]},
                        incidents=[{"step": "probe", "cause": "timeout"}])
    schemas.validate("cli/result-record.schema.json", rec)
