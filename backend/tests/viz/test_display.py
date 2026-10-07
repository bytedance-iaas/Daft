"""The display configuration of a registered dataset (design doc 21 §6, C4 2.7.0 ``VizDisplay``, F15.6):
read with the defaults an editor starts from, saved after a check against the dataset's current model,
applied by the models - cameras, curve groups (LeRobot / Lance), the track - and restored; a task's
mini player takes the cameras, groups and track only, and what a changed dataset left behind is
skipped, never refused."""
from __future__ import annotations

import numpy as np
import pyarrow.parquet as pq
import pytest

from .conftest import assert_error, assert_schema
from .test_api import API, app  # noqa: F401 - the fixture

CONFIG = {
    "layout": {"template": "custom", "cols": 2, "rows": 1,
               "cells": [{"kind": "video", "key": "wrist"}, {"kind": "curve", "key": "arm"}]},
    "cameras": [{"key": "wrist", "name": "腕部", "hidden": False}, {"key": "front", "hidden": True}],
    "curves": {"groups": [
        {"key": "arm", "name": "arm", "unit": "rad", "smart": True,
         "lines": [{"source": "observation.state", "dim": 0, "name": "j0", "role": "state"},
                   {"source": "action", "dim": 0, "name": "j0 cmd", "role": "action"}]},
        {"key": "force", "name": "force", "smart": False,
         "lines": [{"source": "observation.force", "dim": 2, "name": "fz", "role": "other"}]}],
        "hidden": {"arm": ["j0 cmd"]}},
    "track": "flags",
    "playback": {"speed": 1.5, "loop": True},
}


def _put(c, ds, config, key="display-put-1"):
    return c.put(f"{API}/datasets/{ds}/viz/display", json={"config": config}, headers={"Idempotency-Key": key})


def test_the_defaults_before_anything_is_saved(app):
    r = app.get(f"{API}/datasets/{app.ids['v2']}/viz/display")
    assert r.status_code == 200, r.text
    doc = r.json()
    assert_schema("VizDisplay", doc)
    assert (doc["config"], doc["version"], doc["updated_at"]) == (None, 0, None)
    d = doc["defaults"]
    assert [c["key"] for c in d["cameras"]] == ["front", "wrist"] and d["groups_editable"]
    assert [g["key"] for g in d["groups"]] == ["observation_state", "observation_state.gripper", "observation_force"]
    assert len(d["dimensions"]) == 8 + 8 + 3
    assert {"source": "observation.force", "dim": 2, "name": "dim_2", "role": "other"} in d["dimensions"]
    assert {"low_level_task_index", "flags"} <= {t["key"] for t in d["tracks"]}
    model = app.get(f"{API}/datasets/{app.ids['v2']}/viz").json()
    assert model["display"] is None and not any(c["hidden"] for c in model["cameras"])


