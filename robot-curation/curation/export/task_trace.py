"""任务成败判定的判定痕迹落盘(details/task_details.json)。

为什么要有这个文件:判定的中间读数算完即弃 —— 判"通过"的条目在交付里只剩一个
"pass",弃权的把两个数字嵌在中文理由里。校准要做的事(改一条阈值会翻多少条、
每条判据各管着多少数据)得靠这些草稿纸,而重烧一遍 VLM 既贵又**不可复现**
(同配置两遍结论就不同)。所以每次质检都要把草稿纸交上来。

本模块是**纯装配**:只读 task_success/endstate_review 已经产出的 detail,
一个数都不重算、一次 VLM 都不调。判定行为与本文件在不在毫无关系。

口径:一条 episode 一条记录,**跑过成败判定的才有**。被前面的硬门(时间戳/
运动学/同步)早杀的条目根本没走到这一步,如实不记 —— 编一条"未判定"的空壳
会让离线统计把它算进分母。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

#: 检查结果三态 → 与交付里 checks 的用词一致(别在这里另造一套词)。
_STATE = {True: "pass", False: "拒绝", None: "弃权"}

#: 打分臂要留的读数。原始精度优先(detail["raw"],2026-08-12 起),
#: 老 detail 没有 raw 时退回四舍五入过的那份 —— 有什么记什么,拿不到的键不出现。
_SCORING_KEYS = ("voc", "completion_final", "completion_peak", "completion_gap",
                 "completion_last", "dip")


def build_task_trace(episode_id: str, passed, detail: dict, *,
                     instruction: str = "", instruction_source: str = "") -> dict:
    """一条 episode 的判定痕迹(纯函数)。detail = task_success 检查的 detail dict。

    instruction/instruction_source 由调用方给全量原文:detail 里那份被截到 80 字
    (界面用),校准要对得上原始标注就不能用截断版。给空则退回 detail 里的截断版。
    """
    raw = detail.get("raw") or {}
    rec: dict = {"episode_id": episode_id, "result": _STATE.get(passed, "?")}

    ins = str(instruction or detail.get("task_desc") or "").strip()
    if ins:
        rec["instruction"] = ins
    src = str(instruction_source or detail.get("task_desc_source") or "").strip()
    if src:
        rec["instruction_source"] = src

    # 规则轨迹:各臂结论 + 走过哪些判据 + 最终理由
    for key, out in (("init_verdict", "init_verdict"), ("verdict", "verdict"),
                     ("reason", "reason")):
        v = detail.get(key)
        if v not in (None, ""):
            rec[out] = v
    if detail.get("rules"):
        rec["rules"] = list(detail["rules"])

    scoring: dict = {}
    for k in _SCORING_KEYS:
        v = raw.get(k, detail.get(k))
        if v is not None:
            scoring[k] = v
    comps = raw.get("completions", detail.get("completions"))
    if comps is not None:
        scoring["completions"] = list(comps)
    if detail.get("probe_frames") is not None:
        scoring["probe_frames"] = list(detail["probe_frames"])
    if detail.get("strong_score") is not None:
        scoring["strong_score"] = bool(detail["strong_score"])
    if scoring:
        rec["scoring"] = scoring

    review: dict = {}
    if detail.get("cam_votes"):
        review["cam_votes"] = dict(detail["cam_votes"])
    if detail.get("review"):
        review["tally"] = detail["review"]
    if detail.get("cams"):
        review["cameras"] = list(detail["cams"])
    for key, out in (("review_disputed", "disputed"), ("review_split", "split")):
        if detail.get(key):
            review[out] = True
    if detail.get("endstate"):
        review["note"] = str(detail["endstate"])[:80]
    if review:
        rec["review"] = review

    # 取证仲裁链留痕(仅弃权条目触发才有):core 已按规格装好整个 dict
    # (intent/intent_source/intent_conflict/spec/lines/n_effective/consensus/applied),
    # 这里原样搬运 —— 纯装配纪律,一个数不重算。
    if isinstance(detail.get("arbitration"), dict):
        rec["arbitration"] = detail["arbitration"]
    return rec


#: 文件里那句口径。不写清楚,读的人会把"没有这条 episode"当成"判定漏了"。
TRACE_NOTE = (
    "任务成败判定的逐条判定痕迹(只记录,不参与判定)。一条 episode 一条,"
    "**只含真正跑过成败判定的条目** —— 被时间戳/运动学/同步等硬门提前拒掉的"
    "根本没走到这一步,故不在此列。scoring=打分臂读数(原始精度,未四舍五入);"
    "review=逐机位复核票与汇票;rules=依次触发的协议判据(语义化标识,"
    "离线扫规则按它分桶);init_verdict=打分臂结论,verdict=复核合成后的最终结论;"
    "arbitration=取证仲裁链留痕(仅打分+复核后弃权的条目触发才有)。")


def write_task_details(det_dir: str, rows: list, dataset: str = "") -> str | None:
    """落 details/task_details.json;rows 为空则**不产文件**(--lite / 未启用该检查)。

    ⚠️ 写法:先写本地临时文件再 shutil.copyfile —— 交付目录在 TOS 的 FSX 挂载上,
    库/进程对着挂载路径直写已经咬过五次。回验只做"读得回来"这一步,不看文件大小。
    """
    if not rows:
        return None
    os.makedirs(det_dir, exist_ok=True)
    dst = os.path.join(det_dir, "task_details.json")
    payload = {"数据集": dataset, "口径": TRACE_NOTE,
               "episodes": {r["episode_id"]: r for r in rows}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8",
                                     delete=False) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=1, default=str)
        tmp_path = tmp.name
    try:
        shutil.copyfile(tmp_path, dst)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return dst
