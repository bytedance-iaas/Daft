"""manifest.json (C2) + manifest.detail.json: keys, fingerprint, structural check, round trip."""
from __future__ import annotations

import json

import pytest

from curation.contracts import schemas
from curation.export.manifest import (DEFAULT_PARAMS, EpisodeEntry, ExportState, FileRecord,
                                      check_manifest, digest_json, export_fingerprint,
                                      normalize_params, task_key)

DIGEST = "sha256:" + "a" * 64


def _passed(*eps):
    return [{"episode_index": i, "task_text": {"text": t, "source": s}} for i, t, s in eps]


BASE = _passed((3, "pick", "原始标注"), (7, "place", "自产caption"))


def _fp(passed=BASE, **kw):
    args = {"source_format": "lerobot_v2", "source_digest": DIGEST, "params": None}
    args.update(kw)
    return export_fingerprint(passed, **args)


def test_fingerprint_is_a_contract_digest_and_stable():
    fp = _fp()
    assert fp.startswith("sha256:") and len(fp) == 71
    # key order, extra fields of the entries (soft_score, reasons ...) do not matter
    noisy = [dict(reversed(list(e.items())), soft_score=0.9) for e in BASE]
    assert _fp(noisy) == fp
    assert _fp(params=dict(DEFAULT_PARAMS)) == fp


@pytest.mark.parametrize("change", [
    {"passed": list(reversed(BASE))},                                      # order
    {"passed": _passed((3, "pick", "原始标注"))},                           # list
    {"passed": _passed((3, "pick up", "原始标注"), (7, "place", "自产caption"))},   # text
    {"passed": _passed((3, "pick", "人工改标"), (7, "place", "自产caption"))},     # source
    {"source_digest": "sha256:" + "b" * 64},
    {"source_format": "lerobot_v3"},
    {"params": {"video_file_mb": 50}},
])
def test_fingerprint_changes_with_what_is_delivered(change):
    assert _fp(**change) != _fp()


def test_fingerprint_rejects_unknown_input():
    with pytest.raises(ValueError):
        _fp(source_format="rrd")
    with pytest.raises(ValueError):
        normalize_params({"crf": 18})


def test_task_key_covers_text_and_source():
    assert task_key(["pick"], "原始标注") == task_key(["pick"], "原始标注")
    assert task_key(["pick"], "原始标注") != task_key(["pick"], "人工改标")
    assert task_key(["pick"], "原始标注") != task_key(["pick", "place"], "原始标注")


def test_digest_json_ignores_numpy_vs_python_types():
    import numpy as np
    assert digest_json({"a": np.int64(3), "b": np.array([1.5, 2.0])}) == \
        digest_json({"b": [1.5, 2.0], "a": 3})


def _state(fmt="lerobot_v2") -> ExportState:
    ep = EpisodeEntry(episode_index=34, new_index=0, content_key=DIGEST, task_key=DIGEST,
                      tasks=["pick"], task_source="原始标注", task_index=0, length=120,
                      index_from=0, parquet="data/chunk-000/episode_000000.parquet",
                      videos={"wrist": "videos/chunk-000/wrist/episode_000000.mp4"})
    if fmt == "lerobot_v3":
        ep.parquet = "data/chunk-000/file-000.parquet"
        ep.videos = {"wrist": "videos/wrist/chunk-000/file-000.mp4"}
        ep.data_file, ep.video_files, ep.windows = 0, {"wrist": 0}, {"wrist": [0.0, 8.0]}
    return ExportState(source_format=fmt, fingerprint=DIGEST, episodes=[ep],
                       meta_files=["meta/info.json"],
                       files={ep.parquet: FileRecord(10, DIGEST, DIGEST)},
                       codebase_version="v2.1", params={}, source={"uri": "/x", "digest": DIGEST},
                       stats="computed", task_table=["pick"])


@pytest.mark.parametrize("fmt", ["lerobot_v2", "lerobot_v3"])
def test_manifest_is_valid_against_the_contract(fmt):
    man = _state(fmt).manifest()
    assert schemas.errors("cli/export-manifest.schema.json", man) == []
    assert check_manifest(man) == []
    if fmt == "lerobot_v3":
        assert man["episodes"][0]["artifacts"]["chunk"] == 0


def test_contract_examples_agree_with_the_runtime_check():
    ex = json.loads((schemas.contracts_dir() / "examples" / "export-manifest.json").read_text())
    for doc in ex["valid"]:
        assert check_manifest(doc) == []
    for doc in ex["invalid"]:
        assert check_manifest(doc)


@pytest.mark.parametrize("breakage", [
    lambda m: m.update(extra=1),
    lambda m: m.pop("fingerprint"),
    lambda m: m.update(fingerprint="md5:abc"),
    lambda m: m["episodes"][0].update(new_index=-1),
    lambda m: m["episodes"][0]["artifacts"].pop("videos"),
    lambda m: m["episodes"][0].update(task_text="pick"),
])
def test_runtime_check_refuses_what_the_schema_refuses(breakage):
    man = _state().manifest()
    breakage(man)
    assert check_manifest(man)
    assert schemas.errors("cli/export-manifest.schema.json", man)


def test_state_round_trip_and_pairing():
    st = _state("lerobot_v3")
    back = ExportState.from_documents(st.manifest(), st.detail())
    assert back.manifest() == st.manifest() and back.detail() == st.detail()
    other = st.detail()
    other["fingerprint"] = "sha256:" + "c" * 64
    with pytest.raises(ValueError, match="different exports"):
        ExportState.from_documents(st.manifest(), other)
    shifted = st.detail()
    shifted["episodes"][0]["new_index"] = 5
    with pytest.raises(ValueError, match="disagree"):
        ExportState.from_documents(st.manifest(), shifted)
