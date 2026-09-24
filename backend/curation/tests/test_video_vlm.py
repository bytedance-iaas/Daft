"""Continuous media, transport and production video-judgement regression tests."""
import base64
import copy
import io
import json
from fractions import Fraction

import av
import numpy as np
import pytest

from curation.adapters.video_input import VideoClip, prepare_videos
from curation.adapters.video_vlm import make_video_assessor, parse_assessment
from curation.adapters.vlm_client import vlm_completion_from_config
from curation.pipeline.video_task import judge_video_episode


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "merged.mp4"
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx264", rate=10)
        stream.width, stream.height, stream.pix_fmt = 32, 32, "yuv420p"
        for i in range(30):
            frame = av.VideoFrame.from_ndarray(np.full((32, 32, 3), i * 8, dtype=np.uint8), format="rgb24")
            frame.pts, frame.time_base = i, Fraction(1, 10)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return {"cam": {"path": str(path), "from_ts": 1.0, "to_ts": 2.0}}


def clip(camera="cam"):
    return VideoClip(camera, "data:video/mp4;base64,YWJj", "abc", 0, 2, 20, 3)


def answer(verdict="success", camera="cam"):
    return {"verdict": verdict, "task_type": "transient", "completion": 0.9,
            "reason": "object visibly moves with gripper",
            "evidence": [{"camera": camera, "start_s": 0.2, "end_s": 0.8,
                          "observation": "object is lifted"}]}


def test_clip_preserves_continuous_frames_and_excludes_adjacent_episodes(video):
    clips = prepare_videos(video)
    assert len(clips) == 1 and clips[0].frames == 10
    assert clips[0].start_s == 0 and clips[0].end_s == 1
    raw = base64.b64decode(clips[0].url.split(",", 1)[1])
    with av.open(io.BytesIO(raw)) as container:
        frames = list(container.decode(video=0))
        times = [float(f.pts * f.time_base) for f in frames]
        marks = [f.to_ndarray(format="rgb24").mean() for f in frames]
    assert times == pytest.approx(np.arange(10) / 10, abs=0.001)
    assert marks == pytest.approx(np.arange(10, 20) * 8, abs=3)
    assert "url" not in clips[0].metadata()


def test_clip_decimates_to_fps_on_grid_keeping_original_timestamps(video):
    # 30 帧源 @10fps，窗口 [1.0, 2.0)：相对 0..1s。fps=2 → 网格 0, 0.5，
    # 每个网格点保留其后第一帧（原始第 10、15 帧），PTS 仍是 episode 相对时刻。
    clips = prepare_videos(video, fps=2)
    assert len(clips) == 1 and clips[0].frames == 2
    raw = base64.b64decode(clips[0].url.split(",", 1)[1])
    with av.open(io.BytesIO(raw)) as container:
        frames = list(container.decode(video=0))
        times = [float(f.pts * f.time_base) for f in frames]
        marks = [f.to_ndarray(format="rgb24").mean() for f in frames]
    assert times == pytest.approx([0.0, 0.5], abs=0.001)
    assert marks == pytest.approx([10 * 8, 15 * 8], abs=3)


def test_clip_fps_matches_server_sampling_and_rejects_invalid(video):
    # fps=5 时保留的帧与全帧编码后服务端按 5fps 抽样的帧一致（每个 0.2s 网格一帧）。
    clips = prepare_videos(video, fps=5)
    assert clips[0].frames == 5
    for bad in (0.1, 5.1, 0, float("nan"), True):
        with pytest.raises(ValueError, match="fps"):
            prepare_videos(video, fps=bad)


def test_video_limit_and_missing_camera_are_explicit(video):
    with pytest.raises(ValueError, match="byte limit"):
        prepare_videos(video, max_bytes=10)
    with pytest.raises(ValueError, match="no cameras"):
        prepare_videos({})
    bad = copy.deepcopy(video)
    bad["cam"]["to_ts"] = 1.0
    with pytest.raises(ValueError, match="window"):
        prepare_videos(bad)
    bad["cam"]["to_ts"] = 10
    with pytest.raises(ValueError, match="ends before"):
        prepare_videos(bad)


class Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self.text}}]}


