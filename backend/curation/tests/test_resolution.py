"""FILE-7: the picture size the frames have, against what the dataset and the header say.

The comparison is a pure function (``extensions.integrity.resolution``), so every branch is here
without a video file. Carried over from the standalone module of 2026-09-24 (PR 158), whose axis
handling these keep.
"""
from curation.extensions.integrity import resolution as R


def _feature(shape, names=None, dtype="video"):
    out = {"dtype": dtype, "shape": shape}
    if names is not None:
        out["names"] = names
    return out


def test_the_declared_size_comes_out_of_the_shape_whatever_order_the_axes_are_in():
    """LeRobot writes [h, w, c]; datasets in the wild also write [c, h, w]. The feature's own
    names decide when it labels its axes, else a leading channel count means channel-first -
    assuming one order would make every episode of the other kind a false mismatch."""
    assert R.size_of_feature(_feature([720, 1280, 3])) == (720, 1280)
    assert R.size_of_feature(_feature([3, 720, 1280])) == (720, 1280)
    assert R.size_of_feature(_feature([1280, 720, 3], ["width", "height", "channels"])) == (720, 1280)
    assert R.size_of_feature(_feature([3, 1280, 720], {"axes": ["channels", "width", "height"]})) == (720, 1280)
    # a square picture is ambiguous without names and stays as written; nothing to get wrong
    assert R.size_of_feature(_feature([480, 480, 3])) == (480, 480)
    assert R.size_of_feature(_feature("nope")) == (None, None)
    assert R.size_of_feature(_feature([720])) == (None, None)
    assert R.size_of_feature(None) == (None, None)


def test_only_video_features_that_say_a_size_make_a_claim():
    """A camera the dataset says nothing usable about is left out, so nothing is ever compared
    against a guess."""
    info = {"features": {"observation.images.wrist": _feature([720, 1280, 3]),
                         "observation.images.top": _feature([360, 640, 3]),
                         "observation.images.odd": _feature(None),
                         "action": _feature([14], dtype="float32")}}
    assert R.declared(info) == {"observation.images.wrist": (720, 1280),
                                "observation.images.top": (360, 640)}
    assert R.declared({}) == {} and R.declared(None) == {}


def test_a_size_that_matches_is_no_finding():
    assert R.mismatches((720, 1280), (720, 1280), (720, 1280)) == []
    assert R.mismatches((720, 1280), (None, None), None) == []       # nothing to compare against


def test_the_declared_size_and_the_header_are_each_their_own_mismatch():
    """Both comparisons are reported, in pixels as people read them (width x height)."""
    said = R.mismatches((360, 640), (360, 640), (720, 1280))
    assert said == [{"kind": "declared", "expected": [1280, 720], "actual": [640, 360]}]
    header = R.mismatches((360, 640), (720, 1280), (360, 640))
    assert header == [{"kind": "header", "expected": [1280, 720], "actual": [640, 360]}]
    both = R.mismatches((360, 640), (720, 1280), (720, 1280))
    assert [m["kind"] for m in both] == ["declared", "header"]
    assert "实际是 640×360" in R.text("wrist", both)
    assert "元数据声明 1280×720" in R.text("wrist", both) and "容器头写 1280×720" in R.text("wrist", both)


def test_nothing_measured_is_never_a_verdict():
    """A camera whose frames were not decoded (no frames, decode stopped) says nothing at all -
    an unmeasured size must not read as a mismatch against the declaration."""
    assert R.mismatches((None, None), (720, 1280), (360, 640)) == []
    assert R.mismatches((0, 0), (720, 1280), (360, 640)) == []
