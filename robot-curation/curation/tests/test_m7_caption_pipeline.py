"""M7 集成验收(全 stub,无 GPU):caption→taxonomy→assign→两级画像→审计。"""
from __future__ import annotations

import json

import numpy as np
import pytest

from curation.dataset_level.audit import audit_labels
from curation.dataset_level.caption import caption_episodes
from curation.dataset_level.profile import skill_profile_two_level
from curation.dataset_level.taxonomy import assign, induce_taxonomy

TAX = {"families": [
    {"name": "manipulate", "subskills": [
        {"name": "place objects", "members": ["place the cup on the table"]}]},
    {"name": "open/close", "subskills": [
        {"name": "microwave", "members": ["close the microwave door"]}]},
    {"name": "fold", "subskills": [
        {"name": "fold cloth", "members": ["fold the cloth"]}]},
]}


def _rows(n=4):
    return [{"episode_id": f"ep{i}", "instruction": ins,
             "video": {"cam": {"path": __file__, "from_ts": 0.0, "to_ts": 1.0}},
             "action": np.zeros((10, 2), dtype=np.float32), "fps": 10.0}
            for i, ins in enumerate(
                ["put cup on table", "close microwave", "bmbfbbfgjjg", "fold the cloth"])]


def test_caption_episodes_with_stub(monkeypatch):
    """captioner 注入;解码失败(path 非视频)→ 空串不崩批。"""
    calls = []

    def fake_captioner(frames):
        calls.append(len(frames))
        return "place the cup on the table."

    import curation.dataset_level.caption as C

    monkeypatch.setattr("curation.adapters.decode.decode_window",
                        lambda *a, **k: ([np.zeros((8, 8, 3), np.uint8)] * 10, np.arange(10)))
    caps = caption_episodes(_rows(), fake_captioner, n_frames=8)
    assert caps == ["place the cup on the table"] * 4     # 尾部句号/引号被剥
    assert all(c == 8 for c in calls)                     # 均匀 8 帧


def test_caption_failure_isolated():
    def broken(frames):
        raise TimeoutError()

    caps = caption_episodes(_rows(), broken)              # decode 也会失败(path 非视频)
    assert caps == ["", "", "", ""]


def test_taxonomy_induce_and_assign():
    seen = {}

    def fake_llm(prompt_text):
        seen["prompt"] = prompt_text
        return "```json\n" + json.dumps(TAX) + "\n```"    # 带 markdown 围栏也要能解析

    tax = induce_taxonomy(["close the microwave door", "fold the cloth",
                           "close the microwave door", ""], fake_llm)
    assert [f["name"] for f in tax["families"]] == ["manipulate", "open/close", "fold"]
    assert "close the microwave door" in seen["prompt"]
    assert seen["prompt"].count("close the microwave door") == 1  # 去重后才进 prompt

    fams, subs = assign(["Close the Microwave Door", "fold the cloth", "???"], tax)
    assert fams == ["open/close", "fold", "未归类"]        # 大小写不敏感
    assert subs == ["microwave", "fold cloth", "-"]


def test_two_level_profile_pct_two_decimals():
    rows = _rows(4)[:3]
    prof = skill_profile_two_level(
        rows, ["manipulate", "open/close", "open/close"],
        ["place objects", "microwave", "microwave"],
        ["place the cup on the table", "close the microwave door", "close the microwave door"])
    assert prof["families"]["open/close"]["pct"] == 66.67          # 两位小数
    assert prof["families"]["open/close"]["subskills"]["microwave"]["count"] == 2
    assert prof["families"]["manipulate"]["subskills"]["place objects"][
        "raw_labels_top"] == ["put cup on table"]                  # 原始标注仅陈列


def test_audit_two_tiers():
    def fake_llm(prompt_text):
        return json.dumps({"map": [
            {"label": "put cup on table", "family": "manipulate"},
            {"label": "close microwave", "family": "open/close"},
            {"label": "bmbfbbfgjjg", "family": "garbage"},
            {"label": "fold the cloth", "family": "fold"},
        ]})

    ids = ["ep0", "ep1", "ep2", "ep3"]
    ins = ["put cup on table", "close microwave", "bmbfbbfgjjg", "fold the cloth"]
    caps = ["place the cup on the table", "place the red object in the drawer",
            "spread the cloth", "grab the cloth"]
    cfams = ["manipulate", "manipulate", "fold", "manipulate"]
    out = audit_labels(ids, ins, caps, cfams, TAX, fake_llm)
    ids_high = {f["id"]: f["reason"] for f in out["high"]}
    assert ids_high["ep2"] == "garbage"
    assert "分歧" in ids_high["ep1"]                       # 开合↔操纵=高对比度,立案
    # ep3: fold(标注) vs manipulate(自产描述)——fold 不在等价类 → 也高置信
    assert "ep3" in ids_high
    assert ids_high.get("ep0") is None                     # 同族不立案


def test_audit_empty_inputs():
    out = audit_labels([], [], [], [], {"families": []}, lambda p: "{}")
    assert out == {"high": [], "mid_for_review": []}

