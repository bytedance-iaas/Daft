"""The data integrity module on damaged datasets (design doc 14 §8; F7.2-F7.4).

Every sample is a copy of a clean fixture with one kind of damage per episode, so each
episode's expected finding is known: LeRobot v2 (``parity.fixtures``), LeRobot v3 with
shared files (``tests/export/v3_fixture``) and mcap. The clean fixtures pass, except the
two byte-equal episodes the dedup fixture carries on purpose (3 and 7), which are
suspects.
"""
from __future__ import annotations

import json
import os
import shutil

import numpy as np
import pytest

from curation.contracts import schemas
from curation.extensions.integrity import files as F

from . import integrity_samples as S

MOD = "data_integrity"


def results(run_dir: str) -> dict[int, dict]:
    path = os.path.join(run_dir, "checks", MOD, "results.jsonl")
    with open(path, encoding="utf-8") as fh:
        return {r["episode_index"]: r for r in map(json.loads, fh)}


def check(cli, dataset: str, run_dir: str, episodes: str = "0-7", *extra) -> dict:
    res = cli("check", "--modules", MOD, "--input", dataset, "--run-dir", run_dir,
              "--episodes", episodes, *extra)
    assert res.rc == 0, res.doc
    for rec in results(run_dir).values():
        assert schemas.errors("cli/result-record.schema.json", rec) == []
    return res.doc["modules"][MOD]


def codes(rec: dict) -> list[tuple[str, str]]:
    return [(f["level"], f["code"]) for f in rec["details"]["findings"]]


def verdicts(recs: dict[int, dict]) -> dict[int, str]:
    return {e: r["verdict"] for e, r in sorted(recs.items())}


# ---------------------------------------------------------------- LeRobot v2


def test_a_clean_dataset_passes_but_its_byte_copies(cli, dataset, tmp_path):
    rd = str(tmp_path / "run")
    doc = check(cli, dataset, rd)
    assert doc["episodes"] == {"total": 8, "pass": 6, "fail": 0, "abstain": 2, "scored": 0, "error": 0}
    recs = results(rd)
    assert verdicts(recs) == {0: "pass", 1: "pass", 2: "pass", 3: "abstain", 4: "pass", 5: "pass",
                              6: "pass", 7: "abstain"}
    assert codes(recs[3]) == codes(recs[7]) == [("suspect", "duplicate_content")] * 2
    assert recs[3]["details"]["reason"].startswith("需要人工裁决：exterior 相机的视频与 ep 7 的内容完全相同")
    d = recs[0]["details"]
    assert d["outcome"] == "pass" and d["tiers"] == {"L1": True, "L2": True, "L3": False}
    by_kind = {f["file"].rsplit(".", 1)[1]: f for f in d["files"]}
    assert by_kind["parquet"]["rows"] == 75 and by_kind["mp4"]["frames"] == 75     # episode 0: 75 frames
    dataset_doc = json.load(open(os.path.join(rd, "checks", MOD, "dataset.json")))
    assert dataset_doc["findings"] == [] and sorted(dataset_doc["episode_findings"]) == ["3", "7"]
    assert doc["input_digest"].startswith("sha256:")


def test_each_kind_of_damage_rejects_its_episode(cli, dataset, tmp_path):
    truth = S.damage_lerobot_v2(dataset)
    rd = str(tmp_path / "run")
    survivors = str(tmp_path / "integrity.txt")
    check(cli, dataset, rd, "0-7", "--survivors-out", survivors)
    recs = results(rd)
    with open(survivors, encoding="utf-8") as fh:                   # what goes on to the numeric stage
        assert fh.read().split() == ["5", "7"]                      # rejects stop here, suspects go on
    assert verdicts(recs) == {0: "fail", 1: "fail", 2: "fail", 3: "fail", 4: "fail", 5: "pass",
                              6: "fail", 7: "abstain"}
    first = {e: codes(r)[0] for e, r in recs.items() if r["verdict"] == "fail"}
    assert first == {0: ("reject", "file_empty"), 1: ("reject", "file_truncated"),
                     2: ("reject", "file_truncated"), 3: ("reject", "row_invalid"),
                     4: ("reject", "zero_filled"), 6: ("reject", "row_invalid")}
    for ep, (level, code) in first.items():
        assert f"{level} {code}" in truth[ep]                      # the sample's own truth
    assert "时间戳非严格递增" in recs[3]["details"]["reason"]            # v1's message, verbatim
    assert "NaN/Inf" in recs[6]["details"]["reason"]
    assert recs[1]["details"]["findings"][0]["camera"] == "exterior"
    assert ("suspect", "duplicate_content") in codes(recs[3])              # still said, below the reject