def test_a_saved_configuration_shapes_the_model_the_curves_and_the_track(app, data_root):
    ds = app.ids["v2"]
    r = _put(app, ds, CONFIG)
    assert r.status_code == 200, r.text
    doc = r.json()
    assert_schema("VizDisplay", doc)
    assert doc["version"] == 1 and doc["config"] == CONFIG and doc["updated_at"]
    # the defaults are still the dataset's own
    assert [g["key"] for g in doc["defaults"]["groups"]][0] == "observation_state"
    model = app.get(f"{API}/datasets/{ds}/viz").json()
    assert_schema("VizDataset", model)
    assert [(c["key"], c["name"], c["hidden"]) for c in model["cameras"]] == [("wrist", "腕部", False), ("front", "front", True)]
    series = [s for s in model["streams"] if s["kind"] == "series"]
    assert [(s["key"], s["unit"], s["smart"]) for s in series] == [("arm", "rad", True), ("force", None, False)]
    assert model["display"] == CONFIG
    primary = {s["key"]: s["primary"] for s in model["annotation_sources"] if s["kind"] == "segments"}
    assert primary["flags"] and not primary["low_level_task_index"]
    tree = {n["id"]: n for n in model["field_tree"]}
    assert {n["name"]: n.get("stream") for n in tree["streams"]["children"]} == {
        "observation.state": "arm", "action": "arm", "observation.force": "force"}
    # the configured group's curve is the dataset's own numbers
    s = app.get(f"{API}/datasets/{ds}/episodes/1/series", params={"stream": "arm"}).json()
    assert_schema("VizSeries", s)
    raw = pq.read_table(data_root / "lerobot_v2/data/chunk-000/episode_000001.parquet").to_pandas()
    assert [ln["name"] for ln in s["lines"]] == ["j0", "j0 cmd"] and s["unit"] == "rad"
    assert np.allclose(s["lines"][0]["values"], np.stack(raw["observation.state"].to_numpy())[:, 0], atol=1e-4)
    assert np.allclose(s["lines"][1]["values"], np.stack(raw["action"].to_numpy())[:, 0], atol=1e-4)
    assert_error(app.get(f"{API}/datasets/{ds}/episodes/1/series", params={"stream": "observation_state"}), "not_found")
    ep = app.get(f"{API}/datasets/{ds}/episodes/1/viz").json()
    assert_schema("VizEpisode", ep)
    assert {t["key"]: t["primary"] for t in ep["annotations"]["tracks"]}["flags"]
    # the same request again is answered as before; the change is in the audit log
    assert _put(app, ds, CONFIG).json()["version"] == 1
    events = app.app.state.runtime.repo.list_events(resource=ds).items
    assert any(e.action == "dataset.update" and e.detail.get("fields") == ["display_config"] for e in events)


def test_restoring_the_defaults(app):
    ds = app.ids["v2"]
    assert _put(app, ds, CONFIG).status_code == 200
    r = app.delete(f"{API}/datasets/{ds}/viz/display", headers={"Idempotency-Key": "display-restore-1"})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert_schema("VizDisplay", doc)
    assert (doc["config"], doc["version"]) == (None, 2)
    model = app.get(f"{API}/datasets/{ds}/viz").json()
    assert [c["key"] for c in model["cameras"]] == ["front", "wrist"] and model["display"] is None
    assert "observation_state" in {s["key"] for s in model["streams"]}
    # saved again after a restore: the version keeps counting
    assert _put(app, ds, {"track": "flags"}, key="display-put-2").json()["version"] == 3


def test_a_configuration_that_does_not_fit_is_refused_item_by_item(app):
    ds = app.ids["v2"]
    bad = {
        "cameras": [{"key": "top"}, {"key": "front"}, {"key": "front"}],
        "curves": {"groups": [{"key": "arm", "name": "arm", "smart": True, "lines": [
            {"source": "observation.state", "dim": 9, "name": "x", "role": "state"},
            {"source": "action", "dim": 0, "name": "a", "role": "action"},
            {"source": "action", "dim": 0, "name": "a", "role": "action"}]}],
            "hidden": {"nope": ["x"]}},
        "track": "low_level_nope",
        "layout": {"template": "custom", "cols": 2, "rows": 1, "cells": [{"kind": "curve", "key": "observation_state"}]},
    }
    body = assert_error(_put(app, ds, bad), "validation_failed")
    problems = body["error"]["details"]["errors"]
    assert [p["field"] for p in problems] == [
        "cameras.0.key", "cameras.2.key", "curves.groups.0.lines.0", "curves.groups.0.lines.2",
        "curves.groups.0.lines.2.name", "curves.hidden.nope", "track", "layout.cells"]
    assert "数据集里没有相机 top" in problems[0]["problem"]
    # a cell naming an automatic group the configuration replaces
    cells = {"curves": CONFIG["curves"], "layout": {"template": "custom", "cols": 1, "rows": 1,
                                                    "cells": [{"kind": "curve", "key": "observation_state"}]}}
    body = assert_error(_put(app, ds, cells, key="display-put-cells"), "validation_failed")
    assert [p["field"] for p in body["error"]["details"]["errors"]] == ["layout.cells.0.key"]
    # the Schema first
    assert_error(_put(app, ds, {"playback": {"speed": 3}}, key="display-put-speed"), "validation_failed")
    assert app.get(f"{API}/datasets/{ds}/viz/display").json()["version"] == 0      # nothing saved


