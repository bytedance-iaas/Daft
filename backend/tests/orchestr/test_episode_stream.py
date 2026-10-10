"""Exercise the actual check pool's streaming handoff, persistence and stop logic."""
import os
import threading
from collections import deque
from types import SimpleNamespace

from curation.pipeline.check_stage import StageOptions, StageRun
from curation.pipeline.episode_state import EpisodeState, state_path
from curation.pipeline.records import record_v2


def test_check_pool_refills_after_fast_episode_and_commits_before_notification(tmp_path):
    path = state_path(tmp_path)
    store = EpisodeState(path)
    store.bootstrap(str(tmp_path), ["timestamp_check"])
    store.close()
    second_done = threading.Event()
    completions = []
    incoming = deque([[0, 1]])

    class Stream:
        def receive(self, timeout=0):
            return incoming.popleft() if incoming else []

        def completed(self, episode, survivor):
            check = EpisodeState(path)
            try:
                assert check.progress_for([episode]) == {episode: "frame"}
            finally:
                check.close()
            completions.append(episode)
            if episode == 1:
                incoming.append([2])
            if episode == 2:
                second_done.set()
                incoming.append(None)

    ctx = SimpleNamespace(stop_requested=False, progress=lambda *a, **k: None,
                          log=lambda *a, **k: None, check_stop=lambda *a: None)
    options = StageOptions(run_dir=str(tmp_path), input_dir="unused",
                           modules=["timestamp_check"], episodes=[0, 1, 2], part="0001",
                           cfg={}, concurrency=2, pipeline_state=str(path),
                           pipeline_next="frame", episode_stream=Stream())
    stage = StageRun(ctx, options, None)
    stage._source = lambda todo: None

    def work(source, episode):
        if episode == 0:
            assert second_done.wait(5), "new work was blocked by the slow episode"
        return {"timestamp_check": record_v2("timestamp_check", episode, True, None, {})}

    stage._work = work
    assert stage.run()["modules"]["timestamp_check"]["episodes"] == {"total": 3, "ok": 3, "error": 0}
    assert completions == [1, 2, 0]
    assert stage.survivors() == [0, 1, 2]


def test_stream_pause_drains_active_work_without_admitting_more(tmp_path):
    path = state_path(tmp_path)
    EpisodeState(path).close()
    incoming = deque([[0], [1]])
    completed = []
    ctx = SimpleNamespace(stop_requested=False, progress=lambda *a, **k: None,
                          log=lambda *a, **k: None, check_stop=lambda *a: None)

    class Stream:
        def receive(self, timeout=0):
            return incoming.popleft() if incoming else None

        def completed(self, episode, survivor):
            completed.append(episode)
            ctx.stop_requested = True

    options = StageOptions(run_dir=str(tmp_path), input_dir="unused",
                           modules=["timestamp_check"], episodes=[0, 1], part="0001",
                           cfg={}, concurrency=1, pipeline_state=str(path),
                           episode_stream=Stream())
    stage = StageRun(ctx, options, None)
    stage._source = lambda todo: None
    stage._work = lambda source, ep: {"timestamp_check": {
        "module": "timestamp_check", "episode_index": ep, "verdict": "pass"}}
    stage.run()
    assert completed == [0]
    assert list(incoming) == [[1]]


def test_a_worker_lets_go_of_the_eef_judge_with_its_model_stage():
    """The EEF judge a worker kept goes with the model session (an EEF stage without a model has none): its
    temporary files - an mcap camera's frames written as a video - must not outlive the worker."""
    from curation.extensions.eef_consistency import mcap_media
    from daemon.orchestr.stage_worker import _release

    closed = []

    class Session:
        def __exit__(self, *exc):
            closed.append("session")

    class Judge:
        def close(self):
            closed.append("eef")
            mcap_media.close()

    video_dir = mcap_media._root()
    _release({"session": Session(), "eef": Judge()})
    _release({"session": None, "eef": Judge()})
    _release(None)
    assert closed == ["session", "eef", "eef"]
    assert not os.path.exists(video_dir)
