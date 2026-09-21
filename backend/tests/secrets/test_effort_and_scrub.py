"""The reasoning-effort table (08 §4.1) and the scrubber that keeps secrets out of text."""
from __future__ import annotations

import json
import logging

import pytest

from daemon.secrets.effort import ALL_LEVELS, TABLE_ENV, EffortNotAllowed, EffortTable, load_table
from daemon.secrets.scrub import MASK, Scrubber, clip

DOUBAO = ("minimal", "low", "medium", "high")


@pytest.mark.parametrize("model", [
    "doubao-seed-2-1-pro-260301", "doubao-seed-2-1-turbo-260301", "doubao-seed-evolving",
    "doubao-seed-2-0-pro-260215", "doubao-seed-2-0-lite-260215", "doubao-seed-2-0-mini-260215",
    "doubao-seed-1-8-251228", "doubao-seed-1-6-251015", "Doubao-Seed-2-0-Pro-260215",
    "volcengine/doubao-seed-2-0-pro-260215",
])
def test_doubao_models_get_their_four_effective_levels(model):
    levels = EffortTable().lookup(model)
    assert levels.known and levels.levels == DOUBAO


def test_defaults_follow_the_table():
    t = EffortTable()
    assert t.lookup("doubao-seed-2-1-pro-260301").default == "high"
    assert t.lookup("doubao-seed-2-0-pro-260215").default == "medium"


@pytest.mark.parametrize("model", ["glm-4.5v", "deepseek-v3", "ep-20260921-abcde",
                                   "doubao-seed-1-6-flash-250715", "Qwen2.5-VL-72B", ""])
def test_unknown_models_get_all_seven_levels(model):
    levels = EffortTable().lookup(model)
    assert not levels.known and levels.levels == ALL_LEVELS


def test_check_accepts_null_and_effective_levels_only():
    t = EffortTable()
    t.check("doubao-seed-2-0-pro-260215", None)
    for lv in DOUBAO:
        t.check("doubao-seed-2-0-pro-260215", lv)
    with pytest.raises(EffortNotAllowed) as err:
        t.check("doubao-seed-2-0-pro-260215", "xhigh")
    assert "minimal、low、medium、high" in err.value.message_zh
    assert "等同于 high" in err.value.message_zh
    with pytest.raises(EffortNotAllowed):
        t.check("doubao-seed-2-0-pro-260215", "none")
    for lv in ALL_LEVELS:
        t.check("glm-4.5v", lv)                                   # unknown: the server maps it
    with pytest.raises(EffortNotAllowed):
        t.check("glm-4.5v", "turbo")                              # not an Ark value at all


def test_the_site_can_override_the_table(tmp_path):
    rows = [{"prefix": ["glm-4.5", "glm-4.6"], "levels": ["high", "low"], "default": "low"},
            {"prefix": "doubao-seed-2-0", "levels": ["minimal", "high"]}]
    inline = load_table({TABLE_ENV: json.dumps(rows)})
    assert inline.levels_for("glm-4.5v") == ("low", "high")       # kept in Ark's order
    # the site's rows win even over a longer built-in prefix
    assert inline.levels_for("doubao-seed-2-0-pro-260215") == ("minimal", "high")
    assert inline.levels_for("doubao-seed-1-8-251228") == DOUBAO
    path = tmp_path / "effort.json"
    path.write_text(json.dumps(rows[:1]))
    assert load_table({TABLE_ENV: str(path)}).levels_for("glm-4.6") == ("low", "high")


@pytest.mark.parametrize("raw", ["[{\"prefix\": \"x\", \"levels\": [\"ultra\"]}]", "{}", "[1]",
                                 "/does/not/exist.json", "[{\"levels\": [\"low\"]}]"])
def test_a_broken_override_is_logged_and_ignored(raw, caplog):
    caplog.set_level(logging.ERROR, logger="daemon.secrets")
    table = load_table({TABLE_ENV: raw})
    assert table.levels_for("doubao-seed-2-0-pro-260215") == DOUBAO
    assert TABLE_ENV in caplog.text


def test_scrubber_masks_known_values_and_their_encodings():
    secret = "SK/with+plus=="
    s = Scrubber([secret, None, "", "ab"])                        # short values are ignored
    quoted = "SK%2Fwith%2Bplus%3D%3D"                             # as it appears in a URL
    out = s(" ".join(["bad", secret, "and", quoted, "in a query"]))
    assert secret not in out and quoted not in out and out.count(MASK) == 2
    assert s("about ab") == "about ab"
    assert Scrubber(["abcd", "abcdefgh"])("xx abcdefgh yy") == f"xx {MASK} yy"   # longest first


def test_scrubber_applies_the_log_patterns_too():
    s = Scrubber()
    assert "tok123" not in s("Authorization: Bearer tok123")
    assert "hunter2" not in s("password=hunter2")
    assert s(None) == ""


def test_clip_makes_one_short_line():
    assert clip("a\n b\t c") == "a b c"
    long = clip("x" * 500, 20)
    assert len(long) == 20 and long.endswith("…")
