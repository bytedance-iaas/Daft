"""Which episodes keep their sync curves (``pipeline.sync_plots`` flagged mode): since the sync review (registry 7.3)
also the aligned episodes whose cameras disagree with each other - the person asked on sync_check needs the curves."""
from __future__ import annotations

from curation.pipeline.episode_checks import _cameras_disagree, sync_plot_worthy


def _det(**per):
    return {"verdict": "aligned", "flagged_cameras": [], "abstained_cameras": [], "per_camera": per}


def test_cameras_that_disagree_keep_their_curves_under_flagged():
    agree = _det(front={"trusted": True, "lag_s": 0.02}, wrist={"trusted": True, "lag_s": 0.05})
    assert not _cameras_disagree(agree) and not sync_plot_worthy("flagged", agree)
    whole = _det(front={"trusted": True, "lag_s": 0.4}, wrist={"trusted": True, "lag_s": 0.05})
    assert _cameras_disagree(whole) and sync_plot_worthy("flagged", whole)
    # an untrusted whole-episode reading does not count; the windowed medians do, whichever camera is trusted
    untrusted = _det(front={"trusted": False, "lag_s": 0.9}, wrist={"trusted": True, "lag_s": 0.0})
    assert not _cameras_disagree(untrusted)
    windowed = _det(front={"trusted": False, "lag_s": 0.9, "lag_windows": {"lag_s": -0.18}},
                    wrist={"trusted": True, "lag_s": 0.0, "lag_windows": {"lag_s": 0.02}})
    assert _cameras_disagree(windowed) and sync_plot_worthy("flagged", windowed)
    # one camera, or no readings: nothing to disagree
    assert not _cameras_disagree(_det(front={"trusted": True, "lag_s": 0.4}))
    assert not _cameras_disagree({"verdict": "aligned"})
    assert sync_plot_worthy("all", agree) and not sync_plot_worthy("off", whole)
