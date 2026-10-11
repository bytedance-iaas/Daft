"""A dataset declaration's mcap ``fields`` in the record comparison (design doc 25 §6.1, C7 ``semantics.pose`` /
``semantics.joints``).

C7 writes ``fields`` as a list of paths into the decoded message, and the visualizer reads it so: the numbers at each
path, in order, make one frame (list indexes and ``*`` included). The record comparison reads the declaration's
records (``declared.record_block``) the same way; the ``eef-mapping/1.1`` spelling, one dotted path, still reads.
The mini mcap twin's ``/action`` carries the joint track as ``data``, so every spelling of it agrees with the LeRobot
column it was written from.
"""
from __future__ import annotations

import pytest

from .test_eef_record import MAPPING, _bundle, _compare, _lerobot, _mapping

JOINTS = {"units": "rad", "robot": "franka_panda", "reference_frame": "robot_base"}


def _declared(tmp_path, joints: dict, name: str) -> str:
    """The ``eef-mapping/1.1`` file a declaration with these ``semantics.joints`` amounts to (what ``check`` parses)."""
    from curation.extensions.eef_consistency import declared

    block = declared.record_block({"schema_version": "dataset-declaration/1.0", "semantics": {"joints": joints}})
    return _mapping(tmp_path, block, name)


def test_a_declared_fields_list_reads_the_mcap_twin_like_its_column(mini_dataset, tmp_path):
    pytest.importorskip("mcap_ros2", reason="mcap-ros2-support decodes the fixture's cdr")
    from parity.fixtures import make_mini_mcap

    from curation.extensions.eef_consistency import record as RC

    mcap = RC.McapRecords(make_mini_mcap(str(tmp_path / "mcap")))
    traj = _bundle(tmp_path)
    a = _compare(traj, _mapping(tmp_path, MAPPING, "lr.json"), _lerobot(mini_dataset))["sources"]["joints"]
    spellings = (["data"], [f"data.{k}" for k in range(7)], ["data.*"], "data")
    for i, fields in enumerate(spellings):
        b = _compare(traj, _declared(tmp_path, {"topic": "/action", "fields": fields, **JOINTS}, f"mc-{i}.json"),
                     mcap)["sources"]["joints"]
        assert b["alignment"] == "frame_index" and b["status"] == a["status"], (fields, b)
        assert b["residual"] == a["residual"] and b["reasons"] == a["reasons"], fields
    # the paths are concatenated in their order: the joints the other way round are another arm
    rev = _compare(traj, _declared(tmp_path, {"topic": "/action", "fields": [f"data.{k}" for k in range(6, -1, -1)],
                                              **JOINTS}, "rev.json"), mcap)["sources"]["joints"]
    assert rev["status"] == "suspect" and rev["residual"] != a["residual"]


def test_each_path_is_read_in_order_and_a_missing_one_drops_the_message():
    from curation.extensions.eef_consistency import record as RC

    msg = {"header": {"stamp": 7}, "q": [0.1, 0.2], "grip": {"value": 3.0}}
    assert RC.message_vector(msg, ("grip", "q")).tolist() == [3.0, 0.1, 0.2]
    assert RC.message_vector(msg, ("q.1", "q.0")).tolist() == [0.2, 0.1]
    assert RC.message_vector(msg, ("q", "absent")) is None
    assert RC.message_vector({"data": [1.0, 2.0]}, None).tolist() == [1.0, 2.0]        # no fields: by its shape


@pytest.mark.parametrize("fields, expect", [("data", ("data",)), (["data"], ("data",)),
                                            (["pose.position", "pose.orientation"], ("pose.position", "pose.orientation")),
                                            ([], None), (None, None)])
def test_fields_is_a_path_or_a_list_of_them(fields, expect):
    from curation.extensions.eef_consistency import record as RC

    doc = {"schema_version": "eef-mapping/1.1",
           "record": {"joints": {"topic": "/arm", **({} if fields is None else {"fields": fields}), **JOINTS}}}
    spec = RC.parse_mapping(doc).sources["joints"]
    assert spec.fields == expect
    assert spec.as_dict().get("fields") == (", ".join(expect) if expect else None)    # the console shows one string


@pytest.mark.parametrize("fields", [[1], ["data", ""], {"data": 1}, 3])
def test_fields_that_name_no_path_are_refused(fields):
    from curation.extensions.eef_consistency import record as RC

    doc = {"schema_version": "eef-mapping/1.1",
           "record": {"joints": {"topic": "/arm", "fields": fields, **JOINTS}}}
    with pytest.raises(RC.RecordMappingError, match=r"record\.joints\.fields"):
        RC.parse_mapping(doc)