def mock_transport(monkeypatch, responses):
    sent = []
    replies = iter(responses)

    def post(url, *, json, headers, timeout):
        sent.append(copy.deepcopy(json))
        return Response(next(replies))

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("curation.adapters.vlm_client.hedged_request", lambda send, **kw: send(2))
    return sent


def test_video_transport_and_schema_repair(monkeypatch):
    malformed = answer()
    malformed["evidence"][0]["end_s"] = 100
    sent = mock_transport(monkeypatch, [json.dumps(malformed), json.dumps(answer())])
    assess = make_video_assessor("http://test/v1", "video-model", fps=3)
    assert assess([clip()], "pick up object") == answer()
    assert len(sent) == 2
    content = sent[0]["messages"][0]["content"]
    videos = [v for v in content if v["type"] == "video_url"]
    assert videos == [{"type": "video_url", "video_url": {"url": clip().url, "fps": 3}}]
    assert all(v["type"] != "image_url" for v in content)
    assert "Invalid answer" in sent[1]["messages"][-1]["content"]


@pytest.mark.parametrize("mutate", [
    lambda a: a.update(completion=float("nan")),
    lambda a: a.update(completion=True),
    lambda a: a.update(evidence=[]),
    lambda a: a["evidence"][0].update(camera="nonexistent"),
    lambda a: a["evidence"][0].update(start_s=-1),
    lambda a: a["evidence"][0].update(end_s=3),
])
def test_invalid_video_evidence_rejected(mutate):
    a = answer()
    mutate(a)
    with pytest.raises(ValueError):
        parse_assessment(json.dumps(a), [clip()])


def test_evidence_camera_suffix_match_and_repair_message(monkeypatch):
    # doubao-seed-2.0-pro 丢掉长名前缀（observation.images.）时，唯一后缀匹配仍然接受；
    # 歧义或未知名字依旧拒绝，且修复提示词列出供给的相机名。
    long_clip = VideoClip("observation.images.robot0_camera0", clip().url, "abc", 0, 2, 20, 3)
    short = answer(camera="robot0_camera0")
    assert parse_assessment(json.dumps(short), [long_clip]) == short
    for bad in ("camera0", "robot1_camera0", " observation.images.robot0_camera0"):
        a = answer(camera=bad.strip() if bad == " observation.images.robot0_camera0" else bad)
        if bad == " observation.images.robot0_camera0":
            assert parse_assessment(json.dumps(a), [long_clip]) == a
        else:
            with pytest.raises(ValueError):
                parse_assessment(json.dumps(a), [long_clip])
    sent = mock_transport(monkeypatch, [json.dumps(answer(camera="robot1_camera0")), json.dumps(short)])
    assess = make_video_assessor("http://test/v1", "video-model")
    assess([long_clip], "pick up object")
    assert "observation.images.robot0_camera0" in sent[1]["messages"][-1]["content"]


@pytest.mark.parametrize("primary,review,passed", [
    ("success", "success", True), ("failure", "failure", False),
    ("success", "failure", None), ("failure", "success", None),
    ("failure", "uncertain", None), ("uncertain", "success", True),
])
def test_video_decision_has_no_probe_thresholds(monkeypatch, primary, review, passed):
    monkeypatch.setattr("curation.pipeline.video_task.prepare_videos", lambda *a, **kw: [clip()])
    result = judge_video_episode({}, {}, "pick up object",
                                 lambda *a, **kw: answer(primary),
                                 lambda *a, **kw: answer(review))
    assert result.passed is passed
    assert result.detail["input_mode"] == "video"
    assert result.detail["video_evidence"]
    assert not {"voc", "probe_frames", "completions", "completion_gap"} & result.detail.keys()


def test_video_timeout_is_not_failure_and_has_no_image_fallback(monkeypatch):
    monkeypatch.setattr("curation.pipeline.video_task.prepare_videos", lambda *a, **kw: [clip()])

    def broken(*a, **kw):
        raise TimeoutError("video timeout")

    result = judge_video_episode({}, {}, "pick", broken, broken)
    assert result.passed is None and result.detail["rules"] == ["video_score_failed"]


def test_media_preparation_failure_is_execution_error_not_model_abstention():
    from curation.pipeline.video_task import VideoPreparationError

    with pytest.raises(VideoPreparationError):
        judge_video_episode({}, {}, "pick", lambda *a: pytest.fail("model called"), None)


