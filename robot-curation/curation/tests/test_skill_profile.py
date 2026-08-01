"""技能画像的降级路径测试:标签分组判定 + 单级画像统计。

(聚类/命名/embedding 模块 2026-07-16 已删——caption 路线 8 次迭代定稿后成为
唯一主路径,KMeans 路线从未接线;历史见 git log 7396e47 之前。)
"""
from __future__ import annotations

import csv
import numpy as np

from curation.dataset_level.profile import (SKILL_ASSIGNMENT_COLUMNS,
                                            instruction_grouping_available, skill_profile,
                                            skill_assignment_rows, skill_profile_two_level,
                                            write_skill_assignment_csv)


def _rows(instrs, t=50, fps=10.0):
    return [{"episode_id": f"ep{i}", "instruction": s,
             "action": np.zeros((t, 2), dtype=np.float32), "fps": fps}
            for i, s in enumerate(instrs)]


def test_instruction_grouping_preference():
    assert instruction_grouping_available(_rows(["a", "b", "c", ""]))       # 3/4 有标签
    assert not instruction_grouping_available(_rows(["a", "", "", ""]))     # 1/4


def test_profile_counts_and_undersampled():
    rows = _rows(["pick"] * 12 + ["place"] * 7 + ["rare"] * 1)
    prof = skill_profile(rows, [r["instruction"] for r in rows], undersampled_pct=10.0)
    assert prof["n_skills"] == 3
    assert prof["skills"]["pick"]["count"] == 12
    assert prof["skills"]["pick"]["pct"] == 60.0
    assert prof["skills"]["pick"]["avg_len_s"] == 5.0
    assert prof["undersampled"] == ["rare"]


# ───────── episode 归属落盘(2026-07-31):画像要答得出"是哪几条" ─────────
#
# 这组测试钉死一件事:每条 episode 在画像里**有且只有一个**归属,且 CSV 与
# JSON 永远说同一件事。归属一旦漂移(错位一格),整份画像和审计会安静地全错。

FAMS = ["抓取", "抓取", "抓取", "开合", "开合", "未归类"]
SUBS = ["拿起放下", "拿起放下", "推动", "开门", "开门", "-"]
CAPS = ["pick up the cup", "pick up the block", "push the box",
        "open the door", "open the drawer", ""]


def _two_level():
    rows = _rows(["c1", "c2", "c3", "c4", "c5", ""])      # ep0..ep5
    return rows, skill_profile_two_level(rows, FAMS, SUBS, CAPS)


def test_two_level_episodes_exact_and_sorted():
    rows, prof = _two_level()
    subs = prof["families"]
    assert subs["抓取"]["subskills"]["拿起放下"]["episodes"] == ["ep0", "ep1"]
    assert subs["抓取"]["subskills"]["推动"]["episodes"] == ["ep2"]
    assert subs["开合"]["subskills"]["开门"]["episodes"] == ["ep3", "ep4"]
    assert subs["未归类"]["subskills"]["-"]["episodes"] == ["ep5"]
    for f in subs.values():                               # 族级不重复存(子技能并集即族)
        assert "episodes" not in f
    for f in subs.values():
        for s in f["subskills"].values():
            assert s["episodes"] == sorted(s["episodes"])  # 排序稳定 → 两次交付可 diff
            assert len(s["episodes"]) == s["count"]        # count 与名单必须自洽


def test_two_level_episodes_partition_all():
    """并集 == 全部输入:一条不丢、一条不重(分区性质)。"""
    rows, prof = _two_level()
    got = [e for f in prof["families"].values()
           for s in f["subskills"].values() for e in s["episodes"]]
    assert sorted(got) == sorted(r["episode_id"] for r in rows)
    assert len(got) == len(set(got)) == prof["n_episodes"]


def test_flat_profile_episodes():
    rows = _rows(["pick", "place", "pick"])
    prof = skill_profile(rows, [r["instruction"] for r in rows])
    assert prof["skills"]["pick"]["episodes"] == ["ep0", "ep2"]
    assert prof["skills"]["place"]["episodes"] == ["ep1"]
    got = [e for s in prof["skills"].values() for e in s["episodes"]]
    assert sorted(got) == ["ep0", "ep1", "ep2"] and len(got) == len(set(got))


