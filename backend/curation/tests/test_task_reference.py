"""裁决卡上的任务标注(2026-08-21 用户定:标注是每张卡的第一行)。纯函数,离线。"""
from __future__ import annotations

import json
import os

import pytest

from curation.ui import manifest as mf


@pytest.fixture
def run(tmp_path):
    d = tmp_path / "deliv" / "20260821-000000"
    (d / "details").mkdir(parents=True)
    (d / "details" / "task_details.json").write_text(json.dumps({
        "episodes": {
            "ep000001": {"instruction": "open the drawer", "instruction_source": "原始标注"},
            "ep000002": {"instruction": "put cup on plate", "instruction_source": "自产caption"},
            "ep000003": {"instruction": "", "instruction_source": ""},
        }}, ensure_ascii=False), encoding="utf-8")
    (d / "details" / "captions.json").write_text(json.dumps({
        "ep000003": "stack the blocks", "ep000005": "wipe the table"}), encoding="utf-8")
    return str(d)


def _m(run, audit_queue=()):
    return {"path": run, "audit_queue": list(audit_queue), "task_review": []}


def test_task_text_priority_details_then_audit_label_then_caption(run):
    m = _m(run, [{"id": "ep000004", "label": "fold towel", "caption": "fold cloth"},
                 {"id": "ep000001", "label": "SHOULD NOT WIN", "caption": "x"}])
    assert mf.episode_task_text(m, "ep000001") == ("open the drawer", mf.TASK_SOURCE_LABEL)
    assert mf.episode_task_text(m, "ep000002") == ("put cup on plate", mf.TASK_SOURCE_CAPTION)
    assert mf.episode_task_text(m, "ep000003") == ("stack the blocks", mf.TASK_SOURCE_CAPTION), \
        "task_details 里 instruction 为空 → 退到 captions.json"
    assert mf.episode_task_text(m, "ep000004") == ("fold towel", mf.TASK_SOURCE_LABEL), \
        "不在 task_details 的条目用分歧队列里的原始标注"
    assert mf.episode_task_text(m, "ep000005") == ("wipe the table", mf.TASK_SOURCE_CAPTION)
    assert mf.episode_task_text(m, "ep000099") == ("", "")


def test_reference_line_and_question_follow_adopted_relabel(run):
    m = _m(run)
    assert mf.task_reference_md(m, "ep000001") == "任务:「open the drawer」 · 来源:原始标注"
    assert mf.task_reference_md(m, "ep000099") == "任务:(交付里没有记录这条的任务文本)"
    adopted = {"decision": "采纳建议改标", "new_label": "open the top drawer"}
    assert "open the top drawer" in mf.task_reference_md(m, "ep000001", adopted)
    assert "已改标" in mf.task_reference_md(m, "ep000001", adopted)
    kept = {"decision": "维持原标注", "new_label": "ignored"}
    assert mf.task_reference_md(m, "ep000001", kept).endswith("来源:原始标注")
    assert mf.task_question_md(m, "ep000001") == "按任务「open the drawer」看,这条完成了吗?"
    assert "open the top drawer" in mf.task_question_md(m, "ep000001", adopted)
    assert "没有记录任务文本" in mf.task_question_md(m, "ep000099")


def test_review_table_has_task_column_truncated(run):
    m = _m(run)
    m["task_review"] = [{"id": "ep000001", "current": "通过", "reason": "r", "readings": {}}]
    (lambda: None)()
    long = "x" * 80
    td = json.loads(open(os.path.join(run, "details", "task_details.json"), encoding="utf-8").read())
    td["episodes"]["ep000001"]["instruction"] = long
    open(os.path.join(run, "details", "task_details.json"), "w", encoding="utf-8").write(json.dumps(td))
    os.utime(os.path.join(run, "details", "task_details.json"), None)
    mf._DETAILS_JSON_CACHE.clear()
    rows = mf.merged_queue_rows(m)
    assert mf.QUEUE_HEADERS[2] == "原始标注"
    assert rows[0][2] == long                    # 标注来自 task_details(120 内不截断)
    assert rows[0][1] == "成败弃权" and len(rows[0]) == len(mf.QUEUE_HEADERS)
    # 判定用的是自产 caption 的条目:文本进「自产描述」列,不冒充原始标注
    td = json.loads(open(os.path.join(run, "details", "task_details.json"),
                         encoding="utf-8").read())
    td["episodes"]["ep000001"]["instruction"] = "put cup on plate"
    td["episodes"]["ep000001"]["instruction_source"] = "自产caption"
    open(os.path.join(run, "details", "task_details.json"), "w",
         encoding="utf-8").write(json.dumps(td))
    os.utime(os.path.join(run, "details", "task_details.json"), None)
    mf._DETAILS_JSON_CACHE.clear()
    row = mf.merged_queue_rows(m)[0]
    assert row[2] == "" and row[3] == "put cup on plate"


def test_missing_or_broken_details_never_raise(tmp_path):
    m = {"path": str(tmp_path), "audit_queue": [], "task_review": []}
    assert mf.episode_task_text(m, "ep0") == ("", "")
    (tmp_path / "details").mkdir()
    (tmp_path / "details" / "task_details.json").write_text("{not json")
    mf._DETAILS_JSON_CACHE.clear()
    assert mf.episode_task_text(m, "ep0") == ("", "")
    assert mf.task_reference_md({}, "ep0").startswith("任务:(")


def test_reference_html_is_prominent_and_escaped(run):
    m = _m(run)
    h = mf.task_reference_html(m, "ep000001")
    assert "open the drawer" in h and "#165DFF" in h and "font-weight:700" in h
    assert "来源:原始标注" in h
    adopted = {"decision": "采纳建议改标", "new_label": "<b>x</b>"}
    h2 = mf.task_reference_html(m, "ep000001", adopted)
    assert "&lt;b&gt;x&lt;/b&gt;" in h2 and "已改标" in h2, "用户输入的改标文本必须转义"
    assert "没有记录" in mf.task_reference_html(m, "ep000099")