def test_guideline_in_prompt_no_numeric_range():
    """判据显式化(2026-07-11):guideline 进 prompt;数字范围(4-8族)已废除。"""
    seen = {}

    def spy_llm(prompt):
        seen["prompt"] = prompt
        return '{"families": []}'

    induce_taxonomy(["push the block"], spy_llm)
    p = seen["prompt"]
    assert "GUIDELINE" in p and "NOT grouping" in p          # 默认判据在场
    assert "4-8" not in p and "1-6" not in p                 # 数字范围已死
    # 自定义 guideline 整段替换
    induce_taxonomy(["push the block"], spy_llm, guideline="按物体类型分类。")
    assert "按物体类型分类" in seen["prompt"]
    assert "NOT grouping" not in seen["prompt"]              # 默认判据被替换,非叠加


def test_criterion_propagates():
    """LLM 自述的归类理由(criterion)可提取,进报告可审计。"""
    from curation.dataset_level.taxonomy import criteria_of
    tax = {"families": [{"name": "Push", "criterion": "推类动作",
                         "subskills": [{"name": "push-flat", "criterion": "平面上推",
                                        "members": ["push the block"]}]}]}
    fam_c, sub_c = criteria_of(tax)
    assert fam_c["Push"] == "推类动作"
    assert sub_c[("Push", "push-flat")] == "平面上推"
    # 缺 criterion 不炸(向后兼容旧格式)
    fam_c2, _ = criteria_of({"families": [{"name": "X", "subskills": []}]})
    assert fam_c2["X"] == ""


def test_repair_unassigned():
    """漏抄补齐:漏网 caption 二次指认;乱指(类名不存在)拒收。"""
    import json as _j
    from curation.dataset_level.taxonomy import repair_unassigned
    tax = {"families": [{"name": "place", "subskills": [
        {"name": "place-onto-surface", "members": ["put a on b"]}]}]}

    def llm(prompt):
        return _j.dumps({"map": [
            {"caption": "move pot to burner", "family": "place",
             "subskill": "place-onto-surface"},                     # 合法
            {"caption": "fold towel", "family": "编造族",
             "subskill": "编造子技能"}]})                            # 乱指 → 拒收

    fix = repair_unassigned(["move pot to burner", "fold towel"], tax, llm)
    assert fix == {"move pot to burner": ("place", "place-onto-surface")}


def test_repair_llm_failure_not_fatal():
    from curation.dataset_level.taxonomy import repair_unassigned
    tax = {"families": [{"name": "f", "subskills": [{"name": "s", "members": []}]}]}
    assert repair_unassigned(["x"], tax, lambda p: "不是JSON") == {}


def test_refine_merges_on_yes():
    """二值裁决 yes → 代码合并:成员并集,合并名=公共前缀去连接词。"""
    from curation.dataset_level.taxonomy import refine_taxonomy
    orig = {"families": [{"name": "move", "subskills": [
        {"name": "move-to-burner", "members": ["move pot to burner"]},
        {"name": "move-to-side", "members": ["move cup to side"]}]}]}
    out = refine_taxonomy(orig, lambda q: "yes")
    subs = out["families"][0]["subskills"]
    assert len(subs) == 1 and subs[0]["name"] == "move"
    assert sorted(subs[0]["members"]) == ["move cup to side", "move pot to burner"]


def test_refine_keeps_on_no_or_error():
    from curation.dataset_level.taxonomy import refine_taxonomy
    orig = {"families": [{"name": "place", "subskills": [
        {"name": "place-onto-surface", "members": ["a"]},
        {"name": "place-into-container", "members": ["b"]}]}]}
    assert len(refine_taxonomy(orig, lambda q: "no")["families"][0]["subskills"]) == 2
    def boom(q):
        raise RuntimeError("端点崩了")
    assert len(refine_taxonomy(orig, boom)["families"][0]["subskills"]) == 2


# ───────── 分歧措辞中性化 + 打标稳定度定级(2026-07-31)─────────
#
# 背景(硬证据,别把措辞改回去):droid ep000143 真实剧本"从洗手池拿毛巾→贴墙擦→
# 放回台面",原始标注逐字正确,我方 caption 却说成 "Hang the towel on the hook";
# 且同条 episode 方舟 temperature=0 连打 5 次得到 5 种说法。拿不稳的东西当基准
# 审判客户 = 产品级缺陷 → reason 只描述分歧、必须标明"自产描述(VLM 生成)"。

def test_audit_reason_is_neutral_no_bare_screen_claim():
    """reason 不许出现"画面=X"这类裸表述(读起来像客观事实,实际是模型一句猜测)。"""
    def fake_llm(prompt):
        return json.dumps({"map": [{"label": "close microwave", "family": "open/close"}]})

    out = audit_labels(["ep1"], ["close microwave"], ["place the cup on the table"],
                       ["manipulate"], TAX, fake_llm)
    reason = out["high"][0]["reason"]
    assert "画面=" not in reason and "标注=" not in reason
    assert "分歧" in reason and "自产描述(VLM 生成)" in reason and "需人工判定" in reason