def test_assignment_rows_flat_family_blank():
    """扁平降级路径没有族层 → family 留空串(不拿子技能名冒充族)。"""
    rows = _rows(["pick", "place"])
    prof = skill_profile(rows, [r["instruction"] for r in rows])
    got = skill_assignment_rows(prof)
    assert got == [{"episode_id": "ep0", "family": "", "subskill": "pick", "caption": ""},
                   {"episode_id": "ep1", "family": "", "subskill": "place", "caption": ""}]


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return r.fieldnames, list(r)


def test_assignment_csv_matches_profile(tmp_path):
    """最要紧的一条:CSV 与 JSON 交叉校验,钉死两者不会漂移。"""
    rows, prof = _two_level()
    cap_of = {r["episode_id"]: c for r, c in zip(rows, CAPS)}
    p = write_skill_assignment_csv(str(tmp_path), prof, cap_of)
    headers, got = _read_csv(p)
    assert headers == SKILL_ASSIGNMENT_COLUMNS == [
        "episode_id", "family", "subskill", "caption"]
    assert len(got) == len(rows) == prof["n_episodes"]           # 一行一条,不多不少
    assert [g["episode_id"] for g in got] == sorted(r["episode_id"] for r in rows)
    # 逐行回到画像里核对归属(CSV 说的族/子技能,画像里该条必须真在那个名单上)
    for g in got:
        s = prof["families"][g["family"]]["subskills"][g["subskill"]]
        assert g["episode_id"] in s["episodes"]
        assert g["caption"] == cap_of[g["episode_id"]]
    # 反向:画像里的每个名额都在 CSV 里出现且只出现一次
    assert {(g["episode_id"], g["family"], g["subskill"]) for g in got} == {
        (e, fn, sn) for fn, f in prof["families"].items()
        for sn, s in f["subskills"].items() for e in s["episodes"]}


def test_assignment_csv_flat_and_utf8(tmp_path):
    """降级画像同样落盘;非 ASCII 技能名能写能读(容器 locale 常是 POSIX)。"""
    rows = _rows(["把杯子放进水槽", "把杯子放进水槽"])
    prof = skill_profile(rows, [r["instruction"] for r in rows])
    headers, got = _read_csv(write_skill_assignment_csv(str(tmp_path), prof))
    assert len(got) == 2 and got[0]["subskill"] == "把杯子放进水槽"
    assert got[0]["family"] == "" and got[0]["caption"] == ""


def test_assignment_edge_cases(tmp_path):
    """空输入 / 单条 / 族有成员却无子技能。"""
    empty = skill_profile_two_level([], [], [], [])
    assert empty["n_episodes"] == 0 and skill_assignment_rows(empty) == []
    assert write_skill_assignment_csv(str(tmp_path), empty) is None   # 不留空文件
    assert skill_assignment_rows({}) == [] and skill_assignment_rows(
        {"n_episodes": 0, "skills": {}, "families": {}}) == []

    one = skill_profile_two_level(_rows(["x"]), ["抓取"], ["拿起放下"], ["pick it up"])
    assert one["families"]["抓取"]["subskills"]["拿起放下"]["episodes"] == ["ep0"]
    assert skill_assignment_rows(one, {"ep0": "pick it up"}) == [
        {"episode_id": "ep0", "family": "抓取", "subskill": "拿起放下",
         "caption": "pick it up"}]

    # 边界:族有成员但一个子技能都没有(兜底分支)→ 成员不能凭空消失,子技能留空
    odd = {"n_episodes": 1, "families": {"抓取": {"count": 1, "pct": 100.0,
                                                  "subskills": {}, "episodes": ["ep0"]}}}
    assert skill_assignment_rows(odd) == [
        {"episode_id": "ep0", "family": "抓取", "subskill": "", "caption": ""}]


def test_old_delivery_without_episodes_degrades():
    """老交付的画像没有 episodes 字段 → 反推为空,不能抛异常(下游降级)。"""
    old = {"n_episodes": 2, "families": {"抓取": {"count": 2, "pct": 100.0,
                                                  "subskills": {"拿起放下": {"count": 2}}}}}
    assert skill_assignment_rows(old) == []
    old_flat = {"n_episodes": 1, "skills": {"pick": {"count": 1}}}
    assert skill_assignment_rows(old_flat) == []
