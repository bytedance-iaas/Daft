"""标注优先归类(2026-08-16 方针)钉子:归类输入 = instruction.strip() or caption。

防的事故(droid-200-new 实测,用户拍板改方针的直接证据):分歧队列 29 条人工复核,
26 条是我方 caption 错、客户原始标注对(90%)——"Open the airfryer" 被 caption 写成
放咖啡胶囊、"Wipe the table" 被写成折毛巾,再按 caption 归类,26 条全躺错格子。
这组测试钉死:①有标注的条目归类输入必须是**标注**(把输入改回 caption 就红);
②无标注的条目归类输入是 caption;③caption 照旧全员生成并陈列;④换了归类输入
**不许削弱分歧检出**(护栏:分歧队列是人工裁决的入口,少报一条 = 一条错 caption
永远没人看见)。
"""
from __future__ import annotations

import json

from curation.pipeline.run import _skill_profile_stage

WIPE_LABEL = "wipe the table with the yellow towel"
WRONG_CAP = "fold the yellow towel"          # 我方 caption 看错(实案同款)
CUP_CAP = "pick up the cup"

#: 三族体系(由归类文本归纳出来的样子);folding 是 caption 补漏可指认的既有类
TAX = {"families": [
    {"name": "wiping", "criterion": "cyclic surface motion",
     "subskills": [{"name": "wipe-surface", "criterion": "wipe",
                    "members": [WIPE_LABEL]}]},
    {"name": "grasping", "criterion": "grasp and transport",
     "subskills": [{"name": "pick-up", "criterion": "pick",
                    "members": [CUP_CAP]}]},
    {"name": "folding", "criterion": "deformable folding",
     "subskills": [{"name": "fold-cloth", "criterion": "fold", "members": []}]},
]}


def _rows():
    return [{"episode_id": "ep0", "instruction": WIPE_LABEL},   # 有标注(caption 是错的)
            {"episode_id": "ep1", "instruction": ""}]           # 无标注

_CAPS = {"ep0": WRONG_CAP, "ep1": CUP_CAP}

_CFG = {"skill_profile": {"audit_recheck_n": 1, "caption_concurrency": 2}}


def _make_llm(seen, judge="pair"):
    """假 LLM:按 prompt 特征分流(归纳/补漏/判官/族级映射),全程录音供断言。"""
    def llm(prompt):
        seen.append(prompt)
        if "Build a TWO-LEVEL skill taxonomy" in prompt:
            return json.dumps(TAX)
        if "CAPTION:" in prompt:                           # caption 归族补漏(一次一条)
            return json.dumps({"family": "folding", "subskill": "fold-cloth"})
        if "IRRECONCILABLE" in prompt:                    # 文本对判官
            if judge != "pair":
                return "不是JSON"                          # 逼它走族级回退路
            verdicts = []
            for line in prompt.splitlines():
                if ". ANNOTATION: " not in line:
                    continue
                i = int(line.split(".")[0])
                ann, cap = line.split("ANNOTATION: ")[1].split(" /// DESCRIPTION: ")
                v = "different" if ("wipe" in ann and "fold" in cap) else "same"
                verdicts.append({"i": i, "verdict": v, "why": "wipe vs fold"})
            return json.dumps({"pairs": verdicts})
        if "For each annotation below" in prompt:         # 族级回退:标注映射进族
            return json.dumps({"map": [{"label": WIPE_LABEL, "family": "wiping"}]})
        raise AssertionError(f"意外的 LLM 调用:{prompt[:80]}")
    return llm


def _run_stage(judge="pair"):
    seen: list = []
    out = _skill_profile_stage(_rows(), _CFG,
                               captioner=lambda groups: "",   # precomputed 全覆盖,不该被调
                               llm_ask=_make_llm(seen, judge),
                               auto_caps=dict(_CAPS))
    return out, seen


