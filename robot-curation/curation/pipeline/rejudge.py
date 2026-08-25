"""人工裁决闭环:`curation rejudge`(③,2026-08-05;成败裁决 2026-08-06 并入)。

两条裁决线,一条命令消化(人工确认卡在 caption 与重判之间 = 自产自证陷阱的断路器):

① 标注分歧(human-decisions/label_decisions.csv)
    采纳建议改标 → 用**人工确认过的新标注**重跑任务成败检测(多视角 v7.3 全协议),
                  按新判定把 episode 搬进 passed/review/reject,带完整改标溯源;
                  **例外**(2026-08-16):同一条 episode 人工还给了成败结论
                  (判成功/判失败)→ 不重判,直接采信人的结论(理由与溯源纪律
                  见 apply_decisions 的 docstring);
    维持原标注   → 只在分歧队列上标记已裁决(审计误旗,原判定原样);
    拿不准       → 只记一笔,队列保留(2026-08-19 新增,与成败线同词同义:
                  "我判不了" ≠ "我判了,原标注对");
    弃用该条     → 搬进 reject。界面上它**不在标注块里**,而是卡片级的
                  「其它原因-整条弃用」——"这条数据要不要"和"标注 vs caption
                  谁错"是两个正交维度,弃用不是后者的第三个答案(用户点破)。
                  🔴 它**压过成败裁决**:点了就弃用,不管另一块点了什么
                  (拦截在 run_rejudge 里,见那处注释)。
② 任务成败弃权(human-decisions/task_verdicts.csv)——**不跑 VLM**:系统已经诚实说了
   "判不了",再问一次只会得到同样的弃权,人说了算。
    判成功 → 出待裁决队列,判决改「通过(人工裁决)」,成败检查落 pass;
    判失败 → 三边摘除进 reject,并随现有同步机制从 episodes_parquet /
             lerobot_curated / rrd_curated 里剔除(裁决只改报告 = 交出去还是脏数据);
    拿不准 → 只记一笔,队列保留(等更多信息;2026-08-19 由「搁置」改名)。
③ 被拒复议(human-decisions/reject_appeals.csv)——**语义判定的杀可以被人捞回**,这是
   "证据够就杀"的保险丝。同样不跑 VLM:系统已经给过结论,人看完视频推翻它。
    捞回     → 从拒绝翻为通过,回到 passed / 交付数据集(溯源 verdict_source=human_review);
    维持拒绝 → 只记档,判决一个字不改。
   ⚠️ 准入只认"拒因归因于任务成败判定"的条目(is_task_success_reject):时间戳/
   残段/运动学/同步这些物理与结构硬门是终局,不进复议 —— 界面不给入口,这里再
   校验一次(裁决 CSV 是可以被手改的文件,不能只靠界面把门)。

  三件套原地更新 + report.md 追加小节 + 该次跑批的 details/rejudge_results.json 留档
  (裁决 CSV 在交付根 human-decisions/,机器产出仍在被裁决的那一次跑批目录里)。

架构:apply_decisions() / apply_task_verdicts() 是**纯数据函数**(只操作已加载的
JSON dict,可严格单测);重判本体注入(rerun_fn),生产由 run_rejudge 组装真 VLM,
测试注入假函数——与 task_success 的依赖注入同一哲学。

输入格式(P5,2026-08-10):重判要回源重读画面,而源可能是 LeRobot 也可能是 RRD。
读这一步走 `_episode_row_reader` 的嗅探分派(与 run.py 同一套),重判本体
(解码→多视角→终态复核)对格式一无所知。
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Callable

from ..dataset_level import decisions as human
from ..export.safe_write import delivery_dir, delivery_file, write_json, write_text

TASK_CN = "任务成败判定"

#: 三件套条目上的人工溯源键。标注线与成败线各一个,互不覆盖(同一条 episode
#: 可能先被改标重判、后又被人工判成败),排查时一眼看得出是哪条线动的。
#: 单一事实源在 dataset_level/decisions.py(UI 的已应用/未应用计数与这里的幂等
#: 跳过必须认同一套键);此处留同名别名,rejudge.PROV_* 的老引用不破。
PROV_LABEL = human.PROV_LABEL
PROV_RELABEL = human.PROV_RELABEL
PROV_TASK = human.PROV_TASK
PROV_APPEAL = human.PROV_APPEAL
_PROV_KEYS = (PROV_RELABEL, PROV_LABEL, PROV_TASK, PROV_APPEAL)


def _state_cn(passed) -> str:
    return {True: "pass", False: "拒绝", None: "弃权"}[passed]


def _take_entry(p_eps: dict, r_eps: dict, j_eps: dict, eid: str) -> dict:
    """从三件套里取出该 episode 的现有条目——**三边都摘**,返回信息最全的一份。

    为什么不是"第一个命中就收手":FSX 改写文件有读延迟,rejudge 连跑时曾读到
    旧版 review,让已搬走的条目"复活"成双份(2026-08-06 droid-30 实锤:Episodes
    页签僵尸待裁决)。全量摘除让任何双份在下一次搬移时就地自愈。
    带人工溯源的那份信息最全(它是上一轮裁决的产物),优先留下。"""
    best: dict = {}
    for d in (p_eps, r_eps, j_eps):
        if eid in d:
            e = d.pop(eid)
            if not best or any(k in e for k in _PROV_KEYS):
                best = e
    return best


def apply_decisions(passed: dict, review: dict, reject: dict,
                    decisions: dict, rejudged: dict,
                    human_concluded=frozenset()) -> dict:
    """按裁决在三件套视图间搬移/标注(**原地修改**传入的 dict)→ 摘要。

    decisions: {eid: {"decision","new_label","note","at"}}(decisions.py schema)
    rejudged:  {eid: {"passed","verdict","detail"}}(仅"采纳建议改标"条目需要;
               缺席 = 重判没跑成,该条不动并记入摘要,绝不臆断)
    human_concluded: 这些 episode 的「采纳建议改标」**不重判**(人工已给成败
               结论:判成功/判失败)。这是对「改标必须重判」铁律的**有意例外**
               (2026-08-16 用户拍板):人正看着视频给出的结论,他就是 ground
               truth;让机器去复核人刚给的结论,是把断路器装反了 —— 断路器防的
               是"机器自产自证",不是防人。这里只在现存条目上落改标溯源(写明
               重判被跳过),搬移交给 apply_task_verdicts 按人的结论做;溯源两处
               都写人工(「标注修正」+「人工裁决」),**绝不伪装成机器判的**。
    """
    p_eps = passed.setdefault("episodes", {})
    r_eps = review.setdefault("episodes", {})
    j_eps = reject.setdefault("episodes", {})
    queue = review.get("标注-画面分歧复核队列") or []
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = {"adopted_pass": [], "adopted_review": [], "adopted_reject": [],
               "adopted_human": [], "dropped": [], "kept": [], "held": [],
               "skipped": []}

    def _take(eid):
        return _take_entry(p_eps, r_eps, j_eps, eid)

    for eid, dec in decisions.items():
        kind = dec.get("decision")
        qhit = [q for q in queue if q.get("id") == eid]
        for q in qhit:
            q["decision"] = kind                     # 队列标记已裁决(三种裁决都标)
            q["decided_at"] = dec.get("at") or now

        if kind == "维持原标注":
            summary["kept"].append(eid)
            continue

        if kind == _hold():
            # 标注线的「拿不准」(2026-08-19 新增,与成败线同词同义):**只留痕**,
            # 判定一个字不动,队列保留 —— 它是"待定"不是结论。
            # 与「维持原标注」的区别是语义而非后果:那个是"我判了,原标注对";
            # 这个是"我判不了"。两者混成一个的话,报告里"人已确认原标注无误"
            # 的条数会把"人还没想好"的也算进去,那是假的确信。
            summary["held"].append(eid)
            continue

        if kind == "弃用该条":
            old = _take(eid)
            j_eps[eid] = {"判决": "拒绝", "原因": "人工裁决弃用(标注-画面分歧复核)",
                          "综合软分": old.get("综合软分"),
                          "checks": old.get("checks", {}),
                          "标注裁决": {"裁决": kind, "备注": dec.get("note", ""),
                                     "裁决时间": dec.get("at", now)}}
            summary["dropped"].append(eid)
            continue

        if kind == "采纳建议改标":
            if eid in human_concluded:
                # 人工已给成败结论 → 只落改标溯源,不搬移不重判(见函数 docstring
                # 的例外说明)。只写**第一份**现存副本:弃权条目在 passed 与
                # review 双呈现,两份都写的话 _take_entry 的 best 选择会被后一份
                # 稀疏 review 副本抢走(它没有 checks/综合软分),人的结论落库时
                # VLM 读数就丢了;apply_task_verdicts 搬移时会经 keep 带走这份溯源。
                prov = {"裁决": kind, "原标注": dec.get("old_label", ""),
                        "新标注": dec.get("new_label", ""),
                        "备注": dec.get("note", ""),
                        "裁决时间": dec.get("at", now),
                        "重判": "跳过 —— 人工已给成败结论,以人的结论为准"}
                hit = False
                for d in (p_eps, r_eps, j_eps):
                    if eid in d:
                        d[eid][PROV_RELABEL] = prov
                        hit = True
                        break
                # 三件套里查无此条(裁决文件与交付对不上)= 应用不了,记 skipped
                (summary["adopted_human"] if hit else summary["skipped"]).append(eid)
                continue
            rj = rejudged.get(eid)
            if rj is None:
                summary["skipped"].append(eid)       # 重判没跑成:原样不动,留待下次
                continue
            old = _take(eid)
            prov = {"裁决": kind, "原标注": dec.get("old_label", ""),
                    "新标注": dec.get("new_label", ""), "备注": dec.get("note", ""),
                    "裁决时间": dec.get("at", now), "重判时间": now,
                    "重判判定": rj.get("verdict", "")}
            checks = dict(old.get("checks", {}))
            entry_check = {"结果": _state_cn(rj.get("passed"))}
            if rj.get("detail"):
                entry_check["detail"] = rj["detail"]
            checks[TASK_CN] = entry_check
            if rj.get("passed") is True:
                p_eps[eid] = {"判决": "通过(标注修正后)", "综合软分": old.get("综合软分"),
                              "checks": checks, "标注修正": prov}
                summary["adopted_pass"].append(eid)
            elif rj.get("passed") is False:
                j_eps[eid] = {"判决": "拒绝", "原因": "标注修正后重判仍未完成",
                              "综合软分": old.get("综合软分"),
                              "checks": checks, "标注修正": prov}
                summary["adopted_reject"].append(eid)
            else:
                r_eps[eid] = {"当前判决": "通过", "待裁决项": [TASK_CN],
                              "弃权原因": {TASK_CN: rj.get("verdict", "重判弃权")},
                              "checks": checks, "标注修正": prov}
                summary["adopted_review"].append(eid)
            continue

        summary["skipped"].append(eid)               # 未知裁决词:不动

    _sync_counts(review, reject, r_eps, j_eps)
    return summary


def _sync_counts(review: dict, reject: dict, r_eps: dict, j_eps: dict) -> None:
    """计数字段与文件同步(有才更,不发明新键)。"""
    if "待人工裁决总数" in review:
        review["待人工裁决总数"] = len(r_eps)
    if "被拒总数" in reject:
        reject["被拒总数"] = len(j_eps)


def _hold() -> str:
    """「拿不准」的现行词(单一事实源在 decisions,别在这儿再写一遍字面量)。"""
    from ..dataset_level.decisions import VERDICT_HOLD
    return VERDICT_HOLD


def apply_task_verdicts(passed: dict, review: dict, reject: dict,
                        verdicts: dict) -> dict:
    """按**任务成败**人工裁决在三件套视图间搬移/标注(**原地修改**传入的 dict)→ 摘要。

    verdicts: {eid: {"verdict","note","at"}}(decisions.py 的 task_verdicts schema)

    与 apply_decisions 的分工:那边处理"标注写错了"(改标 → 必须重判,机器复核人的
    输入);这边处理"机器判不了"(人直接给结论,**不重判**)。两者可以先后作用在
    同一条 episode 上,溯源键不同,互不覆盖。
    """
    p_eps = passed.setdefault("episodes", {})
    r_eps = review.setdefault("episodes", {})
    j_eps = reject.setdefault("episodes", {})
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = {"verdict_pass": [], "verdict_fail": [], "verdict_hold": [],
               "verdict_skipped": []}

    for eid, v in verdicts.items():
        # 老值「搁置」在这儿也认(2026-08-19 改名):本函数是**纯数据函数**,
        # 调用方可能直接喂 CSV 原始行而没过 load_task_verdicts 的归一。
        kind = human.normalize_verdict(v.get("verdict"))
        # ⚠️ 认不出的裁决词**一律不动**,不能往下走 —— 下面那句
        # `"pass" if kind == "判成功" else "拒绝"` 会把任何不认识的词当成"拒绝",
        # 也就是**把一条数据悄悄扔掉**。裁决 CSV 是可以被手改的文件,拼错一个字
        # 就丢数据,这个代价不对等(2026-08-19 改名时顺手堵上)。
        if kind not in human.VERDICT_CHOICES:
            summary["verdict_skipped"].append(eid)
            continue
        prov = {"裁决": kind, "备注": v.get("note", ""),
                "裁决时间": v.get("at", now)}

        if kind == _hold():
            # 队列保留(还没想好,不是结论)——只在**所有**现存副本上留一笔,
            # 让 UI/报告都能看出"这条有人看过了,先挂着",不搬不改判。
            hit = False
            for d in (p_eps, r_eps, j_eps):
                if eid in d:
                    d[eid][PROV_TASK] = prov
                    hit = True
            (summary["verdict_hold"] if hit else summary["verdict_skipped"]).append(eid)
            continue

        if kind not in ("判成功", "判失败"):
            summary["verdict_skipped"].append(eid)   # 未知裁决词:不动
            continue

        old = _take_entry(p_eps, r_eps, j_eps, eid)
        if not old:
            # 三件套里根本没这条(裁决文件与交付对不上,如换了交付目录):
            # 不凭空造条目——那会造出一条没有任何检查记录的假 episode。
            summary["verdict_skipped"].append(eid)
            continue
        checks = dict(old.get("checks", {}))
        entry = dict(checks.get(TASK_CN) or {})
        # 结果字段跟着人的结论走:留着 "弃权" 的话,Episodes 页和汇总统计还会
        # 把这条算进"系统未决",与判决自相矛盾。detail(VLM 当时的读数)原样保留,
        # 溯源里写明是人改的——两边都在,谁也没被抹掉。
        entry["结果"] = "pass" if kind == "判成功" else "拒绝"
        checks[TASK_CN] = entry
        # 条目是重建的(弃权原因/待裁决项该随之作废),但**上一条裁决线留下的溯源
        # 必须带走**:同一条 episode 常常先被改标重判、再被人工判成败,把改标履历
        # 抹掉的话,交付里就再也看不出这条的标注被人动过。
        keep = {k: old[k] for k in (PROV_RELABEL, PROV_LABEL, PROV_APPEAL) if k in old}

        if kind == "判成功":
            p_eps[eid] = {"判决": "通过(人工裁决)", "综合软分": old.get("综合软分"),
                          "checks": checks, **keep, PROV_TASK: prov}
            # 弃权原因/待裁决项随条目重建自然清空:它说的是"系统判不了",
            # 现在有人判了,再留着就是过期信息。
            summary["verdict_pass"].append(eid)
        else:
            j_eps[eid] = {"判决": "拒绝", "原因": "人工裁决判失败(任务未完成)",
                          "综合软分": old.get("综合软分"),
                          "checks": checks, **keep, PROV_TASK: prov}
            summary["verdict_fail"].append(eid)

    _sync_counts(review, reject, r_eps, j_eps)
    return summary


def apply_reject_appeals(passed: dict, review: dict, reject: dict,
                         appeals: dict) -> dict:
    """按**被拒复议**结论在三件套视图间搬移/标注(**原地修改**传入的 dict)→ 摘要。

    appeals: {eid: {"appeal","note","at"}}(decisions.py 的 reject_appeals schema)

    与另两条线的分工:标注线处理"标注写错了"(要重判),成败弃权线处理"机器判不了"
    (人给结论);这条线处理"机器判了、而且判错了"——人把杀掉的条目捞回来。
    同样不重判:再问一次 VLM 只会得到同样的结论,人看过画面就是最终事实。

    准入在这里再校验一次(界面已经过滤过一遍):拒因不是任务成败判定的,一律不捞。
    物理硬门是终局这件事,不能只由界面把门 —— CSV 是人能直接编辑的文件。
    """
    p_eps = passed.setdefault("episodes", {})
    r_eps = review.setdefault("episodes", {})
    j_eps = reject.setdefault("episodes", {})
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = {"appeal_restored": [], "appeal_upheld": [], "appeal_skipped": []}

    from ..dataset_level.decisions import is_task_success_reject

    for eid, a in appeals.items():
        kind = a.get("appeal")
        if kind not in ("捞回", "维持拒绝"):
            summary["appeal_skipped"].append(eid)      # 未知复议词:不动
            continue
        cur = j_eps.get(eid)
        if not cur or not is_task_success_reject(cur.get("原因")):
            # 不在拒绝清单里(可能已被上一轮捞回,或裁决文件与交付对不上),
            # 或者它压根不是语义判定杀的 —— 两种都不动,记一笔让报告说得出话。
            summary["appeal_skipped"].append(eid)
            continue

        prov = {"复议结论": kind, "备注": a.get("note", ""),
                "复议时间": a.get("at", now), "生效时间": now,
                # 溯源字段用固定英文键:下游(训练侧)按它区分"机器判的"与"人判的",
                # 与中文展示字段各司其职。
                "verdict_source": "human_review"}

        if kind == "维持拒绝":
            cur[PROV_APPEAL] = prov                    # 只记档,判决一个字不改
            summary["appeal_upheld"].append(eid)
            continue

        # 同一条 episode 可能在 review 里还有一份副本,记的是**另一个维度**没解决的
        # 问题(droid-200 ep000023:同步测不准)。复议只推翻成败判定这一条,管不着
        # 它 —— 跟着三边摘除一起销案的话,一个未决问题就这么无声无息没了。
        pend = [c for c in ((r_eps.get(eid) or {}).get("待裁决项") or []) if c != TASK_CN]
        rev_copy = dict(r_eps[eid]) if (eid in r_eps and pend) else None
        old = _take_entry(p_eps, r_eps, j_eps, eid)
        # 读数与软分一律以**拒绝条目**(cur)为准,不用 _take_entry 挑的那份:上面那种
        # 稀疏 review 副本会先被命中,拿它当底,VLM 当时的读数和综合软分就全丢了
        # (droid-200 ep000023 实锤:捞回后 checks 只剩一个"结果": "pass")。
        checks = dict(cur.get("checks") or {})
        entry = dict(checks.get(TASK_CN) or {})
        # 与成败裁决同款:结果跟着人的结论走,VLM 当时的读数(detail)原样留着,
        # 谁也没被抹掉 —— 复盘时"机器当时怎么想的"是最重要的线索。
        entry["结果"] = "pass"
        checks[TASK_CN] = entry
        keep = {}
        for src in (old, cur):                 # cur 是权威那份,后写赢
            keep.update({k: src[k] for k in (PROV_RELABEL, PROV_LABEL, PROV_TASK)
                         if k in src})
        p_eps[eid] = {"判决": "通过(人工复议)", "综合软分": cur.get("综合软分"),
                      "checks": checks, **keep, PROV_APPEAL: prov}
        if rev_copy is not None:
            rev_copy["待裁决项"] = pend
            rev_copy["弃权原因"] = {k: v for k, v in
                                    (rev_copy.get("弃权原因") or {}).items()
                                    if k != TASK_CN}
            rev_copy["当前判决"] = "通过"      # 判决已翻,视图里的旧值不能留着骗人
            r_eps[eid] = rev_copy
        summary["appeal_restored"].append(eid)

    _sync_counts(review, reject, r_eps, j_eps)
    return summary


def _carryover_section(records: list) -> list:
    """report.md 的「沿用」小节行(纯函数;records = decisions_view 的输出)。

    只统计**已落库**的裁决:没应用的决定还没进任何结论,谈不上沿用(它有自己的
    「未应用」提醒,在 UI)。计数把「改变结果的」与「仅标记的」分开报(用户点名:
    混成一个数会让人以为 38 条结论全被人动过,而实际只有其中十几条改了结果)。
    没有任何沿用、且新旧可判时返回空列表 —— 整节不出现,不制造噪音;时间解析
    不出(老布局交付/手写占位时间)则如实写「无法判定新旧」,不许猜。
    """
    applied = [r for r in records if r["status"] == "applied"]
    carry = [r for r in applied if r["when"] == "carryover"]
    undated = [r for r in applied if r["when"] == "unknown"]
    fresh = [r for r in applied if r["when"] == "fresh"]
    if not carry and not undated:
        return []
    sec = ["\n### 沿用自此前的人工裁决\n",
           "人工裁决记录在交付根 human-decisions/ 下跨跑批共用:裁决时间早于本次"
           "跑批开始的,是此前人工裁过、本次自动沿用的结论。\n"]
    if carry:
        n_chg = sum(1 for r in carry if r["changes_result"])
        sec.append(f"- 沿用 {len(carry)} 条:改变结果的 {n_chg} 条"
                   f"(采纳改标/弃用/判成功/判失败/捞回),"
                   f"仅标记的 {len(carry) - n_chg} 条"
                   f"(维持原标注/搁置/维持拒绝,不改变任何数据)\n")
        sec.append(f"- 本轮新裁 {len(fresh)} 条\n")
        sec.append("\n| episode | 裁决 | 裁决时间 |\n|---|---|---|\n")
        for r in carry:
            sec.append(f"| {r['id']} | {r['kind']} | {r['at']} |\n")
    if undated:
        sec.append(f"- ⚠️ {len(undated)} 条已应用的裁决无法判定新旧"
                   f"(裁决时间或本次跑批开始时间解析不出),不区分沿用与新裁\n")
    return sec


def _sync_dataset_summary(files: dict) -> None:
    """裁决应用后,把 passed.json 的 dataset 汇总块对回真实条目数。

    2026-08-25 droid-50 实战复盘 ①:rejudge 剔了 5 条、passed.episodes 只剩
    39,可 dataset.delivered 还写着 44 —— 报告页总览读的正是这个块,客户刚点完
    执行回头一看数字没动,像没生效。汇总块是交付的活账本,谁改条目谁就得平账;
    report.md 不动(它是那次跑批的历史快照,该保持原样)。

    全部**从当前条目重算**而非增量加减 —— rejudge 可反复执行,幂等靠重算。
    老交付没有 dataset 块/没有 delivered 字段就整个跳过:不占位、不猜。
    「人工裁决」类的判废量取 reject 条目 `原因` 以「人工裁决」开头的计数
    (弃用/判失败两条线落库时写的就是这个前缀);改标重判失败的条目原因是
    机器判定文案,归机器类,不算在这里。
    """
    p = files.get("passed") or {}
    d = p.get("dataset")
    if not isinstance(d, dict) or "delivered" not in d:
        return
    delivered = len(p.get("episodes") or {})
    if d.get("delivered") == delivered and not d.get("human_drop"):
        return                                  # 没动过条目,账本原样
    d["delivered"] = delivered
    n_input = d.get("input_episodes")
    dedup = d.get("dedup_removed") or 0
    if isinstance(n_input, int):
        d["verdict_drop"] = n_input - (dedup if isinstance(dedup, int) else 0)             - delivered
    n_human = sum(1 for e in (files.get("reject", {}).get("episodes") or {}).values()
                  if str((e or {}).get("原因") or "").startswith("人工裁决"))
    if n_human:
        d["human_drop"] = n_human
    else:
        d.pop("human_drop", None)
    ss = d.get("summary_stats")
    if isinstance(ss, dict):
        if "pass_rate_pct" in ss and isinstance(n_input, int) and n_input:
            ss["pass_rate_pct"] = round(delivered * 100.0 / n_input, 1)
        if "avg_soft_score" in ss:
            scores = [e.get("综合软分") for e in (p.get("episodes") or {}).values()
                      if isinstance(e.get("综合软分"), (int, float))]
            if scores:
                ss["avg_soft_score"] = round(sum(scores) / len(scores), 4)


def run_rejudge(delivery: str, input_dir: str, cfg: dict,
                rerun_fn: Callable | None = None) -> dict:
    """读裁决 → 重判(采纳条目)→ 更新交付。rerun_fn 注入(测试用假函数);
    生产缺省 = _build_rerun(cfg)(多视角 v7.3 全协议,与漏斗同源构件)。

    收尾必清 RRD 的临时视频缓存(P5):RRD 输入的重判要把 .rrd 里的视频字节解成
    本地 mp4,一条几十 MB 躺在容器可写层;rejudge 是可以被反复调用的命令,不清就是
    每裁决一轮涨一批(与 run 的 finally 同一道纪律)。LeRobot 输入下这是空操作。
    """
    try:
        return _run_rejudge(delivery, input_dir, cfg, rerun_fn)
    finally:
        from ..ingest.rrd_reader import cleanup_video_cache
        cleanup_video_cache(input_dir)


def _run_rejudge(delivery: str, input_dir: str, cfg: dict,
                 rerun_fn: Callable | None = None) -> dict:
    decisions = human.load_label_decisions(delivery)
    verdicts = human.load_task_verdicts(delivery)
    appeals = human.load_reject_appeals(delivery)
    # 快照留给报告的「沿用」小节:下面的幂等跳过会从工作字典里 pop,而沿用统计
    # 要看**全量**(跳过的那些正是"此前裁过、本次仍生效"的主力)。
    all_lines = (dict(decisions), dict(verdicts), dict(appeals))
    if not decisions and not verdicts and not appeals:
        return {"note": "无裁决记录(交付目录 human-decisions/ 下的 "
                        "label_decisions.csv、task_verdicts.csv、reject_appeals.csv "
                        "都不存在或为空;2026-08-14 之前的交付也会去老位置 details/ "
                        "找一遍),未做任何事"}

    files = {}
    for name in ("passed", "review", "reject"):
        path = os.path.join(delivery, f"{name}.json")
        with open(path, encoding="utf-8") as f:
            files[name] = json.load(f)

    # ⚠️ 不做全局"去重清扫":弃权条目**同时**出现在 passed(当前判决=通过)与
    # review(待裁决视图)是交付格式的设计,不是残留(2026-08-06 误清 14 条实锤,
    # 靠 passed 的弃权明细重建救回)。只允许**定向**清理:已裁决条目的旧副本。

    # 幂等跳过:同一条裁决(裁决时间+内容都没变)已经落库的,整条不再进 apply ——
    # 采纳改标那条线还省下重复付 VLM 重判的钱(2026-08-06 用户点名:重跑 rejudge
    # 曾把两条又判了一遍);弃用/维持/搁置/维持拒绝虽然便宜,重复应用会在报告里
    # 把同一件事反复计数。判据取三件套里的落库溯源,而非旁路文件——以真实交付
    # 状态为准。⚠️ 判据本体在 dataset_level/decisions.py(*_applied_in),与 UI 的
    # 已应用/未应用计数**同源**;不许在这里另写一套比对,两套判据迟早说两种话
    # (有测试用替换共享判据的方式钉死这条调用链,改回内联写法就红)。
    adopt = {e: d for e, d in decisions.items()
             if d.get("decision") == "采纳建议改标" and str(d.get("new_label", "")).strip()}
    unchanged: list = []
    for eid in list(decisions):
        d = decisions[eid]
        hit = human.label_decision_applied_in(files, eid, d)
        if not hit:
            continue
        unchanged.append(eid)
        decisions.pop(eid)                  # 整条不再进 apply,交付保持原样
        if adopt.pop(eid, None) is not None:
            # 定向清理(只有采纳改标有跨文件搬移史):裁决已落库,该 episode 在
            # 其它文件里的无溯源旧副本即为残留(老 _take 只摘第一处留下的僵尸),
            # 就地清掉
            for other in ("passed", "review", "reject"):
                if other != hit:
                    oe = files[other].get("episodes", {})
                    if (eid in oe and PROV_RELABEL not in oe[eid]
                            and PROV_LABEL not in oe[eid]):
                        oe.pop(eid)

    # 成败裁决与复议的幂等(同一套判据,合流进 unchanged):这两条线不花 VLM 的钱,
    # 但重复应用会把"拿不准"/"维持拒绝"当成新事件在报告里反复计数,越滚越多。
    for eid in list(verdicts):
        if human.task_verdict_applied_in(files, eid, verdicts[eid]):
            unchanged.append(eid)
            verdicts.pop(eid)               # 整条不再进 apply,交付保持原样
    for eid in list(appeals):
        if human.reject_appeal_applied_in(files, eid, appeals[eid]):
            unchanged.append(eid)
            appeals.pop(eid)                # 整条不再进 apply,交付保持原样

    # 「改标 + 人工成败结论」→ 跳过 VLM 重判,直接采信人的结论(2026-08-16 用户
    # 拍板的有意例外,理由见 apply_decisions 的 docstring)。判据看**全量** CSV
    # 快照而不是幂等剩下的工作字典:成败结论可能是此前某轮裁的、本轮被幂等跳过,
    # 但它仍是人的结论 —— 机器不该复核它。搁置不算(它是「待定」不是结论),
    # 只改标、没给结论的条目**完全维持今天的行为**(照旧走下面的 VLM 重判)。
    human_concluded = {e for e in adopt
                       if all_lines[1].get(e, {}).get("verdict")
                       in ("判成功", "判失败")}
    for e in human_concluded:
        adopt.pop(e)

    rejudged: dict = {}
    _lat_mark = 0
    if adopt:
        if rerun_fn is None:
            rerun_fn = _build_rerun(cfg)
        # 重判也要入账:记下当前进程延时明细的水位,重判结束后把增量并进交付
        # 的 vlm_latency.csv(2026-08-06 用户抓包:四次 rejudge 上百次 VLM 调用
        # 全没进延时剖析,表上数字永远是原始 run 的快照)
        try:
            from ..adapters.vlm_client import latency_rows
            _lat_mark = len(latency_rows())
        except Exception:  # noqa: BLE001
            _lat_mark = -1

        def _one(item):
            eid, d = item
            try:
                return eid, rerun_fn(input_dir, eid, d["new_label"])
            except Exception as e:  # noqa: BLE001  单条重判失败不拖垮整批
                print(f"[rejudge] ⚠️ {eid} 重判失败({type(e).__name__}: {e}),该条原样不动",
                      flush=True)
                return eid, None

        # episode 级并行(重判段 VLM 占 ~100% 墙钟;客户端帧级并发之上再叠 episode 级)
        import time as _t2
        from concurrent.futures import ThreadPoolExecutor
        _rj_t0 = _t2.time()
        print(f"[rejudge] 重判 {len(adopt)} 条(完整多视角协议,并发≤8;"
              f"方舟每条约 2 分钟,自托管约 30 秒)…", flush=True)
        _done = 0
        with ThreadPoolExecutor(max_workers=min(8, len(adopt))) as ex:
            for eid, res in ex.map(_one, list(adopt.items())):
                _done += 1
                verdict = (res or {}).get("verdict", "失败")
                print(f"[rejudge] 重判 {_done}/{len(adopt)}:{eid} → {verdict}"
                      f" | 已用 {int(_t2.time() - _rj_t0)}s", flush=True)
                if res is not None:
                    rejudged[eid] = res

    summary = apply_decisions(files["passed"], files["review"], files["reject"],
                              decisions, rejudged,
                              human_concluded=human_concluded)
    # 成败裁决后于标注裁决应用:改标重判可能刚把某条从弃权变成了 pass/reject,
    # 此时它已不在待裁决队列里,但人工可能在更早的一轮就给过成败裁决——以人为准。
    #
    # 🔴 **整条弃用压过成败裁决**(2026-08-19 用户定义的语义:「一旦点了这个按钮,
    # 不管打标还是成败点了什么,这条都弃用」)。不挡的话顺序就要了命:弃用在
    # apply_decisions 里已经把它搬进 reject,而这里的「判成功」会**原地重建条目
    # 写回 passed** —— 弃用被无声翻掉,人点的最重的那个动作反而最不算数。
    # 挡在入口而不是改 apply_task_verdicts:那是个纯数据函数,让它去关心"别的线
    # 干了什么"就把两条线耦上了;这里过滤一次,两个函数各自还是只管自己那摊。
    _dropped = set(summary.get("dropped") or ())
    _verdicts = {e: v for e, v in verdicts.items() if e not in _dropped}
    summary["verdict_overridden_by_drop"] = sorted(
        e for e in verdicts if e in _dropped)
    summary.update(apply_task_verdicts(files["passed"], files["review"],
                                       files["reject"], _verdicts))
    # 复议最后应用:它的输入是"当前拒绝清单",前两条线可能刚往里放进条目
    # (改标重判仍未完成 / 人工判失败)。那些是本轮刚判的,拒因不是语义判定
    # 就进不了复议,顺序在这里只影响"看到的是哪一版拒绝清单"——以最新为准。
    summary.update(apply_reject_appeals(files["passed"], files["review"],
                                        files["reject"], appeals))
    summary["unchanged"] = unchanged

    # 收尾报账(2026-08-16 用户点名):改标重判后仍判不出的条目会掉回「待你裁决」
    # 队列 —— 这里不明说,用户下次打开界面才自己发现还有第二轮。
    n_applied = len(decisions) + len(verdicts) + len(appeals)
    still = list(summary["adopted_review"])
    closing = f"本轮处理 {n_applied} 条裁决"
    if still:
        closing += (f",其中 {len(still)} 条重判后仍判不出,已进入「待你裁决」队列"
                    f"({'、'.join(still)})—— 需要到「人工裁决 · 待你裁决」"
                    "补个成败结论,再执行一次")
    else:
        closing += ",没有重判后仍判不出的条目"
    summary["closing"] = closing

    det = os.path.join(delivery, "details")
    os.makedirs(det, exist_ok=True)

    # 技能画像同步(2026-08-16 标注优先方针的第③条):裁决改变了交付集合与标注,
    # 画像不跟着动 = 技能分布里数着已被剔除的数据、错格子的条目继续错 —— 报告在
    # 骗人。必须排在下面那遍三件套落盘**之前**(它要改 files["passed"]["skills"],
    # 与延时入账同一条纪律:同一个产物一次只落一遍)。
    _ps = _sync_profile(delivery, files, summary, decisions, cfg)
    if _ps is not None:
        summary["profile_sync"] = _ps

    # 重判段延时增量入账(整写非追加——FSX 拒绝 O_APPEND)+ 刷新汇总快照
    # ⚠️ 这一段**必须排在下面那遍落盘之前**:它要往 files["passed"] 里塞
    # vlm_latency,原来放在落盘之后 ⇒ 同一个 passed.json 两秒内被写了两遍。
    # 2026-08-14 事故:那份 passed.json 最后是 138719 字节全 `\0`(发布那步当时
    # 还是就地截断,两遍撞上就永久停在中间态)。发布已改成原子改名,写两遍不再
    # 致命 —— 但"同一个产物一次只落一遍"本身就该是纪律,不靠下游兜底。
    if rejudged and _lat_mark >= 0:
        try:
            from ..adapters.vlm_client import (LATENCY_CSV_HEADER,
                                               latency_rows, latency_summary,
                                               read_latency_csv)
            delta = latency_rows()[_lat_mark:]
            if delta:
                csv_path = os.path.join(det, "vlm_latency.csv")
                import csv as _csv
                rows_all: list = []
                if os.path.exists(csv_path):
                    # 读老档走同一份降级读法(三列/四列老 CSV 缺列补默认),
                    # 别在这里手写第二份解析——2026-08-15 加列时就是靠它免改
                    rows_all = read_latency_csv(csv_path)
                rows_all.extend(delta)
                with delivery_file(csv_path, newline="") as f:
                    w = _csv.writer(f)
                    w.writerow(LATENCY_CSV_HEADER)
                    for t, s, ok, st, cid, att, fk in rows_all:
                        w.writerow([t, s, int(ok), "" if st is None else st,
                                    cid or "", att, fk or ""])
                ds = files["passed"].setdefault("dataset", {})
                ds["vlm_latency"] = latency_summary(rows_all)
        except Exception as e:  # noqa: BLE001  入账失败不影响重判结果
            print(f"[rejudge] ⚠️ 延时入账失败({type(e).__name__}: {e})", flush=True)

    _sync_dataset_summary(files)
    for name, data in files.items():
        write_json(os.path.join(delivery, f"{name}.json"), data, default=str)
    with delivery_file(os.path.join(det, "rejudge_results.json")) as f:
        json.dump({"at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "summary": summary,
                   "rejudged": {e: {k: v for k, v in r.items() if k != "detail"}
                                for e, r in rejudged.items()},
                   # 本次真正应用的成败裁决(幂等跳过的不在此列)
                   "task_verdicts": {e: v.get("verdict", "")
                                     for e, v in verdicts.items()},
                   "reject_appeals": {e: a.get("appeal", "")
                                      for e, a in appeals.items()},
                   "待人工裁决总数": len(files["review"].get("episodes") or {})},
                  f, ensure_ascii=False, indent=1)

    # 交付数据集同步(2026-08-06 出数据闭环):裁决不能只改报告——episodes_parquet
    # 里的 instruction 还是旧标注,弃用条目还躺在数据里,交出去就是脏数据。
    # 采纳改标 → 改写该条 instruction(溯源=人工裁决改标);弃用 → 整行剔除;
    # 重判仍失败(adopted_reject)→ 同样剔除。report-only 交付没有 parquet,跳过。
    pq_dir = os.path.join(delivery, "episodes_parquet")
    # 成败裁决判失败的条目走**同一段**同步逻辑(只是它不改 instruction,只剔行):
    # 不接进来的话,人判了失败、报告写了拒绝,数据集里那条还在,交出去仍是脏数据。
    # 复议捞回的条目方向相反:它在原 run 里就被拒了,交付数据集里**根本没有**这一行
    # (不是改一改,是要回源把它加回来)——所以它也必须触发这段同步。
    # 「改标 + 人工成败结论」条目里仍在交付的那部分(人工判成功/结论早已应用):
    # 新标注要写进交付数据集;人工判失败的那部分走 verdict_fail 的剔行,不在此列。
    human_kept = [e for e in summary.get("adopted_human", [])
                  if e in (files["passed"].get("episodes") or {})
                  or e in (files["review"].get("episodes") or {})]
    touched = (summary["adopted_pass"] + summary["adopted_review"]
               + summary["adopted_reject"] + summary["dropped"]
               + summary["verdict_fail"] + summary["appeal_restored"]
               + human_kept)
    if touched and os.path.isdir(pq_dir):
        try:
            import daft as _daft
            df = _daft.read_parquet(pq_dir).to_pydict()
            n = len(df["episode_id"])
            rows_pq = [{k: df[k][i] for k in df} for i in range(n)]
            drop_ids = (set(summary["dropped"]) | set(summary["adopted_reject"])
                        | set(summary["verdict_fail"]))
            relabel = {e: decisions[e]["new_label"] for e in
                       summary["adopted_pass"] + summary["adopted_review"]
                       + human_kept
                       if e in decisions}
            out_rows = []
            for r in rows_pq:
                eid = r["episode_id"]
                if eid in drop_ids:
                    continue
                if eid in relabel:
                    r["instruction"] = relabel[eid]
                    if "instruction_source" in r:
                        r["instruction_source"] = "人工裁决改标"
                out_rows.append(r)
            n_drop = n - len(out_rows)
            # 复议捞回是**加行**:这条当初被拒 ⇒ 从来没进过 episodes_parquet,
            # 改不出来,只能回源重读补进去。读走与重判同一条现成路径
            # (_episode_row_reader,LeRobot / RRD 一视同仁)。
            have = {r["episode_id"] for r in out_rows}
            restore = [e for e in summary["appeal_restored"] if e not in have]
            n_add = 0
            if restore:
                _read_src = _episode_row_reader(input_dir, cfg)
                src = {r["episode_id"]: r for r in _read_src(
                    input_dir, episode_indices={int(e[2:]) for e in restore},
                    validate=False)}
                for eid in restore:
                    r = src.get(eid)
                    if r is None:
                        print(f"[rejudge] ⚠️ {eid} 回源没读到,交付数据集里补不进这一行"
                              f"(三件套已按复议翻判)", flush=True)
                        continue
                    # 只投影到 parquet 现有列上:多的列不带进去(schema 一变
                    # from_pydict 就炸),缺的列留 None 由 daft 按列类型处理。
                    row = {k: r.get(k) for k in df}
                    if "instruction_source" in df:
                        # 质检时的自产 caption 补标在这里拿不到 —— 按源数据如实写,
                        # 宁可写"无"也不给交付编一句描述。
                        row["instruction_source"] = ("原始标注"
                                                     if str(r.get("instruction") or "").strip()
                                                     else "无")
                    out_rows.append(row)
                    n_add += 1
                out_rows.sort(key=lambda x: str(x["episode_id"]))
            if len(out_rows) != n or relabel:
                import shutil as _sh
                cols = {k: [r[k] for r in out_rows] for k in out_rows[0]} if out_rows else {}
                if cols:
                    # 曾是 rmtree 再 write_parquet(先删后写,中间零保护):写失败 /
                    # 进程被杀,客户拿走的数据集就彻底没了。改为 staging 写好再原子
                    # 换位 —— 新数据落定之前,原目录一个字节都不动。
                    with delivery_dir(pq_dir) as _staging:
                        _daft.from_pydict(cols).write_parquet(_staging)
                else:
                    # 全部条目被剔:数据集不复存在(与旧行为一致,目录移除)。
                    # 纯删除没有"删了一半再写"的窗口,rmtree 即是本意。
                    _sh.rmtree(pq_dir)
                print(f"[rejudge] 交付数据集已同步:改标 {len(relabel)} 条,"
                      f"剔除 {n_drop} 条,复议捞回补入 {n_add} 条"
                      f"(episodes_parquet)", flush=True)
                # LeRobot 包同步:v2 源重导出=纯文件拷贝;v3 源要把通过的轨迹视频重新
                # 剪切编码(2026-08-21 用户定:这是 bug 不是限制 —— 之前只打印"请重跑 run",
                # 而 run 不会应用人工裁决,v3 客户永远拿不到落实了裁决的成品包)。两代
                # 导出器签名相同,以同步后的 parquet 为唯一事实源重建导出参数(终态条目集
                # + 非原始标注的任务覆写);新包在同级临时目录写好再原子换位。
                curated = os.path.join(delivery, "lerobot_curated")
                if os.path.isdir(curated):
                    from ..export import lerobot_writer as _lw
                    from ..ingest.lerobot_reader import _load_info
                    is_v3 = str(_load_info(input_dir).get("codebase_version", "")).startswith("v3")
                    exporter = _lw.export_lerobot_v3 if is_v3 else _lw.export_lerobot_v2
                    keep_idx = [int(r["episode_id"][2:]) for r in out_rows]
                    ov = {int(r["episode_id"][2:]): r["instruction"]
                          for r in out_rows
                          if r.get("instruction_source") not in (None, "", "原始标注")
                          and str(r.get("instruction") or "").strip()}
                    # 相机流健康度旁挂文件跟着重导出走:换位之前先读回来,
                    # 否则裁决一次交付集就把它弄丢了(它不随裁决变化,只是重编号)
                    _ch = None
                    _chp = os.path.join(curated, "meta",
                                        "curation_camera_health.json")
                    if os.path.exists(_chp):
                        try:
                            with open(_chp, encoding="utf-8") as _f:
                                _old = json.load(_f)
                            _ch = {"dataset": _old.get("dataset") or {},
                                   "episodes": {r["source_episode_id"]: r
                                                for r in _old.get("episodes") or []
                                                if r.get("source_episode_id")}}
                        except Exception:  # noqa: BLE001  读不回就不写,不拦裁决
                            _ch = None
                    if is_v3:
                        print(f"[rejudge] lerobot_curated(v3)重导出:{len(keep_idx)} 条的"
                              "视频需重新编码,耗时与首轮导出相当,请稍候", flush=True)
                    if keep_idx:
                        with delivery_dir(curated) as _staging:
                            exporter(input_dir, keep_idx, _staging,
                                     task_overrides=ov, camera_health=_ch)
                    else:
                        _sh.rmtree(curated)        # 全部剔除:成品包不复存在
                    print(f"[rejudge] lerobot_curated 已重导出"
                          f"({'v3 视频重编码' if is_v3 else 'v2 纯拷贝'}):"
                          f"{len(keep_idx)} 条,任务覆写 {len(ov)} 条", flush=True)
                # RRD 包同步(2026-08-10):交付里有 rrd_curated ⇒ 输入是 RRD 源。
                # 重导出同样便宜(通过条目=字节拷贝,改标条目只重写 /task),没有
                # 理由留给用户手动。同样以同步后的 parquet 为唯一事实源。
                # (P5:重判本体已格式无关 —— 见 _episode_row_reader,RRD 输入的
                #  "采纳改标 → 用新标注重跑成败判定"与 LeRobot 走同一条路。)
                rrd_curated = os.path.join(delivery, "rrd_curated")
                if os.path.isdir(rrd_curated):
                    from ..export.rrd_writer import export_rrd_curated
                    keep_eids = [r["episode_id"] for r in out_rows]
                    rrd_ov = {r["episode_id"]: r["instruction"] for r in out_rows
                              if r.get("instruction_source") not in (None, "", "原始标注")
                              and str(r.get("instruction") or "").strip()}
                    rrd_eps = {r["episode_id"]: {
                        "verdict": "通过",
                        "instruction": r.get("instruction") or "",
                        "instruction_source": r.get("instruction_source") or "",
                    } for r in out_rows}
                    _sh.rmtree(rrd_curated)
                    export_rrd_curated(
                        delivery, input_dir, keep_eids, relabels=rrd_ov,
                        episodes=rrd_eps,
                        generated_at=datetime.datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"))
                    print(f"[rejudge] rrd_curated 已重导出:{len(keep_eids)} 条,"
                          f"改标 {len(rrd_ov)} 条", flush=True)
        except Exception as e:  # noqa: BLE001  数据集同步失败不吞掉裁决结果
            print(f"[rejudge] ⚠️ 交付数据集同步失败({type(e).__name__}: {e});"
                  f"三件套已更新,episodes_parquet 仍是旧标注", flush=True)

    # 报告小节用"读全文+整写"而非追加:交付目录在 TOS 的 FSX 挂载上,
    # open(..., "a") 直接 EINVAL(2026-08-06 生产实锤,与 decisions.py 同坑)。
    md = os.path.join(delivery, "report.md")
    if os.path.exists(md):
        with open(md, encoding="utf-8") as f:
            body = f.read()
        sec = ["\n## 人工裁决(curation rejudge)\n",
               f"- 执行时间:{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n",
               "\n### 标注裁决与重判\n",
               f"- 采纳改标并重判:通过 {len(summary['adopted_pass'])} / "
               f"弃权 {len(summary['adopted_review'])} / "
               f"仍未完成 {len(summary['adopted_reject'])} 条\n",
               f"- 人工弃用:{len(summary['dropped'])} 条;"
               f"维持原标注(审计误旗):{len(summary['kept'])} 条\n"
               f"标注拿不准(仍在待裁决队列):{len(summary.get('held') or [])} 条\n"]
        if summary.get("adopted_human"):
            sec.append(f"- 采纳改标且人工已给成败结论:{len(summary['adopted_human'])} 条"
                       "(跳过重判,以人的结论为准;标注与成败溯源分别见条目上的"
                       "「标注修正」与「人工裁决」)\n")
        for e in summary["adopted_pass"]:
            sec.append(f"  - {e}:标注修正后重判通过,已回归交付(溯源见 passed.json)\n")
        sec.append("\n### 任务成败人工裁决(不重判,以人的结论为准)\n")
        sec.append(f"- 判成功回归交付:{len(summary['verdict_pass'])} 条;"
                   f"判失败剔除:{len(summary['verdict_fail'])} 条;"
                   f"拿不准(仍在待裁决队列):{len(summary['verdict_hold'])} 条\n")
        for e in summary["verdict_fail"]:
            sec.append(f"  - {e}:人工判失败,已移出交付数据集(溯源见 reject.json)\n")
        if summary.get("verdict_skipped"):
            sec.append(f"- ⚠️ 未处理(裁决词不识别/交付里找不到该条):"
                       f"{summary['verdict_skipped']}\n")
        # 复议小节只在真有复议时出现:没人复议过的交付里,写一行"捞回 0 条"
        # 只会让人以为系统在这一轮干了什么。
        if (summary["appeal_restored"] or summary["appeal_upheld"]
                or summary["appeal_skipped"]):
            sec.append("\n### 被拒复议(只限「任务成败判定」拒掉的条目)\n")
            sec.append(f"- 人工复议捞回 {len(summary['appeal_restored'])} 条;"
                       f"维持拒绝 {len(summary['appeal_upheld'])} 条\n")
            for e in summary["appeal_restored"]:
                sec.append(f"  - {e}:人工复议判为可用,已从拒绝清单移除并回归交付"
                           f"(溯源见 passed.json)\n")
            if summary["appeal_skipped"]:
                sec.append(f"- ⚠️ 未处理(不在拒绝清单里/该条的拒绝理由不可复议):"
                           f"{summary['appeal_skipped']}\n")
        # 裁决沿用要看得见(2026-08-16 用户拍板):裁决 CSV 住在交付根、跨跑批
        # 共用,新跑一批之后 rejudge 会把三周前的人工裁决自动再应用上来 —— 报告
        # 不说,读者会以为这批结论全是机器判的。判据与幂等跳过同源(decisions_view
        # 内部走同一批 *_applied_in),沿用/新裁按「裁决时间 vs 本次跑批开始」分。
        sec.extend(_carryover_section(human.decisions_view(
            files, *all_lines, run_started=human.run_started_at(delivery))))
        _psx = summary.get("profile_sync")
        if _psx and "note" not in _psx:
            sec.append("\n### 技能画像同步(标注优先方针,不重跑 caption/不重归纳体系)\n")
            sec.append(f"- 重归类 {len(_psx.get('reassigned', []))} 条;"
                       f"移除(已出交付){len(_psx.get('removed', []))} 条;"
                       f"复议补回 {len(_psx.get('restored', []))} 条;"
                       f"未归类 {len(_psx.get('unassigned', []))} 条\n")
            if _psx.get("label_missing_kept"):
                sec.append(f"- ⚠️ 取不到原始标注、维持原归类:"
                           f"{_psx['label_missing_kept']}\n")
        elif _psx and _psx.get("note"):
            sec.append(f"\n- ⚠️ {_psx['note']}\n")
        sec.append(f"\n- 剩余待人工裁决:{len(files['review'].get('episodes') or {})} 条\n")
        if summary.get("unchanged"):
            sec.append(f"- 裁决未变跳过(不重复重判):{len(summary['unchanged'])} 条\n")
        if summary["skipped"]:
            sec.append(f"- ⚠️ 未处理(重判失败/裁决词不识别):{summary['skipped']}\n")
        write_text(md, body + "".join(sec))

    # 落盘回验(与 run 同款,2026-08-06):rejudge 改写的文件在对象存储上有读
    # 可见延迟,不回验就宣布完成 → 用户立刻刷 UI 看到的还是旧数据(实锤)。
    from .run import _verify_delivery_visible
    vis = [(n + ".json", os.path.join(delivery, n + ".json"), "json")
           for n in ("passed", "review", "reject")]
    vis.append(("rejudge_results.json",
                os.path.join(det, "rejudge_results.json"), "json"))
    pq = os.path.join(delivery, "episodes_parquet")
    if os.path.isdir(pq):
        vis.append(("episodes_parquet", pq, "dir"))
    _verify_delivery_visible(vis, timeout_s=180.0)
    # 收尾报账放在最后一行打印:回验之后才宣布,数字与用户马上会在界面看到的一致
    print(f"[rejudge] {summary['closing']}", flush=True)
    return summary


def _sync_profile(delivery: str, files: dict, summary: dict,
                  decisions: dict, cfg: dict) -> dict | None:
    """裁决 → 技能画像与 skill_assignment.csv 同步(2026-08-16 标注优先方针③)。

    判据一句话:**画像描述的是"我们交付的那批数据"**。三条裁决线对画像各自的动作:
    - 采纳改标重判通过/弃权(仍在交付)→ 用**新标注**对现有体系重新归类;
      重判仍失败(进 reject)→ 与"弃用"同款移除;
    - 弃用该条 / 成败裁决判失败 → 从画像与 CSV 移除(已被剔出交付,继续数着
      = 报告在骗人);
    - 维持原标注 → 用**原始标注**重新归类(老交付按旧策略是 caption 归的,26 条
      正躺在错误格子里);幂等:已是标注归的重算结果相同,零变化;
    - 复议捞回 → 归类补回:有原始标注按标注;只有质检时的自产 caption 就按 caption
      (task_details 里现成的,**绝不重跑 VLM**);两样都没有留「未归类」并如实报数。

    **不重跑 caption、不重新归纳体系**:全部动作 = 纯文本对既有体系再分配,归不进
    去的才(配了 VLM 时)问一次 LLM 补漏。老交付没画像 / 降级扁平画像 → 返回 None
    (体系都没有,无从归类,不硬造)。
    """
    import csv as _csv

    from ..dataset_level.reassign import (NO_SUBSKILL, SRC_CAPTION, SRC_LABEL,
                                          SRC_NONE, UNASSIGNED, member_map_of,
                                          reassign_texts, rebuild_profile,
                                          taxonomy_from_profile)

    removals = set(summary.get("dropped", []) + summary.get("adopted_reject", [])
                   + summary.get("verdict_fail", []))
    # 「改标 + 人工成败结论」条目(adopted_human)只取仍在交付里的:人工判失败的
    # 已进 reject,再按新标注归进画像就是在数一条不存在的数据
    human_kept = [e for e in summary.get("adopted_human", [])
                  if e in ((files.get("passed") or {}).get("episodes") or {})
                  or e in ((files.get("review") or {}).get("episodes") or {})]
    relabel = {e: str(decisions[e].get("new_label", "")).strip()
               for e in (summary.get("adopted_pass", [])
                         + summary.get("adopted_review", [])
                         + human_kept) if e in decisions}
    kept = list(summary.get("kept", []))
    restored = list(summary.get("appeal_restored", []))
    if not removals and not relabel and not kept and not restored:
        return None

    profile = (files.get("passed") or {}).get("skills") or {}
    csv_path = os.path.join(delivery, "details", "skill_assignment.csv")
    if not profile.get("families") or not os.path.exists(csv_path):
        return {"note": "画像未同步:该交付没有两级技能体系或缺 "
                        "skill_assignment.csv(老交付/降级画像),体系不重新归纳"}
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))

    captions: dict = {}
    cap_json = os.path.join(delivery, "details", "captions.json")
    if os.path.exists(cap_json):
        try:
            with open(cap_json, encoding="utf-8") as f:
                captions = json.load(f)
        except Exception:  # noqa: BLE001
            captions = {}
    # 质检时的判定痕迹:维持原标注/捞回条目的标注与自产 caption 都在这里现成躺着
    task_recs: dict = {}
    td = os.path.join(delivery, "details", "task_details.json")
    if os.path.exists(td):
        try:
            with open(td, encoding="utf-8") as f:
                task_recs = json.load(f).get("episodes") or {}
        except Exception:  # noqa: BLE001
            task_recs = {}
    queue_label = {q.get("id"): str(q.get("label") or "")
                   for q in (files.get("review") or {}).get("标注-画面分歧复核队列")
                   or []}

    old = {str(r.get("episode_id") or ""): r for r in rows}
    old.pop("", None)
    member_map = member_map_of(rows)
    tax = taxonomy_from_profile(profile)

    assignment: dict = {}
    new_text: dict = {}
    new_src: dict = {}
    for eid, r in old.items():
        if eid in removals:
            continue
        assignment[eid] = (str(r.get("family") or UNASSIGNED),
                           str(r.get("subskill") or NO_SUBSKILL))
        t = str(r.get("grouping_text") or r.get("caption") or "")
        new_text[eid] = t
        # 老 CSV 没有来源列:旧策略的归类输入就是 caption,如实回填而非臆造
        new_src[eid] = (str(r.get("grouping_text_source") or "")
                        or (SRC_CAPTION if t else SRC_NONE))

    to_assign: dict = {}
    label_missing: list = []
    for eid, lab in relabel.items():
        if eid in removals or not lab:
            continue
        to_assign[eid] = lab
        new_text[eid], new_src[eid] = lab, SRC_LABEL
        assignment.setdefault(eid, (UNASSIGNED, NO_SUBSKILL))
    for eid in kept:
        if eid in removals or eid not in assignment:
            continue
        rec = task_recs.get(eid) or {}
        lab = queue_label.get(eid, "")
        if not lab and str(rec.get("instruction_source") or "") == SRC_LABEL:
            lab = str(rec.get("instruction") or "").strip()
        if lab:
            to_assign[eid] = lab
            new_text[eid], new_src[eid] = lab, SRC_LABEL
        else:
            label_missing.append(eid)      # 取不到原始标注:维持原归类,不许猜
    for eid in restored:
        if eid in assignment:
            continue                       # 已在画像里(异常态),不重复补
        rec = task_recs.get(eid) or {}
        src = str(rec.get("instruction_source") or "")
        txt = str(rec.get("instruction") or "").strip()
        if txt and src in (SRC_LABEL, "人工裁决改标"):
            to_assign[eid] = txt
            new_text[eid], new_src[eid] = txt, SRC_LABEL
        elif txt and src == SRC_CAPTION:   # 质检时的自产 caption,现成的,不重跑 VLM
            to_assign[eid] = txt
            new_text[eid], new_src[eid] = txt, SRC_CAPTION
        else:
            assignment[eid] = (UNASSIGNED, NO_SUBSKILL)
            new_text[eid], new_src[eid] = "", SRC_NONE
        assignment.setdefault(eid, (UNASSIGNED, NO_SUBSKILL))

    from .reprofile import build_llm_ask_from_cfg
    assignment.update(reassign_texts(to_assign, member_map, tax,
                                     build_llm_ask_from_cfg(cfg)))

    cap_of = {eid: str(captions.get(eid)
                       or (old.get(eid) or {}).get("caption") or "")
              for eid in assignment}
    instr_of = {eid: new_text[eid] for eid in assignment
                if new_src.get(eid) == SRC_LABEL}
    new_profile = rebuild_profile(assignment, profile, cap_of, instr_of)
    # ⚠️ 只动 skills 段:成败判定字段(判决/checks)一个字不碰,由 apply_* 独管。
    files["passed"]["skills"] = new_profile
    from ..dataset_level.profile import write_skill_assignment_csv
    os.makedirs(os.path.join(delivery, "details"), exist_ok=True)
    write_skill_assignment_csv(os.path.join(delivery, "details"), new_profile,
                               cap_of, new_text, new_src)

    out = {"removed": sorted(e for e in removals if e in old),
           "reassigned": sorted(to_assign),
           "restored": sorted(e for e in restored if e in assignment and e not in old),
           "unassigned": sorted(e for e, a in assignment.items()
                                if a[0] == UNASSIGNED),
           "label_missing_kept": sorted(label_missing)}
    print(f"[rejudge] 画像同步:重归类 {len(out['reassigned'])} 条,"
          f"移除 {len(out['removed'])} 条,补回 {len(out['restored'])} 条"
          f"(未归类 {len(out['unassigned'])} 条)", flush=True)
    return out


def _episode_row_reader(input_dir: str, cfg: dict) -> Callable:
    """"按 episode 号回源重读行"的**格式无关**分派(与 run.py 的输入嗅探同一套)。

    重判必须回源:新标注要配着重新解码的画面一起问 VLM,交付里没有画面。而源可能是
    LeRobot 也可能是 RRD —— 这里只换"谁来读",返回的函数签名统一为
    `f(input_dir, episode_indices=..., validate=...) -> rows`,重判本体一个字不用改。

    RRD 的 fps 走与原 run 同一个配置键 `ingest.rrd_fps`(rejudge 的 --config 应与原
    run 一致);数据里自带时间戳(bridge 那种)时该键留空也读得出来。
    """
    from ..ingest.rrd_reader import is_rrd_dataset

    if not is_rrd_dataset(input_dir):
        from ..ingest.lerobot_reader import read_lerobot_rows
        return read_lerobot_rows

    from functools import partial

    from ..ingest.lerobot_reader import NotADatasetError
    from ..ingest.rrd_reader import read_rrd_rows

    base = partial(read_rrd_rows, fps=(cfg.get("ingest") or {}).get("rrd_fps"))

    def read(dataset_dir: str, **kw):
        try:
            return base(dataset_dir, **kw)
        except NotADatasetError as e:
            # reader 只知道"没有时间信息",给的出路是 run 的 `--set`;rejudge 没有
            # 这个参数,不补一句的话用户会照抄一条跑不通的命令。
            raise NotADatasetError(
                f"{e}\n  ⚠️ rejudge 的 RRD 输入需在配置里给 ingest.rrd_fps"
                "(与原 run 一致):在 --config 的 YAML 里写 `ingest:\\n  rrd_fps: 30`"
            ) from e

    return read


def _build_rerun(cfg: dict) -> Callable:
    """生产重判器:与漏斗同源的构件组装(多视角联合打分 + 逐机位投票复核)。

    rerun(input_dir, episode_id, new_label) -> {"passed","verdict","detail"}
    """
    from ..adapters.decode import decode_window
    from ..adapters.vlm_client import make_endstate_voter, vlm_completion_from_config
    from ..core.checks.task_success import endstate_review, task_success

    pcfg = cfg.get("pipeline", {})
    interval = pcfg.get("frame_sample_interval_s", 0.5)
    max_side = pcfg.get("frame_max_side", 448)
    max_cams = pcfg.get("max_endstate_cams", 4)
    es_frames = pcfg.get("endstate_frames", 8)
    p_task = cfg["checks"]["task_success"].get("params", {})
    vcfg = cfg["checks"]["task_success"]["vlm"]
    vlm = vlm_completion_from_config(cfg)
    from ..adapters.vlm_client import timeout_for
    voter = make_endstate_voter(vcfg["endpoint"], vcfg["model"],
                                timeout_s=timeout_for("endstate", vcfg),
                                api_key_env=vcfg.get("api_key_env"))

    def rerun(input_dir: str, episode_id: str, new_label: str) -> dict:
        # 嗅探放在这里而不是 _build_rerun 顶上:input_dir 是逐次调用才给的参数
        # (同一个重判器可以喂不同数据集),而嗅探本身只是一次 glob,便宜。
        read_rows = _episode_row_reader(input_dir, cfg)
        rows = read_rows(input_dir, episode_indices={int(episode_id[2:])},
                         validate=True)
        row = next(r for r in rows if r["episode_id"] == episode_id)
        cam_frames = {}
        for cam in sorted(row["video"])[:max_cams]:
            v = row["video"][cam]
            try:
                fr, _ = decode_window(v["path"], v["from_ts"], v["to_ts"],
                                      sample_interval_s=interval, max_side=max_side)
                if fr:
                    cam_frames[cam.split(".")[-1]] = fr
            except Exception:  # noqa: BLE001
                continue
        if not cam_frames:
            raise RuntimeError("所有相机解码失败")
        nmin = min(len(f) for f in cam_frames.values())
        names = list(cam_frames)
        mv = [[(n, cam_frames[n][i]) for n in names] for i in range(nmin)]
        res = task_success(mv, new_label, vlm, **p_task)
        res = endstate_review(res, new_label, voter, cam_frames,
                              endstate_frames=es_frames)
        return {"passed": res.passed, "verdict": res.detail.get("verdict", ""),
                "detail": json.dumps(res.detail, ensure_ascii=False, default=str)}

    return rerun
