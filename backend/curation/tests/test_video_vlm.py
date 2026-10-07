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


def judged(verdict="success", camera="cam", cam_verdict=None):
    """The judgement's answer (D71): the five fields plus the per-camera block."""
    return dict(answer(verdict, camera),
                cameras={camera: {"verdict": cam_verdict or verdict, "reason": "this view",
                                  "camera_check": {}}})


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
    malformed = judged()
    malformed["evidence"][0]["end_s"] = 100
    sent = mock_transport(monkeypatch, [json.dumps(malformed), json.dumps(judged())])
    assess = make_video_assessor("http://test/v1", "video-model", fps=3)
    out = assess([clip()], "pick up object")
    assert {k: out[k] for k in answer()} == answer() and out["cameras"]["cam"]["verdict"] == "success"
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
    sent = mock_transport(monkeypatch, [json.dumps(judged(camera="robot1_camera0")),
                                        json.dumps(judged(camera="robot0_camera0"))])
    assess = make_video_assessor("http://test/v1", "video-model")
    assess([long_clip], "pick up object")
    assert "observation.images.robot0_camera0" in sent[1]["messages"][-1]["content"]


@pytest.mark.parametrize("primary,camera,passed", [
    ("success", "success", True), ("failure", "failure", False),
    ("success", "failure", None), ("failure", "success", None),
    ("failure", "uncertain", None), ("uncertain", "success", None),   # no rescue by a camera (D71)
])
def test_video_decision_has_no_probe_thresholds(monkeypatch, primary, camera, passed):
    monkeypatch.setattr("curation.pipeline.video_task.prepare_videos", lambda *a, **kw: [clip()])
    result = judge_video_episode({}, {}, "pick up object",
                                 lambda *a, **kw: judged(primary, cam_verdict=camera))
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
    # the judgement is ONE request answering for every camera (D71); v1's own rejudge takes the
    # same path for video input, so it is one request too
    sent = mock_transport(monkeypatch, [json.dumps(judged()), json.dumps(judged())])
    scorer = wrap_call(vlm_completion_from_config(cfg), IncidentLog(), step="probe", call_kind="probe")
    deps = TaskDeps(scorer, None, None, lambda *a, **kw: pytest.fail("sparse decode called"))
    result = task_check_episode(cfg, None, deps, video, "pick", "原始标注", 10, None, None, "")
    assert result["passed"] is True
    assert len(sent) == 1
    voter = wrap_voter(build_endstate_voter(cfg), IncidentLog())
    assert rerun_task_success(cfg, video, "pick", scorer, voter).passed is True
    assert len(sent) == 2
    assert all(any(c["type"] == "video_url" for c in p["messages"][0]["content"]) for p in sent)


def test_caption_uses_continuous_video_with_wrapped_client(monkeypatch, video):
    from curation.dataset_level.caption import caption_episodes, make_vlm_captioner
    from curation.pipeline.incidents import IncidentLog, wrap_call

    sent = mock_transport(monkeypatch, ["pick up object"])
    monkeypatch.setattr("curation.adapters.decode.decode_window", lambda *a, **kw: pytest.fail("sparse decode"))
    capper = wrap_call(make_vlm_captioner("http://test/v1", "m"), IncidentLog(), step="caption", call_kind="caption")
    assert caption_episodes([{"episode_id": "ep0", "video": video}], capper) == ["pick up object"]
    assert any(c["type"] == "video_url" for c in sent[0]["messages"][0]["content"])


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


# ---------------------------------------------------------------- camera_check (camera_defects)

def _check(**over):
    base = {"glitch": {"level": "none", "times": [], "note": ""},
            "shake": {"level": "minor", "times": [[0.5, 1.0]], "note": "slight jitter"},
            "contamination": {"level": "severe", "kind": "smudge", "times": [[0, 2]], "note": "smear"}}
    base.update(over)
    return base


def test_camera_check_is_optional_and_normalised():
    from curation.adapters.video_vlm import parse_camera_check

    plain = answer()
    assert parse_assessment(json.dumps(plain), [clip()]) == plain          # absent stays absent
    with_check = dict(answer(), camera_check=_check())
    got = parse_assessment(json.dumps(with_check), [clip()])["camera_check"]
    assert got["glitch"] == {"level": "none", "times": [], "note": ""}
    assert got["shake"] == {"level": "minor", "times": [[0.5, 1.0]], "note": "slight jitter"}
    assert got["contamination"]["level"] == "severe" and got["contamination"]["kind"] == "smudge"
    assert got["problems"] == []
    # malformed pieces degrade to unknown / are dropped, never raise
    bad = parse_camera_check({"glitch": {"level": "awful", "times": [[5, 9], [1, "x"], [0, 1]]},
                              "shake": "yes", "contamination": {"level": "minor", "kind": "mud"}}, [clip()])
    assert bad["glitch"]["level"] == "unknown" and bad["glitch"]["times"] == [[0, 1]]
    assert bad["shake"]["level"] == "unknown"
    assert bad["contamination"] == {"level": "minor", "times": [], "note": "", "kind": "other"}
    assert len(bad["problems"]) == 5          # level, two intervals, missing shake, kind
    assert parse_camera_check("nope", [clip()])["problems"] == ["camera_check is not an object"]
    # the five core fields are still exactly required
    with pytest.raises(ValueError):
        parse_assessment(json.dumps(dict(answer(), extra=1)), [clip()])


