"""The report of one result revision (design doc 02 §3.8, 06 §6).

``report.json`` (``cli/report.schema.json``) drives the report page: an
overview, then one section per selected module in registry order - a module
that failed keeps its section, with the error - then the skipped modules and
the data-package integrity. ``perf.json`` is the performance profile with v1's
latency buckets unchanged (``vlm_client.latency_summary`` over
``details/vlm_latency.csv``: per-kind P50/P90/P99, wall clock as the union of
busy intervals, hedging), the token ledgers (actual and attributed, never added
together) and the episodes redone after an interruption (D26). Detail tables go
to ``tables/<id>.parquet``, sorted by episode index, one per C1 ``TableSpec``.
``report.md`` is the same content for people. ``commit.json`` is written last:
the parts each module's results came from, the decisions applied, the source
digest and every other file of the revision with its sha256.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from ..contracts import modules as registry
from .aggregate import NAMES_CN
from .records import (CRASHES_NAME, LATENCY_FILE, PLAN_NAME, SOURCE_MANIFEST_NAME, USAGE_FILE,
                      latest_results, module_dir, parts_used, read_jsonl, write_json_atomic,
                      write_text_atomic)

SCHEMA_VERSION = "1.0"
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens")


def _read(path: str, default=None):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- inputs

class Revision:
    """Everything a report reads: the four lists, the module results, the run files."""

    def __init__(self, run_dir: str, revision: int, modules: list[str]):
        from .records import revision_dir

        self.run_dir, self.revision = run_dir, int(revision)
        self.dir = revision_dir(run_dir, revision)
        self.modules = [m.id for m in registry.MODULES if m.id in set(modules)]
        self.lists = {}
        for name in ("passed", "reject", "held", "review"):
            doc = _read(os.path.join(self.dir, f"{name}.json"))
            if doc is None:
                raise FileNotFoundError(f"{self.dir}/{name}.json is missing: run "
                                        f"aggregate --phase final --revision {revision} first")
            self.lists[name] = doc
        self.verdicts = read_jsonl(os.path.join(self.dir, "verdicts.jsonl"))
        self.results = {m: latest_results(run_dir, m) for m in self.modules}
        self.plan = _read(os.path.join(run_dir, PLAN_NAME), {}) or {}
        self.preflight = _read(os.path.join(run_dir, "preflight.json"), {}) or {}
        self.source = _read(os.path.join(run_dir, SOURCE_MANIFEST_NAME), {}) or {}
        self.applied = (_read(os.path.join(self.dir, "adjudications.json"), {}) or {}) \
            .get("applied") or []

    def episodes(self, name: str) -> list[dict]:
        return self.lists[name]["episodes"]


# ---------------------------------------------------------------- tokens and latency

def usage_totals(run_dir: str) -> tuple[dict, list[dict]]:
    """(``token_usage`` of the actual ledger, rows per ledger x module x kind x model)."""
    rows: dict[tuple, dict] = {}
    for line in read_jsonl(os.path.join(run_dir, USAGE_FILE)):
        key = (line.get("ledger", "actual"), line.get("module"), line.get("call_kind"),
               line.get("model"))
        acc = rows.setdefault(key, {"requests": 0, "requests_unknown_usage": 0,
                                    **dict.fromkeys(TOKEN_FIELDS, 0)})
        for k in acc:
            acc[k] += int(line.get(k) or 0)
    actual = [v for (lg, *_), v in rows.items() if lg == "actual"]
    tot = {k: sum(r[k] for r in actual) for k in ("requests", "requests_unknown_usage",
                                                  *TOKEN_FIELDS)}
    token_usage = {"prompt": tot["prompt_tokens"], "completion": tot["completion_tokens"],
                   "reasoning": tot["reasoning_tokens"], "cached": tot["cached_tokens"],
                   "requests": tot["requests"],
                   "requests_unknown_usage": tot["requests_unknown_usage"]}
    table = [{"ledger": lg, "module": m, "call_kind": ck, "model": mo, **v}
             for (lg, m, ck, mo), v in sorted(rows.items(), key=lambda kv: str(kv[0]))]
    return token_usage, table


def latency(run_dir: str) -> dict:
    from ..adapters.vlm_client import latency_summary, read_latency_csv

    path = os.path.join(run_dir, LATENCY_FILE)
    if not os.path.isfile(path):
        return {"by_kind": {}, "requests": 0, "wall_s": None, "effective_concurrency": None}
    rows = read_latency_csv(path)
    by_kind = latency_summary(rows)
    stamped = sorted((r[3], r[3] + r[1]) for r in rows if r[3] is not None)
    wall = None
    if stamped:
        wall, cs, ce = 0.0, stamped[0][0], stamped[0][1]
        for s, e in stamped[1:]:
            if s > ce:
                wall += ce - cs
                cs, ce = s, e
            else:
                ce = max(ce, e)
        wall = round(wall + ce - cs, 2)
    busy = sum(r[1] for r in rows)
    return {"by_kind": by_kind, "requests": len(rows), "wall_s": wall,
            "effective_concurrency": round(busy / wall, 2) if wall else None}


def redone_after_interruption(run_dir: str, modules) -> int:
    total = 0
    for m in modules:
        doc = _read(os.path.join(module_dir(run_dir, m), CRASHES_NAME), {}) or {}
        total += sum(int(v) for v in doc.values())
    return total


# ---------------------------------------------------------------- modules

def _counts(results: dict) -> dict:
    c = {"total": len(results), "pass": 0, "fail": 0, "abstain": 0, "scored": 0, "error": 0}
    for rec in results.values():
        c[rec["verdict"]] += 1
    return c


def _missing(rev: Revision, module: str) -> list[int]:
    """Episodes that reached the module but have no result (the module failed as a whole)."""
    out = []
    for line in rev.verdicts:
        if module in (line.get("error_modules") or []) \
                and line["episode_index"] not in rev.results[module]:
            out.append(line["episode_index"])
    return out


def _summary(rev: Revision, m: str) -> dict:
    res = rev.results[m]
    out: dict = {"counts": _counts(res)}
    scores = [r["score"] for r in res.values() if r.get("score") is not None]
    if scores:
        out["mean_score"] = round(sum(scores) / len(scores), 4)
    if m == "task_success":
        from .funnel import arbitration_stats

        out["arbitration"] = arbitration_stats(
            [{"detail": json.dumps(r.get("details") or {})} for r in res.values()
             if r["verdict"] != "error"])
        reasons: dict[str, int] = {}
        for r in res.values():
            if r["verdict"] == "abstain":
                why = str((r.get("details") or {}).get("reason") or "未注明")[:80]
                reasons[why] = reasons.get(why, 0) + 1
        out["abstain_reasons"] = dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:5])
    if m == "dedup":
        groups = _read(os.path.join(module_dir(rev.run_dir, "dedup"), "groups.json"), {}) or {}
        out["collision_groups"] = len(groups.get("action_collisions") or [])
        out["removed"] = len(groups.get("dropped") or [])
    if m == "skill_profile":
        prof = _read(os.path.join(module_dir(rev.run_dir, "skill_profile"), "profile.json"),
                     {}) or {}
        fams = prof.get("families") or {}
        out["families"] = len([f for f in fams if f != "未归类"])
        out["subskills"] = sum(len((f.get("subskills") or {})) for f in fams.values())
        out["undersampled"] = list(prof.get("undersampled") or [])[:20]
    if not registry.get(m).affects_dataset_verdict:        # advisory (registry 1.4, design doc 12)
        from ..extensions.eef_consistency import report as eef_report

        out.update(eef_report.summary(res))
    if m == "timestamp_check":
        why: dict[str, int] = {}
        for r in res.values():
            if r["verdict"] == "fail":
                d = r.get("details") or {}
                key = "fragment" if d.get("duration_s", 99) < 1.0 else (
                    "gap" if d.get("gap_frames") else "other")
                why[key] = why.get(key, 0) + 1
        out["fail_kinds"] = why
    return out


def _tables(rev: Revision, m: str, out_dir: str) -> list[dict]:
    import pandas as pd

    spec = registry.get(m)
    res = rev.results[m]
    tables = []
    for t in spec.tables:
        rows = _table_rows(rev, m, t.id, res)
        cols = list(dict.fromkeys(["episode_index", *t.sortable,
                                   *(k for r in rows for k in r)]))
        df = pd.DataFrame(rows, columns=cols)
        if len(df):
            df = df.sort_values("episode_index", kind="stable")
        path = os.path.join(out_dir, "tables", f"{t.id}.parquet")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp-{os.getpid()}"
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
        tables.append({"id": t.id, "rows": int(len(df)), "file": f"tables/{t.id}.parquet"})
    return tables


def _table_rows(rev: Revision, m: str, table: str, res: dict) -> list[dict]:
    if table.startswith("eef_"):
        from ..extensions.eef_consistency import report as eef_report

        return eef_report.table_rows(table, res)
    out = []
    for ep, r in sorted(res.items()):
        d = r.get("details") or {}
        base = {"episode_index": int(ep), "verdict": r["verdict"]}
        if table == "timestamp_check":
            out.append({**base, "duration_s": d.get("duration_s"), "max_dt": d.get("max_dt"),
                        "reason": str(d.get("reason") or "")})
        elif table == "kinematic_violations":
            for v in d.get("violations") or []:
                out.append({**base, "type": str(v.get("type") or ""),
                            "joint": str(v.get("joint") if v.get("joint") is not None else ""),
                            "frame": v.get("frame"), "value": _num(v.get("value")),
                            "limit": str(v.get("limit"))})
        elif table == "motion_quality":
            out.append({**base, "score": r.get("score"), "fluency": _num(d.get("fluency")),
                        "active_ratio": _num(d.get("active_ratio")),
                        "stuck": bool(d.get("stuck_joints"))})
        elif table == "visual_quality":
            for cam, cd in (d.get("per_camera_detail") or {}).items():
                out.append({**base, "camera": str(cam).split(".")[-1],
                            "score": _num(cd.get("score")),
                            "sharpness": _num(cd.get("sharpness")),
                            "exposure": _num(cd.get("exposure")),
                            "integrity": _num(cd.get("integrity"))})
            if not d.get("per_camera_detail"):
                out.append({**base, "camera": "", "score": r.get("score")})
        elif table == "video_action_sync":
            for cam, cd in (d.get("per_camera") or {}).items():
                cd = cd if isinstance(cd, dict) else {}
                out.append({**base, "camera": str(cam), "lag_s": _num(cd.get("lag_s")),
                            "corr_peak": _num(cd.get("corr_peak")),
                            "code": str(cd.get("code") or "")})
            if not d.get("per_camera"):
                out.append({**base, "camera": "", "lag_s": None, "corr_peak": None,
                            "code": str(d.get("verdict") or "")})
        elif table == "task_success":
            out.append({**base, "judgement": str(d.get("verdict") or ""),
                        "rules": ",".join(d.get("rules") or []),
                        "task_desc_source": str(d.get("task_desc_source") or ""),
                        "reason": str(d.get("reason") or "")})
        elif table == "dedup_groups":
            if r["verdict"] == "fail":
                out.append({**base, "duplicate_of": d.get("duplicate_of")})
        elif table == "skill_assignment":
            out.append({**base, "family": str(d.get("family") or ""),
                        "subskill": str(d.get("subskill") or ""),
                        "caption": str(d.get("caption") or ""),
                        "grouping_text": str(d.get("grouping_text") or ""),
                        "grouping_text_source": str(d.get("grouping_text_source") or "")})
    return out


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def module_sections(rev: Revision) -> list[dict]:
    review = rev.episodes("review")
    sections = []
    for m in rev.modules:
        spec = registry.get(m)
        res = rev.results[m]
        missing = _missing(rev, m)
        errors = [e for e, r in res.items() if r["verdict"] == "error"] + missing
        sec: dict = {"id": m, "gate": spec.gate, "summary": _summary(rev, m),
                     "tables": _tables(rev, m, rev.dir), "adjudication": None}
        if not res and missing:
            sec["state"] = "failed"
            sec["error"] = (f"no results: the module did not run or failed as a whole "
                            f"({len(missing)} episodes wait for it)")
        else:
            sec["state"] = "completed_with_errors" if errors else "succeeded"
        if errors:
            sec["episodes_error"] = len(errors)
        if spec.produces_adjudication:
            sec["adjudication"] = _adjudication(review, m, spec)
        sections.append(sec)
    return sections


def skipped_modules(rev: Revision) -> list[dict]:
    out = []
    for note in ((rev.plan.get("estimates") or {}).get("notes") or []):
        head, sep, reason = str(note).partition(" skipped: ")
        if sep and head in registry.ids():
            out.append({"id": head, "reason": reason})
    return out


def _adjudication(review: list, module: str, spec) -> dict:
    """A module's open review items, per episode (C1 review lines): ``pending`` - the
    questions it raised that must be decided (``counts_as_pending``); for an
    appealable module ``appealable`` - its rejects with an open appeal item (never
    pending: an appeal is a choice)."""
    def has(entry, pending: bool) -> bool:
        for item in entry["review"]:
            line = registry.review_line(item["line"]) if item.get("line") \
                else registry.review_line_of_kind(item["kind"])
            if item["source_module"] == module and line.counts_as_pending == pending:
                return True
        return False

    out = {"pending": sum(1 for e in review if has(e, True))}
    if spec.appealable:
        out["appealable"] = sum(1 for e in review if has(e, False))
    return out


def integrity(rev: Revision) -> dict:
    pf = rev.preflight
    if not pf:
        return {}
    ds = pf.get("dataset") or {}
    return {"format": pf.get("format"), "validation": pf.get("validation") or [],
            "warnings": pf.get("warnings") or [], "labels": ds.get("labels"),
            "profile": ds.get("profile"), "robot_type": ds.get("robot_type")}


# ---------------------------------------------------------------- the report

def build(rev: Revision) -> tuple[dict, dict]:
    """(report.json, perf.json)."""
    from .skipped import all_skipped, as_list

    skipped = as_list(all_skipped(rev.run_dir))
    counts = {"total": sum(rev.lists[n]["count"] for n in ("passed", "reject", "held")),
              "passed": rev.lists["passed"]["count"], "rejected": rev.lists["reject"]["count"],
              "held": rev.lists["held"]["count"], "review": rev.lists["review"]["count"],
              "skipped": len(skipped)}
    reasons: dict[str, int] = {}
    for e in rev.episodes("reject"):
        for mod in dict.fromkeys(r["module"] for r in e.get("reasons") or []
                                 if r.get("kind") != "execution_error"):
            reasons[mod] = reasons.get(mod, 0) + 1
    token_usage, usage_rows = usage_totals(rev.run_dir)
    lat = latency(rev.run_dir)
    ds = (rev.preflight.get("dataset") or {})
    overview = {
        "dataset": {"input": rev.source.get("input"),
                    "source_digest": (rev.source.get("summary") or {}).get("digest"),
                    "episode_count": ds.get("episode_count"), "cameras": ds.get("cameras"),
                    "fps": ds.get("fps"), "robot_type": ds.get("robot_type")},
        "run": {"run_dir": os.path.basename(rev.run_dir.rstrip("/")),
                "revision": rev.revision, "modules": rev.modules,
                "adjudications_applied": len(rev.applied)},
        "counts": counts,
        "pass_rate": round(counts["passed"] / counts["total"], 4) if counts["total"] else None,
        "reject_reasons": [{"module": m, "count": n}
                           for m, n in sorted(reasons.items(), key=lambda kv: -kv[1])],
        "token_usage": token_usage,
        "duration_s": None,
    }
    perf = {"schema_version": SCHEMA_VERSION, "revision": rev.revision,
            "latency": lat["by_kind"], "vlm_requests": lat["requests"],
            "vlm_wall_s": lat["wall_s"], "effective_concurrency": lat["effective_concurrency"],
            "token_usage": token_usage, "usage_rows": usage_rows,
            "usage_note": "合并请求按比例分摊(分摊账);任务总量只算实际调用账,两本账不相加",
            "redone_after_interruption": redone_after_interruption(rev.run_dir, rev.modules)}
    report = {"schema_version": SCHEMA_VERSION, "revision": rev.revision,
              "overview": overview, "modules": module_sections(rev),
              "skipped_modules": skipped_modules(rev),
              # what was not checked and why (D40): the manifest's list and read-time finds
              "integrity": {**integrity(rev), "skipped_episodes": skipped},
              "perf": {"vlm_requests": lat["requests"], "vlm_wall_s": lat["wall_s"],
                       "effective_concurrency": lat["effective_concurrency"],
                       "redone_after_interruption": perf["redone_after_interruption"]}}
    return report, perf


def markdown(rev: Revision, report: dict, perf: dict) -> str:
    """``report.md``: the same content, in Chinese, for people."""
    from ..vlm_call_kinds import CALL_KIND_LABELS, CALL_KIND_ORDER

    ov = report["overview"]
    c = ov["counts"]
    rate = "-" if ov["pass_rate"] is None else "{:.1f}%".format(ov["pass_rate"] * 100)
    lines = [f"# 数据集质检报告(结果版本 r{rev.revision:04d})",
             f"- 数据集: {ov['dataset'].get('input') or '(未记录)'}",
             f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
             "", "## 总览",
             f"- 参与质检:{c['total']} 条 = 通过 {c['passed']} + 拒绝 {c['rejected']} + "
             f"待补跑 {c['held']}(三者不重不漏)",
             f"- 通过率:{rate}(待补跑既不算通过也不算拒绝)",
             f"- 待人工确认:{c['review']} 条(多数在通过里,保守放行、等人确认)"]
    if c.get("skipped"):
        lines.append(f"- 缺源文件未质检:{c['skipped']} 条(照 v1 剔除,不计入参与质检的总数,"
                     f"明细见文末「未质检的条目」)")
    if ov["reject_reasons"]:
        lines.append("- 拒绝原因:" + ";".join(
            f"「{NAMES_CN.get(r['module'], r['module'])}」{r['count']} 条"
            for r in ov["reject_reasons"]))
    held = rev.episodes("held")
    if held:
        lines.append("- 待补跑(执行出错,暂不交付,等「重试」):")
        for e in held[:30]:
            lines.append(f"  - ep{e['episode_index']:06d}:"
                         + ";".join(r["text"] for r in e.get("reasons") or []))
    lines.append("")
    lines.append("## 各模块")
    for sec in report["modules"]:
        spec = registry.get(sec["id"]) if sec["id"] in registry.ids() else None
        cn = NAMES_CN.get(sec["id"]) or (spec.name_zh if spec else sec["id"])
        state = {"succeeded": "完成", "completed_with_errors": "完成(部分出错)",
                 "failed": "失败"}[sec["state"]]
        lines.append(f"### {cn}({state})")
        cnt = sec["summary"]["counts"]
        if spec is not None and not spec.affects_dataset_verdict:
            # advisory (registry 1.4): no pass / fail / abstain, never part of the verdict
            s = sec["summary"]
            lines.append(f"- 建议性结果，不影响判决{'（阈值未校准）' if s.get('uncalibrated') else ''}:"
                         f"候选 {s.get('candidates', 0)} · 全部可评估 {s.get('assessed', 0)} · "
                         f"部分可评估 {s.get('partially_assessable', 0)} · 无法评估 {s.get('not_assessable', 0)} · "
                         f"出错 {cnt['error']}")
            sus = "、".join(f"{x['name']} {x['count']}" for x in s.get("suspect_by_subitem") or []) or "无"
            lines.append(f"- 可疑分项(条数):{sus}")
            hyp = "、".join(f"{x['name']} {x['count']}" for x in s.get("supported_hypotheses") or []) or "无"
            lines.append(f"- 被支持的诊断假设:{hyp}")
            if s.get("coverage_median") is not None:
                lines.append(f"- 可比覆盖率中位数 {s['coverage_median']}(最低 {s.get('coverage_min')})")
            lines.append("")
            continue
        lines.append(f"- 通过 {cnt['pass']} · 判废 {cnt['fail']} · 弃权 {cnt['abstain']}"
                     f" · 打分 {cnt['scored']} · 出错 {cnt['error']}")
        if "mean_score" in sec["summary"]:
            lines.append(f"- 平均分:{sec['summary']['mean_score']}")
        if sec["id"] == "dedup":
            lines.append(f"- 重复组 {sec['summary']['collision_groups']} 组,"
                         f"剔除 {sec['summary']['removed']} 条")
        if sec["id"] == "skill_profile":
            lines.append(f"- 技能族 {sec['summary']['families']} 个,"
                         f"子技能 {sec['summary']['subskills']} 个")
        if sec["id"] == "task_success":
            arb = sec["summary"].get("arbitration") or {}
            if arb.get("triggered"):
                lines.append(f"- 取证仲裁触发 {arb['triggered']} 条:救回 "
                             f"{arb.get('adopted_success', 0)},判废 "
                             f"{arb.get('adopted_failure', 0)},仍弃权 {arb.get('abstained', 0)}")
            for why, n in (sec["summary"].get("abstain_reasons") or {}).items():
                lines.append(f"  - 弃权原因:{why}({n} 条)")
        if sec.get("adjudication"):
            lines.append(f"- 待人工裁决:{sec['adjudication']['pending']} 条")
        if sec.get("error"):
            lines.append(f"- ⚠️ {sec['error']}")
        lines.append("")
    if report["integrity"].get("skipped_episodes"):
        lines.append("## 未质检的条目(源文件缺失,照 v1 剔除)")
        for s in report["integrity"]["skipped_episodes"][:50]:
            lines.append(f"- ep{s['episode_index']:06d}:缺 " + "、".join(s["missing"]))
        lines.append("- 补齐文件后另建任务即可")
        lines.append("")
    if report["skipped_modules"]:
        lines.append("## 未执行的模块")
        for s in report["skipped_modules"]:
            lines.append(f"- {NAMES_CN.get(s['id'], s['id'])}:{s['reason']}")
        lines.append("")
    if rev.applied:
        lines += ["## 人工裁决", f"- 已应用 {len(rev.applied)} 项裁决(以人的结论为准,来源如实记为人工)",
                  ""]
    tu = ov["token_usage"]
    lines += ["## Token 用量(实际调用账)",
              f"- 请求 {tu['requests']} 次(另有 {tu['requests_unknown_usage']} 次拿不到用量,不估算)",
              f"- 输入 {tu['prompt']} · 输出 {tu['completion']} · 思维链 {tu['reasoning']}"
              f" · 缓存命中 {tu['cached']}", ""]
    lat = perf["latency"]
    if lat:
        lines += ["## 模型调用延时(客户端视角,秒)",
                  "| 调用类型 | 发起次数 | 没等到回应 | P50 | P90 | P99 | 墙钟 |",
                  "|---|---|---|---|---|---|---|"]
        for tag in [t for t in CALL_KIND_ORDER if t in lat] + [t for t in lat
                                                              if t not in CALL_KIND_ORDER]:
            s = lat[tag]
            lines.append(f"| {CALL_KIND_LABELS.get(tag, tag)} | {s.get('attempts', s['n'])} | "
                         f"{s.get('unanswered', 0)} | {s.get('p50_s', '-')} | "
                         f"{s.get('p90_s', '-')} | {s.get('p99_s', '-')} | "
                         f"{s.get('wall_s', '-')} |")
        lines.append("")
    if perf.get("redone_after_interruption"):
        lines.append(f"- 因中断而重做的 episode:{perf['redone_after_interruption']} 次")
    return "\n".join(lines) + "\n"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def write(rev: Revision, *, formats=("md", "json"), subtask_id: str | None = None,
          now_ms: int | None = None) -> dict:
    """Write the report files, then ``commit.json`` last; returns their paths."""
    report, perf = build(rev)
    files: dict = {}
    rj = os.path.join(rev.dir, "report.json")
    write_json_atomic(rj, report)
    files["report_json"] = rj
    pj = os.path.join(rev.dir, "perf.json")
    write_json_atomic(pj, perf)
    files["perf_json"] = pj
    md = os.path.join(rev.dir, "report.md")
    write_text_atomic(md, markdown(rev, report, perf) if "md" in formats else
                      f"# r{rev.revision:04d}\n")
    files["report_md"] = md
    files["tables"] = sorted(os.path.join(rev.dir, t["file"])
                             for sec in report["modules"] for t in sec["tables"])
    digest = (rev.source.get("summary") or {}).get("digest") \
        or "sha256:" + hashlib.sha256(b"").hexdigest()
    listed = {}
    for dirpath, _, names in os.walk(rev.dir):
        for n in sorted(names):
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, rev.dir).replace(os.sep, "/")
            if rel == "commit.json" or ".tmp-" in n:
                continue
            listed[rel] = sha256_file(full)
    commit = {"schema_version": SCHEMA_VERSION, "revision": rev.revision,
              "created_at": int(time.time() * 1000) if now_ms is None else int(now_ms),
              "source_digest": digest,
              "parts": {m: parts_used(rev.run_dir, m) for m in rev.modules},
              "adjudications_applied": [int(i) for i in rev.applied],
              "subtask_id": subtask_id, "files": dict(sorted(listed.items()))}
    cj = os.path.join(rev.dir, "commit.json")
    write_json_atomic(cj, commit)
    files["commit"] = cj
    return files
