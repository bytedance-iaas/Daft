"""The visualizer's format logic (curation/viz): names, cameras, curve groups, annotations, series."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile

import numpy as np
import pytest

from curation.viz import annotations as A
from curation.viz import lerobot_info as L
from curation.viz.groups import curve_groups
from curation.viz.series import episode_times, read_episode_columns, thin, window

from .fixtures import LENGTHS, SUBTASKS, make_v2, make_v3


# ---------------------------------------------------------------- info.json

@pytest.mark.parametrize("names, size, want", [
    (["a", "b"], 2, ["a", "b"]),
    ({"motors": ["a", "b"]}, 2, ["a", "b"]),                 # FastUMI
    ([["a", "b"]], 2, ["a", "b"]),                           # G1
    (None, 3, None),                                         # RH20T
    (["a"], 2, None),                                        # too few for the shape
    (["a", "b", "c"], 2, ["a", "b"]),
])
def test_flat_names(names, size, want):
    assert L.flat_names(names, size) == want


def test_cameras_codecs_and_transcode():
    info = {"fps": 20, "features": {
        "observation.images.left": {"dtype": "video", "shape": [720, 1280, 3],
                                    "info": {"video.codec": "mpeg4", "video.width": 1280, "video.height": 720}},
        "observation.images.cam_third": {"dtype": "video", "shape": [3, 480, 640], "info": {"video.codec": "av1"}},
        "observation.images.old": {"dtype": "video", "shape": [96, 96, 3],
                                   "video_info": {"video.codec": "libsvtav1", "video.fps": 10.0, "video.pix_fmt": "yuv420p10le"}},
    }}
    cams = {c["name"]: c for c in L.camera_info_of(info)}
    assert cams["left"]["needs_transcode"] and (cams["left"]["width"], cams["left"]["height"]) == (1280, 720)
    assert (cams["cam_third"]["width"], cams["cam_third"]["height"]) == (640, 480)        # [c, h, w]
    assert cams["old"]["codec"] == "av1" and cams["old"]["fps"] == 10.0 and not cams["old"]["needs_transcode"]
    assert L.codec_string("av1", "yuv420p10le") == "av01.0.08M.10" and L.codec_string("h264") == "avc1.640028"
    assert L.needs_transcode(None) is False and L.needs_transcode("hevc") is False
    assert L.depth_features({"features": {"observation.images.front.depth": {"dtype": "uint16", "shape": [480, 640]},
                                          "observation.depths.cam": {"dtype": "uint16", "shape": [240, 424]},
                                          "observation.state": {"dtype": "float32", "shape": [6]}}}) == [
        "observation.images.front.depth", "observation.depths.cam"]


# ---------------------------------------------------------------- curve groups (§5.3)

def _f(n, names=None, dtype="float32"):
    return {"dtype": dtype, "shape": [n], "names": names}


def test_groups_pair_state_action_split_by_prefix_and_gripper():
    sides = [f"{s}_{a}" for s in ("left", "right") for a in ("x", "y", "z", "roll", "pitch", "yaw")]
    names = sides[:6] + ["left_gripper"] + sides[6:] + ["right_gripper"]
    groups = curve_groups({"features": {"observation.state": _f(14, {"motors": names}),
                                        "action": _f(14, {"motors": names}),
                                        "observation.force": _f(3)}})
    by = {g.key: g for g in groups}
    assert [g.key for g in groups] == ["observation_state.left", "observation_state.right",
                                       "observation_state.gripper", "observation_force"]
    assert [(ln.name, ln.role) for ln in by["observation_state.left"].lines][:2] == [("left_x", "state"), ("left_x", "action")]
    # the dataset's own names (2026-10-04: nothing translated)
    assert by["observation_state.left"].name == "observation.state / action · left"
    assert by["observation_state.gripper"].name == "observation.state / action · gripper" and len(by["observation_state.gripper"].lines) == 4
    assert by["observation_force"].smart is False and [ln.role for ln in by["observation_force"].lines] == ["other"] * 3


def test_groups_split_columns_pair_by_suffix_and_unpaired_are_not_smart():
    groups = curve_groups({"features": {
        "observation.state.left_arm": _f(6, [f"l{i}" for i in range(6)], "float64"),
        "observation.state.left_arm.velocities": _f(6, None, "float64"),
        "action.left_arm": _f(6, [f"t{i}" for i in range(6)], "float64"),
        "observation.state.left_gripper": _f(1, ["g"], "float64"),
        "action.left_gripper": _f(1, ["tg"], "float64"),
    }})
    smart = [g.key for g in groups if g.smart]
    assert "observation_state_left_arm" in smart and "observation_state_left_gripper" in smart
    arm = next(g for g in groups if g.key == "observation_state_left_arm")
    assert {ln.source for ln in arm.lines} == {"observation.state.left_arm", "action.left_arm"}
    assert not next(g for g in groups if "velocities" in g.key).smart


def test_groups_without_names_chunk_by_seven_and_long_names_by_family():
    groups = curve_groups({"features": {"observation.state": _f(15), "action": _f(8)}})        # RH20T
    assert [(g.name, len(g.lines)) for g in groups] == [("observation.state · 1–7", 7), ("observation.state · 8–14", 7),
                                                       ("observation.state · 15–15", 1), ("action", 8)]   # no names, no lengths alike: not paired
    hiw = [f"k{side}{j}.q" for side in ("Left", "Right") for j in ("HipPitch", "HipRoll", "Knee", "Ankle", "Shoulder")] + ["kWaistYaw.q"]
    groups = curve_groups({"features": {"observation.state": _f(len(hiw), hiw)}})
    names = [g.name for g in groups]
    assert any("Left" in n for n in names) and any("Right" in n for n in names)
    assert all(len(g.lines) <= 8 for g in groups)


# ---------------------------------------------------------------- annotations (§4.5)

def test_detect_sources_on_the_known_layouts(tmp_path):
    root = make_v2(str(tmp_path / "v2"))
    info = json.load(open(f"{root}/meta/info.json"))
    srcs = A.detect_sources(info, ["meta/episodes.jsonl", "meta/info.json", "meta/subtasks.jsonl", "meta/tasks.jsonl"],
                            episode_fields={"task_status"})
    by = {s.key: s for s in srcs}
    assert by["low_level_task_index"].table == "meta/subtasks.jsonl" and by["low_level_task_index"].primary
    assert by["flags"].columns == ["is_intervention_segment"] and not by["flags"].primary
    assert by["subtask"].format == "string_column"
    assert by["episode:task_status"].kind == "labels"
    # an index column with no table and no annotation-like name is not an annotation
    assert A.detect_sources({"features": {"source.state_step_index": {"dtype": "int64"}}}, []) == []
    unknown = A.detect_sources({"features": {"annotation.failure.failure_type_id": {"dtype": "int8"}}}, [])
    assert [(s.format, s.supported) for s in unknown] == [("unknown", False)]
    rlds = A.detect_sources({"features": {"is_first": {"dtype": "bool"}, "is_last": {"dtype": "bool"},
                                          "is_episode_successful": {"dtype": "bool"}}}, [])
    assert [(s.key, s.kind) for s in rlds] == [("is_episode_successful", "labels")]


def test_episode_annotations_tracks_events_and_placeholders():
    srcs = A.detect_sources({"features": {
        "low_level_task_index": {"dtype": "int64"}, "subtask": {"dtype": "string"},
        "is_error_segment": {"dtype": "bool"}, "language_events": {"dtype": "language"}}},
        ["meta/subtasks.jsonl"], episode_fields={"task_status"})
    times = [i * 0.1 for i in range(6)]
    ann = A.episode_annotations(srcs, {
        "low_level_task_index": [0, 0, 1, 1, 1, 2], "subtask": ["TODO"] * 6,
        "is_error_segment": [0, 1, 1, 0, 0, 0],
        "language_events": [[], [{"content": "grasp", "timestamp": 0.1}], [{"content": "grasp", "timestamp": 0.1}], [], [], []],
    }, times, lookups={"meta/subtasks.jsonl": {0: "a", 1: "b"}}, episode_row={"task_status": "recovered"})
    steps = next(t for t in ann.tracks if t["key"] == "low_level_task_index")
    assert steps["primary"] and [(s["label"], s["start_s"], s["end_s"]) for s in steps["segments"]] == [
        ("a", 0.0, 0.2), ("b", 0.2, 0.5), ("low_level_task_index = 2", 0.5, 0.6)]
    flags = next(t for t in ann.tracks if t["key"] == "flags")
    assert [(s["flags"], s["start_s"], s["end_s"]) for s in flags["segments"]] == [(["is_error_segment"], 0.1, 0.3)]
    assert ann.events == [{"t_s": 0.1, "label": "grasp", "outcome": None, "source": "language_events"}]
    assert ann.labels == [{"key": "episode:task_status", "name": "任务状态", "value": "recovered", "source": "episode 表的 task_status"}]
    assert [w["code"] for w in ann.warnings] == ["annotation_placeholder"]


def test_galaxea_steps_carry_their_quality():
    srcs = A.detect_sources({"features": {k: {"dtype": "int64"} for k in
                                          ("task_index", "coarse_task_index", "quality_index", "coarse_quality_index")}},
                            ["meta/tasks.jsonl"])
    ann = A.episode_annotations(srcs, {"task_index": [3, 3, 4], "quality_index": [9, 9, 9], "coarse_quality_index": [8] * 3},
                                [0.0, 0.1, 0.2], tasks={3: "抓@grasp", 4: "放@place", 8: "unqualified", 9: "qualified"})
    track = ann.tracks[0]
    assert [(s["label"], s["quality"]) for s in track["segments"]] == [("抓@grasp", "qualified"), ("放@place", "qualified")]
    assert ann.labels[0]["value"] == "不合格"


def test_argus_files_zip_json_and_errors():
    doc = {"event_labels": [{"t_s": 2.0, "end_s": 4.0, "verb_class": "press lid", "object": "bin", "arm": "right",
                             "contribution": "advancing"}, {"t_s": 0.0, "end_s": 2.0, "verb_class": "approach"}],
           "key_events": [{"t_s": 3.0, "label": "lid opens", "outcome": "success"}],
           "goal_alignment": {"relation": "aligned"}}
    ann = A.argus_annotations(doc)
    assert [s["label"] for s in ann.tracks[0]["segments"]] == ["approach", "press lid · bin"]
    assert ann.tracks[0]["segments"][1]["arm"] == "right" and ann.events[0]["outcome"] == "success"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("labels/episode_000012.json", json.dumps(doc))
        zf.writestr("labels/ep7.json", json.dumps(doc))
    assert sorted(A.read_label_file(buf.getvalue(), "labels.zip")) == [7, 12]
    assert list(A.read_label_file(json.dumps(doc).encode(), "episode_dual_arm__x__002334.json")) == [2334]
    assert list(A.read_label_file(json.dumps({"5": doc}).encode(), "all.json")) == [5]
    assert list(A.read_label_file(json.dumps([{**doc, "episode_index": 3}]).encode(), "all.json")) == [3]
    with pytest.raises(A.AnnotationFileError) as err:
        A.read_label_file(json.dumps({"timeline_x": []}).encode(), "ep1.json")
    assert err.value.errors[0]["code"] == "annotation_unsupported"
    with pytest.raises(A.AnnotationFileError):
        A.read_label_file(json.dumps(doc).encode(), "labels.json")                 # which episode?


# ---------------------------------------------------------------- series

def test_thin_keeps_extremes_and_shared_times():
    t = np.arange(10_000) / 100.0
    y = np.sin(t)
    y[5000] = 9.0
    tt, (yy,), thinned = thin(t, [y], 400)
    assert thinned and len(tt) == len(yy) == 400 and np.all(np.diff(tt) >= 0)
    assert yy.max() == 9.0 and np.isclose(yy.min(), -1.0, atol=1e-3)
    same = thin(t[:100], [y[:100]], 400)
    assert same[2] is False and len(same[0]) == 100
    assert window(t, 1.0, 2.0) == slice(100, 201)


def test_read_v3_episode_reads_only_its_row_groups(tmp_path):
    import pyarrow.parquet as pq

    root = make_v3(str(tmp_path / "v3"))
    path = f"{root}/data/chunk-000/file-000.parquet"
    assert pq.ParquetFile(path).metadata.num_row_groups == 5
    reads = []

    class Spy:
        def __init__(self, fh):
            self.fh = fh

        def read(self, n=-1):
            data = self.fh.read(n)
            reads.append(len(data))
            return data

        def __getattr__(self, name):
            return getattr(self.fh, name)

    with open(path, "rb") as fh:
        cols = read_episode_columns(Spy(fh), ["observation.state", "subtask_index", "timestamp", "nope"],
                                    from_index=LENGTHS[0], to_index=LENGTHS[0] + LENGTHS[1])
    assert set(cols) == {"observation.state", "subtask_index", "timestamp"}
    assert cols["observation.state"].shape == (LENGTHS[1], 8)
    t = episode_times(cols, 10, LENGTHS[1])
    assert t[0] == 0.0 and np.isclose(t[-1], (LENGTHS[1] - 1) / 10)


def test_transcode_cuts_a_window_to_h264(tmp_path):
    import av

    root = make_v2(str(tmp_path / "v2"))
    src = f"{root}/videos/chunk-000/observation.images.wrist/episode_000000.mp4"
    out = str(tmp_path / "out.mp4")
    r = subprocess.run([sys.executable, "-m", "curation.viz.transcode", "--in", src, "--out", out,
                        "--from", "0.5", "--to", "1.5"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stderr.strip().splitlines()[-1]) == {"progress": 1.0, "frames": 10}
    with av.open(out) as inp:
        st = inp.streams.video[0]
        frames = list(inp.decode(st))
        assert st.codec_context.name == "h264" and [round(f.time, 3) for f in frames[:2]] == [0.0, 0.1]
    bad = subprocess.run([sys.executable, "-m", "curation.viz.transcode", "--in", f"{root}/meta/info.json",
                          "--out", out], capture_output=True, text=True)
    assert bad.returncode in (1, 2) and "error" in bad.stderr


def test_subtasks_parquet_lookup(tmp_path):
    root = make_v3(str(tmp_path / "v3"))
    table = A.parse_lookup_parquet(open(f"{root}/meta/subtasks.parquet", "rb").read())
    assert table == dict(enumerate(SUBTASKS))


def test_a_wrong_window_in_the_episode_table_still_reads_the_episodes_rows(tmp_path):
    """svla_so101_index_injected: the table says [6, 309) for an episode whose rows are 0..302."""
    root = make_v3(str(tmp_path / "v3"))
    path = f"{root}/data/chunk-000/file-000.parquet"
    with open(path, "rb") as fh:
        cols = read_episode_columns(fh, ["frame_index", "episode_index", "timestamp"],
                                    from_index=6, to_index=6 + LENGTHS[0], episode_index=0)
    assert len(cols["frame_index"]) == LENGTHS[0] and set(cols["episode_index"].tolist()) == {0}
    backwards = {"timestamp": np.array([0.0, 0.1, 0.05]), "frame_index": np.array([0, 1, 2])}
    assert np.allclose(episode_times(backwards, 10, 3), [0.0, 0.1, 0.2])        # a clock going back is not used