def test_the_server_is_asked_to_constrain_the_answer_to_json(monkeypatch):
    """Root cause of the broken answers: nothing forced the model's output to be valid JSON.
    Every video call now sends response_format, and a backend that refuses it is asked plainly
    from then on instead of failing."""
    sent = mock_transport(monkeypatch, [json.dumps(answer())])
    make_video_assessor("http://test/v1", "video-model", tag="endstate")([clip()], "pick up")
    assert sent[0]["response_format"] == {"type": "json_object"}

    class Refuses:
        status_code = 400
        text = '{"error": {"message": "response_format is not supported"}}'

        def raise_for_status(self):
            raise AssertionError("the 400 should have been handled before this")

    replies = [Refuses(), Response(json.dumps(answer()))]
    seen = []

    def post(url, *, json, headers, timeout):
        seen.append(copy.deepcopy(json))
        return replies.pop(0)

    monkeypatch.setattr("requests.post", post)
    out = make_video_assessor("http://test/v1", "video-model", tag="endstate")([clip()], "pick up")
    assert out["verdict"] == "success"
    assert "response_format" in seen[0] and "response_format" not in seen[1]
    assert len(seen) == 2                                  # one refusal, one plain retry


def test_a_defect_report_with_broken_json_costs_only_itself(monkeypatch):
    """Seen on real data: the model wrote `"times":[[2,5] [9,12]]`, a missing comma inside
    camera_check, and the whole review used to fail with ValueError - which made the episode an
    error. The field is cut out, the five core fields stand, and no repair is asked for."""
    core = json.dumps(answer())[:-1]                     # drop the closing brace
    broken = core + ', "camera_check": {"glitch": {"level": "none", "times": []}, ' \
                    '"shake": {"level": "minor", "times": [[2, 5] [9, 12]]}}}'
    sent = mock_transport(monkeypatch, [broken])
    review = make_video_assessor("http://test/v1", "video-model", tag="endstate")([clip()], "pick up")
    assert len(sent) == 1                                # the verdict was never sent back for repair
    assert review["verdict"] == "success" and review["completion"] == 0.9
    assert {k: v["level"] for k, v in review["camera_check"].items() if isinstance(v, dict)} == \
        {"glitch": "unknown", "shake": "unknown", "contamination": "unknown"}
    assert "not valid JSON" in review["camera_check"]["problems"][0]

    # the field first, so the cut has to close over a member in the middle of the object
    first = '{"camera_check": {"shake": {"times": [[2, 5] [9, 12]]}}, ' + json.dumps(answer())[1:]
    sent = mock_transport(monkeypatch, [first])
    review = make_video_assessor("http://test/v1", "video-model", tag="endstate")([clip()], "pick up")
    assert len(sent) == 1 and review["verdict"] == "success"

    # a core field broken is still a repair and then an error, exactly as before the rider
    sent = mock_transport(monkeypatch, ['{"verdict": "success" "task_type": "transient"}',
                                        '{"verdict": "success" "task_type": "transient"}'])
    with pytest.raises(ValueError):
        make_video_assessor("http://test/v1", "video-model", tag="endstate")([clip()], "pick up")
    assert len(sent) == 2


def test_endstate_review_asks_for_camera_check_without_a_repair(monkeypatch):
    from curation.adapters.video_vlm import CAMERA_CHECK_PROMPT

    reply = dict(answer(), camera_check={"glitch": "garbage"})      # malformed report, valid review
    sent = mock_transport(monkeypatch, [json.dumps(reply)])
    review = make_video_assessor("http://test/v1", "video-model", tag="endstate")([clip()], "pick up")
    assert len(sent) == 1                                          # no repair round trip
    assert review["camera_check"]["glitch"]["level"] == "unknown"
    prompt = sent[0]["messages"][0]["content"][0]["text"]
    assert "Independently review ONLY this camera" in prompt and CAMERA_CHECK_PROMPT.strip() in prompt
    sent = mock_transport(monkeypatch, [json.dumps(judged())])
    make_video_assessor("http://test/v1", "video-model")([clip()], "pick up")
    prompt = sent[0]["messages"][0]["content"][0]["text"]
    assert 'more field "cameras"' in prompt and len(sent) == 1       # the judgement asks per camera, once