def test_a_task_takes_the_cameras_groups_and_track(app, data_root):
    rt = app.app.state.runtime
    from curation.contracts import modules as registry
    from daemon.repo import protocol as P

    ds = app.ids["v2"]
    assert _put(app, ds, CONFIG).status_code == 200
    rows = [P.TaskModule(task_id="", module_id=m, selected=False, availability="available") for m in registry.ids()]
    task = rt.repo.create_task(P.TaskCreate(
        name="viz", input_source="local", input_uri=str(data_root / "lerobot_v2"), output_uri="tos://b/out",
        delivery_key="tos://b/out", episode_selector={"mode": "all"}, params={}, modules=rows, dataset_id=ds))
    body = app.get(f"{API}/tasks/{task.id}/viz").json()
    assert_schema("VizDataset", body)
    assert body["display"] == {"cameras": CONFIG["cameras"], "curves": {"groups": CONFIG["curves"]["groups"]},
                               "track": "flags"}
    assert [c["key"] for c in body["cameras"]] == ["wrist", "front"]
    s = app.get(f"{API}/tasks/{task.id}/episodes/2/series", params={"stream": "force"}).json()
    assert_schema("VizSeries", s)
    assert [ln["name"] for ln in s["lines"]] == ["fz"]


def test_what_a_changed_dataset_left_behind_is_skipped(app):
    rt = app.app.state.runtime
    ds = app.ids["v2"]
    owner = rt.repo.get_dataset(ds, owner=None).owner_id
    stale = {"cameras": [{"key": "gone", "name": "x"}, {"key": "wrist", "hidden": True}],
             "curves": {"groups": [{"key": "g", "name": "g", "smart": True,
                                    "lines": [{"source": "observation.joint", "dim": 0, "name": "x", "role": "state"}]}]},
             "track": "nope"}
    rt.repo.update_dataset(ds, owner=owner, display_config={"version": 3, "updated_at": 1, "config": stale})
    r = app.get(f"{API}/datasets/{ds}/viz")
    assert r.status_code == 200, r.text
    model = r.json()
    assert_schema("VizDataset", model)
    assert [(c["key"], c["hidden"]) for c in model["cameras"]] == [("wrist", True), ("front", False)]
    assert "observation_state" in {s["key"] for s in model["streams"]}           # no group left: the automatic ones
    assert {s["key"]: s["primary"] for s in model["annotation_sources"]}["low_level_task_index"]
    doc = app.get(f"{API}/datasets/{ds}/viz/display").json()
    assert (doc["version"], doc["config"]) == (3, stale)


@pytest.mark.parametrize("version", ["v3"])
def test_v3_takes_a_configuration_the_same_way(app, version):
    ds = app.ids[version]
    model = app.get(f"{API}/datasets/{ds}/viz").json()
    first = next(s for s in model["streams"] if s["kind"] == "series")
    ln = first["lines"][0]
    cfg = {"curves": {"groups": [{"key": "one", "name": "one", "smart": True,
                                  "lines": [{"source": ln["source"], "dim": ln["dim"], "name": "only", "role": ln["role"]}]}]}}
    assert _put(app, ds, cfg).status_code == 200
    s = app.get(f"{API}/datasets/{ds}/episodes/1/series", params={"stream": "one"}).json()
    assert_schema("VizSeries", s)
    assert [x["name"] for x in s["lines"]] == ["only"] and s["total_points"] > 0


def test_a_column_this_module_did_not_write_is_no_configuration(app):
    rt = app.app.state.runtime
    ds = app.ids["v2"]
    owner = rt.repo.get_dataset(ds, owner=None).owner_id
    rt.repo.update_dataset(ds, owner=owner, display_config={"track": "flags", "cameras": ["wrist", "front"]})
    model = app.get(f"{API}/datasets/{ds}/viz").json()
    assert_schema("VizDataset", model)
    assert model["display"] is None and [c["key"] for c in model["cameras"]] == ["front", "wrist"]
    doc = app.get(f"{API}/datasets/{ds}/viz/display").json()
    assert (doc["config"], doc["version"]) == (None, 0)
    # saving over it starts the versions at 1
    assert _put(app, ds, {"track": "flags"}).json()["version"] == 1
