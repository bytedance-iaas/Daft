"""按标注优先方针重算一份交付的技能画像:`curation reprofile`(2026-08-16)。

背景:droid-200-new 分歧队列 29 条人工复核,26 条是我方 caption 错、客户原始标注对
(90%)。老交付是按旧策略("全员 caption 归类")跑的,错归的条目正躺在错误格子里;
本命令**只拿交付里已有的东西**按新方针重算归属:

- **不重跑任何 VLM caption、不重新归纳体系**(用户 2026-08-16 明确):体系是客户已经
  看过的坐标系,重算只做"归类文本 = instruction.strip() or caption"对既有体系的
  纯文本再分配,成本近似为零;
- 归不进体系的文本才(配了 VLM 时)问一次 LLM 补漏;没配就诚实留「未归类」并报数;
- **不碰任何成败判定结论**:只动 passed.json 的 skills 段与 details/skill_assignment.csv;
- 幂等:同一份交付连跑两次,第二次 0 条变化、零写盘。

标注来源三级取法(2026-08-16 定):episodes_parquet 的 instruction(rejudge 改标后
它是"当前生效标注",溯源列 instruction_source 区分补标 caption 与真标注)→
details/task_details.json → 都取不到的条目**维持原归类**并如实报数,不许猜。
"""
from __future__ import annotations

import json
import os
from typing import Callable

from ..dataset_level.reassign import (NO_SUBSKILL, SRC_CAPTION, SRC_NONE,
                                      UNASSIGNED, grouping_text_and_source,
                                      member_map_of, reassign_texts,
                                      rebuild_profile, taxonomy_from_profile)

#: instruction_source 里算"真标注"的取值(人工裁决改标 = 人确认过的标注,同级)。
_LABEL_SOURCES = ("原始标注", "人工裁决改标")


def build_llm_ask_from_cfg(cfg: dict | None) -> Callable | None:
    """配置里有 VLM 端点就给一个 llm_ask(补漏用),没有/残缺就 None(留未归类)。

    不探活:补漏是可选增强,端点不通时 repair_unassigned 自己会失败降级,
    没必要为它把命令挡在门外。
    """
    try:
        vcfg = (cfg or {})["checks"]["task_success"]["vlm"]
        if not vcfg.get("endpoint") or not vcfg.get("model"):
            return None
        from ..adapters.vlm_client import make_llm_ask, timeout_for
        sp = (cfg or {}).get("skill_profile") or {}
        return make_llm_ask(vcfg["endpoint"], vcfg["model"],
                            timeout_s=timeout_for("llm", vcfg),
                            api_key_env=vcfg.get("api_key_env"),
                            max_in_flight=int(sp.get("llm_concurrency", 16)))
    except Exception:  # noqa: BLE001  配置残缺=没配,诚实降级而不是炸命令
        return None


def _load_instructions(run_dir: str) -> dict | None:
    """→ {episode_id: 标注文本}("" = 确知无标注);None = 两个来源都取不到。

    parquet 的 instruction 可能是补标进去的自产 caption(2026-08-06 出数据闭环),
    必须按 instruction_source 甄别 —— 把我们自己写的 caption 当"客户标注"用,
    正是这次方针要纠正的那类错误。
    """
    pq = os.path.join(run_dir, "episodes_parquet")
    if os.path.isdir(pq):
        try:
            import daft
            d = daft.read_parquet(pq).to_pydict()
            if "episode_id" in d and "instruction" in d:
                srcs = d.get("instruction_source") or [None] * len(d["episode_id"])
                out = {}
                for eid, ins, src in zip(d["episode_id"], d["instruction"], srcs):
                    if src is None or src in _LABEL_SOURCES:
                        out[str(eid)] = str(ins or "").strip()
                    else:                     # 自产caption补标 / 无 → 确知无真标注
                        out[str(eid)] = ""
                return out
        except Exception as e:  # noqa: BLE001  读不动就退下一级,别把命令炸死
            print(f"[reprofile] ⚠️ episodes_parquet 读取失败({type(e).__name__}),"
                  f"改用 task_details.json", flush=True)
    td = os.path.join(run_dir, "details", "task_details.json")
    if os.path.exists(td):
        try:
            with open(td, encoding="utf-8") as f:
                eps = (json.load(f).get("episodes") or {})
            out = {}
            for eid, rec in eps.items():
                src = str(rec.get("instruction_source") or "")
                if not src or src in _LABEL_SOURCES:
                    out[str(eid)] = str(rec.get("instruction") or "").strip()
                else:
                    out[str(eid)] = ""
            return out
        except Exception:  # noqa: BLE001
            pass
    return None