def test_without_the_module_a_bad_row_is_still_an_error(cli, dataset, tmp_path):
    """D51 applies only when the module is selected: otherwise v1's row validation errs as before."""
    S.rewrite_parquet(S.parquet(dataset, 6), S.nan_action)
    rd = str(tmp_path / "run")
    res = cli("check", "--modules", "timestamp_check", "--input", dataset, "--run-dir", rd, "--episodes", "6")
    assert res.rc == 0 and res.doc["modules"]["timestamp_check"]["error_episodes"] == [6]


def test_a_truncated_faststart_video_is_placed_in_time(cli, dataset, tmp_path):
    path = S.video(dataset, "exterior", 1)
    S.faststart(path)
    s = S.samples_of(path)
    assert s.offsets[0] > s.offsets[-1] - s.sizes[-1] - os.path.getsize(path)      # moov first
    os.truncate(path, s.offsets[40] + 1)                                            # frames 40.. gone
    rd = str(tmp_path / "run")
    check(cli, dataset, rd, "1")
    rec = results(rd)[1]
    [f] = [f for f in rec["details"]["findings"] if f["code"] == "file_truncated"]
    assert f["span_s"] == [round(s.times[40], 3), None] and "第 40 帧" in f["message"] and "KB 处被截断" in f["message"]


