"""人工裁决闭环:`curation rejudge`(③,2026-08-05;成败裁决 2026-08-06 并入)。

两条裁决线,一条命令消化(人工确认卡在 caption 与重判之间 = 自产自证陷阱的断路器):

① 标注分歧(details/label_decisions.csv)
    采纳建议改标 → 用**人工确认过的新标注**重跑任务成败检测(多视角 v7.3 全协议),
                  按新判定把 episode 搬进 passed/review/reject,带完整改标溯源;
    弃用该条     → 搬进 reject(人工裁决弃用);
    维持原标注   → 只在分歧队列上标记已裁决(审计误旗,原判定原样)。
② 任务成败弃权(details/task_verdicts.csv)——**不跑 VLM**:系统已经诚实说了
   "判不了",再问一次只会得到同样的弃权,人说了算。
    判成功 → 出待裁决队列,判决改「通过(人工裁决)」,成败检查落 pass;
    判失败 → 三边摘除进 reject,并随现有同步机制从 episodes_parquet /
             lerobot_curated 里剔除(裁决只改报告 = 交出去还是脏数据);
    搁置   → 只记一笔,队列保留(等更多信息)。

  三件套原地更新 + report.md 追加小节 + details/rejudge_results.json 留档。

架构:apply_decisions() / apply_task_verdicts() 是**纯数据函数**(只操作已加载的
JSON dict,可严格单测);重判本体注入(rerun_fn),生产由 run_rejudge 组装真 VLM,
测试注入假函数——与 task_success 的依赖注入同一哲学。
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Callable

TASK_CN = "任务成败判定"

#: 三件套条目上的人工溯源键。标注线与成败线各一个,互不覆盖(同一条 episode
#: 可能先被改标重判、后又被人工判成败),排查时一眼看得出是哪条线动的。
PROV_LABEL = "标注裁决"
PROV_RELABEL = "标注修正"
PROV_TASK = "人工裁决"
_PROV_KEYS = (PROV_RELABEL, PROV_LABEL, PROV_TASK)


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
                    decisions: dict, rejudged: dict) -> dict:
    """按裁决在三件套视图间搬移/标注(**原地修改**传入的 dict)→ 摘要。

    decisions: {eid: {"decision","new_label","note","at"}}(decisions.py schema)
    rejudged:  {eid: {"passed","verdict","detail"}}(仅"采纳建议改标"条目需要;
               缺席 = 重判没跑成,该条不动并记入摘要,绝不臆断)
    """
    p_eps = passed.setdefault("episodes", {})
    r_eps = review.setdefault("episodes", {})
    j_eps = reject.setdefault("episodes", {})
    queue = review.get("标注-画面分歧复核队列") or []
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = {"adopted_pass": [], "adopted_review": [], "adopted_reject": [],
               "dropped": [], "kept": [], "skipped": []}

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
        kind = v.get("verdict")
        prov = {"裁决": kind, "备注": v.get("note", ""),
                "裁决时间": v.get("at", now)}

        if kind == "搁置":
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
        keep = {k: old[k] for k in (PROV_RELABEL, PROV_LABEL) if k in old}

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


def run_rejudge(delivery: str, input_dir: str, cfg: dict,
                rerun_fn: Callable | None = None) -> dict:
    """读裁决 → 重判(采纳条目)→ 更新交付。rerun_fn 注入(测试用假函数);
    生产缺省 = _build_rerun(cfg)(多视角 v7.3 全协议,与漏斗同源构件)。"""
    from ..dataset_level.decisions import (load_label_decisions,
                                           load_task_verdicts)

    decisions = load_label_decisions(delivery)
    verdicts = load_task_verdicts(delivery)
    if not decisions and not verdicts:
        return {"note": "无裁决记录(details/label_decisions.csv 与 "
                        "details/task_verdicts.csv 都不存在或为空),未做任何事"}

    files = {}
    for name in ("passed", "review", "reject"):
        path = os.path.join(delivery, f"{name}.json")
        with open(path, encoding="utf-8") as f:
            files[name] = json.load(f)

    # ⚠️ 不做全局"去重清扫":弃权条目**同时**出现在 passed(当前判决=通过)与
    # review(待裁决视图)是交付格式的设计,不是残留(2026-08-06 误清 14 条实锤,
    # 靠 passed 的弃权明细重建救回)。只允许**定向**清理:已裁决条目的旧副本。

    # 幂等跳过:同一条 episode、同一次裁决(裁决时间+新标注都没变)已经采纳落库的,
    # 不再重复付 VLM 重判的钱(2026-08-06 用户点名:重跑 rejudge 曾把两条又判了一遍)。
    # 判据取三件套里的落库溯源,而非旁路文件——以真实交付状态为准。
    adopt = {e: d for e, d in decisions.items()
             if d.get("decision") == "采纳建议改标" and str(d.get("new_label", "")).strip()}
    unchanged: list = []
    for eid in list(adopt):
        d = adopt[eid]
        for name in ("passed", "review", "reject"):
            entry = files[name].get("episodes", {}).get(eid)
            if entry:
                prov = entry.get("标注修正") or {}
                if (prov.get("裁决时间") == d.get("at")
                        and prov.get("新标注") == str(d.get("new_label", "")).strip()):
                    unchanged.append(eid)
                    adopt.pop(eid)
                    decisions.pop(eid)      # 整条不再进 apply,交付保持原样
                    # 定向清理:裁决已落库,该 episode 在其它文件里的无溯源旧副本
                    # 即为残留(老 _take 只摘第一处留下的僵尸),就地清掉
                    for other in ("passed", "review", "reject"):
                        if other != name:
                            oe = files[other].get("episodes", {})
                            if eid in oe and "标注修正" not in oe[eid]                                     and "标注裁决" not in oe[eid]:
                                oe.pop(eid)
                break

    # 成败裁决的幂等(同样以落库溯源为判据,与上面同一套语义,合流进 unchanged):
    # 这条线不花 VLM 的钱,但重复应用会把"搁置"当成新事件反复计数,报告里越滚越多。
    for eid in list(verdicts):
        v = verdicts[eid]
        for name in ("passed", "review", "reject"):
            entry = files[name].get("episodes", {}).get(eid)
            if entry:
                prov = entry.get(PROV_TASK) or {}
                if (isinstance(prov, dict)
                        and prov.get("裁决时间") == v.get("at")
                        and prov.get("裁决") == v.get("verdict")):
                    unchanged.append(eid)
                    verdicts.pop(eid)       # 整条不再进 apply,交付保持原样
                break

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
                              decisions, rejudged)
    # 成败裁决后于标注裁决应用:改标重判可能刚把某条从弃权变成了 pass/reject,
    # 此时它已不在待裁决队列里,但人工可能在更早的一轮就给过成败裁决——以人为准。
    summary.update(apply_task_verdicts(files["passed"], files["review"],
                                       files["reject"], verdicts))
    summary["unchanged"] = unchanged
    for name, data in files.items():
        with open(os.path.join(delivery, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, default=str)

    det = os.path.join(delivery, "details")
    os.makedirs(det, exist_ok=True)

    # 重判段延时增量入账(整写非追加——FSX 拒绝 O_APPEND)+ 刷新汇总快照
    if rejudged and _lat_mark >= 0:
        try:
            from ..adapters.vlm_client import latency_rows, latency_summary
            delta = latency_rows()[_lat_mark:]
            if delta:
                csv_path = os.path.join(det, "vlm_latency.csv")
                import csv as _csv
                rows_all: list = []
                if os.path.exists(csv_path):
                    with open(csv_path, newline="", encoding="utf-8") as f:
                        for r in _csv.DictReader(f):
                            st = (r.get("started_at") or "").strip()
                            rows_all.append((r["call_type"], float(r["seconds"]),
                                             bool(int(r["ok"])),
                                             float(st) if st else None))
                rows_all.extend(delta)
                with open(csv_path, "w", newline="", encoding="utf-8") as f:
                    w = _csv.writer(f)
                    w.writerow(["call_type", "seconds", "ok", "started_at"])
                    for t, s, ok, st in rows_all:
                        w.writerow([t, s, int(ok), "" if st is None else st])
                ds = files["passed"].setdefault("dataset", {})
                ds["vlm_latency"] = latency_summary(rows_all)
                with open(os.path.join(delivery, "passed.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(files["passed"], f, ensure_ascii=False, indent=1,
                              default=str)
        except Exception as e:  # noqa: BLE001  入账失败不影响重判结果
            print(f"[rejudge] ⚠️ 延时入账失败({type(e).__name__}: {e})", flush=True)
    with open(os.path.join(det, "rejudge_results.json"), "w", encoding="utf-8") as f:
        json.dump({"at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "summary": summary,
                   "rejudged": {e: {k: v for k, v in r.items() if k != "detail"}
                                for e, r in rejudged.items()},
                   # 本次真正应用的成败裁决(幂等跳过的不在此列)
                   "task_verdicts": {e: v.get("verdict", "")
                                     for e, v in verdicts.items()},
                   "待人工裁决总数": len(files["review"].get("episodes") or {})},
                  f, ensure_ascii=False, indent=1)

    # 交付数据集同步(2026-08-06 出数据闭环):裁决不能只改报告——episodes_parquet
    # 里的 instruction 还是旧标注,弃用条目还躺在数据里,交出去就是脏数据。
    # 采纳改标 → 改写该条 instruction(溯源=人工裁决改标);弃用 → 整行剔除;
    # 重判仍失败(adopted_reject)→ 同样剔除。report-only 交付没有 parquet,跳过。
    pq_dir = os.path.join(delivery, "episodes_parquet")
    # 成败裁决判失败的条目走**同一段**同步逻辑(只是它不改 instruction,只剔行):
    # 不接进来的话,人判了失败、报告写了拒绝,数据集里那条还在,交出去仍是脏数据。
    touched = (summary["adopted_pass"] + summary["adopted_review"]
               + summary["adopted_reject"] + summary["dropped"]
               + summary["verdict_fail"])
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
            if len(out_rows) != n or relabel:
                import shutil as _sh
                _sh.rmtree(pq_dir)
                cols = {k: [r[k] for r in out_rows] for k in out_rows[0]} if out_rows else {}
                if cols:
                    _daft.from_pydict(cols).write_parquet(pq_dir)
                print(f"[rejudge] 交付数据集已同步:改标 {len(relabel)} 条,"
                      f"剔除 {n - len(out_rows)} 条(episodes_parquet)", flush=True)
                # LeRobot 包同步:v2 源重导出=纯文件拷贝,便宜到没有理由留给用户
                # 手动;v3 源要视频重编码,只提醒不擅动。以同步后的 parquet 为唯一
                # 事实源重建导出参数(终态条目集 + 非原始标注的任务覆写)。
                curated = os.path.join(delivery, "lerobot_curated")
                if os.path.isdir(curated):
                    from ..ingest.lerobot_reader import _load_info
                    if _load_info(input_dir)["codebase_version"].startswith("v3"):
                        print("[rejudge] ⚠️ lerobot_curated(v3,视频重编码)未自动重做;"
                              "需要最新 LeRobot 包请重跑 curation run(非 report-only)",
                              flush=True)
                    else:
                        from ..export.lerobot_writer import export_lerobot_v2
                        keep_idx = [int(r["episode_id"][2:]) for r in out_rows]
                        ov = {int(r["episode_id"][2:]): r["instruction"]
                              for r in out_rows
                              if r.get("instruction_source") not in (None, "", "原始标注")
                              and str(r.get("instruction") or "").strip()}
                        _sh.rmtree(curated)
                        export_lerobot_v2(input_dir, keep_idx, curated,
                                          task_overrides=ov)
                        print(f"[rejudge] lerobot_curated 已重导出(v2 纯拷贝):"
                              f"{len(keep_idx)} 条,任务覆写 {len(ov)} 条", flush=True)
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
               f"维持原标注(审计误旗):{len(summary['kept'])} 条\n"]
        for e in summary["adopted_pass"]:
            sec.append(f"  - {e}:标注修正后重判通过,已回归交付(溯源见 passed.json)\n")
        sec.append("\n### 任务成败人工裁决(不重判,以人的结论为准)\n")
        sec.append(f"- 判成功回归交付:{len(summary['verdict_pass'])} 条;"
                   f"判失败剔除:{len(summary['verdict_fail'])} 条;"
                   f"搁置(仍在待裁决队列):{len(summary['verdict_hold'])} 条\n")
        for e in summary["verdict_fail"]:
            sec.append(f"  - {e}:人工判失败,已移出交付数据集(溯源见 reject.json)\n")
        if summary.get("verdict_skipped"):
            sec.append(f"- ⚠️ 未处理(裁决词不识别/交付里找不到该条):"
                       f"{summary['verdict_skipped']}\n")
        sec.append(f"\n- 剩余待人工裁决:{len(files['review'].get('episodes') or {})} 条\n")
        if summary.get("unchanged"):
            sec.append(f"- 裁决未变跳过(不重复重判):{len(summary['unchanged'])} 条\n")
        if summary["skipped"]:
            sec.append(f"- ⚠️ 未处理(重判失败/裁决词不识别):{summary['skipped']}\n")
        with open(md, "w", encoding="utf-8") as f:
            f.write(body + "".join(sec))

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
    return summary


def _build_rerun(cfg: dict) -> Callable:
    """生产重判器:与漏斗同源的构件组装(多视角联合打分 + 逐机位投票复核)。

    rerun(input_dir, episode_id, new_label) -> {"passed","verdict","detail"}
    """
    from ..adapters.decode import decode_window
    from ..adapters.vlm_client import make_endstate_voter, vlm_completion_from_config
    from ..core.checks.task_success import endstate_review, task_success
    from ..ingest.lerobot_reader import read_lerobot_rows

    pcfg = cfg.get("pipeline", {})
    interval = pcfg.get("frame_sample_interval_s", 0.5)
    max_side = pcfg.get("frame_max_side", 448)
    max_cams = pcfg.get("max_endstate_cams", 4)
    es_frames = pcfg.get("endstate_frames", 8)
    p_task = cfg["checks"]["task_success"].get("params", {})
    vcfg = cfg["checks"]["task_success"]["vlm"]
    vlm = vlm_completion_from_config(cfg)
    voter = make_endstate_voter(vcfg["endpoint"], vcfg["model"],
                                api_key_env=vcfg.get("api_key_env"))

    def rerun(input_dir: str, episode_id: str, new_label: str) -> dict:
        rows = read_lerobot_rows(input_dir, episode_indices={int(episode_id[2:])},
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