def test_labeled_episode_grouped_by_instruction_not_caption():
    """①+③ 核心钉子:有标注 → 体系归纳与分配吃的是**标注**;caption 只陈列。

    判据是归纳 prompt 的原文:标注在场、错 caption 不在场——把归类输入改回
    caps(旧写法)这条立刻红(prompt 里会出现 fold、丢掉 wipe)。
    """
    (profile, caption_of, gt_of, gs_of, _), seen = _run_stage()
    induce = [p for p in seen if "Build a TWO-LEVEL skill taxonomy" in p]
    assert len(induce) == 1
    assert WIPE_LABEL in induce[0]                 # 标注是归类输入
    assert CUP_CAP in induce[0]                    # 无标注条目用 caption
    assert WRONG_CAP not in induce[0]              # 错 caption 不再当归类输入
    # 分配结果:ep0 按标注进 wiping(不是 caption 指向的 folding)
    assert profile["families"]["wiping"]["subskills"]["wipe-surface"][
        "episodes"] == ["ep0"]
    assert profile["families"]["grasping"]["subskills"]["pick-up"][
        "episodes"] == ["ep1"]
    # caption 照旧全员生成并记录(它还是分歧检出的一端)
    assert caption_of == {"ep0": WRONG_CAP, "ep1": CUP_CAP}
    # 归类文本与来源留痕(进 CSV 的两新列)
    assert gt_of == {"ep0": WIPE_LABEL, "ep1": CUP_CAP}
    assert gs_of == {"ep0": "原始标注", "ep1": "自产caption"}


def test_divergent_caption_still_enters_audit_queue():
    """④ 护栏(主路径):标注说擦桌子、caption 说折毛巾 → 照样进分歧队列。

    判官比的是"标注 vs caption"两句原文——若实现把归类文本当 caption 喂进去,
    这一对会变成 wipe vs wipe,判官答 same,队列少这一条 → 本测试红。
    """
    (_, _, _, _, label_audit), _ = _run_stage()
    ids_high = {e["id"]: e for e in label_audit["high"]}
    assert "ep0" in ids_high
    assert ids_high["ep0"]["label"] == WIPE_LABEL
    assert ids_high["ep0"]["caption"] == WRONG_CAP


def test_divergence_survives_family_fallback_path():
    """④ 护栏(降级路):判官失败回退族级比对时,检出也不许被削弱。

    体系现在由标注文本归纳,caption 不再天然是成员——实现必须给 caption 单独
    归族(精确分配+LLM 补漏)再喂审计。若省掉这步,ep0 的 caption 归"未归类",
    族级比对按"宁少勿错"跳过它 → 分歧漏报 → 本测试红。
    """
    (_, _, _, _, label_audit), _ = _run_stage(judge="broken")
    ids_high = {e["id"]: e["reason"] for e in label_audit["high"]}
    assert "ep0" in ids_high
    # 族级措辞:标注归 wiping,自产描述归 folding(补漏指认的结果)
    assert "wiping" in ids_high["ep0"] and "folding" in ids_high["ep0"]


def test_all_unlabeled_dataset_groups_by_caption():
    """② 无标注数据集(droid 原始态):归类输入回到 caption,来源如实记自产。"""
    rows = [{"episode_id": "ep0", "instruction": ""},
            {"episode_id": "ep1", "instruction": "   "}]   # 全空白=无标注(要 strip)
    caps = {"ep0": WIPE_LABEL, "ep1": CUP_CAP}
    seen: list = []
    profile, caption_of, gt_of, gs_of, _ = _skill_profile_stage(
        rows, _CFG, captioner=lambda groups: "",
        llm_ask=_make_llm(seen), auto_caps=caps)
    induce = [p for p in seen if "Build a TWO-LEVEL skill taxonomy" in p][0]
    assert WIPE_LABEL in induce and CUP_CAP in induce
    assert gt_of == {"ep0": WIPE_LABEL, "ep1": CUP_CAP}
    assert gs_of == {"ep0": "自产caption", "ep1": "自产caption"}