def test_a_zeroed_block_inside_the_video_data(tmp_path):
    """L2: an aligned 64 KiB run of zeros inside mdat, placed at the frames it hits."""
    import av

    path = str(tmp_path / "noise.mp4")
    rng = np.random.default_rng(0)
    with av.open(path, "w") as c:
        st = c.add_stream("libx264", rate=15)
        st.width, st.height, st.pix_fmt = 320, 240, "yuv420p"
        st.options = {"crf": "0", "preset": "ultrafast"}
        for _ in range(60):
            frame = av.VideoFrame.from_ndarray(rng.integers(0, 255, (240, 320, 3), dtype=np.uint8), format="rgb24")
            for p in st.encode(frame):
                c.mux(p)
        for p in st.encode():
            c.mux(p)
    size = os.path.getsize(path)
    s = S.samples_of(path)
    at = (s.offsets[30] // F.ZERO_BLOCK + 1) * F.ZERO_BLOCK
    with open(path, "r+b") as fh:
        fh.seek(at)
        fh.write(bytes(2 * F.ZERO_BLOCK))
    rep = F.FileReport("noise.mp4", "mp4", size)
    blob = F.Blob("noise.mp4", size, path=path)
    F.mp4_l1(blob, rep)
    assert rep.findings == []                                       # the structure is intact
    F.mp4_l2(blob, rep)
    [f] = rep.findings
    assert f.code == "zero_filled" and f.span is not None and f.span[0] <= s.times[31] < f.span[1] + 1
    assert f.args["offset"] == at and f.args["bytes"] == 2 * F.ZERO_BLOCK


def test_a_storage_failure_is_an_error_not_a_finding(cli, dataset, tmp_path, monkeypatch):
    target = "videos/chunk-000/observation.images.wrist/episode_000002.mp4"
    real = F.Blob.read
    left = {"n": 1}

    def flaky(self, start, n):
        if self.key == target and left["n"]:
            left["n"] -= 1
            raise F.ReadFailure("TosServerError: 503 ServiceUnavailable")
        return real(self, start, n)

    monkeypatch.setattr(F.Blob, "read", flaky)
    rd = str(tmp_path / "run")
    doc = check(cli, dataset, rd)
    assert doc["error_episodes"] == [2]
    [inc] = results(rd)[2]["error"]["incidents"]
    assert inc["step"] == "read" and "503" in inc["cause"]
    again = check(cli, dataset, rd, "0-7", "--resume")
    assert again["skipped_existing"] == 7 and results(rd)[2]["verdict"] == "pass"


# ---------------------------------------------------------------- L3


def test_the_decode_test_finds_what_the_structure_cannot(cli, dataset, tmp_path):
    S.garble_sample(S.video(dataset, "exterior", 4), 30)
    rd = str(tmp_path / "run")
    check(cli, dataset, rd)
    assert results(rd)[4]["verdict"] == "pass"                      # L1 and L2 see an intact file
    doc = check(cli, dataset, rd, "0-7", "--resume", "--param", f"{MOD}.decode_test=true")
    assert doc["skipped_existing"] == 0                             # another configuration: redone
    recs = results(rd)
    assert all(r["details"]["tiers"]["L3"] for r in recs.values())
    assert [c for c in codes(recs[4]) if c[1].startswith("decode")] in (
        [("reject", "decode_failed")], [("suspect", "decode_concealed")])
    assert verdicts(recs)[0] == "pass" and verdicts(recs)[4] in ("fail", "abstain")


# ---------------------------------------------------------------- LeRobot v3


@pytest.fixture
def v3(tmp_path) -> str:
    from tests.export.v3_fixture import make_mini_lerobot_v3

    return make_mini_lerobot_v3(str(tmp_path / "v3"))


def test_v3_clean_and_a_shared_video_cut_in_the_middle(cli, v3, tmp_path):
    rd = str(tmp_path / "clean")
    check(cli, v3, rd, "0-5")
    clean = results(rd)
    assert verdicts(clean) == {e: "pass" for e in range(6)}
    files = clean[2]["details"]["files"]
    assert {f.get("window_s") is not None for f in files if f["file"].endswith(".mp4")} == {True}
    truth = S.damage_lerobot_v3(v3)
    rd = str(tmp_path / "cut")
    check(cli, v3, rd, "0-5")
    recs = results(rd)
    assert verdicts(recs) == {0: "pass", 1: "pass", 2: "fail", 3: "fail", 4: "pass", 5: "pass"}
    assert codes(recs[2])[0] == ("reject", "file_truncated") and codes(recs[3])[0] == ("reject", "file_truncated")
    assert all(truth[e] == "pass" for e in (0, 1, 4, 5))


# ---------------------------------------------------------------- mcap


pytest.importorskip("mcap", reason="mcap is needed for the mcap format")


@pytest.fixture(scope="module")
def mini_mcap(tmp_path_factory) -> str:
    pytest.importorskip("mcap_ros2", reason="mcap-ros2-support writes the fixture")
    from parity.fixtures import make_mini_mcap

    return make_mini_mcap(str(tmp_path_factory.mktemp("mcap") / "mini_mcap"))


def test_mcap_damage(cli, mini_mcap, tmp_path):
    ds = str(tmp_path / "mcap")
    shutil.copytree(mini_mcap, ds)
    rd = str(tmp_path / "clean")
    check(cli, ds, rd)
    assert verdicts(results(rd)) == {0: "pass", 1: "pass", 2: "pass", 3: "abstain", 4: "pass", 5: "pass",
                                     6: "pass", 7: "abstain"}
    assert results(rd)[0]["details"]["files"][0]["crc"] == "mcap_chunk"
    S.damage_mcap(ds)
    rd = str(tmp_path / "damaged")
    check(cli, ds, rd)
    recs = results(rd)
    assert codes(recs[2])[0] == ("reject", "structure_invalid") and "摘要区" in recs[2]["details"]["reason"]
    assert codes(recs[6])[0] in (("reject", "crc_mismatch"), ("reject", "structure_invalid"))
    assert codes(recs[4])[0] in (("reject", "file_truncated"), ("suspect", "cut_off"))
    assert ("suspect", "rate_outlier") in codes(recs[1]) and recs[1]["verdict"] == "abstain"
    assert "topic /observation.images.exterior 的频率" in recs[1]["details"]["reason"]
    assert [c for c in codes(recs[1]) if c[1] == "rate_outlier"] == [("suspect", "rate_outlier")]
    assert verdicts(recs)[0] == verdicts(recs)[5] == "pass"


def test_mcap_chunk_crc(tmp_path):
    """L2: an uncompressed chunk whose bytes changed fails its CRC."""
    from mcap.writer import CompressionType, Writer

    path = str(tmp_path / "plain.mcap")
    with open(path, "wb") as fh:
        w = Writer(fh, compression=CompressionType.NONE)
        w.start()
        sid = w.register_schema("s", "raw", b"")
        ch = w.register_channel("/x", "raw", sid)
        for i in range(200):
            w.add_message(ch, i, b"payload-%05d" % i, i)
        w.finish()
    with open(path, "r+b") as fh:
        data = fh.read()
        at = data.index(b"payload-00100")
        fh.seek(at)
        fh.write(b"PAYLOAD")
    rep = F.FileReport("plain.mcap", "mcap", os.path.getsize(path))
    F.mcap_l1(path, rep)
    assert rep.findings == []
    F.mcap_l2(path, rep)
    assert [f.code for f in rep.findings] == ["crc_mismatch"] and "数据块" in rep.findings[0].message


# ---------------------------------------------------------------- the whole chain


def test_on_a_clean_dataset_the_verdicts_do_not_change(mini_dataset, tmp_path):
    """design doc 14 §8 ①: the chain with the module selected (as tasks select it by default) gives
    the same passed / reject / held lists as without it; review only gains its suspects' cards."""
    from .fakevlm_server import FakeVlmServer
    from .pipeline import Chain

    v1 = "timestamp_check,kinematic_limits,motion_quality,visual_quality,video_action_sync,task_success,dedup,skill_profile"

    def lists(rd: str) -> dict[str, list]:
        out = {}
        for name in ("passed", "reject", "held", "review"):
            with open(os.path.join(rd, "revisions", "r0001", f"{name}.json"), encoding="utf-8") as fh:
                out[name] = json.load(fh)["episodes"]
        return out

    with FakeVlmServer() as vlm:
        plain = Chain(mini_dataset, str(tmp_path / "plain"), vlm.url)
        plain.front()
        plain.funnel()
        plain.post()
        c = Chain(mini_dataset, str(tmp_path / "integrity"), vlm.url)
        c.front()
        c.step("plan", "plan", "--preflight", c.path("preflight.json"), "--modules", f"{MOD},{v1}",
               "--episodes", "0-7", "--out", c.path("plan.json"))
        os.makedirs(c.path("stages"))
        c.step("integrity", "check", "--modules", MOD, *c.common(), "--episodes", "0-7",
               "--survivors-out", c.path("stages", "integrity.txt"))
        c.funnel("@" + c.path("stages", "integrity.txt"))
        c.post()
    with open(c.path("stages", "integrity.txt"), encoding="utf-8") as fh:
        assert fh.read().split() == [str(e) for e in range(8)]          # suspects go on
    a, b = lists(plain.rd), lists(c.rd)
    for name in ("passed", "reject", "held"):
        assert b[name] == a[name], name
    theirs = []
    for entry in b["review"]:
        items = [i for i in entry["review"] if i["kind"] != "integrity_suspect"]
        if items:
            theirs.append({**entry, "review": items})
    assert theirs == a["review"]
    added = {e["episode_index"] for e in b["review"] for i in e["review"] if i["kind"] == "integrity_suspect"}
    assert added == {3}                                  # 7, its byte copy, is dedup's reject: not asked
    with open(c.path("revisions", "r0001", "report.json"), encoding="utf-8") as fh:
        report = json.load(fh)
    [sec] = [s for s in report["modules"] if s["id"] == MOD]
    assert sec["summary"]["integrity_outcomes"] == {"pass": 6, "reject": 0, "suspect": 2}
    assert sec["summary"]["integrity_files"]["files"] == 24
    with open(c.path("revisions", "r0001", "report.md"), encoding="utf-8") as fh:
        assert "### 数据完整性" in fh.read()
