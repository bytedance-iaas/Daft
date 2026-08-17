"""`curation reprofile` 钉子:按标注优先方针重算老交付的画像。

防的事故(droid-200-new 实锤,方针变更的直接起因):老交付按"全员 caption 归类"
跑,26 条错 caption 把条目归错格子;reprofile 要能**不重跑 VLM、不重归纳体系**,
只用交付里已有的东西把它们搬回按标注归的格子——且成败判定结论一个字不许变、
连跑两次第二次 0 变化(幂等)。
"""
from __future__ import annotations

import csv
import json
import os

import pytest

from curation.pipeline.reprofile import run_reprofile

WIPE_LABEL = "Wipe the table with the yellow towel"     # 客户原话(大小写照抄)
WIPE_CAP = "wipe the table with the yellow towel"       # ep1 的 caption(正确)
WRONG_CAP = "fold the yellow towel"                     # ep2 的 caption(看错了)
CUP_CAP = "pick up the cup"


def _profile():
    def sub(eps, caps):
        return {"count": len(eps), "pct": round(100.0 * len(eps) / 3, 2),
                "episodes": eps, "captions_top": caps, "raw_labels_top": []}
    return {"n_episodes": 3, "n_families": 3,
            "families": {
                "wiping": {"count": 1, "pct": 33.33, "criterion": "cyclic wipe",
                           "subskills": {"wipe-surface": sub(["ep000001"], [WIPE_CAP])}},
                "folding": {"count": 1, "pct": 33.33, "criterion": "deformable fold",
                            "subskills": {"fold-cloth": sub(["ep000002"], [WRONG_CAP])}},
                "grasping": {"count": 1, "pct": 33.33, "criterion": "grasp",
                             "subskills": {"pick-up": sub(["ep000003"], [CUP_CAP])}}},
            "undersampled": ["wiping", "folding", "grasping"], "guideline": "G"}


