"""Exercise the actual check pool's streaming handoff, persistence and stop logic."""
import threading
from collections import deque
from types import SimpleNamespace

from curation.pipeline.check_stage import StageOptions, StageRun
from curation.pipeline.episode_state import EpisodeState, state_path


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
        return {"timestamp_check": {"module": "timestamp_check",
                                    "episode_index": episode, "verdict": "pass"}}

    stage._work = work
    assert stage.run()["modules"]["timestamp_check"]["episodes"]["pass"] == 3
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