def run_reprofile(run_dir: str, cfg: dict | None = None,
                  llm_ask: Callable | None = None, preview: int = 5) -> dict:
    """一次跑批目录 → 按标注优先重算画像与 skill_assignment.csv → 摘要 dict。

    llm_ask 注入式(测试用假函数);生产缺省 = build_llm_ask_from_cfg(cfg)。
    """
    import csv as _csv

    passed_path = os.path.join(run_dir, "passed.json")
    det = os.path.join(run_dir, "details")
    csv_path = os.path.join(det, "skill_assignment.csv")
    if not os.path.exists(passed_path):
        return {"note": f"{run_dir} 不是一次跑批目录(缺 passed.json),未做任何事"}
    with open(passed_path, encoding="utf-8") as f:
        passed = json.load(f)
    profile = passed.get("skills") or {}
    if not profile.get("families"):
        return {"note": "该交付没有两级技能体系(画像未跑,或为 VLM 不可用时的降级"
                        "扁平画像)——reprofile 不重新归纳体系,未做任何事"}
    if not os.path.exists(csv_path):
        return {"note": "缺 details/skill_assignment.csv,无从重建体系的成员映射,"
                        "未做任何事"}
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    if not rows:
        return {"note": "skill_assignment.csv 为空,未做任何事"}
    captions: dict = {}
    cap_json = os.path.join(det, "captions.json")
    if os.path.exists(cap_json):
        try:
            with open(cap_json, encoding="utf-8") as f:
                captions = json.load(f)
        except Exception:  # noqa: BLE001  读不动就用 CSV 里的 caption 列,不炸
            captions = {}

    instr_of = _load_instructions(run_dir)
    if instr_of is None:
        # 两个标注来源都取不到:一条也判不了"有没有标注",全员维持原归类、
        # 零写盘(连 CSV 列回填都不做——什么都没弄清就动交付文件,是另一种猜)。
        note = ("取不到标注(无 episodes_parquet,details/task_details.json 也缺),"
                "全员维持原归类,未写任何文件")
        print(f"[reprofile] {note}", flush=True)
        return {"total": len(rows), "n_changed": 0, "n_caption_to_label": 0,
                "n_unassigned": 0, "n_label_missing_kept": len(rows),
                "changed": [], "note": note}
    if llm_ask is None:
        llm_ask = build_llm_ask_from_cfg(cfg)

    old = {str(r.get("episode_id") or ""): r for r in rows}
    old.pop("", None)
    member_map = member_map_of(rows)
    tax = taxonomy_from_profile(profile)

    new_text: dict = {}
    new_src: dict = {}
    to_assign: dict = {}
    held: list = []                 # 取不到标注 → 维持原归类(不许猜)
    assignment: dict = {}
    for eid, r in old.items():
        cap = str(captions.get(eid) or r.get("caption") or "")
        if instr_of is None or eid not in instr_of:
            held.append(eid)
            t = str(r.get("grouping_text") or r.get("caption") or "")
            new_text[eid] = t
            # 老 CSV 没有来源列:旧策略的归类输入就是 caption,如实回填而非臆造
            new_src[eid] = (str(r.get("grouping_text_source") or "")
                            or (SRC_CAPTION if t else SRC_NONE))
            assignment[eid] = (str(r.get("family") or UNASSIGNED),
                               str(r.get("subskill") or NO_SUBSKILL))
            continue
        t, s = grouping_text_and_source(instr_of.get(eid), cap)
        new_text[eid], new_src[eid] = t, s
        to_assign[eid] = t
    assignment.update(reassign_texts(to_assign, member_map, tax, llm_ask))

    def _old_pair(r):
        return (str(r.get("family") or ""), str(r.get("subskill") or ""))

    def _old_src(r):
        s = str(r.get("grouping_text_source") or "")
        if s:
            return s
        return SRC_CAPTION if (r.get("grouping_text") or r.get("caption")) else SRC_NONE

    changed = []
    for eid in sorted(old):
        o = _old_pair(old[eid])
        n = assignment[eid]
        if o != tuple(n):
            changed.append({
                "episode_id": eid,
                "old_family": o[0], "old_subskill": o[1],
                "new_family": n[0], "new_subskill": n[1],
                "old_text": str(old[eid].get("grouping_text")
                                or old[eid].get("caption") or ""),
                "new_text": new_text[eid],
                "caption_to_label": (_old_src(old[eid]) != "原始标注"
                                     and new_src[eid] == "原始标注"),
            })
    cols_changed = any(
        str(old[eid].get("grouping_text") or "") != new_text[eid]
        or str(old[eid].get("grouping_text_source") or "") != new_src[eid]
        for eid in old)
    n_unassigned = sum(1 for a in assignment.values() if a[0] == UNASSIGNED)

    summary = {
        "total": len(old),
        "n_changed": len(changed),
        "n_caption_to_label": sum(1 for c in changed if c["caption_to_label"]),
        "n_unassigned": n_unassigned,
        "n_label_missing_kept": len(held),
        "changed": changed,
    }

    if changed or cols_changed:
        from ..dataset_level.profile import write_skill_assignment_csv
        from ..export.safe_write import delivery_file, write_json
        cap_of = {eid: str(captions.get(eid) or old[eid].get("caption") or "")
                  for eid in old}
        instr_render = {eid: (instr_of or {}).get(eid, "") for eid in old}
        new_profile = rebuild_profile(assignment, profile, cap_of, instr_render)
        if llm_ask is not None:
            # 老画像的 name_zh 已按名移植;这里只补新出现的名字,失败回落英文
            from ..dataset_level.taxonomy import apply_name_zh, translate_names
            if any(not f.get("name_zh") or any(not s.get("name_zh")
                                               for s in (f.get("subskills") or {}).values())
                   for fn, f in (new_profile.get("families") or {}).items()
                   if fn != "未归类"):
                apply_name_zh(new_profile, translate_names(new_profile, llm_ask))
        # ⚠️ 只动 skills 段:成败判定结论(episodes/判决/checks)一个字不碰。
        passed["skills"] = new_profile
        write_json(passed_path, passed, default=str)
        os.makedirs(det, exist_ok=True)
        write_skill_assignment_csv(det, new_profile, cap_of, new_text, new_src)
        # 留档(与 rejudge_results.json 同一习惯):交付被改过一定要能查到是谁改的
        import datetime
        with delivery_file(os.path.join(det, "reprofile_results.json")) as f:
            json.dump({"at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "summary": {k: v for k, v in summary.items()
                                   if k != "changed"},
                       "changed": changed}, f, ensure_ascii=False, indent=1)

    # 摘要(打前几条即可,别刷屏)
    print(f"[reprofile] 共 {summary['total']} 条:改变归类 {summary['n_changed']} 条"
          f"(其中从 caption 归改成按标注归 {summary['n_caption_to_label']} 条),"
          f"未归类 {summary['n_unassigned']} 条,取不到标注维持原归类 "
          f"{summary['n_label_missing_kept']} 条", flush=True)
    for c in changed[:max(0, preview)]:
        print(f"[reprofile]   {c['episode_id']}: "
              f"{c['old_family']}/{c['old_subskill']} → "
              f"{c['new_family']}/{c['new_subskill']}"
              f"(归类文本「{c['old_text'][:40]}」→「{c['new_text'][:40]}」)",
              flush=True)
    if len(changed) > preview > 0:
        print(f"[reprofile]   …其余 {len(changed) - preview} 条见 "
              f"details/reprofile_results.json", flush=True)
    if not changed and not cols_changed:
        print("[reprofile] 0 条变化(画像已是标注优先口径),未写任何文件", flush=True)
    return summary