def _write_delivery(tmp_path, legacy_csv=True):
    """老口径交付:CSV 四列(caption 即归类文本),ep000002 被错 caption 归错格子。"""
    det = tmp_path / "details"
    det.mkdir(parents=True, exist_ok=True)
    episodes = {"ep000001": {"判决": "通过", "综合软分": 0.9,
                             "checks": {"任务成败判定": {"结果": "pass"}}},
                "ep000002": {"判决": "通过", "综合软分": 0.8,
                             "checks": {"任务成败判定": {"结果": "pass",
                                                        "detail": "{\"verdict\": \"success\"}"}}},
                "ep000003": {"判决": "通过", "综合软分": 0.7,
                             "checks": {"任务成败判定": {"结果": "弃权"}}}}
    (tmp_path / "passed.json").write_text(json.dumps(
        {"数据集": "d", "skills": _profile(), "episodes": episodes},
        ensure_ascii=False), encoding="utf-8")
    cols = ["episode_id", "family", "subskill", "caption"]
    rows = [["ep000001", "wiping", "wipe-surface", WIPE_CAP],
            ["ep000002", "folding", "fold-cloth", WRONG_CAP],
            ["ep000003", "grasping", "pick-up", CUP_CAP]]
    with open(det / "skill_assignment.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    (det / "captions.json").write_text(json.dumps(
        {"ep000001": WIPE_CAP, "ep000002": WRONG_CAP, "ep000003": CUP_CAP}),
        encoding="utf-8")
    # 标注来源(task_details 级,daft 不在场也能跑):ep2 有原始标注且与 caption 分歧;
    # ep1/ep3 无标注(ep3 的 instruction 是补标的自产 caption,必须被甄别掉)
    (det / "task_details.json").write_text(json.dumps({"episodes": {
        "ep000001": {"episode_id": "ep000001", "instruction": WIPE_CAP,
                     "instruction_source": "自产caption"},
        "ep000002": {"episode_id": "ep000002", "instruction": WIPE_LABEL,
                     "instruction_source": "原始标注"},
        "ep000003": {"episode_id": "ep000003", "instruction": CUP_CAP,
                     "instruction_source": "自产caption"},
    }}, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _read_csv(p):
    with open(p, newline="", encoding="utf-8") as f:
        return {r["episode_id"]: r for r in csv.DictReader(f)}


def test_reprofile_moves_mislabeled_episode_and_keeps_verdicts(tmp_path):
    """核心闭环:错格子的条目按标注搬回正确格子;成败判定逐字节不变;报数正确。"""
    d = _write_delivery(tmp_path)
    before = json.loads((d / "passed.json").read_text(encoding="utf-8"))
    s = run_reprofile(str(d))
    assert s["total"] == 3 and s["n_changed"] == 1
    assert s["n_caption_to_label"] == 1                # 从 caption 归改成按标注归
    assert s["n_unassigned"] == 0 and s["n_label_missing_kept"] == 0
    assert s["changed"][0]["episode_id"] == "ep000002"
    assert s["changed"][0]["new_family"] == "wiping"

    after = json.loads((d / "passed.json").read_text(encoding="utf-8"))
    # 护栏二:成败判定结论一个字不许变——skills 以外的所有键逐字节相同
    for k in before:
        if k != "skills":
            assert after[k] == before[k], f"reprofile 动了 {k}"
    prof = after["skills"]
    assert prof["families"]["wiping"]["subskills"]["wipe-surface"][
        "episodes"] == ["ep000001", "ep000002"]
    assert "folding" not in prof["families"]           # 搬空的族不再出现
    assert prof["families"]["wiping"]["criterion"] == "cyclic wipe"   # 判据回移植
    assert prof["guideline"] == "G"

    by = _read_csv(d / "details" / "skill_assignment.csv")
    assert by["ep000002"]["family"] == "wiping"
    assert by["ep000002"]["grouping_text"] == WIPE_LABEL
    assert by["ep000002"]["grouping_text_source"] == "原始标注"
    assert by["ep000002"]["caption"] == WRONG_CAP      # caption 列语义不变,照旧陈列
    assert by["ep000001"]["grouping_text_source"] == "自产caption"
    assert os.path.exists(d / "details" / "reprofile_results.json")


def test_reprofile_is_idempotent_second_run_zero_changes(tmp_path):
    """幂等:同一份交付连跑两次,第二次 0 条变化且零写盘(文件内容逐字节相同)。"""
    d = _write_delivery(tmp_path)
    run_reprofile(str(d))
    csv1 = (d / "details" / "skill_assignment.csv").read_text(encoding="utf-8")
    passed1 = (d / "passed.json").read_text(encoding="utf-8")
    s2 = run_reprofile(str(d))
    assert s2["n_changed"] == 0 and s2["changed"] == []
    assert (d / "details" / "skill_assignment.csv").read_text(encoding="utf-8") == csv1
    assert (d / "passed.json").read_text(encoding="utf-8") == passed1


def test_reprofile_never_recaptions_or_reinduces(tmp_path, monkeypatch):
    """不重跑 caption、不重新归纳体系(用户明令):相应函数一次都不许被调。"""
    import curation.dataset_level.caption as C
    import curation.dataset_level.taxonomy as T

    def boom(*a, **k):
        raise AssertionError("reprofile 不许重跑 caption / 重新归纳体系")

    monkeypatch.setattr(T, "induce_taxonomy", boom)
    monkeypatch.setattr(T, "refine_taxonomy", boom)
    monkeypatch.setattr(C, "caption_episodes", boom)
    monkeypatch.setattr(C, "make_vlm_captioner", boom)
    d = _write_delivery(tmp_path)
    assert run_reprofile(str(d))["n_changed"] == 1


def test_reprofile_unmatched_label_stays_unassigned_without_llm(tmp_path):
    """诚实弃权:标注归不进体系、又没配 LLM → 留「未归类」并如实报数,绝不硬猜。"""
    d = _write_delivery(tmp_path)
    det = d / "details"
    td = json.loads((det / "task_details.json").read_text(encoding="utf-8"))
    td["episodes"]["ep000002"]["instruction"] = "Open the airfryer"   # 体系里没有的动作
    (det / "task_details.json").write_text(json.dumps(td, ensure_ascii=False),
                                           encoding="utf-8")
    s = run_reprofile(str(d))
    assert s["n_changed"] == 1 and s["n_unassigned"] == 1
    by = _read_csv(det / "skill_assignment.csv")
    assert by["ep000002"]["family"] == "未归类" and by["ep000002"]["subskill"] == "-"


def test_reprofile_unmatched_label_repaired_once_with_llm(tmp_path):
    """配了 LLM:归不进去的才问一次补漏(只允许指认既有类),问完各归其位。"""
    d = _write_delivery(tmp_path)
    det = d / "details"
    td = json.loads((det / "task_details.json").read_text(encoding="utf-8"))
    td["episodes"]["ep000002"]["instruction"] = "Open the airfryer"
    (det / "task_details.json").write_text(json.dumps(td, ensure_ascii=False),
                                           encoding="utf-8")
    calls = []

    def llm(prompt):
        calls.append(prompt)
        assert "left out" in prompt                     # 只走补漏,不走归纳
        return json.dumps({"map": [{"caption": "Open the airfryer",
                                    "family": "grasping", "subskill": "pick-up"}]})

    s = run_reprofile(str(d), llm_ask=llm)
    assert len(calls) == 1 and s["n_unassigned"] == 0
    assert _read_csv(det / "skill_assignment.csv")["ep000002"]["family"] == "grasping"


def test_reprofile_without_label_sources_holds_everything(tmp_path):
    """取不到标注(无 parquet 也无 task_details)→ 全员维持原归类并如实报数,不许猜。"""
    d = _write_delivery(tmp_path)
    os.remove(d / "details" / "task_details.json")
    before = (d / "passed.json").read_text(encoding="utf-8")
    s = run_reprofile(str(d))
    assert s["n_changed"] == 0 and s["n_label_missing_kept"] == 3
    assert (d / "passed.json").read_text(encoding="utf-8") == before


def test_reprofile_refuses_flat_degraded_profile(tmp_path):
    """降级扁平画像(无两级体系)→ 如实退出不硬造:体系不重新归纳,巧妇难为。"""
    (tmp_path / "passed.json").write_text(json.dumps(
        {"skills": {"n_skills": 1, "skills": {"pick": {"count": 2}}},
         "episodes": {}}, ensure_ascii=False), encoding="utf-8")
    s = run_reprofile(str(tmp_path))
    assert "两级技能体系" in s["note"]


def test_reprofile_prefers_parquet_instruction_with_source_screening(tmp_path):
    """parquet 优先且按溯源列甄别:instruction_source=自产caption补标 的行不算标注
    (把我们自己补写的 caption 当客户标注用,正是这次方针要纠正的错误)。"""
    daft = pytest.importorskip("daft")
    d = _write_delivery(tmp_path)
    os.remove(d / "details" / "task_details.json")     # 逼它只认 parquet
    daft.from_pydict({
        "episode_id": ["ep000001", "ep000002", "ep000003"],
        "instruction": [WIPE_CAP, WIPE_LABEL, CUP_CAP],
        "instruction_source": ["自产caption补标", "原始标注", "自产caption补标"],
    }).write_parquet(str(d / "episodes_parquet"))
    s = run_reprofile(str(d))
    assert s["n_changed"] == 1                          # 只有 ep2 有真标注 → 只它搬家
    by = _read_csv(d / "details" / "skill_assignment.csv")
    assert by["ep000002"]["family"] == "wiping"
    assert by["ep000001"]["grouping_text_source"] == "自产caption"   # 补标不冒充标注