def _audit_fixture():
    return {"high": [{"id": "epA", "label": "wipe the wall with the towel",
                      "caption": "Hang the towel on the hook",
                      "reason": "分歧:原始标注归为 wipe,自产描述(VLM 生成)归为 place——需人工判定"},
                     {"id": "epG", "label": "asdkjh", "caption": "x", "reason": "garbage"}],
            "mid_for_review": []}


def test_retier_stable_stays_high():
    """① N 次重打标同族 → 留在 high 且 caption_stable=True(措辞变了不算不稳)。"""
    from curation.dataset_level.audit import retier_by_caption_stability
    fam = {"hang the towel on the hook": "place", "put the towel on the counter": "place",
           "place towel onto the sink counter": "place"}
    out = retier_by_caption_stability(
        _audit_fixture(),
        {"epA": ["put the towel on the counter", "place towel onto the sink counter",
                 "put the towel on the counter"]},
        lambda c: fam.get(c.strip().lower(), "未归类"))
    a = [e for e in out["high"] if e["id"] == "epA"][0]
    assert a["caption_stable"] is True and len(a["recaptions"]) == 3
    assert out["low_caption_unstable"] == []
    # 乱码档与自产描述无关 → 永不参与降级,也不该被塞进稳定度字段
    g = [e for e in out["high"] if e["id"] == "epG"][0]
    assert g == {"id": "epG", "label": "asdkjh", "caption": "x", "reason": "garbage"}


def test_retier_unstable_demoted_with_all_recaptions():
    """② N 次自己就不一致 → 降级出 high,reason 注明我方不稳,N 次原文全留。"""
    from curation.dataset_level.audit import retier_by_caption_stability
    fam = {"hang the towel on the hook": "place",
           "wipe the wall with a towel": "wipe",
           "put the towel on the sink counter": "place"}
    recaps = ["wipe the wall with a towel", "put the towel on the sink counter",
              "wipe the wall with a towel"]
    out = retier_by_caption_stability(_audit_fixture(), {"epA": recaps},
                                      lambda c: fam.get(c.strip().lower(), "未归类"))
    assert [e["id"] for e in out["high"]] == ["epG"]           # epA 已移出 high
    d = out["low_caption_unstable"][0]
    assert d["id"] == "epA" and d["caption_stable"] is False
    assert d["recaptions"] == recaps                           # N 次原文供人看
    assert "我方画面描述不稳定" in d["reason"] and "不足以质疑标注" in d["reason"]
    assert "分歧:原始标注归为 wipe" in d["reason"]             # 原始分歧信息不丢


def test_retier_disabled_is_bitwise_noop():
    """③ audit_recheck_n=1(→ 无重打标数据)→ 与改动前逐字一致,零回归。

    这条最重要:默认关掉时,产出必须与不接本机制时一模一样——不多键、不多字段,
    连归族函数都一次都不许调(调了就是白烧 LLM 额度)。
    """
    from curation.dataset_level.audit import retier_by_caption_stability

    def boom(c):
        raise AssertionError("关闭时不该做归族")

    src = _audit_fixture()
    out = retier_by_caption_stability(src, {}, boom)
    assert out == src                       # 键集与每个字段逐字相同(无 low_ 档)
    assert list(out) == list(src)


def test_retier_empty_flagged_set_ok():
    """④ 被标记条目为空 → 不崩。"""
    from curation.dataset_level.audit import retier_by_caption_stability
    empty = {"high": [], "mid_for_review": []}
    assert retier_by_caption_stability(empty, {}, lambda c: "x") == empty
    out = retier_by_caption_stability(empty, {"epX": ["a"]}, lambda c: "x")
    assert out == {"high": [], "mid_for_review": [], "low_caption_unstable": []}


def test_retier_failed_recaption_is_conservative():
    """⑤ 重打标失败(空串)/归族失败 → 证据不足,维持原档,不误伤两边。"""
    from curation.dataset_level.audit import retier_by_caption_stability

    def boom_family(c):
        raise RuntimeError("归族端点崩了")

    out = retier_by_caption_stability(_audit_fixture(), {"epA": ["", "  ", ""]},
                                      lambda c: "place")
    a = [e for e in out["high"] if e["id"] == "epA"][0]
    assert a["caption_stable"] is None                 # 未知,不是 True 也不是 False
    assert out["low_caption_unstable"] == []           # 没被误降级
    # 归族全失败同理:一条可比样本都没有 → 维持原档
    out2 = retier_by_caption_stability(_audit_fixture(), {"epA": ["a", "b"]}, boom_family)
    assert out2["low_caption_unstable"] == []
    assert [e for e in out2["high"] if e["id"] == "epA"][0]["caption_stable"] is None


def test_audit_recheck_n_configured_with_off_switch():
    """默认 3(开),1=关;site.yaml 不动 → 值从 default.yaml 读得到。"""
    from curation.pipeline.config import load_config
    assert load_config()["skill_profile"]["audit_recheck_n"] == 3
