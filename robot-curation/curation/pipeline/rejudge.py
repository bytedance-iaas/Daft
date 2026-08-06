"""标注裁决闭环:`curation rejudge`(③,2026-08-05)。

流程(人工确认卡在 caption 与重判之间 = 自产自证陷阱的断路器):
  UI/人工在 details/label_decisions.csv 留下三选一裁决 →
  本命令读裁决:
    采纳建议改标 → 用**人工确认过的新标注**重跑任务成败检测(多视角 v7.3 全协议),
                  按新判定把 episode 搬进 passed/review/reject,带完整改标溯源;
    弃用该条     → 搬进 reject(人工裁决弃用);
    维持原标注   → 只在分歧队列上标记已裁决(审计误旗,原判定原样)。
  三件套原地更新 + report.md 追加「标注裁决与重判」节 + details/rejudge_results.json 留档。

架构:apply_decisions() 是**纯数据函数**(只操作已加载的 JSON dict,可严格单测);
重判本体注入(rerun_fn),生产由 run_rejudge 组装真 VLM,测试注入假函数——
与 task_success 的依赖注入同一哲学。
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Callable

TASK_CN = "任务成败判定"


def _state_cn(passed) -> str:
    return {True: "pass", False: "拒绝", None: "弃权"}[passed]


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
        """从三件套里取出该 episode 的现有条目——**三边都摘**,返回信息最全的一份。

        为什么不是"第一个命中就收手":FSX 改写文件有读延迟,rejudge 连跑时曾读到
        旧版 review,让已搬走的条目"复活"成双份(2026-08-06 droid-30 实锤:Episodes
        页签僵尸待裁决)。全量摘除让任何双份在下一次搬移时就地自愈。"""
        best: dict = {}
        for d in (p_eps, r_eps, j_eps):
            if eid in d:
                e = d.pop(eid)
                if not best or ("标注修正" in e or "标注裁决" in e):
                    best = e
        return best

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

    # 计数字段与文件同步(有才更,不发明新键)
    if "待人工裁决总数" in review:
        review["待人工裁决总数"] = len(r_eps)
    if "被拒总数" in reject:
        reject["被拒总数"] = len(j_eps)
    return summary


def run_rejudge(delivery: str, input_dir: str, cfg: dict,
                rerun_fn: Callable | None = None) -> dict:
    """读裁决 → 重判(采纳条目)→ 更新交付。rerun_fn 注入(测试用假函数);
    生产缺省 = _build_rerun(cfg)(多视角 v7.3 全协议,与漏斗同源构件)。"""
    from ..dataset_level.decisions import load_label_decisions

    decisions = load_label_decisions(delivery)
    if not decisions:
        return {"note": "无裁决记录(details/label_decisions.csv 不存在或为空),未做任何事"}

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
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(adopt))) as ex:
            for eid, res in ex.map(_one, list(adopt.items())):
                if res is not None:
                    rejudged[eid] = res

    summary = apply_decisions(files["passed"], files["review"], files["reject"],
                              decisions, rejudged)
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
                                for e, r in rejudged.items()}},
                  f, ensure_ascii=False, indent=1)

    # 报告小节用"读全文+整写"而非追加:交付目录在 TOS 的 FSX 挂载上,
    # open(..., "a") 直接 EINVAL(2026-08-06 生产实锤,与 decisions.py 同坑)。
    md = os.path.join(delivery, "report.md")
    if os.path.exists(md):
        with open(md, encoding="utf-8") as f:
            body = f.read()
        sec = ["\n## 标注裁决与重判(curation rejudge)\n",
               f"- 执行时间:{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n",
               f"- 采纳改标并重判:通过 {len(summary['adopted_pass'])} / "
               f"弃权 {len(summary['adopted_review'])} / "
               f"仍未完成 {len(summary['adopted_reject'])} 条\n",
               f"- 人工弃用:{len(summary['dropped'])} 条;"
               f"维持原标注(审计误旗):{len(summary['kept'])} 条\n"]
        for e in summary["adopted_pass"]:
            sec.append(f"  - {e}:标注修正后重判通过,已回归交付(溯源见 passed.json)\n")
        if summary.get("unchanged"):
            sec.append(f"- 裁决未变跳过(不重复重判):{len(summary['unchanged'])} 条\n")
        if summary["skipped"]:
            sec.append(f"- ⚠️ 未处理(重判失败/裁决词不识别):{summary['skipped']}\n")
        with open(md, "w", encoding="utf-8") as f:
            f.write(body + "".join(sec))
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