def test_production_factory_wrapper_and_rerun_use_video(monkeypatch, video):
    from curation.pipeline.funnel import TaskDeps, build_endstate_voter, task_check_episode
    from curation.pipeline.incidents import IncidentLog, wrap_call, wrap_voter
    from curation.pipeline.rejudge import rerun_task_success

    cfg = {"checks": {"task_success": {"vlm": {"endpoint": "http://test/v1", "model": "m"}}}}
    sent = mock_transport(monkeypatch, [json.dumps(answer())] * 4)
    scorer = wrap_call(vlm_completion_from_config(cfg), IncidentLog(), step="probe", call_kind="probe")
    voter = wrap_voter(build_endstate_voter(cfg), IncidentLog())
    deps = TaskDeps(scorer, voter, None, lambda *a, **kw: pytest.fail("sparse decode called"))
    result = task_check_episode(cfg, None, deps, video, "pick", "原始标注", 10, None, None, "")
    assert result["passed"] is True
    assert rerun_task_success(cfg, video, "pick", scorer, voter).passed is True
    assert len(sent) == 4
    assert all(any(c["type"] == "video_url" for c in p["messages"][0]["content"]) for p in sent)


def test_caption_uses_continuous_video_with_wrapped_client(monkeypatch, video):
    from curation.dataset_level.caption import caption_episodes, make_vlm_captioner
    from curation.pipeline.incidents import IncidentLog, wrap_call

    sent = mock_transport(monkeypatch, ["pick up object"])
    monkeypatch.setattr("curation.adapters.decode.decode_window", lambda *a, **kw: pytest.fail("sparse decode"))
    capper = wrap_call(make_vlm_captioner("http://test/v1", "m"), IncidentLog(), step="caption", call_kind="caption")
    assert caption_episodes([{"episode_id": "ep0", "video": video}], capper) == ["pick up object"]
    assert any(c["type"] == "video_url" for c in sent[0]["messages"][0]["content"])


def test_profile_prefers_video_description_and_falls_back_on_unobservable():
    from curation.dataset_level.reassign import grouping_text_and_source

    assert grouping_text_and_source("old annotation", "pick up object") == ("pick up object", "自产caption")
    assert grouping_text_and_source("old annotation", "unclear") == ("old annotation", "原始标注")


def test_video_caption_does_not_reuse_old_image_caption(monkeypatch, video):
    from curation.dataset_level.caption import VideoCaptionCache, caption_episodes, make_vlm_captioner

    sent = mock_transport(monkeypatch, ["new video caption"])
    capper = make_vlm_captioner("http://test/v1", "m")
    rows = [{"episode_id": "ep0", "video": video}]
    assert caption_episodes(rows, capper, precomputed={"ep0": "old image caption"}) == ["new video caption"]
    assert len(sent) == 1
    assert caption_episodes(rows, capper, precomputed=VideoCaptionCache({"ep0": "cached video"})) == ["cached video"]
    assert len(sent) == 1


@pytest.mark.parametrize("video_opts", [{"fps": 0}, {"fps": float("nan")}, {"fps": True},
                                       {"max_bytes": 0}, {"max_side": -1}, []])
def test_invalid_video_configuration_rejected_before_running(video_opts):
    from curation.pipeline.config import ConfigError, load_config, validate_config

    cfg = load_config()
    cfg["checks"]["task_success"]["vlm"]["video"] = video_opts
    with pytest.raises(ConfigError, match="video"):
        validate_config(cfg)


def test_resume_rejudges_old_frame_records_and_keeps_current_video_records(monkeypatch):
    from types import SimpleNamespace
    from curation.adapters.video_vlm import PROTOCOL
    from curation.pipeline.check_stage import StageRun

    current = {0: {"verdict": "pass", "details": {"completions": [0, 1]}},
               1: {"verdict": "pass", "details": {"protocol": PROTOCOL}}}
    monkeypatch.setattr("curation.pipeline.check_stage.latest_results", lambda *a: current)
    run = SimpleNamespace(o=SimpleNamespace(episodes=[0, 1], resume=True, run_dir="unused",
        modules=["task_success"], stale={}, task_clients=SimpleNamespace(
            vlm_completion=SimpleNamespace(media_input="video"))), _stale_inflight=lambda: {})
    assert StageRun._todo(run) == ([0], 1, set())
