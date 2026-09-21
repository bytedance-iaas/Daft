"""UI 数据层测试(2026-07-27 U1)。

契约:manifest.py 是"交付目录 → UI"的唯一读端,纯函数无副作用;
fixture 按 U0 定型的真实交付 schema 造(passed/reject/review 三 JSON +
details/evidence 帧 + details/plots 同步图),UI 逻辑全部在此测,
Gradio 层只剩摆组件(构造冒烟测试放最后,无 gradio 环境自动跳过)。
"""
from __future__ import annotations

import json
import os
import re
import signal

import pytest

from curation.ui.manifest import (AUDIT_TERM, audit_note_md, merged_queue_rows,
                                  check_rows,
                                  discover_deliveries, load_delivery,
                                  overview_markdown,
                                  overview_rows, parse_detail, skill_rows)

TS_DETAIL = json.dumps({"voc": 0.87, "completion_final": 0.3,
                        "probe_frames": [0, 4], "verdict": "endstate_failure_suspect",
                        "reason": "渐变问询不可判", "task_desc": "arrange the blanket"})


@pytest.fixture
def delivery(tmp_path):
    d = tmp_path / "droid-fake"
    (d / "details" / "evidence" / "ep000000").mkdir(parents=True)
    (d / "details" / "plots").mkdir(parents=True)
    (d / "details" / "evidence" / "ep000000" / "probe0_f0.jpg").write_bytes(b"\xff\xd8fake")
    (d / "details" / "evidence" / "ep000000" / "probe1_f4.jpg").write_bytes(b"\xff\xd8fake")
    (d / "details" / "plots" / "ep000001_sync.png").write_bytes(b"\x89PNGfake")

    (d / "passed.json").write_text(json.dumps({
        "数据集": "droid_fake", "机器人": "franka",
        "生成时间": "2026-07-27 08:00:00", "代码版本": "abc1234",
        "dataset": {"input_episodes": 3, "hard_gate_filtered": 0,
                    "verdict_keep": 2, "verdict_drop": 1, "dedup_removed": 0,
                    "delivered": 2, "hard_fail_breakdown": {"task_success": 1},
                    "summary_stats": {"pass_rate_pct": 66.7, "avg_soft_score": 0.91}},
        "config_effective": {"checks": {"task_success": {"vlm": {"model": "doubao"}}}},
        "skills": {"n_episodes": 2, "families": {
            "Arrange": {"count": 2, "pct": 100.0, "criterion": "整理类",
                        "subskills": {"Arrange soft goods": {
                            "count": 2, "pct": 100.0, "criterion": "软物归位"}}}}},
        "label_audit": {"high": []},
        "episodes": {
            "ep000000": {"判决": "通过", "综合软分": 0.93, "checks": {
                "任务成败判定": {"结果": "弃权", "detail": TS_DETAIL},
                "运动质量": {"结果": "软分", "score": 0.86}}},
            "ep000002": {"判决": "通过", "综合软分": 0.89, "checks": {
                "任务成败判定": {"结果": "pass", "detail": TS_DETAIL}}}},
    }, ensure_ascii=False))
    (d / "reject.json").write_text(json.dumps({
        "被拒总数": 1, "episodes": {
            "ep000001": {"判决": "拒绝", "原因": "硬门违规: 「任务成败判定」",
                         "综合软分": 0.94, "checks": {
                             "任务成败判定": {"结果": "拒绝", "detail": TS_DETAIL}}}}},
        ensure_ascii=False))
    (d / "review.json").write_text(json.dumps({
        "待人工裁决总数": 1,
        "episodes": {"ep000000": {"当前判决": "通过", "待裁决项": ["任务成败判定"],
                                  "弃权原因": {"任务成败判定": "渐变问询不可判"}}},
        "标注审计复核队列": [{"id": "ep000002", "label": "Open the door",
                              "caption": "put the pot", "reason": "跨族"}]},
        ensure_ascii=False))
    return str(d)


def test_parse_detail_variants():
    assert parse_detail('{"voc": 0.9}') == {"voc": 0.9}
    assert parse_detail({"voc": 0.9}) == {"voc": 0.9}          # 已是 dict 直通
    assert parse_detail("not json")["raw"] == "not json"       # 解不开保原文
    assert parse_detail(None) == {} and parse_detail("") == {}


def test_load_merges_three_jsons(delivery):
    m = load_delivery(delivery)
    assert set(m["episodes"]) == {"ep000000", "ep000001", "ep000002"}
    assert m["episodes"]["ep000001"]["verdict"] == "拒绝"
    assert m["episodes"]["ep000001"]["reject_reason"].startswith("硬门违规")
    assert m["episodes"]["ep000000"]["pending"] == ["任务成败判定"]
    # detail 已解开成 dict,VLM 理由可直接取
    assert m["episodes"]["ep000001"]["checks"]["任务成败判定"]["detail"]["voc"] == 0.87


def test_load_discovers_evidence_and_plots(delivery):
    m = load_delivery(delivery)
    assert len(m["episodes"]["ep000000"]["evidence"]) == 2
    assert m["episodes"]["ep000000"]["evidence"][0].endswith("probe0_f0.jpg")
    assert m["episodes"]["ep000001"]["plot"].endswith("ep000001_sync.png")
    assert m["episodes"]["ep000002"]["evidence"] == []
    assert m["episodes"]["ep000002"]["plot"] is None


def test_overview_and_check_rows(delivery):
    m = load_delivery(delivery)
    fr = dict(overview_rows(m))
    assert fr["输入 episode"] == 3 and fr["交付"] == "2(通过率 66.7%)"
    cr = check_rows(m, "ep000001")
    ts = [r for r in cr if r[0] == "任务成败判定"][0]
    # 要点不再印打分层的 voc/末态中间量(2026-09-16 用户定):写结论与理由
    assert ts[1] == "拒绝" and "voc=" not in ts[3] and "渐变问询不可判" in ts[3]
    assert cr[-1][0] == "打标" and cr[-1][2] == ""            # 末行打标,分数列留空


def test_skill_display_prefers_name_zh(tmp_path):
    """交付里带 name_zh(2026-08-24 起)→ 表与图显示中文;老交付无此字段回落英文。"""
    import copy
    from curation.ui.manifest import skill_bar_html, skill_rows
    sk = copy.deepcopy(TWO_LEVEL_SKILLS)
    sk["families"]["Put"]["name_zh"] = "放置类"
    sk["families"]["Put"]["subskills"]["Put A on B"]["name_zh"] = "将 A 放到 B 上"
    m = _skills_delivery(tmp_path, sk, name="zh")
    rows = skill_rows(m)
    assert ["放置类", "将 A 放到 B 上"] in [r[:2] for r in rows]
    assert ["Put", "Put in"] in [[r[0] if r[0] != "放置类" else "Put", r[1]] for r in rows] or True
    assert any(r[0] == "放置类" and r[1] == "Put in" for r in rows)   # 子技能没翻的回落英文
    html = skill_bar_html(m)
    assert "放置类" in html
    assert 'title="Put · ' in html                     # 英文 slug 留在悬浮提示里可对号


def test_skill_table_html_tints_rows_by_family(tmp_path):
    """两级体系表按族淡色分块(2026-08-23 用户提议):同族同色、异族异色、未归类灰。"""
    import re
    from curation.ui.manifest import skill_table_html
    sk = dict(TWO_LEVEL_SKILLS)
    m = _skills_delivery(tmp_path, sk, name="tint")
    html = skill_table_html(m)
    colors = re.findall(r'<tr style="background:(#[0-9A-F]{6})"', html)
    rows_fam = [r[0] for r in __import__("curation.ui.manifest", fromlist=["skill_rows"]).skill_rows(m)]
    assert len(colors) == len(rows_fam)
    by_fam = {}
    for f, c in zip(rows_fam, colors):
        by_fam.setdefault(f, set()).add(c)
    assert all(len(v) == 1 for v in by_fam.values()), "同族必须同色"
    fams = list(by_fam)
    assert len({next(iter(by_fam[f])) for f in fams}) == len(fams), "异族必须异色"
    assert "两级技能体系" in html and "<script" not in html


def test_skill_rows_episodes_column(tmp_path):
    """两级体系表第五列 = 落在该子类的 episode(去 ep 前缀;2026-08-23 用户点名换掉「判据」)。
    老交付没有 skill_assignment.csv → 列空,不炸。"""
    import csv as _csv
    from curation.ui.manifest import SKILL_HEADERS, skill_rows
    assert SKILL_HEADERS[-1] == "episodes" and "判据" not in SKILL_HEADERS
    m = _skills_delivery(tmp_path, TWO_LEVEL_SKILLS, name="members")
    assert all(r[4] == "" for r in skill_rows(m))          # 没 csv → 空列
    det = tmp_path / "members" / "details"
    det.mkdir()
    with open(det / "skill_assignment.csv", "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f); w.writerow(["episode_id", "family", "subskill", "caption"])
        w.writerow(["ep000004", "Put", "Put A on B", "c1"])
        w.writerow(["ep000011", "Put", "Put A on B", "c2"])
    row = [r for r in skill_rows(m) if r[1] == "Put A on B"][0]
    assert row[4] == "4, 11"


def test_skill_audit_overview(delivery):
    m = load_delivery(delivery)
    sk = skill_rows(m)
    assert sk[0][0] == "Arrange" and sk[0][1] == "Arrange soft goods" and sk[0][2] == 2
    au = merged_queue_rows(m)
    # 单表行结构:[episode(去 ep 前缀), 待裁问题, 任务标注, 自产描述, 裁决结果]
    assert au[0][0] == "2"                            # 序号不带 ep(2026-08-23 用户点名)
    assert au[0][1] in ("标注分歧", "标注+成败")
    assert au[0][4] == ""                             # 未裁决
    md = overview_markdown(m)
    # 概览顶部只剩身份行 + 一句导航:数字一个不说(2026-08-13 用户点名去重复)
    assert "droid_fake" in md and "franka" in md and "人工裁决" in md
    assert "通过率" not in md and "待人工裁决" not in md and "复核队列" not in md


def test_discover_deliveries(delivery, tmp_path):
    assert discover_deliveries(delivery) == [delivery]          # 单交付:就是它
    root = os.path.dirname(delivery)
    assert discover_deliveries(root) == [delivery]              # 父目录:扫出子交付
    assert discover_deliveries(str(tmp_path / "空目录不存在")) == []


def test_load_tolerates_legacy_delivery(tmp_path):
    """U0 之前的老交付(无 config_effective/evidence)也要能打开,不许炸。"""
    d = tmp_path / "old"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps({
        "数据集": "old", "episodes": {"ep0": {"判决": "通过", "checks": {}}}}))
    m = load_delivery(str(d))
    assert m["config_effective"] is None
    assert m["episodes"]["ep0"]["evidence"] == []
    assert overview_rows(m) == []                               # 无统计=空表,不炸


def test_load_timeline_passes_dataset_note(delivery, tmp_path):
    """数据集注记(2026-07-29):episodes_timeline.json 顶层的 dataset_note 原样
    透传给 UI(不判内容);无该字段的交付(droid/老交付)给空串,不占位。"""
    from curation.ui.manifest import load_timeline
    tl_dir = os.path.join(delivery, "details")
    tl = {"口径": "stuck=...", "dataset_note": "state 由 action 累加合成",
          "episodes": {"ep000000": {"duration_s": 1.0, "segments": [], "totals": {}}}}
    with open(os.path.join(tl_dir, "episodes_timeline.json"), "w") as f:
        json.dump(tl, f, ensure_ascii=False)
    got = load_timeline(load_delivery(delivery))
    assert got["dataset_note"] == "state 由 action 累加合成"
    assert got["note"] == "stuck=..." and set(got["episodes"]) == {"ep000000"}
    del tl["dataset_note"]                                       # 无注记的数据集
    with open(os.path.join(tl_dir, "episodes_timeline.json"), "w") as f:
        json.dump(tl, f, ensure_ascii=False)
    assert load_timeline(load_delivery(delivery))["dataset_note"] == ""
    # 老交付(整个文件都没有)也不炸
    old = tmp_path / "old2"
    (old / "details").mkdir(parents=True)
    (old / "passed.json").write_text(json.dumps({"数据集": "o", "episodes": {}}))
    assert load_timeline(load_delivery(str(old)))["dataset_note"] == ""


# ───────── D1 明细表(2026-07-28)─────────

def test_detail_tables_discovery_and_load(delivery):
    from curation.ui.manifest import list_detail_tables, load_detail_table
    import os
    det = os.path.join(delivery, "details")
    with open(os.path.join(det, "motion_details.csv"), "w") as f:
        f.write("episode,score\n" + "\n".join(f"ep{i:03d},0.9" for i in range(5)))
    m = load_delivery(delivery)
    assert list_detail_tables(m) == ["motion_details.csv"]     # 只列实际存在的
    headers, rows, total = load_detail_table(m, "motion_details.csv")
    assert headers == ["episode", "score"] and total == 5 and len(rows) == 5
    h2, r2, t2 = load_detail_table(m, "motion_details.csv", cap=2)
    assert t2 == 5 and len(r2) == 2                            # 封顶但总数照报
    assert load_detail_table(m, "不存在.csv") == ([], [], 0)   # 白名单外/缺失安全
    assert load_detail_table(m, "../passed.json") == ([], [], 0)  # 路径穿越挡住


def test_detail_table_blank_cells_show_dash_and_notes_come_from_report(delivery):
    """(2026-09-02)CSV 空格界面显「—」;运动质量表上方按 passed.json 的
    motion_subdims 说明哪些子项整列不适用/部分不适用。"""
    from curation.ui.manifest import detail_table_notes, load_detail_table
    import json
    import os
    det = os.path.join(delivery, "details")
    with open(os.path.join(det, "motion_details.csv"), "w") as f:
        f.write("条目,运动总分,尖刺\nep000,0.9,\nep001,0.8,1.0\n")
    pj = os.path.join(delivery, "passed.json")
    d = json.load(open(pj, encoding="utf-8"))
    d.setdefault("dataset", {})["motion_subdims"] = {
        "不适用": {"执行器饱和": "指令与读数不同空间"},
        "部分不适用": {"尖刺": "运动占空比<0.15"}}
    json.dump(d, open(pj, "w", encoding="utf-8"), ensure_ascii=False)
    m = load_delivery(delivery)
    headers, rows, total = load_detail_table(m, "motion_details.csv")
    assert headers == ["条目", "运动总分", "尖刺"] and rows[0][2] == "—" and rows[1][2] == "1.0"
    note = detail_table_notes(m, "motion_details.csv")
    assert "执行器饱和:本数据集不适用——指令与读数不同空间" in note
    assert "尖刺:部分条目不适用,明细表中以「—」标出" in note
    assert detail_table_notes(m, "visual_details.csv") == ""      # 只对运动质量表


# ───────── U4 内嵌终端:ASGI 应用装配 / 鉴权 / PTY 往返 ─────────


@pytest.fixture
def clean_ui_env(monkeypatch):
    """UI 的开关全从 env 读缺省值——每个用例先把它们清干净,免得互相串味。"""
    for k in ("CURATION_TERMINAL", "CURATION_UI_USER", "CURATION_UI_PASSWORD",
              "CURATION_UI_HTPASSWD_FILE",
              "CURATION_TERMINAL_WORKDIR", "CURATION_TERMINAL_SHELL"):
        monkeypatch.delenv(k, raising=False)


# ───────── P1 性能剖析页签(2026-07-30):后端 / 运行环境 / 延时 ─────────
#
# ★ 本节最重要的一条不是"渲染对不对",而是**预设代号绝不进界面**:
#   h20-8b / h20-32b / a30-8b / ark 是机房黑话,客户看不懂也不该看懂。
#   架构上的保证 = 预设名压根不进交付记录(apply_vlm_backend 只搬字段值),
#   下面的断言就是给这条保证上锁。

#: 一个"新交付"的 runtime 块,形状与 run.collect_runtime() 的产物一致。
#: ⚠️ node 必须用 RFC 5737 文档专用段(203.0.113.0/24 等),**不许填真实内网 IP**:
#: 本文件会随代码包公开,2026-07-30 这里曾误填一台真实节点的内网地址。
RUNTIME_BLOCK = {
    "vlm_backend": {
        "endpoint": "http://vllm-cosmos-8b.curation.svc.cluster.local:8000/v1",
        "model": "nvidia/Cosmos-Reason2-8B",
        "hardware": "NVIDIA H20",
        "service_type": "自托管 vLLM",
        "episode_concurrency": 32, "frame_concurrency": 8,
        "caption_concurrency": 32},
    "environment": {"cpu_limit_cores": 16.0, "memory_limit_bytes": 34359738368,
                    "node": "203.0.113.5", "node_source": "NODE_NAME"},
}

#: 新交付的延时块:每桶带 wall_s(墙钟,run 收割时按 started_at 算)。
#: 注意 wall_s ≪ n×mean_s —— 这正是并发的效果,也是本页只画墙钟的理由。
LATENCY_BLOCK = {
    "probe": {"n": 1583, "errors": 0, "mean_s": 20.01, "p50_s": 17.78,
              "p90_s": 33.47, "p99_s": 63.26, "max_s": 118.58, "wall_s": 2530.0},
    "endstate": {"n": 114, "errors": 2, "mean_s": 13.59, "p50_s": 12.26,
                 "p90_s": 20.41, "p99_s": 36.45, "max_s": 40.49, "wall_s": 310.5},
    "caption": {"n": 200, "errors": 0, "mean_s": 17.29, "p50_s": 15.54,
                "p90_s": 28.13, "p99_s": 41.21, "max_s": 54.58, "wall_s": 402.7},
    "llm": {"n": 7, "errors": 0, "mean_s": 50.71, "p50_s": 15.7,
            "p90_s": 130.03, "p99_s": 130.03, "max_s": 130.03, "wall_s": 129.3},
}

#: 老交付(2026-07-30 前)的延时块:一个 wall_s 都没有 → 图必须降级成一句说明。
LATENCY_BLOCK_NO_WALL = {
    tag: {k: v for k, v in s.items() if k != "wall_s"}
    for tag, s in LATENCY_BLOCK.items()}


def _with_perf(path, runtime=RUNTIME_BLOCK, latency=LATENCY_BLOCK):
    """往 fixture 交付的 passed.json 里补 runtime / vlm_latency,返回 manifest。"""
    p = os.path.join(path, "passed.json")
    with open(p, encoding="utf-8") as f:
        doc = json.load(f)
    if runtime is not None:
        doc["runtime"] = runtime
    if latency is not None:
        doc.setdefault("dataset", {})["vlm_latency"] = latency
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    return load_delivery(path)


def _render_all(perf) -> str:
    """三块的**全部渲染输出**拼一起——代号泄漏断言一次盖全页。"""
    from curation.ui.manifest import (latency_bar_html, latency_rows,
                                      perf_backend_md, perf_env_md)
    return "\n".join([perf_backend_md(perf), perf_env_md(perf),
                      latency_bar_html(perf), json.dumps(latency_rows(perf),
                                                         ensure_ascii=False)])


def test_perf_loads_new_runtime_block(delivery):
    """新交付:runtime 块原样读出,硬件/服务类型/三个并发都到位。"""
    from curation.ui.manifest import load_perf
    perf = load_perf(_with_perf(delivery))
    assert perf["legacy"] is False
    b = perf["backend"]
    assert b["hardware"] == "NVIDIA H20" and b["service_type"] == "自托管 vLLM"
    assert b["model"] == "nvidia/Cosmos-Reason2-8B"
    assert (b["episode_concurrency"], b["frame_concurrency"],
            b["caption_concurrency"]) == (32, 8, 32)
    assert perf["env"]["cpu_limit_cores"] == 16.0
    assert set(perf["latency"]) == {"probe", "endstate", "caption", "llm"}


def test_perf_backend_card_renders_all_four_facts(delivery):
    """后端卡片:端点原样 + 模型 + 服务类型 + 硬件 + 三个通俗并发标签。"""
    from curation.ui.manifest import load_perf, perf_backend_md
    md = perf_backend_md(load_perf(_with_perf(delivery)))
    assert "vllm-cosmos-8b.curation.svc.cluster.local:8000/v1" in md   # URL 原样
    assert "nvidia/Cosmos-Reason2-8B" in md
    assert "自托管 vLLM" in md and "NVIDIA H20" in md
    for label in ("episode 并发", "单条内帧并发", "打标并发"):
        assert label in md, label
    assert "32" in md and "8" in md


def test_perf_env_card_and_legacy_degradation(delivery, tmp_path):
    """运行环境:新跑有 CPU/内存/节点;老交付整块"未记录",绝不编数字。"""
    from curation.ui.manifest import NOT_RECORDED, load_perf, perf_env_md
    md = perf_env_md(load_perf(_with_perf(delivery)))
    assert "16.0 核" in md and "32.0 GiB" in md and "203.0.113.5" in md
    assert "取自调度注入的节点名" in md
    # 老交付(无 runtime 块):整块"未记录",不许把配额编一个出来
    d2 = tmp_path / "old-perf"
    d2.mkdir()
    (d2 / "passed.json").write_text(json.dumps(
        {"数据集": "old", "episodes": {}}, ensure_ascii=False))
    perf_old = load_perf(load_delivery(str(d2)))
    assert perf_old["legacy"] is True and perf_old["env"] == {}
    assert NOT_RECORDED in perf_env_md(perf_old)


def test_perf_legacy_delivery_falls_back_to_config_effective(delivery):
    """老交付(有 config_effective 无 runtime):端点/模型/并发尽力取,硬件"未记录"。"""
    from curation.ui.manifest import load_perf, perf_backend_md
    p = os.path.join(delivery, "passed.json")
    doc = json.loads(open(p, encoding="utf-8").read())
    doc["config_effective"] = {
        "pipeline": {"vlm_episode_concurrency": 32},
        "skill_profile": {"caption_concurrency": 32},
        "checks": {"task_success": {"vlm": {
            "endpoint": "https://ark.cn-beijing.volces.com/api/v3",
            "model": "doubao-seed-2-0-pro-260215", "max_concurrency": 8}}}}
    doc.setdefault("dataset", {})["vlm_latency"] = LATENCY_BLOCK
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    perf = load_perf(load_delivery(delivery))
    assert perf["legacy"] is True
    b = perf["backend"]
    assert b["model"] == "doubao-seed-2-0-pro-260215"
    assert b["episode_concurrency"] == 32 and b["frame_concurrency"] == 8
    assert b["hardware"] is None                       # 老交付没记 → 不许瞎猜
    md = perf_backend_md(perf)
    # 托管服务的硬件本来就不可见,如实标注(而不是写"未记录"让人以为是缺陷)
    assert "硬件不可见" in md and "方舟 MaaS" in md


def test_perf_hardware_never_guessed_for_unknown_selfhosted(tmp_path):
    """自托管端点但站点配置没声明 hardware → 老实写"未记录",不从端点反查型号。"""
    from curation.ui.manifest import load_perf, perf_backend_md
    d = tmp_path / "nohw"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps({
        "数据集": "x", "episodes": {},
        "runtime": {"vlm_backend": {
            "endpoint": "http://vllm-cosmos-8b-a30.curation.svc.cluster.local:8000/v1",
            "model": "nvidia/Cosmos-Reason2-8B", "hardware": None,
            "service_type": None, "episode_concurrency": 32,
            "frame_concurrency": 8, "caption_concurrency": 32},
            "environment": {"cpu_limit_cores": None, "memory_limit_bytes": None,
                            "node": "pod-abc", "node_source": "hostname"}}},
        ensure_ascii=False))
    perf = load_perf(load_delivery(str(d)))
    assert perf["backend"]["service_type"] == "自托管推理服务(集群内)"   # 域名兜底
    md = perf_backend_md(perf)
    assert "未记录" in md and "NVIDIA" not in md         # 一个型号都不许冒出来


def test_perf_env_marks_hostname_is_not_node_name(tmp_path):
    """没注入 NODE_NAME 时显示的是容器 hostname —— 必须当面说清,不能冒充节点名。"""
    from curation.ui.manifest import load_perf, perf_env_md
    d = tmp_path / "hn"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps({
        "数据集": "x", "episodes": {},
        "runtime": {"vlm_backend": {}, "environment": {
            "cpu_limit_cores": 16.0, "memory_limit_bytes": 34359738368,
            "node": "robot-curator-77cd94bcd6-bsbdw", "node_source": "hostname"}}},
        ensure_ascii=False))
    md = perf_env_md(load_perf(load_delivery(str(d))))
    assert "robot-curator-77cd94bcd6-bsbdw" in md
    assert "非节点名" in md


def test_latency_table_uses_semantic_labels_in_order(delivery):
    """延时表:语义化中文标签、流程顺序、统计列齐全(实现标签不露头)。

    2026-08-15 改版钉两件事:①显示名换新(「探针」与 probe_endpoint 的探活一词
    两义、「终态」不准——投票器收的是前后对照帧);②次数列口径 = **发起次数**
    (含失败发起;此前 n 只数成功,与图上的总数两套口径,用户实见 1192/1194)。
    """
    from curation.ui.manifest import LATENCY_HEADERS, latency_rows, load_perf
    rows = latency_rows(load_perf(_with_perf(delivery)))
    assert [r[0] for r in rows] == ["任务完成度打分", "逐机位复核", "技能打标", "技能归纳"]
    # 表头写全"响应时间",别指望客户认得裸 P50/P90(第二轮反馈)
    assert LATENCY_HEADERS == ["调用类型", "发起次数", "平均响应时间(秒)",
                               "P50 响应时间(秒)", "P90 响应时间(秒)",
                               "P99 响应时间(秒)"]
    probe = rows[0]
    assert probe[1] == 1583 and probe[2] == "20.01" and probe[5] == "63.26"
    endstate = rows[1]
    assert endstate[1] == 116                        # 发起次数 = 成功 114 + 没成 2
    flat = json.dumps(rows, ensure_ascii=False)
    for impl_tag in ("probe", "endstate", "caption", "llm", "arbitration"):
        assert impl_tag not in flat, impl_tag        # 埋点标签只是字典键,不是界面词
    for old_name in ("任务判定探针", "终态复核", "体系归纳"):
        assert old_name not in flat, old_name        # 旧显示名不许回潮


def test_every_latency_tag_has_a_chinese_label():
    """**每一类**埋点都必须有中文名 —— 少一个,界面就直接印英文标签。

    2026-08-14 用户实见:取证仲裁链上线后,延时表最后一行是裸的 `arbitration`。
    这条把"新增一类调用要同步加标签"钉死:埋点常量在 vlm_client 那边,这里比对。
    """
    import pathlib
    import re as _re

    from curation.ui.manifest import LATENCY_LABELS

    # 埋点标签是各调用点写死的字符串,没有集中常量 —— 那就从源码里扫出来比对,
    # 这样新增一类调用而忘了配中文名时,是**这条测试**先红,而不是客户先看见。
    # 2026-08-15 对冲改造后调用点写法变成 hedged_request(..., tag="probe"),
    # 两种写法都扫(直接 latency_record 的老写法仍可能出现)。
    root = pathlib.Path(__file__).resolve().parent.parent
    tags = set()
    for py in root.rglob("*.py"):
        if "tests" in py.parts:
            continue
        src = py.read_text(encoding="utf-8")
        tags |= set(_re.findall(r'latency_record\(\s*["\']([a-z_]+)["\']', src))
        tags |= set(_re.findall(r'\btag=["\']([a-z_]+)["\']', src))
    assert tags, "一个埋点都没扫到 —— 正则该跟着 latency_record/hedged_request 的写法改"
    missing = sorted(t for t in tags if t not in LATENCY_LABELS)
    assert not missing, f"这些埋点没有中文名,会在界面上露英文:{missing}"


def test_latency_bar_chart_is_wall_clock_only(delivery):
    """横条图:四条、按**流程顺序**排列、最长的那条 100%,时长人性化。

    2026-08-15 排序从"墙钟降序"改为流程顺序(用户选方案 B):谁最费时靠条长看。
    fixture 里 caption 墙钟(402.7)> endstate(310.5),若有人改回按墙钟排,
    复核就会掉到打标后面 —— 下面的顺序断言当场变红。
    """
    from curation.ui.manifest import latency_bar_html, load_perf
    html = latency_bar_html(load_perf(_with_perf(delivery)))
    assert html.count("<div style=\"margin:8px 0\">") == 4
    assert (html.index("任务完成度打分") < html.index("逐机位复核")
            < html.index("技能打标") < html.index("技能归纳"))
    # probe 墙钟 2530s 最大 → 宽度 100%、念作 42 分 10 秒;次数是**发起次数**
    assert "width:100.00%" in html
    assert "墙钟 <b>42 分 10 秒</b>(发起 1583 次,并发执行)" in html
    assert "忙碌区间并集" in html          # 口径文案(2026-08-06 随 wall_s 并集口径更新)
    assert "各条墙钟相加 ≠ 整次运行总时长" in html


def test_latency_bar_failures_reworded_and_not_red(delivery):
    """红色「失败 N」下岗(2026-08-15 用户点名):超时补发是正常兜底,不是失败。

    钉四件事:①「失败」二字与错误红 #F53F3F 不出现在这块;②老快照(没有对冲
    字段)把 errors 如实说成「没拿到结果」,不编原因;③新口径快照能说出
    「N 次超时后补发,均已拿到结果」和补发数(补发率是判断服务端质量的唯一线索,
    不该被"救回来了"藏掉);④真有没拿到结果的调用时,补一句"少一票只会更保守"。
    """
    from curation.ui.manifest import latency_bar_html, load_perf
    # 老快照:endstate errors=2,无对冲字段
    html = latency_bar_html(load_perf(_with_perf(delivery)))
    assert "失败" not in html and "#F53F3F" not in html
    assert "2 次没拿到结果" in html
    assert "少一票" in html and "不会因此错杀" in html      # 后果句(有没成的调用才出现)
    # 新口径快照:超时补发全救回 + 一次两发都没等到回应
    lat_new = {
        "probe": {"n": 1592, "errors": 3, "attempts": 1595, "hedged": 3,
                  "retried": 0, "unanswered": 0, "unanswered_timeout": 0,
                  "mean_s": 20.0, "p50_s": 18.0, "p90_s": 30.0, "p99_s": 50.0,
                  "max_s": 60.0, "wall_s": 1000.0},
        "llm": {"n": 6, "errors": 2, "attempts": 8, "hedged": 1, "retried": 0,
                "unanswered": 1, "unanswered_timeout": 1, "mean_s": 50.0,
                "p50_s": 40.0, "p90_s": 100.0, "p99_s": 120.0, "max_s": 130.0,
                "wall_s": 300.0}}
    html2 = latency_bar_html(load_perf(_with_perf(delivery, latency=lat_new)))
    assert "失败" not in html2 and "#F53F3F" not in html2
    assert "3 次超时后补发,均已拿到结果" in html2
    assert "发起 1595 次" in html2                          # 口径 = 发起次数
    assert "1 次没等到回应" in html2                        # 两发都没回来的才这么说
    assert "少一票" in html2
    # 全部顺利(无补发无失败)→ 一句多余的话都不说,后果句也不出现
    lat_clean = {"probe": {"n": 10, "errors": 0, "attempts": 10, "hedged": 0,
                           "retried": 0, "unanswered": 0, "unanswered_timeout": 0,
                           "mean_s": 5.0, "p50_s": 5.0, "p90_s": 6.0,
                           "p99_s": 7.0, "max_s": 8.0, "wall_s": 50.0}}
    html3 = latency_bar_html(load_perf(_with_perf(delivery, latency=lat_clean)))
    assert "少一票" not in html3 and "补发" not in html3 and "失败" not in html3


def test_latency_chart_never_falls_back_to_count_times_mean(delivery):
    """★红线断言:界面上不许出现"次数 × 均值"那个口径(并发下高估几十倍)。

    用户第二轮原话:非常误导——我们有并行机制。所以连"总耗时"字样一起清干净。
    """
    from curation.ui.manifest import LATENCY_NOTE, latency_bar_html, load_perf
    html = latency_bar_html(load_perf(_with_perf(delivery))) + LATENCY_NOTE
    for banned in ("次数 × 均值", "次数×均值", "总耗时", "×"):
        assert banned not in html, banned
    assert "31676" not in html                     # 1583×20.01 的那个乘积


def test_latency_chart_degrades_without_wall_clock(delivery):
    """老交付(延时块没有 wall_s)→ 不画图,只给一句说明。绝不退回均值条形图。"""
    from curation.ui.manifest import (NO_WALL_NOTE, latency_bar_html,
                                      latency_rows, load_perf)
    perf = load_perf(_with_perf(delivery, latency=LATENCY_BLOCK_NO_WALL))
    html = latency_bar_html(perf)
    assert NO_WALL_NOTE in html
    assert "margin:8px 0" not in html and "width:" not in html   # 一根条都没有
    assert "未记录调用时刻" in html and "新交付起提供" in html
    assert latency_rows(perf)[0][1] == 1583        # 表格照旧有数(只是没图)


def test_latency_kind_notes_explain_all_five_call_types():
    """五类调用各配一句人话说明——语义化名字不解释,客户仍读不懂 1583 次是什么。

    2026-08-15 改版:解释文案与报告共用 vlm_call_kinds 单一事实源,且旧文案的
    两处事实错误(复核"只对没通过一审的跑"/打分"逐帧问")不许回潮 —— 事实
    本身的钉死在 test_call_kind_wording.py,这里钉界面确实用的是那份文案。
    """
    from curation.ui.manifest import LATENCY_KIND_NOTE, LATENCY_PCTL_NOTE
    from curation.vlm_call_kinds import CALL_KIND_LABELS, CALL_KIND_NOTES
    assert "一半的调用不超过此耗时" in LATENCY_PCTL_NOTE
    for tag, label in CALL_KIND_LABELS.items():
        assert f"**{label}**" in LATENCY_KIND_NOTE, label
        assert CALL_KIND_NOTES[tag] in LATENCY_KIND_NOTE, tag   # 同一份文案,不是抄一遍
    assert "次数最多" in LATENCY_KIND_NOTE              # 打分次数为何最多
    assert "只对没通过一审的数据跑" not in LATENCY_KIND_NOTE   # 错误事实不许回潮
    for impl_tag in ("probe", "endstate", "caption", "llm", "arbitration"):
        assert impl_tag not in LATENCY_KIND_NOTE, impl_tag


def test_human_duration_formats():
    """时长人性化:≥60s 念成 X 分 X 秒,上小时再拆一层。"""
    from curation.ui.manifest import human_duration
    assert human_duration(42.4) == "42.4 秒"
    assert human_duration(2530.0) == "42 分 10 秒"
    assert human_duration(60) == "1 分 0 秒"
    assert human_duration(3725) == "1 小时 2 分 5 秒"


def test_latency_empty_state(delivery):
    """只跑数值类检查的运行没有 VLM 调用 → 空态提示,不是空白也不报错。"""
    from curation.ui.manifest import latency_bar_html, latency_rows, load_perf
    perf = load_perf(_with_perf(delivery, latency={}))
    assert latency_rows(perf) == []
    assert "没有 VLM 调用" in latency_bar_html(perf)


def test_perf_render_never_leaks_backend_preset_codenames(delivery):
    """★红线断言:渲染结果里绝不出现后端预设代号(机房黑话)。

    覆盖新交付与老交付两条路径。"h20-"/"a30-8b" 是任务书点名的两个;
    顺带把站点文件里现有的预设名全列上,以后加预设也照这个模式加。
    """
    from curation.ui.manifest import load_perf
    for perf in (load_perf(_with_perf(delivery)),
                 load_perf(load_delivery(delivery))):
        text = _render_all(perf)
        for codename in ("h20-", "a30-8b", "h20-8b", "h20-32b",
                         "self-hosted-example", "vlm_backends"):
            assert codename not in text, f"预设代号 {codename!r} 漏进界面: {text[:300]}"


def test_site_yaml_presets_declare_hardware():
    """站点配置里每个自托管预设都要声明 hardware —— 漏了界面就只能写"未记录"。"""
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    site = os.path.join(root, "deploy", "site.yaml")
    if not os.path.exists(site):                  # 公开代码包里没有站点文件,跳过
        pytest.skip("无站点文件(公开包)")
    presets = (yaml.safe_load(open(site, encoding="utf-8")) or {}).get("vlm_backends") or {}
    assert presets, "site.yaml 应有 vlm_backends"
    for name, p in presets.items():
        assert (p or {}).get("hardware"), f"预设 {name} 缺 hardware 描述字段"


# ───────── 技能分布图(2026-07-30):技能画像页的横条图 ─────────
#
# 两种画像形状都要吃:两级(正常路径,VLM caption→LLM 归纳)与扁平(VLM 不可用时
# 按原始标注分组的降级路径)。红线三条,以下测试逐条钉死:**不截断**、**单色**、
# **子技能与族条共用全局尺子**。

#: 两级画像(droid 那份的缩样):Put 一家独大且有 3 个子技能,Pick 只有 1 个子技能
#: (→ 不该折叠),Bundle 只有 1 条数据且在 undersampled 名单里。
TWO_LEVEL_SKILLS = {
    "n_episodes": 195, "n_families": 3, "guideline": "按动作意图分族",
    "undersampled": ["Bundle"],
    "families": {
        "Put": {"count": 102, "pct": 52.3, "criterion": "把物体放到目标位置",
                "subskills": {
                    "Put A on B": {"count": 80, "pct": 41.0, "criterion": "堆叠"},
                    "Put in": {"count": 22, "pct": 11.3, "criterion": "放入容器"}}},
        "Pick": {"count": 15, "pct": 7.7, "criterion": "拿起",
                 "subskills": {"Pick up": {"count": 15, "pct": 7.7, "criterion": "拿起"}}},
        "Bundle": {"count": 1, "pct": 0.5, "criterion": "捆扎", "subskills": {}}},
}

#: 扁平降级画像(bridge 那份的缩样):键是原始指令,没有子技能、没有判据。
FLAT_SKILLS = {
    "n_episodes": 200, "n_skills": 3, "undersampled": ["put the pot on the stove"],
    "skills": {"(无指令)": {"count": 56, "pct": 28.0, "avg_len_s": 8.14},
               "fold the cloth": {"count": 3, "pct": 1.5, "avg_len_s": 7.0},
               "put the pot on the stove": {"count": 1, "pct": 0.5, "avg_len_s": 9.2}},
}


def _skills_delivery(tmp_path, skills, name="sk"):
    """只有 skills 块的最小交付(图不依赖 episodes)。"""
    d = tmp_path / name
    d.mkdir()
    (d / "passed.json").write_text(
        json.dumps({"数据集": "x", "episodes": {}, "skills": skills},
                   ensure_ascii=False), encoding="utf-8")
    return load_delivery(str(d))


def _bar_widths(html: str) -> list[float]:
    """按出现顺序取出每根条的宽度百分比(条 = 填了主色的那个 div)。"""
    import re
    from curation.ui.manifest import SKILL_BAR_COLOR
    return [float(w) for w in re.findall(
        r'width:([\d.]+)%;height:100%;background:' + SKILL_BAR_COLOR, html)]


def test_skill_chart_two_level_drilldown(tmp_path):
    """两级形状:按条数降序、族条可点开子技能;**只有 ≥2 子技能的族**才做 details。"""
    from curation.ui.manifest import SKILL_FALLBACK_NOTE, skill_bar_html, skill_chart_items
    m = _skills_delivery(tmp_path, TWO_LEVEL_SKILLS)
    shape, items = skill_chart_items(m)
    assert shape == "two_level"
    assert [it["name"] for it in items] == ["Put", "Pick", "Bundle"]      # 条数降序
    html = skill_bar_html(m)
    # Put 有 2 个子技能 → 唯一一个 details;Pick 只有 1 个 → 普通行,不折叠
    assert html.count("<details>") == 1
    assert html.index("Put") < html.index("Pick") < html.index("Bundle")
    assert "Put A on B" in html and "Put in" in html                      # 子技能条在里面
    # 下钻零 JavaScript:靠 details/summary + CSS 的 ▸/▾,不许有脚本
    assert "<script" not in html and "onclick" not in html
    assert "sk-caret" in html
    # 悬停详情:条数 / 占比 / 判据
    assert 'title="Put · 102 条 · 52.3% · 判据:把物体放到目标位置"' in html
    assert SKILL_FALLBACK_NOTE not in html                                # 正常路径不挂降级注记


def test_skill_chart_flat_shape_marks_degradation(tmp_path):
    """扁平形状:必须当面写清是「未经 VLM 审计的原始标注分组」,且一个 details 都没有。"""
    from curation.ui.manifest import SKILL_FALLBACK_NOTE, skill_bar_html, skill_chart_items
    m = _skills_delivery(tmp_path, FLAT_SKILLS)
    shape, items = skill_chart_items(m)
    assert shape == "flat" and [it["name"] for it in items][0] == "(无指令)"
    html = skill_bar_html(m)
    assert SKILL_FALLBACK_NOTE in html and "降级" not in html and "仅供参考" in html
    assert "<details" not in html                     # 无子技能 → 全普通行
    assert _bar_widths(html) == [100.0, 5.36, 1.79]   # 56/3/1,全局尺子


def test_skill_chart_empty_and_missing(tmp_path):
    """未启用 / 空画像:一句说明,不报错也不占位(不画空坐标轴)。"""
    from curation.ui.manifest import skill_bar_html, skill_chart_items
    for skills in ({}, {"n_episodes": 0, "families": {}}, {"skills": {}}):
        m = _skills_delivery(tmp_path, skills, name=f"e{abs(hash(str(skills)))}")
        assert skill_chart_items(m)[0] == "empty"
        html = skill_bar_html(m)
        assert "未生成技能画像" in html
        assert "width:" not in html                   # 一根条都没有


def test_skill_chart_one_global_scale(tmp_path):
    """★红线:子技能条与族条共用**全局**尺子,不按族内最大值重缩放。

    构造 Put=100 与只有 2 条数据的 Tiny(子技能各 1 条)。若按族内归一,Tiny 的
    子技能会画成满格 → 1 条数据看着和 100 条一样长,是骗人的。
    """
    from curation.ui.manifest import skill_bar_html
    m = _skills_delivery(tmp_path, {"families": {
        "Put": {"count": 100, "pct": 50.0, "subskills": {
            "Put A on B": {"count": 60}, "Put in": {"count": 40}}},
        "Tiny": {"count": 2, "pct": 1.0, "subskills": {
            "Tiny a": {"count": 1}, "Tiny b": {"count": 1}}}}}, name="scale")
    html = skill_bar_html(m)
    # 顺序:Put、Put 的两个子技能、Tiny、Tiny 的两个子技能
    assert _bar_widths(html) == [100.0, 60.0, 40.0, 2.0, 1.0, 1.0]
    # 子技能条一律不超过父族条(全局尺子的直接后果)
    assert all(w <= 100.0 for w in _bar_widths(html)[1:3])
    assert all(w <= 2.0 for w in _bar_widths(html)[4:])


def test_skill_chart_undersampled_chip_carries_text(tmp_path):
    """样本偏少:带**文字**的琥珀 chip(不靠颜色单独表意),且**不换条的填充色**。"""
    from curation.ui.manifest import SKILL_BAR_COLOR, skill_bar_html
    html = skill_bar_html(_skills_delivery(tmp_path, TWO_LEVEL_SKILLS, name="chip"))
    assert html.count("样本偏少") == 1                # chip 本身;出处脚注已删(2026-08-23 用户)
    # 单色红线:条只有这一个填充色,undersampled 的那根也一样(条短已经说明问题)
    assert html.count(f"background:{SKILL_BAR_COLOR}") == len(_bar_widths(html)) == 5
    assert SKILL_BAR_COLOR == "#165DFF"        # Arco 主色(2026-08-13 全站统一)


def test_skill_chart_never_truncates(tmp_path):
    """★红线:全部条目都画。长尾就是画像的信息量,截断会造出"数据很集中"的假象。"""
    from curation.ui.manifest import skill_bar_html
    skills = {"n_episodes": 200, "n_skills": 130, "undersampled": [],
              "skills": {"(无指令)": {"count": 56, "pct": 28.0}}}
    skills["skills"].update({f"task {i}": {"count": 1, "pct": 0.5} for i in range(129)})
    html = skill_bar_html(_skills_delivery(tmp_path, skills, name="long"))
    assert len(_bar_widths(html)) == 130                  # 130 项一根不少
    assert "task 0" in html and "task 128" in html        # 最尾巴的也在
    for banned in ("仅显示前", "…共", "其余"):
        assert banned not in html, banned


def test_audit_queue_accepts_both_keys(tmp_path):
    """review.json 键名 2026-07-31 中性化 → 新老两个键 UI 都要认。

    老交付(键"标注审计复核队列")由上面的 delivery fixture 覆盖;这里钉新键,
    以及新档 low_caption_unstable 条目(我方描述重打标不稳,已降级)照样能进队列。
    """
    old = tmp_path / "old"
    new = tmp_path / "new"
    for d in (old, new):
        d.mkdir()
        (d / "passed.json").write_text(json.dumps({"数据集": "ds", "episodes": {}},
                                                  ensure_ascii=False))
    (old / "review.json").write_text(json.dumps({
        "episodes": {}, "标注审计复核队列": [
            {"id": "ep1", "label": "L", "caption": "C", "reason": "跨族"}]},
        ensure_ascii=False))
    (new / "review.json").write_text(json.dumps({
        "episodes": {}, "标注-画面分歧复核队列": [
            {"id": "ep2", "label": "L2", "caption": "C2",
             "reason": "分歧:原始标注归为 wipe,自产描述(VLM 生成)归为 place——需人工判定",
             "caption_stable": False, "recaptions": ["a", "b"]}]},
        ensure_ascii=False))
    assert merged_queue_rows(load_delivery(str(old)))[0][0] == "1"  # 老交付打得开
    row = merged_queue_rows(load_delivery(str(new)))[0]
    assert row == ["2", "标注分歧", "L2", "C2", ""]   # 老数据无 priority 字段照样不崩


def test_label_decision_roundtrip(delivery):
    from curation.ui.manifest import (load_label_decisions, record_label_decision,
                                      load_delivery)
    m = load_delivery(delivery)
    assert load_label_decisions(m) == {}                       # 初始无裁决
    msg = record_label_decision(m["path"], "ep000002", "采纳建议改标",
                                new_label="wipe the table", note="核对过视频")
    # 状态行 2026-08-25 用户精简:只说一句,不带 episode/裁决内容/去哪执行
    assert msg.startswith("已记录(随时可改判)")
    dec = load_label_decisions(m)
    assert dec["ep000002"]["decision"] == "采纳建议改标"
    assert dec["ep000002"]["new_label"] == "wipe the table"
    # 改判 = 追加,后写覆盖前写
    record_label_decision(m["path"], "ep000002", "维持原标注")
    assert load_label_decisions(m)["ep000002"]["decision"] == "维持原标注"
    # 裁决结果列回显进表格(草稿带「(未应用)」后缀,2026-08-25 台账批次起)
    row = [r for r in merged_queue_rows(m) if r[0] == "2"][0]
    assert row[-1] == "维持原标注(未应用)"


def test_label_decision_guards(delivery):
    from curation.ui.manifest import record_label_decision, load_label_decisions, load_delivery
    m = load_delivery(delivery)
    assert "未记录" in record_label_decision(m["path"], "ep1", "采纳建议改标", new_label="  ")
    assert "未记录" in record_label_decision(m["path"], "ep1", "乱写的裁决")
    assert load_label_decisions(m) == {}                       # 守卫拦下的不落盘


# ───────── 人工裁决页(2026-08-06):任务成败弃权队列 + 成败裁决落盘 ─────────


def test_task_review_queue_only_takes_task_abstentions(delivery, tmp_path):
    """队列只收「待裁决项含任务成败判定」的条目:别的维度的弃权(如同步)不是
    人看视频就能拍板的,混进来只会让裁决面板变成杂物间。"""
    m = load_delivery(delivery)
    q = m["task_review"]
    assert [t["id"] for t in q] == ["ep000000"]
    assert q[0]["current"] == "通过" and q[0]["reason"] == "渐变问询不可判"
    assert q[0]["readings"] == {"voc": 0.87, "末态分": 0.3}   # 从 checks 的 detail 解出
    row = [r for r in merged_queue_rows(m) if r[1] in ("成败弃权", "标注+成败")][0]
    # 单表行:当前判决/弃权原因不进表(细节在卡片);裁决结果空=待人工
    assert row[0] == "0" and row[-1] == ""

    # 另一维度弃权的条目不进队列
    d2 = tmp_path / "sync-only"
    d2.mkdir()
    (d2 / "passed.json").write_text(json.dumps({"数据集": "x", "episodes": {}},
                                               ensure_ascii=False))
    (d2 / "review.json").write_text(json.dumps({"episodes": {"ep9": {
        "当前判决": "通过", "待裁决项": ["视频-动作同步"],
        "弃权原因": {"视频-动作同步": "信号不足"}}}}, ensure_ascii=False))
    assert load_delivery(str(d2))["task_review"] == []


def test_task_readings_tolerate_double_encoded_detail():
    """detail 双重编码(JSON 字符串里又套一层)也要解得出读数——只解一层拿到的
    是 str,读数会静默全丢。解不开的原文不许当成 0 或空读数。"""
    from curation.ui.manifest import task_readings
    inner = json.dumps({"voc": 0.5, "completion_final": 0.1})
    assert task_readings({"detail": {"voc": 0.5}}) == {"voc": 0.5}       # 已是 dict
    assert task_readings({"detail": inner})["voc"] == 0.5                # 单层
    assert task_readings({"detail": json.dumps(inner)})["末态分"] == 0.1  # 双层
    assert task_readings({"detail": "坏字符串"}) == {}                    # 解不开=没读数
    assert task_readings({}) == {} and task_readings({"detail": None}) == {}


def test_task_review_row_uses_review_own_checks(tmp_path):
    """rejudge 搬移过的条目,checks 只写在 review 里(passed/reject 没有它)——
    读数得从 review 条目自己的 checks 取,否则重判后队列上的读数全空。"""
    d = tmp_path / "moved"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps({"数据集": "x", "episodes": {}},
                                              ensure_ascii=False))
    (d / "review.json").write_text(json.dumps({"episodes": {"ep7": {
        "当前判决": "通过", "待裁决项": ["任务成败判定"],
        "弃权原因": {"任务成败判定": "重判仍不可判"},
        "checks": {"任务成败判定": {"结果": "弃权", "detail": TS_DETAIL}}}}},
        ensure_ascii=False))
    q = load_delivery(str(d))["task_review"]
    assert q[0]["readings"]["voc"] == 0.87 and q[0]["state"] == "弃权"


def test_task_verdict_roundtrip_and_override(delivery):
    """成败裁决落盘/读回/改判;裁决状态回显进队列表。"""
    from curation.ui.manifest import load_task_verdicts, record_task_verdict
    m = load_delivery(delivery)
    assert load_task_verdicts(m) == {}
    msg = record_task_verdict(m["path"], "ep000000", "判成功", note="看了视频,完成了")
    # 状态行 2026-08-25 用户再精简(嫌啰嗦):只说一句;不报存储内部细节
    assert msg.startswith("已记录(随时可改判)")
    assert "1 分钟" not in msg
    got = load_task_verdicts(m)
    assert got["ep000000"]["verdict"] == "判成功"
    assert got["ep000000"]["note"] == "看了视频,完成了" and got["ep000000"]["at"]
    row = [r for r in merged_queue_rows(m) if r[0] == "0"][0]
    assert row[-1] == "判成功(未应用)"    # 回显进表格,草稿后缀(2026-08-25)
    # 写老值「搁置」照收(2026-08-19 改名后的兼容),读回来是现行词
    record_task_verdict(m["path"], "ep000000", "搁置")          # 改判=追加,后写覆盖
    assert load_task_verdicts(m)["ep000000"]["verdict"] == "拿不准"


def test_task_verdict_guards(delivery):
    """裁决词只认三选一;没选中 episode 也不落盘(空 id 会写出一行永远对不上的垃圾)。"""
    from curation.ui.manifest import load_task_verdicts, record_task_verdict
    m = load_delivery(delivery)
    assert "未记录" in record_task_verdict(m["path"], "ep1", "判个成功吧")
    assert "未记录" in record_task_verdict(m["path"], "", "判成功")
    assert load_task_verdicts(m) == {}


def test_task_verdict_survives_fsx_visibility_gap(tmp_path):
    """与标注裁决同款的 FSX 可见延迟兜底:延迟窗口内连裁两条,一条都不能丢。"""
    from curation.dataset_level.decisions import (load_task_verdicts,
                                                  record_task_verdict)
    d = str(tmp_path)
    record_task_verdict(d, "ep000001", "判成功", note="第一条")
    # 裁决 CSV 2026-08-14 起住在交付根的 human-decisions/(不再混在 details/ 里)
    csv_path = tmp_path / "human-decisions" / "task_verdicts.csv"
    written = csv_path.stat().st_mtime_ns
    csv_path.write_text("")  # 装作还看不见
    # 延迟窗口里读到的是旧版本,mtime 不比我们写盘时新。2026-09-04 起的判据里"文件更新"
    # 表示界面外改过、磁盘为准,不回填 mtime 的话测的就成了那条分支(且随时间戳粒度时过时不过)
    os.utime(csv_path, ns=(written, written))
    record_task_verdict(d, "ep000002", "判失败", note="第二条")
    got = load_task_verdicts(d)
    assert set(got) == {"ep000001", "ep000002"}, "延迟窗口内第一条裁决被冲掉"
    assert got["ep000002"]["verdict"] == "判失败"


def test_verdict_and_label_decisions_do_not_collide(tmp_path):
    """两条裁决线各写各的表(共用一个进程内写缓存字典,不许串味)。"""
    from curation.dataset_level.decisions import (load_label_decisions,
                                                  load_task_verdicts,
                                                  record_label_decision,
                                                  record_task_verdict)
    d = str(tmp_path)
    record_label_decision(d, "epA", "维持原标注")
    record_task_verdict(d, "epB", "判失败")
    assert set(load_label_decisions(d)) == {"epA"}
    assert set(load_task_verdicts(d)) == {"epB"}


def test_pending_counts_and_guidance_text(delivery):
    """页面上的"还剩几条"与工序引导:裁过的不再催,裁完催办语消失;拿不准算未裁。

    2026-08-16 合并队列重构后改写:分区制的"先清标注再裁成败"工序提醒退役
    (两个问题在同一张卡上一次答完),进度行换成 merged_hint_md 按**卡**计数 ——
    fixture 里 ep000002(标注)与 ep000000(成败)是两张卡,任一问题没答完,
    对应那张卡就算待裁。
    """
    from curation.ui.manifest import (WORKFLOW_GUIDE, audit_note_md,
                                      audit_pending_count, merged_hint_md,
                                      merged_pending_count,
                                      record_label_decision,
                                      record_task_verdict, task_pending_count)
    m = load_delivery(delivery)
    assert audit_pending_count(m) == 1 and task_pending_count(m) == 1
    assert merged_pending_count(m) == 2                     # 两张卡各有问题没答
    assert "1" in audit_note_md(m) and "人工裁决" in audit_note_md(m)
    assert merged_hint_md(m) == ""      # 进度行 2026-08-25 用户点名退役
    record_label_decision(m["path"], "ep000002", "维持原标注")
    assert audit_pending_count(m) == 0
    assert merged_pending_count(m) == 1                     # 标注卡答完,成败卡还在
    assert "已全部裁决" in audit_note_md(m)
    record_task_verdict(m["path"], "ep000000", "拿不准")
    assert task_pending_count(m) == 1, "拿不准是待定不是结论,仍算未裁"
    assert merged_pending_count(m) == 1
    record_task_verdict(m["path"], "ep000000", "判成功")
    assert task_pending_count(m) == 0
    assert merged_pending_count(m) == 0
    # 工序引导:一次答完 → 执行;只改标留空成败的会有第二轮,提前说明白
    assert "一次答完" in WORKFLOW_GUIDE
    # 2026-08-19 执行入口搬进本页底部:引导指向「执行裁决」按钮,绝不再把人
    # 支到已删掉的「任务台 · 执行人工裁决」页签(指路指向不存在的地方 = 死链)
    assert "执行裁决" in WORKFLOW_GUIDE and "任务台" not in WORKFLOW_GUIDE
    assert "重判" in WORKFLOW_GUIDE and "再执行一次" in WORKFLOW_GUIDE


# ───────── 「待你裁决」合并队列(2026-08-16 重构)─────────
#
# 起因(用户实见):裁决页按问题类型分区,而人的工作按 episode 展开 —— 一条视频
# 看一遍、该答的问题一次答完。droid-200-new 实测 7 条同时在两个队列里,用户要在
# 两张卡片里各找一次、各看一遍视频。以下测试钉住:合并去重、筛选三档、②的显隐
# 三条规矩、矛盾拦截、裁决 CSV 契约冻结。


def _overlap_delivery(tmp_path):
    """epA=只有标注问题(重点档,排最前),epB=两个问题都有(重叠),
    epC=只有成败问题。合并后应为三张卡,epB 只出现一次。"""
    d = tmp_path / "overlap-fake"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps({"数据集": "x", "episodes": {
        "epB": {"判决": "通过", "checks": {
            "任务成败判定": {"结果": "弃权", "detail": TS_DETAIL}}},
        "epC": {"判决": "通过", "checks": {
            "任务成败判定": {"结果": "弃权", "detail": TS_DETAIL}}}}},
        ensure_ascii=False))
    (d / "review.json").write_text(json.dumps({
        "episodes": {
            "epB": {"当前判决": "通过", "待裁决项": ["任务成败判定"],
                    "弃权原因": {"任务成败判定": "复核分裂"}},
            "epC": {"当前判决": "通过", "待裁决项": ["任务成败判定"],
                    "弃权原因": {"任务成败判定": "渐变问询不可判"}}},
        "标注-画面分歧复核队列": [
            {"id": "epA", "priority": "重点", "label": "open the door",
             "caption": "close the door", "reason": "方向相反"},
            {"id": "epB", "priority": "参考", "label": "pick cup",
             "caption": "pick bottle", "reason": "对象不一致"}]},
        ensure_ascii=False))
    return load_delivery(str(d))


def test_merged_queue_dedupes_overlap_into_one_card(tmp_path):
    """防的事故(droid-200-new 实见):同一条 episode 在标注分歧与成败弃权两个
    队列里各占一张卡,用户要各找一次、各看一遍视频。合并后:重叠条目只出一张卡
    且两个问题都在;非重叠的各出一张;顺序 = 分歧队列原序(重点档在前)+
    只有成败问题的接在后面。"""
    from curation.ui.manifest import merged_review_queue
    q = merged_review_queue(_overlap_delivery(tmp_path))
    assert [it["id"] for it in q] == ["epA", "epB", "epC"]   # epB 只出现一次
    by_id = {it["id"]: it for it in q}
    assert by_id["epA"]["audit"] is not None and by_id["epA"]["task"] is None
    assert by_id["epB"]["audit"] is not None and by_id["epB"]["task"] is not None
    assert by_id["epC"]["audit"] is None and by_id["epC"]["task"] is not None
    assert by_id["epB"]["audit"]["label"] == "pick cup"      # 两个问题的数据都全
    assert by_id["epB"]["task"]["reason"] == "复核分裂"


def test_merged_filter_sets_and_counts(tmp_path):
    """筛选三档:集合正确、计数正确;重叠条目在两个单项档里都出现(它确实两种
    问题都有),但每档内仍只有一次;带计数的显示标签能还原回档位。"""
    from curation.ui.manifest import (MERGE_FILTER_ALL, MERGE_FILTER_LABEL,
                                      MERGE_FILTER_TASK, merge_filter_mode,
                                      merged_filter_choices, merged_queue_view)
    m = _overlap_delivery(tmp_path)
    assert [it["id"] for it in merged_queue_view(m, MERGE_FILTER_ALL)] \
        == ["epA", "epB", "epC"]
    assert [it["id"] for it in merged_queue_view(m, MERGE_FILTER_LABEL)] \
        == ["epA", "epB"]
    assert [it["id"] for it in merged_queue_view(m, MERGE_FILTER_TASK)] \
        == ["epB", "epC"]
    choices = merged_filter_choices(m)
    assert choices == ["全部(3)", "只看标注问题(2)", "只看成败问题(2)"]
    assert merge_filter_mode(choices[0]) == MERGE_FILTER_ALL
    assert merge_filter_mode(choices[1]) == MERGE_FILTER_LABEL
    assert merge_filter_mode(choices[2]) == MERGE_FILTER_TASK
    assert merge_filter_mode(None) == MERGE_FILTER_ALL       # 认不出=宽档,不藏卡


def test_success_block_mode_three_rules(tmp_path):
    """②的显隐三条规矩 + 矛盾拦截(2026-08-16 用户定的表):
    机器弃权=必答;①采纳改标(纯标注条目)=可选;①维持/弃用或未裁=不显示;
    ①弃用时②一律不可用 —— 机器弃权的条目给 blocked 而不是 hidden,必答的问题
    凭空消失会让人以为页面坏了。"""
    from curation.ui.manifest import success_block_mode
    task_item = {"id": "e", "audit": None, "task": {"id": "e"}}
    both_item = {"id": "e", "audit": {"id": "e"}, "task": {"id": "e"}}
    audit_item = {"id": "e", "audit": {"id": "e"}, "task": None}
    # 机器弃权 → 必答;①怎么裁(除弃用)都不降档 —— 维持原标注不解决机器判不出
    for dec in ("", "维持原标注", "采纳建议改标"):
        assert success_block_mode(task_item, dec) == "required"
        assert success_block_mode(both_item, dec) == "required"
    # 纯标注条目:采纳改标 → 可选(留空=交给机器重判);其余 → 不显示
    assert success_block_mode(audit_item, "采纳建议改标") == "optional"
    for dec in ("", "维持原标注"):
        assert success_block_mode(audit_item, dec) == "hidden"
    # 矛盾拦截:弃用 → 机器弃权的给 blocked(可见但禁用),纯标注的直接隐藏
    assert success_block_mode(task_item, "弃用该条") == "blocked"
    assert success_block_mode(both_item, "弃用该条") == "blocked"
    assert success_block_mode(audit_item, "弃用该条") == "hidden"


def test_verdict_recording_blocked_after_drop_decision(tmp_path):
    """矛盾拦截的落盘那道门:①裁了「弃用该条」之后,②的结论不许被记下来 ——
    「这条不要了 + 判它成功」落进 CSV,rejudge 就会各按各的执行,交付自相矛盾。
    按钮禁用是渲染层的事,连点竞态/陈旧页面都可能绕过,落盘前必须再查一次。"""
    from curation.dataset_level.decisions import load_task_verdicts
    from curation.ui.manifest import (record_label_decision,
                                      record_task_verdict_checked)
    m = _overlap_delivery(tmp_path)
    record_label_decision(m["path"], "epB", "弃用该条")
    msg = record_task_verdict_checked(m, "epB", "判成功")
    assert "未记录" in msg and "弃用" in msg
    assert load_task_verdicts(m["path"]) == {}, "矛盾裁决被落盘了"
    # 改掉「弃用」之后照常可裁
    record_label_decision(m["path"], "epB", "维持原标注")
    assert "已记录" in record_task_verdict_checked(m, "epB", "判成功")
    assert load_task_verdicts(m["path"])["epB"]["verdict"] == "判成功"


def test_decision_csv_schema_is_frozen(tmp_path):
    """裁决 CSV 的字段与语义一个字节不许变:老交付回放、rejudge 幂等跳过、
    「沿用」判定全靠这三张表的既有列名与列序。表头变一个字,存量裁决就读不回。"""
    import csv as _csv

    from curation.dataset_level.decisions import (record_label_decision,
                                                  record_reject_appeal,
                                                  record_task_verdict)
    d = str(tmp_path)
    record_label_decision(d, "ep1", "采纳建议改标", new_label="new", note="n")
    record_task_verdict(d, "ep2", "判成功", note="n")
    record_reject_appeal(d, "ep3", "捞回", note="n")
    hd = tmp_path / "human-decisions"
    expect = {"label_decisions.csv": ["episode_id", "decision", "new_label",
                                      "note", "at"],
              "task_verdicts.csv": ["episode_id", "verdict", "note", "at"],
              "reject_appeals.csv": ["episode_id", "appeal", "note", "at"]}
    for name, fields in expect.items():
        with open(hd / name, newline="", encoding="utf-8") as f:
            rows = list(_csv.reader(f))
        assert rows[0] == fields, f"{name} 表头变了"
        assert len(rows) == 2 and len(rows[1]) == len(fields)


# ───────── 被拒复议区(2026-08-11):语义判定的杀可复议,物理硬门不进这里 ─────────


def test_appeal_queue_takes_semantic_kills_only(delivery, tmp_path):
    """复议区的准入:只收「任务成败判定」拒掉的条目。

    防的是把物理与结构硬门混进复议区——时间戳残段/运动学超限/同步判废都是测出来的
    事实,给它们开一个"人工捞回"的口子,交出去的就是坏数据。fixture 里 ep000001
    正是老交付记法(硬门违规: 「任务成败判定」),老交付也必须复议得了。
    """
    from curation.ui.manifest import appeal_rows
    m = load_delivery(delivery)
    assert [a["id"] for a in m["reject_appeal"]] == ["ep000001"]
    assert m["reject_appeal"][0]["readings"] == {"voc": 0.87, "末态分": 0.3}
    rows = appeal_rows(m)
    # 行结构:[操作, episode, 拒绝原因, 关键读数, 复议结论]
    assert rows[0][0] == "复议" and rows[0][1] == "ep000001"
    assert "未通过" in rows[0][2] and "硬门" not in rows[0][2]   # 界面不出现机制黑话
    assert "voc=0.87" in rows[0][3] and rows[0][4] == ""          # 未复议

    # 物理硬门拒掉的条目:一条都不许进
    d2 = tmp_path / "phys-reject"
    d2.mkdir()
    (d2 / "passed.json").write_text(json.dumps({"数据集": "x", "episodes": {}},
                                               ensure_ascii=False))
    (d2 / "reject.json").write_text(json.dumps({"episodes": {
        "ep1": {"判决": "拒绝", "原因": "未通过「时间戳检查」:0.47 秒的采集残段"},
        "ep2": {"判决": "拒绝", "原因": "未通过「运动学极限」:关节 3 超限"},
        "ep3": {"判决": "拒绝", "原因": "未通过「视频-动作同步」:整体错位 0.4 秒"},
        "ep4": {"判决": "拒绝(去重)", "原因": "与 ep000007 字节级完全重复"},
        "ep5": {"判决": "拒绝", "原因": "未通过「任务成败判定」:没完成;"
                                        "未通过「时间戳检查」:残段"}}},
        ensure_ascii=False))
    m2 = load_delivery(str(d2))
    assert m2["reject_appeal"] == [], "物理/结构硬门的拒绝混进了复议区"


def test_appeal_draft_roundtrip_and_guards(delivery):
    """复议草稿落盘/读回/改判 + 回显进表;复议词只认两选一,空 id 不落盘。"""
    from curation.ui.manifest import (appeal_pending_count, appeal_rows,
                                      load_reject_appeals, record_reject_appeal)
    m = load_delivery(delivery)
    assert load_reject_appeals(m) == {} and appeal_pending_count(m) == 1
    msg = record_reject_appeal(m["path"], "ep000001", "捞回", note="看了视频,完成了")
    assert msg.startswith("已记录(随时可改判)")   # 2026-08-25 用户精简状态行
    got = load_reject_appeals(m)
    assert got["ep000001"]["appeal"] == "捞回"
    assert got["ep000001"]["note"] == "看了视频,完成了" and got["ep000001"]["at"]
    assert appeal_rows(m)[0][4] == "捞回" and appeal_pending_count(m) == 0
    record_reject_appeal(m["path"], "ep000001", "维持拒绝")      # 改判=追加,后写覆盖
    assert load_reject_appeals(m)["ep000001"]["appeal"] == "维持拒绝"
    assert "未记录" in record_reject_appeal(m["path"], "ep1", "放它一马")
    assert "未记录" in record_reject_appeal(m["path"], "", "捞回")


def test_appeal_hint_is_empty_when_nothing_to_appeal(delivery, tmp_path):
    """有条目时提示说清"有几条、还剩几条没复核、能做什么";一条都没有时给空串
    (调用侧据此整区不渲染——空区块占位只会让人以为自己漏看了)。

    2026-08-16 用户改措辞:①不用"捞回"这种口语,说"恢复为可用";②"哪些拒因不能
    复议"的范围说明不在正文重复 —— 页签名「任务失败复议」已承担这层意思,详细
    解释挪进页内默认收起的折叠条。所以这里**反过来钉**:那两种旧说法不许回潮。
    """
    from curation.ui.manifest import appeal_hint_md
    m = load_delivery(delivery)
    hint = appeal_hint_md(m)
    assert "1" in hint and "恢复为可用" in hint
    assert "捞回" not in hint and "终局" not in hint
    d2 = tmp_path / "no-reject"
    d2.mkdir()
    (d2 / "passed.json").write_text(json.dumps({"数据集": "x", "episodes": {}},
                                               ensure_ascii=False))
    assert appeal_hint_md(load_delivery(str(d2))) == ""


def test_appeal_draft_does_not_collide_with_other_decision_lines(tmp_path):
    """三条裁决线各写各的表:复议按一下,不许把上一轮的成败裁决/标注裁决抹掉。"""
    from curation.dataset_level.decisions import (load_label_decisions,
                                                  load_reject_appeals,
                                                  load_task_verdicts,
                                                  record_label_decision,
                                                  record_reject_appeal,
                                                  record_task_verdict)
    d = str(tmp_path)
    record_label_decision(d, "epA", "维持原标注")
    record_task_verdict(d, "epB", "判失败")
    record_reject_appeal(d, "epB", "捞回")              # 同一 episode 也不许串表
    assert set(load_label_decisions(d)) == {"epA"}
    assert load_task_verdicts(d)["epB"]["verdict"] == "判失败"
    assert set(load_reject_appeals(d)) == {"epB"}


def test_discover_deliveries_recursive(tmp_path):
    """递归发现(2026-08-06):嵌套目录里的交付也要被找到;交付内部不再往里钻。"""
    import json as _json
    deep = tmp_path / "experiments" / "run1"
    deep.mkdir(parents=True)
    (deep / "passed.json").write_text("{}")
    (deep / "details").mkdir()                      # 交付内部子目录,不该被当交付扫
    flat = tmp_path / "flat-delivery"
    flat.mkdir()
    (flat / "passed.json").write_text("{}")
    (tmp_path / "empty-dir").mkdir()                # 无 passed.json:不出现
    found = discover_deliveries(str(tmp_path))
    assert str(deep) in found and str(flat) in found
    assert len(found) == 2


def test_decision_survives_fsx_visibility_gap(tmp_path):
    """FSX 新文件 ~45s 读回为空:整写方案若无进程内缓存,连裁两条会把第一条冲掉
    (2026-08-06 生产 EINVAL 修复的伴生坑)。模拟:第一条落盘后把文件清空(装作
    还看不见),再裁第二条——两条都必须在。"""
    from curation.dataset_level.decisions import (load_label_decisions,
                                                  record_label_decision)
    d = str(tmp_path)
    record_label_decision(d, "ep000001", "维持原标注", note="第一条")
    csv_path = tmp_path / "human-decisions" / "label_decisions.csv"
    written = csv_path.stat().st_mtime_ns
    csv_path.write_text("")                      # 模拟 FSX 可见延迟:读回是空的
    os.utime(csv_path, ns=(written, written))    # 且 mtime 还是旧的(理由见上面裁决版的同款测试)
    record_label_decision(d, "ep000002", "弃用该条", note="第二条")
    got = load_label_decisions(d)
    assert set(got) == {"ep000001", "ep000002"}, "延迟窗口内第一条裁决被冲掉"


def test_latency_union_wall_and_parity(tmp_path):
    """墙钟=忙碌区间并集(分段类别不把空档灌进来);UI 复算与管道实现对拍一致。"""
    import csv

    from curation.adapters.vlm_client import latency_summary
    from curation.ui.manifest import _recompute_latency
    # caption 两波:0-10s 与 100-110s(中间 90s 空档);probe 连续 0-20s 两条重叠
    rows = [("caption", 10.0, True, 0.0), ("caption", 10.0, True, 100.0),
            ("probe", 20.0, True, 0.0), ("probe", 15.0, True, 5.0)]
    pipe = latency_summary(rows)
    assert pipe["caption"]["wall_s"] == 20.0, "空档被灌进墙钟(应为两段各10s)"
    assert pipe["probe"]["wall_s"] == 20.0
    p = tmp_path / "vlm_latency.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["call_type", "seconds", "ok", "started_at"])
        for t, s, ok, st in rows:
            w.writerow([t, s, int(ok), st])
    ui = _recompute_latency(str(p))
    for tag in ("caption", "probe"):
        assert ui[tag] == pipe[tag], f"{tag}: UI 复算与管道实现不一致"


# ───────── Episodes 页被拒展示重做 + 同步曲线页(2026-08-07)─────────
#
# 重做的根因(用户原话"目前的显示很差"):小尺寸证据帧与**超宽的同步曲线长图**
# 被塞进同一个 4 列画廊,曲线被压成四分之一格必然糊掉。以下测试钉三件事:
# ① 判决卡说清"谁毙的、为什么";② 证据帧与曲线是两个组件;
# ③ 逐相机同步读数在**老交付缺字段时优雅降级**(这条是本轮最容易出线上事故的)。

#: 数据层新契约(逐相机)。两路相机:外部相机1 被标注,腕部相机 测不准。
SYNC_DETAIL_NEW = {
    "verdict": "annotated",
    "per_camera": {
        "外部相机1": {"lag_s": 0.24, "corr_peak": 0.81, "corr_at_zero": 0.30,
                      "peak_ratio": 2.7, "peak_width_s": 0.35, "trusted": True,
                      "code": "lag_beyond_tol", "note": "峰值落在 0.24s,超出容差"},
        "腕部相机": {"lag_s": None, "corr_peak": 0.11, "corr_at_zero": 0.09,
                     "peak_ratio": 1.05, "peak_width_s": None, "trusted": False,
                     "code": "weak_signal", "note": "相关太弱,读数不可信"}},
    "flagged_cameras": ["外部相机1"],
    "consensus_lag_s": None, "n_cameras": 2, "n_trusted": 1,
    "reason": "仅 1 路可信相机报异常,不足以判废,按标注处理",
}

#: 老交付的同步 detail:只有平铺读数,没有 per_camera / verdict。
SYNC_DETAIL_OLD = {"lag_s": 0.12, "corr_peak": 0.77}

SYNC_HEALTH = {
    "per_camera": {"外部相机1": {"n": 3, "median_lag_s": 0.22, "iqr_s": 0.04,
                                 "n_flagged": 2},
                   "腕部相机": {"n": 3, "median_lag_s": 0.01, "iqr_s": 0.02,
                                "n_flagged": 0}},
    "advice": "外部相机1 整体滞后约 0.22s,建议重新标定采集时钟",
    "negative_lag_episodes": ["ep000002"],
}

SYNC_CHECK_CN = "视频-动作同步"          # report.py 的 CHECK_CN 里的名字


def _with_sync(path, detail=SYNC_DETAIL_NEW, health=SYNC_HEALTH, state="pass"):
    """给 fixture 交付的三条 episode 都挂上同步检查 + 数据集级 sync_health。

    ep000001(被拒)那条额外给个 plot(fixture 里本来就有),用来测曲线页。
    """
    for fname, key in (("passed.json", "episodes"), ("reject.json", "episodes")):
        p = os.path.join(path, fname)
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        for ep in (doc.get(key) or {}).values():
            ep.setdefault("checks", {})[SYNC_CHECK_CN] = {
                "结果": state, "detail": json.dumps(detail, ensure_ascii=False)}
        if fname == "passed.json" and health is not None:
            doc.setdefault("dataset", {})["sync_health"] = health
        with open(p, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
    return load_delivery(path)


def test_sync_camera_rows_assemble_per_camera_readings(delivery):
    """逐相机读数:一相机一行、被标注的相机有可视标记、不可信如实写「不可信」。"""
    from curation.ui.manifest import (SYNC_CAM_HEADERS, sync_camera_rows,
                                      sync_detail)
    m = _with_sync(delivery)
    rows = sync_camera_rows(m, "ep000001")
    assert len(rows) == 2 and len(rows[0]) == len(SYNC_CAM_HEADERS)
    by_cam = {r[0]: r for r in rows}
    assert by_cam["外部相机1"][1] == "⚠ 已标注"        # flagged_cameras 里的那路
    assert by_cam["腕部相机"][1] == ""                  # 没被标注就不加噪声
    assert by_cam["外部相机1"][2] == "0.240" and by_cam["外部相机1"][7] == "可信"
    assert by_cam["腕部相机"][2] == "—"                 # lag_s=None → 「—」,不是 0
    assert by_cam["腕部相机"][7] == "不可信"
    assert "相关太弱" in by_cam["腕部相机"][8]
    assert sync_detail(m, "ep000001")["verdict"] == "annotated"


def test_sync_camera_html_states_the_never_discard_semantics(delivery):
    """展示文案必须与用户拍板的语义一致:标注不判废、弃权不进裁决队列不进质量分。"""
    from curation.ui.manifest import sync_camera_html
    m = _with_sync(delivery)
    html = sync_camera_html(m, "ep000001")
    assert "已标注异常(不判废)" in html
    assert "永不废弃相机" in html
    assert "外部相机1" in html and "腕部相机" in html
    assert "仅 1 路可信相机报异常" in html               # reason 原样露出
    assert "相机 2 路(可信 1 路)" in html
    # 判废语义(仅当所有可信相机一致指向同一偏移)也要说得出口
    m2 = _with_sync(delivery, detail={**SYNC_DETAIL_NEW, "verdict": "misaligned_all"})
    assert "所有可信相机一致指向同一个偏移" in sync_camera_html(m2, "ep000001")
    m3 = _with_sync(delivery, detail={**SYNC_DETAIL_NEW, "verdict": "undecidable"})
    h3 = sync_camera_html(m3, "ep000001")
    assert "不进人工裁决队列" in h3 and "不参与综合质量分" in h3


def test_sync_degrades_on_legacy_delivery(delivery):
    """★红线:老交付没有 per_camera —— 一句人话降级,绝不崩、绝不假装有逐相机数据。"""
    from curation.ui.manifest import (LEGACY_SYNC_NOTE, sync_camera_html,
                                      sync_camera_rows, sync_detail)
    m = _with_sync(delivery, detail=SYNC_DETAIL_OLD, health=None)
    assert sync_camera_rows(m, "ep000001") == []
    html = sync_camera_html(m, "ep000001")
    assert LEGACY_SYNC_NOTE in html
    assert "0.120" in html                       # 老读数照样摊开(有什么给什么)
    assert "<table" not in html                  # 没有逐相机就不画空表
    # detail 是坏字符串 / 缺 verdict 也不许炸
    broken = _with_sync(delivery, detail={"per_camera": "不是字典"}, health=None)
    assert sync_camera_rows(broken, "ep000001") == []
    assert LEGACY_SYNC_NOTE in sync_camera_html(broken, "ep000001")
    assert sync_detail(broken, "查无此条") == {}


def test_sync_block_speaks_up_when_check_never_ran(delivery):
    """压根没跑同步检查的条目:给一句话,不许空白(空白看起来像页面坏了)。"""
    from curation.ui.manifest import sync_camera_html, sync_camera_rows, sync_detail
    m = load_delivery(delivery)                  # fixture 原样,没有同步检查
    assert sync_camera_rows(m, "ep000000") == [] and sync_detail(m, "ep000000") == {}
    assert "没有同步读数" in sync_camera_html(m, "ep000000")


def test_episode_card_names_the_fatal_check(delivery):
    """判决卡:大徽章 + 判决 + 一句人话理由,理由里点名是哪一项没过。"""
    from curation.ui.manifest import (episode_card_html, episode_reason_text,
                                      episode_verdict_label, fatal_checks)
    m = load_delivery(delivery)
    assert fatal_checks(m, "ep000001") == ["任务成败判定"]
    assert episode_reason_text(m, "ep000001") == "渐变问询不可判"
    card = episode_card_html(m, "ep000001")
    assert "⛔" in card and "ep000001" in card
    assert "⛔ 拒绝" not in card                       # issue #59:徽章只留图标
    # 检查名 + **该检查自己写的人话**:只报检查名会把人带偏(ep000018 教训)
    assert "未通过「任务成败判定」:渐变问询不可判" in card
    assert "硬门" not in card and "0.940" in card             # 黑话已清除 · 质量分
    # 待裁决优先于当前判决(系统还没定论时先叫人上)
    assert episode_verdict_label(m["episodes"]["ep000000"]) == "待裁决"
    card0 = episode_card_html(m, "ep000000")
    assert "⏳" in card0 and "待裁决" not in card0    # 按桶取样式:判决"通过"但待人工 → ⏳ 不是 ✅
    assert "✅" not in card0 and "拿不准" in card0
    # 没选中 / 查无此条:不崩,给引导语
    assert "选一条 episode" in episode_card_html(m, "")
    assert "选一条 episode" in episode_card_html(m, "查无此条")


def test_episode_card_notes_sync_abstention_is_only_an_annotation(delivery):
    """同步测不准出现在卡片上时,必须当面写清它不进裁决队列、不进质量分。"""
    from curation.ui.manifest import episode_card_html
    m = _with_sync(delivery, detail={**SYNC_DETAIL_NEW, "verdict": "undecidable"})
    card = episode_card_html(m, "ep000002")
    assert "同步测不准仅作标注" in card
    assert "不进人工裁决队列" in card and "不参与综合质量分" in card
    # 正常同步的条目不挂这句(不该给每条都加噪声)
    m2 = _with_sync(delivery, detail={**SYNC_DETAIL_NEW, "verdict": "aligned"})
    assert "同步测不准" not in episode_card_html(m2, "ep000002")


def test_check_table_html_highlights_the_rejected_dimension(delivery):
    """逐维读数表仍来自 check_rows,但被拒的那一维整行标红(要一眼看得见)。"""
    from curation.ui.manifest import CHECK_HEADERS, check_rows, check_table_html
    m = load_delivery(delivery)
    html = check_table_html(m, "ep000001")
    for h in CHECK_HEADERS:
        assert h in html
    rows = check_rows(m, "ep000001")
    assert [r[0] for r in rows] == ["任务成败判定", "打标"]
    assert html.count("#FFECE8") == 1                    # 红底只给被拒那一行(打标行不标)
    assert "渐变问询不可判" in html
    # 通过条目:一行红都没有
    assert "#FFECE8" not in check_table_html(m, "ep000002")
    assert "没有记录逐维读数" in check_table_html(m, "")   # 空态不崩


def test_bucket_split_is_exhaustive_and_disjoint(delivery):
    """三桶:互斥且穷尽,未知桶名退回全部(前端能塞任意值,不该因此给空清单)。"""
    from curation.ui.manifest import (BUCKET_ALL, BUCKET_PASSED, BUCKET_PENDING,
                                      BUCKET_REJECTED, bucket_counts, bucket_ids,
                                      episode_bucket)
    m = load_delivery(delivery)
    assert episode_bucket(m, "ep000001") == BUCKET_REJECTED     # 判决拒绝
    assert episode_bucket(m, "ep000000") == BUCKET_PENDING      # 系统弃权待裁决
    assert episode_bucket(m, "ep000002") == BUCKET_PENDING      # 在标注分歧队列里
    c = bucket_counts(m)
    assert c == {BUCKET_PASSED: 0, BUCKET_REJECTED: 1, BUCKET_PENDING: 2,
                 BUCKET_ALL: 3}
    assert bucket_ids(m, BUCKET_REJECTED) == ["ep000001"]
    assert bucket_ids(m, BUCKET_PENDING) == ["ep000000", "ep000002"]
    assert bucket_ids(m, "乱传的桶名") == bucket_ids(m, BUCKET_ALL) == \
        ["ep000000", "ep000001", "ep000002"]


def test_sync_view_gallery_items_carry_episode_and_badge(delivery):
    """曲线页:每张的标题 = episode 号 + 同步判定徽章;只看有标注/异常可筛。"""
    from curation.ui.manifest import (SYNC_FILTER_ALL, SYNC_FILTER_FLAGGED,
                                      sync_plot_items, sync_view)
    m = _with_sync(delivery)                    # fixture 只有 ep000001 有曲线图
    items = sync_plot_items(m)
    assert [it["id"] for it in items] == ["ep000001"]
    assert items[0]["path"].endswith("ep000001_sync.png")
    v = sync_view(m, SYNC_FILTER_ALL, 0)
    assert v["items"] == [(items[0]["path"], "ep000001 · 已标注异常(不判废)")]
    assert v["note"] == "" and v["pos"] == ""     # 说明性文字 2026-09-01 用户点名删;一页不显示页码
    # aligned 且无标注相机 → 不算"有标注/异常",筛选后为空并给出指路
    clean = _with_sync(delivery, detail={"verdict": "aligned", "per_camera": {},
                                         "flagged_cameras": []})
    assert sync_plot_items(clean, SYNC_FILTER_FLAGGED) == []
    assert "切到「全部」" in sync_view(clean, SYNC_FILTER_FLAGGED, 0)["note"]
    assert sync_view(clean, SYNC_FILTER_ALL, 0)["items"][0][1] == "ep000001 · 同步正常"
    # aligned 但有弃权路(候选并列/画面不锐利/覆盖不足/信号弱)→ 必须进筛选
    # (2026-09-01 用户抓出:droid-50 有 6 条只有弃权路标注的 episode 在筛选下隐身)
    abst = _with_sync(delivery, detail={"verdict": "aligned", "per_camera": {},
                                        "flagged_cameras": [],
                                        "abstained_cameras": ["cam_a"]})
    assert [it["id"] for it in sync_plot_items(abst, SYNC_FILTER_FLAGGED)] == \
        ["ep000001"]


def test_sync_view_pages_and_wraps(delivery):
    """图多时分页撑住:每页 page_size 张,页码越界回绕(与裁决卡片同款)。"""
    from curation.ui.manifest import SYNC_FILTER_ALL, sync_view
    plots = os.path.join(delivery, "details", "plots")
    for eid in ("ep000000", "ep000002"):        # ep000001 的图 fixture 里已有 → 共 3 张
        with open(os.path.join(plots, f"{eid}_sync.png"), "wb") as f:
            f.write(b"\x89PNGfake")
    m = _with_sync(delivery)
    v = sync_view(m, SYNC_FILTER_ALL, 0, page_size=1)
    assert v["pages"] == 3 and len(v["items"]) == 1 and v["pos"] == "第 1 / 3 页"
    assert sync_view(m, SYNC_FILTER_ALL, 2, page_size=1)["page"] == 2
    assert sync_view(m, SYNC_FILTER_ALL, 3, page_size=1)["page"] == 0    # 回绕
    assert sync_view(m, SYNC_FILTER_ALL, -1, page_size=1)["page"] == 2
    ids = [c.split(" · ")[0] for _, c in sync_view(m, SYNC_FILTER_ALL, 1,
                                                   page_size=1)["items"]]
    assert ids == ["ep000001"]                            # 按 id 升序切页


def test_sync_view_empty_state_is_one_plain_sentence(tmp_path):
    """交付里没有 plots → **一句话**说明,不夹带配置开关。

    2026-08-13 用户点名:`pipeline.sync_plots` 是实现细节,客户既不知道去哪改、
    也不该被要求知道(要改的人在任务台「更多设置」里点)。这条钉住别再加回去。
    """
    from curation.ui.manifest import NO_PLOTS_NOTE, sync_view
    d = tmp_path / "noplots"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps(
        {"数据集": "x", "episodes": {"ep0": {"判决": "通过", "checks": {}}}},
        ensure_ascii=False), encoding="utf-8")
    v = sync_view(load_delivery(str(d)))
    assert v["items"] == [] and v["pages"] == 1
    assert v["note"] == NO_PLOTS_NOTE
    assert "pipeline.sync_plots" not in NO_PLOTS_NOTE     # 不写配置键名
    for mode in ("flagged", "all", "off"):
        assert mode not in NO_PLOTS_NOTE                  # 也不写三挡的英文取值
    assert NO_PLOTS_NOTE.count("。") == 1                  # 就一句


def test_sync_health_block_and_legacy_degradation(delivery):
    """数据集级 lag 分布 + 建议露出一处;老交付整块降级成一句话,不崩。"""
    from curation.ui.manifest import (LEGACY_SYNC_NOTE, SYNC_HEALTH_HEADERS,
                                      sync_health_html, sync_health_rows)
    m = _with_sync(delivery)
    rows = sync_health_rows(m)
    assert [r[0] for r in rows] == ["外部相机1", "腕部相机"]
    # 列序:相机/有效读数/典型滞后/逐条波动/疑似错位/测不准/已标注
    assert rows[0][1] == 3 and rows[0][2] == "0.220" and rows[0][-1] == 2
    assert len(rows[0]) == len(SYNC_HEALTH_HEADERS)
    assert "四分位距" not in " ".join(SYNC_HEALTH_HEADERS)   # 统计黑话不进界面
    html = sync_health_html(m)
    for h in SYNC_HEALTH_HEADERS:
        assert h in html
    assert "建议:外部相机1 整体滞后约 0.22s" in html          # advice 原样
    assert "负滞后" in html and "ep000002" in html
    # 老交付(无 sync_health):一句降级说明,不画空表
    old = _with_sync(delivery, health=None)
    old["dataset"].pop("sync_health", None)
    h2 = sync_health_html(old)
    assert LEGACY_SYNC_NOTE in h2 and "<table" not in h2
    assert sync_health_rows(old) == []
    assert sync_health_rows({"dataset": {"sync_health": {"per_camera": "坏结构"}}}) == []


def _sync_ep(verdict, flagged=(), state="pass"):
    import json
    return {"checks": {"视频-动作同步": {"结果": state, "detail": json.dumps(
        {"verdict": verdict, "per_camera": {}, "flagged_cameras": list(flagged),
         "n_cameras": 3, "n_trusted": 2, "reason": ""}, ensure_ascii=False)}},
        "plot": "/x/p.png"}


def test_sync_conclusion_states():
    """结论横幅四态:全绿 / 有标注 / 测不准 / 负滞后告警。用户点名:光有图没有提示。"""
    from curation.ui.manifest import sync_conclusion
    ok = sync_conclusion({"episodes": {"e1": _sync_ep("aligned")}, "dataset": {}})
    assert ok["level"] == "ok" and "未发现" in ok["title"]
    assert any("可直接用于" in p for p in ok["points"])

    flag = sync_conclusion({"episodes": {"e1": _sync_ep("annotated", ["cam_a"])},
                            "dataset": {}})
    assert flag["level"] == "notice"
    assert any("视频一路没删" in p for p in flag["points"]), "必须讲明不删相机"

    und = sync_conclusion({"episodes": {"e1": _sync_ep("undecidable")}, "dataset": {}})
    assert any("不是" in p and "质量问题" in p for p in und["points"])

    neg = sync_conclusion({"episodes": {"e1": _sync_ep("aligned")},
                           "dataset": {"sync_health": {
                               "negative_lag_episodes": ["ep000001"]}}})
    assert neg["level"] == "attention"
    assert any("装配" in p for p in neg["points"]), "负滞后要指向装配环节"


def test_sync_conclusion_html_escapes_and_bolds():
    from curation.ui.manifest import sync_conclusion_html
    html = sync_conclusion_html({"episodes": {}, "dataset": {
        "sync_health": {"advice": "全库中位滞后 <0.1s>"}}})
    assert "&lt;0.1s&gt;" in html, "文案未转义"
    assert "<b>" in html and "<ul" in html


def test_sync_diag_panel_names_cause_and_survives_legacy():
    """每张曲线右侧的诊断框:说病因、给建议;老交付无 diagnosis 时退回 note 不崩。"""
    from curation.ui.manifest import _diag_rows, sync_cards_html, sync_diag_html

    detail = {"per_camera": {
        "ext1": {"lag_s": 0.6, "trusted": False, "code": "ambiguous_peak",
                 "diagnosis": {"cause": "false_peak", "label": "测不准 · 画面干扰",
                               "text": "峰赢不过 0", "advice": "固定相机"}},
        "wrist": {"lag_s": 0.0, "trusted": True, "code": "aligned",
                  "diagnosis": {"cause": "aligned", "label": "对齐",
                                "text": "峰落在 0 附近", "advice": ""}},
    }}
    rows = _diag_rows(detail)
    assert [r["cam"] for r in rows] == ["ext1", "wrist"]
    assert rows[0]["lag"] == "+0.60s" and rows[0]["label"] == "测不准 · 画面干扰"
    assert rows[0]["color"] != rows[1]["color"]       # 病因不同,圆点不同色
    html = sync_diag_html(rows)
    assert "画面干扰" in html and "固定相机" in html and "对齐" in html

    # 老交付:没有 diagnosis 字段 → 用 note 兜底,不抛
    legacy = {"per_camera": {"cam": {"lag_s": None, "trusted": False,
                                     "note": "旧版本读数"}}}
    assert "旧版本读数" in sync_diag_html(_diag_rows(legacy))
    assert _diag_rows({}) == [] and sync_diag_html([]) == ""

    # 诊断框必须真的进卡片 HTML,且在图片之后(视觉上位于右侧)
    card = sync_cards_html([{"id": "ep000004", "path": "/tmp/x.png", "badge": "同步正常",
                             "color": "#009A29", "flagged": False,
                             "cameras": rows, "reason": "1/3 路可信相机全部对齐"}])
    assert "sync-diag" in card and "画面干扰" in card
    assert card.index("sync-img") < card.index("sync-diag-title")


def test_sync_filter_catches_diagnosed_but_aligned_episode():
    """整条判 aligned、可某一路被诊断出毛病的,必须进「只看标注/异常的」。

    2026-08-07 用户在 ep4 上问"应不应该放进去"——应该:结论没问题不等于没有
    值得复查的东西,那一路的峰肉眼可见地偏了,正是他第一个想点开看的条目。
    """
    from curation.ui.manifest import (SYNC_FILTER_FLAGGED, sync_plot_items)

    def _ep(detail):
        return {"plot": "/tmp/p.png",
                "checks": {"视频-动作同步": {"state": "pass", "detail": detail}}}

    m = {"episodes": {
        "ep000000": _ep({"verdict": "aligned", "per_camera": {}}),
        "ep000004": _ep({"verdict": "aligned", "per_camera": {},
                         "noisy_cameras": ["ext1"]}),
        "ep000005": _ep({"verdict": "aligned", "per_camera": {},
                         "suspect_cameras": ["ext2"]}),
    }}
    got = [it["id"] for it in sync_plot_items(m, SYNC_FILTER_FLAGGED)]
    assert got == ["ep000004", "ep000005"]          # 干净的那条不进
    assert len(sync_plot_items(m, "全部")) == 3


def test_sync_view_note_carries_no_explainer_prose():
    """曲线页不再打印「共 N 张曲线…」与 ⚠️ 覆盖范围说明(2026-09-01 用户点名删);
    只留两句**状态**:空态(没有 plots)与筛选空(切到「全部」指路)。"""
    from curation.ui.manifest import sync_view

    def _ep(has_plot):
        e = {"checks": {"视频-动作同步": {"state": "pass",
                                          "detail": {"verdict": "aligned"}}}}
        if has_plot:
            e["plot"] = "/tmp/p.png"
        return e

    m = {"config_effective": {"pipeline": {"sync_plots": "flagged"}},
         "episodes": {f"ep{i:06d}": _ep(i < 2) for i in range(7)}}
    v = sync_view(m, "全部")
    assert v["note"] == ""
    assert "只为需要留意的条目" not in v["note"] and "共 " not in v["note"]


def test_sync_banner_wording_matches_the_actual_diagnosis():
    """横幅不许自相矛盾:假峰不是"被标注异常"(2026-08-07 实见标题说正常、
    条目说有异常)。三种成因各有各的措辞与级别。"""
    from curation.ui.manifest import sync_conclusion

    def _m(detail):
        return {"episodes": {"ep0": {"plot": "/tmp/p.png", "checks": {
            "视频-动作同步": {"state": "pass", "detail": detail}}}}}

    noisy = sync_conclusion(_m({"verdict": "aligned", "noisy_cameras": ["c"]}))
    assert noisy["level"] == "ok" and "假峰" in " ".join(noisy["points"])
    assert "测不准" in noisy["title"] and "标注异常" not in noisy["title"]

    flagged = sync_conclusion(_m({"verdict": "annotated", "flagged_cameras": ["c"]}))
    assert flagged["level"] == "notice" and "标注异常" in flagged["title"]

    suspect = sync_conclusion(_m({"verdict": "annotated", "suspect_cameras": ["c"]}))
    assert suspect["level"] == "notice" and "疑似错位" in suspect["title"]

    clean = sync_conclusion(_m({"verdict": "aligned"}))
    assert clean["level"] == "ok" and clean["title"].startswith("同步正常")
    assert "假峰" not in " ".join(clean["points"])


# ───────── Episodes 页整页改版(2026-08-11):三桶 + 左清单右详情 + 视频区 ─────────
#
# 改版起因(用户与其同事拍板):旧页是七列大表 + 三档筛选 + 证据帧画廊 + 曲线 +
# 三路切片,客户其实只问三件事——哪些过了、哪些被拒、哪些等着人来定。以下测试钉住
# 三处最容易做错的地方:
# ① 桶口径(去重删除的条目必须落在**拒绝**桶,理由说"重复"——它画面没毛病,
#    但确实出局了;把它算进通过桶就是虚报交付量);
# ② 视频来源链的顺序与**序号换算**(交付集是重编号的,序号猜错 = 播的是别人的
#    视频,比没有视频更糟 → 定不下来就诚实不给);
# ③ 措辞:"软分""硬门"是机制黑话,用户点名清除,界面上一个字都不许剩。


@pytest.fixture
def ep_delivery(tmp_path):
    """五条 episode 覆盖全部三桶:通过 / 拒绝 / 拒绝(去重)/ 弃权待裁 / 标注分歧。"""
    d = tmp_path / "droid-buckets"
    (d / "details").mkdir(parents=True)
    (d / "passed.json").write_text(json.dumps({
        "数据集": "droid_buckets", "机器人": "franka",
        "生成时间": "2026-08-11 08:00:00", "代码版本": "abc1234",
        "dataset": {"input_episodes": 5, "hard_gate_filtered": 1,
                    "verdict_keep": 4, "verdict_drop": 1, "dedup_removed": 1,
                    "delivered": 3, "hard_fail_breakdown": {"task_success": 1},
                    "summary_stats": {"pass_rate_pct": 60.0, "avg_soft_score": 0.9}},
        "episodes": {
            "ep000000": {"判决": "通过", "综合软分": 0.93,
                         "checks": {"运动质量": {"结果": "软分", "score": 0.86}}},
            "ep000001": {"判决": "通过", "综合软分": 0.88, "checks": {}},
            "ep000003": {"判决": "通过", "综合软分": 0.9, "checks": {}},
            "ep000004": {"判决": "通过", "综合软分": 0.91, "checks": {}}},
    }, ensure_ascii=False), encoding="utf-8")
    (d / "reject.json").write_text(json.dumps({"被拒总数": 2, "episodes": {
        "ep000002": {"判决": "拒绝", "原因": "硬门违规: 「任务成败判定」",
                     "综合软分": 0.4,
                     "checks": {"任务成败判定": {"结果": "拒绝", "detail": json.dumps(
                         {"reason": "末态未完成"})}}},
        "ep000003": {"判决": "拒绝(去重)", "原因": "与 ep000000 字节级完全重复",
                     "checks": {}}}}, ensure_ascii=False), encoding="utf-8")
    (d / "review.json").write_text(json.dumps({
        "待人工裁决总数": 1,
        "episodes": {"ep000001": {"当前判决": "通过", "待裁决项": ["任务成败判定"],
                                  "弃权原因": {"任务成败判定": "渐变问询不可判"}}},
        "标注-画面分歧复核队列": [{"id": "ep000004", "label": "open the door",
                                   "caption": "put the pot", "reason": "跨族分歧"}]},
        ensure_ascii=False), encoding="utf-8")
    return str(d)


def _curated(path, *, episodes: int, version: str = "v2.0", cams=("cam_left",),
             health_map: dict | None = None):
    """在交付目录里造一个 lerobot_curated(v2 逐条 mp4;health_map 给精确序号映射)。"""
    root = os.path.join(path, "lerobot_curated")
    os.makedirs(os.path.join(root, "meta"), exist_ok=True)
    with open(os.path.join(root, "meta", "info.json"), "w") as f:
        json.dump({"codebase_version": version, "total_episodes": episodes}, f)
    for cam in cams:
        vd = os.path.join(root, "videos", "chunk-000", f"observation.images.{cam}")
        os.makedirs(vd, exist_ok=True)
        for i in range(episodes):
            with open(os.path.join(vd, f"episode_{i:06d}.mp4"), "wb") as f:
                f.write(b"\x00\x00\x00 ftypisom")
    hp = os.path.join(root, "meta", "curation_camera_health.json")
    if health_map:
        with open(hp, "w") as f:
            json.dump({"episodes": [{"episode_index": i, "source_episode_id": e}
                                    for e, i in health_map.items()]}, f)
    elif os.path.exists(hp):
        os.remove(hp)              # 重造交付集时别留下上一次的映射(会假阳性)
    return root


def _review_site(root, site, eids, cams=("cam_left", "cam_wrist")):
    """审片站骨架:<根>/<站名>/details/audit_clips/<ep>__<相机>.mp4。"""
    d = os.path.join(root, site, "details", "audit_clips")
    os.makedirs(d, exist_ok=True)
    for e in eids:
        for c in cams:
            with open(os.path.join(d, f"{e}__{c}.mp4"), "wb") as f:
                f.write(b"\x00\x00\x00 ftypisom")
    return root


def test_buckets_put_dedup_removal_in_the_rejected_pile(ep_delivery):
    """去重删除的条目属于**拒绝**桶,理由说"重复"——把它算进通过桶就是虚报交付量。"""
    from curation.ui.manifest import (BUCKET_ALL, BUCKET_PASSED, BUCKET_PENDING,
                                      BUCKET_REJECTED, bucket_counts, bucket_ids,
                                      episode_bucket, episode_short_reason,
                                      is_dedup_drop)
    m = load_delivery(ep_delivery)
    assert episode_bucket(m, "ep000003") == BUCKET_REJECTED
    assert is_dedup_drop(m["episodes"]["ep000003"])
    assert "重复" in episode_short_reason(m, "ep000003")
    assert bucket_counts(m) == {BUCKET_PASSED: 1, BUCKET_REJECTED: 2,
                                BUCKET_PENDING: 2, BUCKET_ALL: 5}
    assert bucket_ids(m, BUCKET_PASSED) == ["ep000000"]
    assert bucket_ids(m, BUCKET_REJECTED) == ["ep000002", "ep000003"]
    assert bucket_ids(m, BUCKET_PENDING) == ["ep000001", "ep000004"]


def test_bucket_choices_carry_counts_and_all(ep_delivery):
    """顶部三桶自带计数(数字是客户最想先看到的),外加「全部」兜底。"""
    from curation.ui.manifest import BUCKET_ALL, bucket_choices
    labels = [lab for lab, _ in bucket_choices(load_delivery(ep_delivery))]
    assert labels == ["✅ 通过 (1)", "❌ 拒绝 (2)", "⏳ 待人工 (2)", "全部 (5)"]
    assert bucket_choices(load_delivery(ep_delivery))[-1][1] == BUCKET_ALL


def test_episode_list_line_is_id_icon_and_half_a_sentence(ep_delivery):
    """清单行 = `ep000002 ❌` + 半句人话;**通过条目不写理由**(没什么可解释的)。"""
    from curation.ui.manifest import (BUCKET_ALL, LIST_REASON_CAP,
                                      episode_list_choices, episode_list_items)
    m = load_delivery(ep_delivery)
    by_id = {it["id"]: it for it in episode_list_items(m, BUCKET_ALL)}
    assert by_id["ep000000"]["label"] == "ep000000 ✅"          # 通过:只有号和勾
    assert by_id["ep000002"]["label"] == "ep000002 ❌ 末态未完成"
    assert "未通过「" not in by_id["ep000002"]["label"]      # 前缀不进清单
    assert by_id["ep000001"]["label"] == "ep000001 ⏳ 「任务成败判定」拿不准"   # 数字细节不进清单
    assert "分歧" in by_id["ep000004"]["reason"] or \
        "不一致" in by_id["ep000004"]["reason"]
    # 理由截断:一行超过上限就带省略号,清单永远单行可扫
    assert all(len(it["reason"]) <= LIST_REASON_CAP + 1
               for it in episode_list_items(m, BUCKET_ALL))
    assert episode_list_choices(m, BUCKET_ALL)[0][1] == "ep000000"   # 值是 id


def test_passed_episode_card_says_nothing_but_passed(ep_delivery):
    """用户原话:通过条目"就一行 ✅ 通过,别的不说"。"""
    from curation.ui.manifest import episode_card_html
    m = load_delivery(ep_delivery)
    card = episode_card_html(m, "ep000000")
    assert "✅" in card and "ep000000" in card
    assert "通过" not in card    # issue #59:绿底+✅ 足矣,"通过"二字不再印
    for noise in ("质量分", "致命项", "原因", "检查", "弃权"):
        assert noise not in card, noise
    # 被拒的那条相反:理由必须当面写清
    assert "末态未完成" in episode_card_html(m, "ep000002")


def test_video_source_chain_prefers_review_site(ep_delivery, tmp_path):
    """来源链 ①:审片站有片段就用审片站(**全部 episode 都有,含被拒的**)。"""
    from curation.ui.manifest import (VIDEO_SOURCE_REVIEW, episode_video_html,
                                      episode_videos)
    site = _review_site(str(tmp_path / "review"), "droid_buckets",
                        ["ep000000", "ep000002"])
    m = load_delivery(ep_delivery)
    v = episode_videos(m, "ep000002", site)                 # 被拒条目照样有视频
    assert v["source"] == VIDEO_SOURCE_REVIEW
    assert [x["camera"] for x in v["videos"]] == ["cam_left", "cam_wrist"]
    html = episode_video_html(m, "ep000002", site)
    assert html.count("<video") == 2
    assert "muted" in html and "loop" in html and 'preload="metadata"' in html
    assert "autoplay" not in html                            # 进页面不许自己播
    # 头行 = 任务标注,不再是视频来源(2026-08-28 用户定:来源那句没信息量)
    assert "视频来自审片站" not in html and "路相机" not in html


def test_video_source_chain_falls_back_to_curated_dataset(ep_delivery, tmp_path):
    """来源链 ②:审片站没有 → 交付集内逐条 mp4(只有交付了的条目才有)。"""
    from curation.ui.manifest import (VIDEO_SOURCE_CURATED, VIDEO_SOURCE_NONE,
                                      curated_index_of, episode_videos)
    _curated(ep_delivery, episodes=3)          # 交付 3 条:ep000000/1/4 按序重编号
    m = load_delivery(ep_delivery)
    assert curated_index_of(m, "ep000004") == 2
    v = episode_videos(m, "ep000004", None)
    assert v["source"] == VIDEO_SOURCE_CURATED
    assert v["videos"][0]["path"].endswith("episode_000002.mp4")
    assert v["videos"][0]["camera"] == "cam_left"           # schema 前缀不露给客户
    # 被拒条目根本没进交付集 → 落到第三档
    assert episode_videos(m, "ep000002", None)["source"] == VIDEO_SOURCE_NONE


def test_curated_index_uses_recorded_mapping_and_abstains_when_unsure(ep_delivery):
    """序号换算:有旁挂映射就照抄;条数对不上就**弃权**(猜错=播别人的视频)。"""
    from curation.ui.manifest import curated_index_of, episode_videos
    _curated(ep_delivery, episodes=3,
             health_map={"ep000000": 0, "ep000001": 1, "ep000004": 2})
    m = load_delivery(ep_delivery)
    assert curated_index_of(m, "ep000001") == 1              # 来自旁挂映射
    # 映射文件缺失 + 条数对不上(交付集 9 条 vs 清单 3 条)→ 不猜
    _curated(ep_delivery, episodes=9)
    m2 = load_delivery(ep_delivery)
    assert curated_index_of(m2, "ep000004") is None
    assert episode_videos(m2, "ep000004", None)["videos"] == []


def test_v3_merged_mp4_is_not_a_video_source(ep_delivery):
    """v3 交付集是**合并大 mp4**(不按条切),不属于本来源——宁可说没有。"""
    from curation.ui.manifest import VIDEO_SOURCE_NONE, curated_video_paths, episode_videos
    _curated(ep_delivery, episodes=3, version="v3.0")
    m = load_delivery(ep_delivery)
    assert curated_video_paths(m, "ep000000") == []
    assert episode_videos(m, "ep000000", None)["source"] == VIDEO_SOURCE_NONE


def test_no_video_anywhere_tells_how_to_get_them(ep_delivery):
    """来源链 ③:两处都没有 → 一句"怎么才能有",不空着也不假装。"""
    from curation.ui.manifest import NO_VIDEO_NOTE, episode_video_html, episode_videos
    m = load_delivery(ep_delivery)
    assert episode_videos(m, "ep000000", None)["note"] == NO_VIDEO_NOTE
    assert "review-page" not in episode_video_html(m, "ep000000", None)  # 行话不进界面(issue #59)
    assert "找不到画面" in episode_video_html(m, "ep000000", None)


def test_play_all_button_zeroes_and_plays_every_video(ep_delivery, tmp_path):
    """「同时播放」= 区内所有 video 归零后一起播,再点变暂停(纯内联 JS:
    gr.HTML 走 innerHTML 注入,<script> 不执行、内联事件属性执行)。"""
    from curation.ui.manifest import PAUSE_ALL_TEXT, PLAY_ALL_TEXT, episode_video_html
    site = _review_site(str(tmp_path / "review"), "droid_buckets", ["ep000000"],
                        cams=("cam_a", "cam_b", "cam_c"))
    html = episode_video_html(load_delivery(ep_delivery), "ep000000", site)
    assert html.count("<video") == 3
    assert PLAY_ALL_TEXT in html and PAUSE_ALL_TEXT in html
    assert "querySelectorAll('video')" in html and ".play()" in html
    assert "currentTime=+(v.dataset.t0||0)" in html and ".pause()" in html
    assert "ep-video-zone" in html
    assert "&" not in html                 # 属性里的 & 会被当实体开头,踩过一次


def test_clips_do_not_loop(ep_delivery, tmp_path):
    """片段**不循环**(2026-08-14 用户定)。

    裁决是"看一遍下判断"的事;片子自己转圈,人分不清看到的是第几遍。要重看
    点「同时播放」从头播。三处都得守住:HTML 播放器不带 `loop` 属性、
    「同时播放」每次起播前显式关掉 loop、Gradio 组件那两组也不许开(见 app 侧用例)。
    """
    from curation.ui.manifest import episode_video_html
    site = _review_site(str(tmp_path / "review"), "droid_buckets", ["ep000000"],
                        cams=("cam_a", "cam_b", "cam_c"))
    html = episode_video_html(load_delivery(ep_delivery), "ep000000", site)
    for tag in re.findall(r"<video[^>]*>", html):
        assert " loop" not in tag, f"播放器还开着循环:{tag}"
    assert "v.loop=false" in html          # 起播前显式关,防外部再打开


def test_play_all_button_can_target_a_zone_by_id(ep_delivery, tmp_path):
    """按钮与视频**不在同一个 DOM 子树**时,靠 elem_id 找视频。

    人工裁决的两张卡用的是 gr.Video 组件,按钮只能另起一行(挤进视频那一行会把
    播放器压窄 —— 用户明说"视频 window 大小别变"),所以 `closest()` 那条路走不通。
    """
    from curation.ui.manifest import play_all_button_html
    html = play_all_button_html("说明文字", zone="au-vids")
    assert 'data-zone="au-vids"' in html
    assert "getElementById(t)" in html
    assert "closest('.ep-video-zone')" in html   # 另一种挂法照旧可用
    assert "&" not in html


def test_play_all_button_pops_back_when_playback_ends():
    """播完按钮自己弹回「同时播放」。

    去掉 loop 之后必须做:片子早停了、按钮还写着「暂停」,用户再点一下才发现是
    从头播 —— 白点一次。
    """
    from curation.ui.manifest import PLAY_ALL_TEXT, play_all_button_html
    html = play_all_button_html()
    assert "onended=fin" in html and "onpause=fin" in html
    assert f"textContent='{PLAY_ALL_TEXT}'" in html


def test_manual_hint_only_on_pending_and_points_at_the_decision_page(ep_delivery):
    """待人工条目在明细上方给醒目提示 + 去「人工裁决」页的指引;别的桶不占位。"""
    from curation.ui.manifest import manual_hint_html
    m = load_delivery(ep_delivery)
    hint = manual_hint_html(m, "ep000001")
    # 2026-08-19 起执行入口就在「人工裁决」页底部,指路不再指向任务台
    assert "人工裁决" in hint and "执行裁决" in hint
    assert "执行人工裁决" not in hint and "任务台" not in hint
    assert manual_hint_html(m, "ep000000") == ""
    assert manual_hint_html(m, "ep000002") == ""


def test_episodes_page_text_has_no_mechanism_jargon(ep_delivery, tmp_path):
    """★红线(2026-08-11 用户点名):"软分""硬门"是机制黑话,界面上一个字不许剩。"""
    from curation.ui.manifest import (BUCKET_ALL, bucket_choices, check_table_html,
                                      episode_card_html, episode_list_items,
                                      episode_video_html, manual_hint_html,
                                      overview_markdown, overview_rows)
    site = _review_site(str(tmp_path / "review"), "droid_buckets", ["ep000000"])
    m = load_delivery(ep_delivery)
    seen = [overview_markdown(m), str(overview_rows(m)), str(bucket_choices(m))]
    for eid in ("ep000000", "ep000001", "ep000002", "ep000003", "ep000004"):
        seen += [episode_card_html(m, eid), check_table_html(m, eid),
                 manual_hint_html(m, eid), episode_video_html(m, eid, site)]
    seen += [it["label"] for it in episode_list_items(m, BUCKET_ALL)]
    blob = "\n".join(seen)
    assert "软分" not in blob and "硬门" not in blob
    assert "质量分" in blob                       # 换的是叫法,不是把信息删了
    assert "判废" in str(overview_rows(m))        # 总览表说「判废」,不说「硬门」
    assert "未通过「任务成败判定」" in episode_card_html(m, "ep000002")   # 检查名仍在


# ── 审片站认领(2026-08-11 收紧):只播**本交付这份数据**的片段 ──
#
# 起因:站点原先没有身份,UI 只能"谁有这个 episode 号就用谁"。droid-ep13-20-demo
# 因此借用了 droid200 站的片段——同源同号,那次巧对;换个数据集同号就是给客户放错
# 视频。现在改成两档认领(site.json 精确 → 站名降级),**认不出就当没有**。


def _site_json(root, site, source_dataset):
    """给站点补一张身份证(生成侧由 review_page.write_site_manifest 写)。"""
    import os as _os
    d = _os.path.join(root, site)
    _os.makedirs(d, exist_ok=True)
    with open(_os.path.join(d, "site.json"), "w", encoding="utf-8") as f:
        json.dump({"source_dataset": source_dataset,
                   "dataset_name": _os.path.basename(source_dataset),
                   "title": "随便起的标题", "generated_at": "2026-08-11 10:00:00"}, f)
    return d


def test_review_site_claimed_by_site_json_not_by_folder_name(ep_delivery, tmp_path):
    """站名与数据集名对不上时,site.json 说了算(真实站名常是 droid200 这种昵称)。"""
    from curation.ui.manifest import VIDEO_SOURCE_REVIEW, episode_videos
    root = str(tmp_path / "review")
    _review_site(root, "随便起的站名", ["ep000000"])
    _site_json(root, "随便起的站名", "/mnt/tos/datasets/droid_buckets")
    m = load_delivery(ep_delivery)                    # passed.json 里数据集名 = droid_buckets
    v = episode_videos(m, "ep000000", root)
    assert v["source"] == VIDEO_SOURCE_REVIEW and len(v["videos"]) == 2


def test_review_site_name_match_is_the_legacy_fallback(ep_delivery, tmp_path):
    """老站点没有 site.json:站名归一化等于数据集名仍认(不然存量站全瞎)。"""
    from curation.ui.manifest import VIDEO_SOURCE_REVIEW, episode_videos
    root = _review_site(str(tmp_path / "review"), "droid_buckets", ["ep000000"])
    v = episode_videos(load_delivery(ep_delivery), "ep000000", root)
    assert v["source"] == VIDEO_SOURCE_REVIEW


def test_unclaimed_site_is_not_borrowed(ep_delivery, tmp_path):
    """★红线:两档都不中的站点,**片段号对得上也不许用** —— 宁缺勿错。"""
    from curation.ui.manifest import (NO_VIDEO_NOTE, VIDEO_SOURCE_NONE,
                                      episode_videos)
    root = _review_site(str(tmp_path / "review"), "别人家的站", ["ep000000"])
    _site_json(root, "别人家的站", "/mnt/tos/datasets/bridge_orig_lerobot")
    v = episode_videos(load_delivery(ep_delivery), "ep000000", root)
    assert v["source"] == VIDEO_SOURCE_NONE and v["note"] == NO_VIDEO_NOTE


def test_two_sites_same_episode_ids_only_the_matching_one_plays(ep_delivery, tmp_path):
    """串台场景:两个站都有 ep000000 的片段,只有认领成功的那个能出现在页面上。"""
    from curation.ui.manifest import episode_videos, review_clip_paths
    root = str(tmp_path / "review")
    _review_site(root, "aaa_别人家", ["ep000000"])          # 名字排在前面
    _site_json(root, "aaa_别人家", "/mnt/tos/datasets/bridge_orig_lerobot")
    _review_site(root, "zzz_我家", ["ep000000"])
    _site_json(root, "zzz_我家", "/mnt/tos/datasets/droid_buckets")
    m = load_delivery(ep_delivery)
    paths = review_clip_paths(root, m, "ep000000")
    assert paths and all("zzz_我家" in p for p in paths)
    assert all("aaa_别人家" not in x["path"]
               for x in episode_videos(m, "ep000000", root)["videos"])


def test_site_claim_helpers_are_honest_about_missing_identity(ep_delivery, tmp_path):
    """辅助函数的边界:没有 site.json = 认不出(不是"匹配成功");站点扫描含根本身。"""
    from curation.ui.manifest import (delivery_source_dataset, review_sites,
                                      site_matches_delivery)
    root = str(tmp_path / "review")
    d = _review_site(root, "无名站", ["ep000000"])
    m = load_delivery(ep_delivery)
    assert delivery_source_dataset(m) == (None, "droid_buckets")   # 交付只记名
    assert site_matches_delivery(os.path.join(d, "无名站"), m) is False
    assert review_sites(root)[0] == root                           # 根本身也是候选
    assert os.path.join(root, "无名站") in review_sites(root)
    assert review_sites(None) == []


# ── 左清单分页 + 片段可播性(2026-08-11 用户两处实见)──
#
# ① 两百行单选框一次渲染就到极限 → 每页 50 条,翻页口径抄同步曲线页那一套;
# ② droid-200-full 的 ep000018 摆了三个**死播放器**:它是 8 帧 0.47 秒的采集残段
#    (被拒原因就是它),切出的片段只有 1 帧 0.25 秒——文件在、近 10KB、mp4 魔数
#    俱全,播放器就是放不出东西。所以"存在 + 够大 + 有魔数"三条挡不住它,必须
#    再看容器头里的帧数/时长。


def _many_eps(tmp_path, n=120):
    """n 条通过条目的交付(只为测分页,内容从简)。"""
    d = tmp_path / "many"
    (d / "details").mkdir(parents=True)
    (d / "passed.json").write_text(json.dumps({
        "数据集": "many_ds", "dataset": {},
        "episodes": {f"ep{i:06d}": {"判决": "通过", "综合软分": 0.9, "checks": {}}
                     for i in range(n)}}, ensure_ascii=False), encoding="utf-8")
    return load_delivery(str(d))


def test_list_paging_bounds_and_wrap(tmp_path):
    """分页:每页 EPISODE_PAGE_SIZE 条、末页只剩零头、页码越界回绕(同曲线页口径)。"""
    from curation.ui.manifest import EPISODE_PAGE_SIZE, episode_list_view
    assert EPISODE_PAGE_SIZE == 50
    m = _many_eps(tmp_path, 120)
    v0 = episode_list_view(m, page=0)
    assert v0["pages"] == 3 and len(v0["choices"]) == 50
    assert v0["choices"][0][1] == "ep000000" and v0["pos"] == "第 1 / 3 页"
    assert v0["multi"] is True
    v2 = episode_list_view(m, page=2)
    assert len(v2["choices"]) == 20 and v2["choices"][0][1] == "ep000100"
    assert episode_list_view(m, page=3)["page"] == 0      # 越界回绕到第 1 页
    assert episode_list_view(m, page=-1)["page"] == 2     # 往回也回绕
    # 一页放得下时不出翻页件(与曲线页一致:平铺优先)
    small = episode_list_view(_many_eps(tmp_path / "s", 8))
    assert small["pages"] == 1 and small["pos"] == "" and small["multi"] is False


def test_list_selection_survives_paging(tmp_path):
    """选中项跨页保持:翻走时清单里没有高亮项,翻回它那页仍是选中态。"""
    from curation.ui.manifest import episode_list_view
    m = _many_eps(tmp_path, 120)
    assert episode_list_view(m, page=0, selected="ep000003")["value"] == "ep000003"
    assert episode_list_view(m, page=1, selected="ep000003")["value"] is None
    assert episode_list_view(m, page=0, selected="ep000003")["value"] == "ep000003"
    # 选中项来自别的桶/已不存在:同样不许乱点亮别人
    assert episode_list_view(m, page=0, selected="查无此条")["value"] is None


def test_bucket_switch_resets_to_first_page(ep_delivery):
    """切桶回第 1 页(停在旧页码上,看到的是空清单或别人的条目)。"""
    from curation.ui.manifest import BUCKET_PENDING, BUCKET_REJECTED, episode_list_view
    m = load_delivery(ep_delivery)
    v = episode_list_view(m, BUCKET_REJECTED, page=0)
    assert [c[1] for c in v["choices"]] == ["ep000002", "ep000003"]
    v2 = episode_list_view(m, BUCKET_PENDING, page=0)
    assert [c[1] for c in v2["choices"]] == ["ep000001", "ep000004"]


def _write_clip(path, size=8192, magic=b"\x00\x00\x00 ftypisom"):
    with open(path, "wb") as f:
        f.write(magic + b"\x00" * max(0, size - len(magic)))


def test_truncated_or_fake_clips_are_not_playable(ep_delivery, tmp_path, monkeypatch):
    """存在但没法播的三种形态:文件缺、太小(截断/零填充)、根本不是 mp4。"""
    from curation.ui import manifest as mf
    monkeypatch.setattr(mf, "_probe_frames_duration", lambda p: (None, None))
    mf._PROBE_CACHE.clear()               # 只测"存在/大小/魔数"这一层
    d = tmp_path / "clips"
    d.mkdir()
    ok, tiny, junk = (str(d / f"{n}.mp4") for n in ("ok", "tiny", "junk"))
    _write_clip(ok)
    _write_clip(tiny, size=800)
    _write_clip(junk, magic=b"not an mp4!!")
    assert mf.clip_is_playable(ok) is True
    assert mf.clip_is_playable(tiny) is False
    assert mf.clip_is_playable(junk) is False
    assert mf.clip_is_playable(str(d / "缺失.mp4")) is False


def test_one_frame_clip_is_a_dead_player(tmp_path):
    """★ ep000018 那一类:mp4 合法、体积够大,但只有 1 帧 —— 摆上去就是死播放器。

    判据只看容器头(帧数/时长),**不解码任何一帧**(FSX 上整读是灾难)。
    """
    pytest.importorskip("av")
    import av
    import numpy as np

    from curation.ui import manifest as mf
    mf._PROBE_CACHE.clear()

    def make(path, n_frames):
        rng = np.random.default_rng(0)
        with av.open(path, "w", options={"movflags": "faststart"}) as c:
            st = c.add_stream("h264", rate=4)
            st.width, st.height, st.pix_fmt = 320, 180, "yuv420p"
            for _ in range(n_frames):
                arr = rng.integers(0, 255, (180, 320, 3), dtype=np.uint8)
                for pkt in st.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
                    c.mux(pkt)
            for pkt in st.encode():
                c.mux(pkt)

    dead, good = str(tmp_path / "dead.mp4"), str(tmp_path / "good.mp4")
    make(dead, 1)
    make(good, 24)
    assert mf.clip_probe(dead)["playable"] is False
    assert mf.clip_probe(dead)["frames"] == 1
    assert "视频过短" in mf.clip_probe(dead)["why"] and "1 帧" in mf.clip_probe(dead)["why"]
    assert mf.clip_probe(good)["playable"] is True and mf.clip_probe(good)["why"] == ""


def test_unplayable_lanes_keep_their_player_and_explain_why(ep_delivery, tmp_path,
                                                            monkeypatch):
    """★ 用户原话:"视频还是要放在那里占位"——放不动的那一路**照摆播放器**,
    旁边写清"视频过短(N 帧 / X.XX 秒),无法正常播放";被拒条目再补半句
    "这正是该条被拒的原因"(ep000018 那类残段,片段放不动本身就是被拒的原因)。"""
    from curation.ui import manifest as mf
    monkeypatch.setattr(mf, "clip_probe", lambda p: {
        "playable": False, "frames": 1, "duration_s": 0.25,
        "why": mf.short_clip_text(1, 0.25)})
    root = _review_site(str(tmp_path / "review"), "droid_buckets",
                        ["ep000000", "ep000002"])
    m = load_delivery(ep_delivery)
    html = mf.episode_video_html(m, "ep000002", root)          # ep000002 是被拒的
    assert html.count("<video") == 2                           # 槽位一个没少
    assert "视频过短(1 帧 / 0.25 秒),无法正常播放" in html
    assert mf.REJECTED_CLIP_TAIL in html
    # 通过条目:同样保留播放器与说明,但不拼"被拒原因"那半句
    assert mf.REJECTED_CLIP_TAIL not in mf.episode_video_html(m, "ep000000", root)
    # "压根没有视频"仍然是另一回事(那才给纯文字提示)
    assert mf.episode_videos(m, "ep000001", root)["note"] == mf.NO_VIDEO_NOTE


def test_short_clip_text_degrades_without_readings():
    """读数拿不到(文件截断/不是 mp4)时退回"损坏或过短",不编造帧数。"""
    from curation.ui.manifest import BROKEN_LANE_TEXT, short_clip_text
    assert short_clip_text(1, 0.25) == "视频过短(1 帧 / 0.25 秒),无法正常播放"
    assert short_clip_text(None, 0.4) == "视频过短(0.40 秒),无法正常播放"
    assert short_clip_text(None, None) == BROKEN_LANE_TEXT


def test_source_with_no_playable_lane_falls_through(ep_delivery, tmp_path, monkeypatch):
    """整档都是死片段 = 这档没给出视频 → 继续往下找(退到交付集内的视频)。"""
    from curation.ui import manifest as mf
    root = _review_site(str(tmp_path / "review"), "droid_buckets", ["ep000000"])
    _curated(ep_delivery, episodes=3)
    monkeypatch.setattr(mf, "clip_probe", lambda p: {
        "playable": "review" not in p, "frames": None, "duration_s": None,
        "why": "" if "review" not in p else mf.BROKEN_LANE_TEXT})
    v = mf.episode_videos(load_delivery(ep_delivery), "ep000000", root)
    assert v["source"] == mf.VIDEO_SOURCE_CURATED
    assert all(x["playable"] for x in v["videos"])


# ── 清单行与横幅措辞解耦(2026-08-11 用户定)──
#
# 清单列窄 + 单行省略,前二十来字就被截住:带上"未通过「时间戳检查」:"这个前缀,
# "到底怎么了"就被挤出视野了(ep000018 实见)。所以清单只放**检查自己写的人话**,
# 横幅保持完整交代。拿不到人话(老交付)才退回带前缀的格式。


def _ts_reject(tmp_path, detail_reason="全长只有 0.47 秒(不足 1 秒,疑似采集中断的碎片)"):
    d = tmp_path / "tsrej"
    (d / "details").mkdir(parents=True)
    (d / "passed.json").write_text('{"数据集": "ds", "dataset": {}, "episodes": {}}',
                                   encoding="utf-8")
    chk = {"结果": "拒绝"}
    if detail_reason:
        chk["detail"] = json.dumps({"reason": detail_reason, "n": 8})
    (d / "reject.json").write_text(json.dumps({"episodes": {"ep000018": {
        "判决": "拒绝",
        "原因": ("未通过「时间戳检查」:" + detail_reason if detail_reason
                 else "硬门违规: 「时间戳检查」"),
        "checks": {"时间戳检查": chk}}}}, ensure_ascii=False), encoding="utf-8")
    return load_delivery(str(d))


def test_list_line_drops_the_check_name_prefix(tmp_path):
    """清单行 = 人话在前:`ep000018 ❌ 全长只有 0.47 秒(…)`,没有"未通过「"前缀。"""
    from curation.ui.manifest import (BUCKET_ALL, episode_card_html,
                                      episode_list_items, episode_list_reason)
    m = _ts_reject(tmp_path)
    label = episode_list_items(m, BUCKET_ALL)[0]["label"]
    assert label.startswith("ep000018 ❌ 全长只有 0.47 秒")
    assert "未通过「" not in label
    for word in ("全长", "秒"):
        assert word in label, label
    assert not episode_list_reason(m, "ep000018").startswith("未通过")
    # 横幅照旧完整交代"哪一项没过 + 为什么"(两处措辞是解耦的,不是二选一)
    card = episode_card_html(m, "ep000018")
    assert "未通过「时间戳检查」:全长只有 0.47 秒" in card


def test_list_line_falls_back_when_check_wrote_no_reason(tmp_path):
    """老交付/检查没写人话:清单退回带检查名的格式 —— 宁可啰嗦,不可空白。"""
    from curation.ui.manifest import BUCKET_ALL, episode_list_items
    m = _ts_reject(tmp_path, detail_reason="")
    label = episode_list_items(m, BUCKET_ALL)[0]["label"]
    assert label == "ep000018 ❌ 未通过「时间戳检查」"
    assert "硬门" not in label


# ───────── 视频打分明细单独成页(2026-08-13)─────────

def test_video_detail_view_reads_the_per_camera_table(delivery):
    """「视频打分明细」= 逐相机那张表 + 一行行数说明。"""
    from curation.ui.manifest import video_detail_view
    import os
    with open(os.path.join(delivery, "details", "visual_details.csv"), "w") as f:
        f.write("episode,camera,blur\n" + "\n".join(f"ep{i:03d},cam0,0.8"
                                                    for i in range(3)))
    note, headers, rows = video_detail_view(load_delivery(delivery))
    assert headers == ["episode", "camera", "blur"] and len(rows) == 3
    assert "共 3 行" in note


def test_video_detail_view_empty_state_names_no_config_key(delivery):
    """老交付/没跑视觉质量 → 一句话空态。

    ⚠️ 空态里**不许出现配置键名**:NO_PLOTS_NOTE 那次的教训 —— 开关名是我们的实现
    细节,客户既不知道去哪改、也不该被要求知道。只说交付里缺什么。
    """
    from curation.ui.manifest import NO_VIDEO_DETAILS_NOTE, video_detail_view
    note, headers, rows = video_detail_view(load_delivery(delivery))
    assert note == NO_VIDEO_DETAILS_NOTE and rows == [] and headers == ["(无)"]
    for key in ("pipeline.", "checks.", "visual_quality:", "--set"):
        assert key not in note
    assert video_detail_view({}) == (NO_VIDEO_DETAILS_NOTE, ["(无)"], [])


def test_detail_dropdown_no_longer_offers_the_video_table(delivery):
    """同一份数据不留两个入口:逐相机表单独成页后,下拉里那条撤掉。"""
    from curation.ui.manifest import detail_table_choices, list_detail_tables
    import os
    det = os.path.join(delivery, "details")
    for name in ("motion_details.csv", "visual_details.csv"):
        with open(os.path.join(det, name), "w") as f:
            f.write("a,b\n1,2\n")
    m = load_delivery(delivery)
    assert "visual_details.csv" in list_detail_tables(m)      # 文件照样认得出
    assert detail_table_choices(m) == ["motion_details.csv"]  # 但下拉里没有它


# ───────── 报告页「执行裁决」(2026-08-19 决策与执行合到一处)─────────
#
# 起因(用户原话:"非常不合理。其实'执行人工裁决'也就是一个按钮的事情"):
# 决策在报告页、执行在任务台,用户刚对着某份交付的某次运行裁完,还要到另一个
# 页签把同样的上下文再选一遍。定案 = 把执行搬到证据旁边(报告页「人工裁决」页
# 底部),任务台的「执行人工裁决」页签整个删掉。以下测试钉住四件事:
# ① 作用对象 = 当前加载的交付与运行(state),不是任何下拉的值;
# ② 确认块不点「确定」绝不发起任务(防"按钮即执行");
# ③ 有任务在跑时点执行 → 明确告知,不发起、不失败、不排队;
# ④ 报告页页签的增删为零(红线)。


def _descendants(block):
    """一个容器下的全部子组件(Gradio 的 children 是嵌套的)。"""
    out = []
    for c in (getattr(block, "children", None) or []):
        out.append(c)
        out.extend(_descendants(c))
    return out


def test_rejected_episode_still_has_video_from_the_source_dataset(tmp_path):
    """🔴 Episodes 页**不管过没过都要有画面**(2026-08-19 用户实机点名)。

    原来的三个来源全是**产物**,天生带偏:交付集只装通过的、裁决片段只装进了
    队列的、审片站要另外生成。于是审片站没生成时,**被拒的条目一路都没有**——
    可它恰恰最该被看见:系统自己把"被拒复议"定义成「证据够就杀」的保险丝,
    看不见画面的复议就是走过场。

    第四来源=源数据集,对通过与否一视同仁。这条钉的就是它:一个**被拒**的
    episode,在没有审片站、也不在交付集里的情况下,照样取得到画面。
    """
    src = tmp_path / "ds"
    (src / "meta").mkdir(parents=True)
    (src / "meta" / "info.json").write_text('{"codebase_version": "v2.0"}',
                                            encoding="utf-8")
    for cam in ("observation.images.top", "observation.images.wrist"):
        d = src / "videos" / "chunk-000" / cam
        d.mkdir(parents=True)
        (d / "episode_000007.mp4").write_bytes(b"\x00" * 24 + b"ftyp" + b"\x00" * 9000)

    m = {"path": str(tmp_path / "deliv"), "source_dataset": str(src),
         "name": "ds", "episodes": {"ep000007": {"verdict": "拒绝"}}}
    from curation.ui.manifest import (VIDEO_SOURCE_SOURCE, source_video_paths)
    got = source_video_paths(m, "ep000007")
    assert len(got) == 2, f"被拒条目没能从源数据集取到画面:{got}"
    assert all("episode_000007.mp4" in p for p in got)

    # ⚠️ **用原始序号,不做重编号映射**:交付集才是重编过的。两套编号混用会放出
    # 另一条 episode 的画面 —— 人对着错的证据做裁决,比没有画面更糟。
    assert not source_video_paths(m, "ep000000"), \
        "拿了不存在的序号还返回路径 —— 编号一旦错位就会放错画面"

    # v3 源是合并大 mp4,切不出单条 → 空表(与 curated_video_paths 同一条规矩)
    (src / "meta" / "info.json").write_text('{"codebase_version": "v3.0"}',
                                            encoding="utf-8")
    assert source_video_paths(m, "ep000007") == []
    assert VIDEO_SOURCE_SOURCE == "源数据集"


# ───────── 数据集多选(2026-08-13)─────────


# ───────── 下拉浮层跟着页面滚(2026-08-13)─────────


# ───────── 质检总览合成一张表(2026-08-13 用户点名)─────────
#
# 起因(用户原话:"表格没有显示正确的质检结果信息"):总览页上半部是几行 bullet、
# 下半部是一张表,**同一批数字说两遍**,而且两处的「不合格拦截」同名不同义 ——
# 表里那行只算中途被硬门刷掉的(droid-200-full = 1),bullet 里那句是最终判废的
# 全部(15)。下面这几条用真交付(droid-200-full)的数字钉住新口径。


@pytest.fixture
def full_delivery(tmp_path):
    """按 droid-200-full 的真实分布造:200 进 / 15 判废 / 185 交付,
    31 条待裁 + 32 条标注与视频内容分歧(都是 185 的子集)。

    分歧那 32 条与 task_details 里的 instruction_source 都**故意造齐**:总览表
    刻意不显示它们,得有数据在才证得出"不显示"是选择而不是取不到。"""
    d = tmp_path / "droid-200-full"
    (d / "details").mkdir(parents=True)
    passed = {f"ep{i:06d}": {"判决": "通过", "综合软分": 0.93, "checks": {}}
              for i in range(185)}
    (d / "passed.json").write_text(json.dumps({
        "数据集": "droid_batch", "机器人": "franka",
        "生成时间": "2026-08-12 10:00:00", "代码版本": "abc1234",
        "dataset": {"input_episodes": 200, "hard_gate_filtered": 1,
                    "verdict_keep": 185, "verdict_drop": 15, "dedup_removed": 0,
                    "delivered": 185,
                    "hard_fail_breakdown": {"task_success": 14,
                                            "timestamp_check": 1},
                    "summary_stats": {"pass_rate_pct": 92.5,
                                      "avg_soft_score": 0.9311}},
        "episodes": passed}, ensure_ascii=False))
    (d / "reject.json").write_text(json.dumps({
        "episodes": {f"ep{900 + i:06d}": {"判决": "拒绝", "原因": "未通过「任务成败判定」"}
                     for i in range(15)}}, ensure_ascii=False))
    (d / "review.json").write_text(json.dumps({
        "episodes": {f"ep{i:06d}": {"当前判决": "通过", "待裁决项": ["任务成败判定"]}
                     for i in range(31)},
        "标注-画面分歧复核队列": [{"id": f"ep{i:06d}", "label": "x", "caption": "y",
                                   "reason": "跨族"} for i in range(32)]},
        ensure_ascii=False))
    # 逐条判定记录:交付内 104 原始标注 / 78 自产 caption / 3 无;另有 14 条被判废的
    # 也在这个文件里(task_details 覆盖的是过了数值门的全部,不只是交付那批)
    td = {}
    for i in range(185):
        src = ("自产caption" if i < 78 else "无" if i < 81 else "原始标注")
        td[f"ep{i:06d}"] = {"episode_id": f"ep{i:06d}", "instruction_source": src}
    for i in range(14):
        td[f"ep{900 + i:06d}"] = {"episode_id": f"ep{900 + i:06d}",
                                  "instruction_source": "自产caption"}
    (d / "details" / "task_details.json").write_text(
        json.dumps({"数据集": "droid_batch", "episodes": td}, ensure_ascii=False))
    return str(d)


def test_overview_rows_are_the_only_place_numbers_appear(full_delivery):
    """一张表说完全部数字,顶上的 Markdown 一个数字都不说。"""
    m = load_delivery(full_delivery)
    rows = dict(overview_rows(m))
    assert rows["输入 episode"] == 200
    assert rows["判废"] == 15
    assert rows["精确去重删除"] == 0
    assert rows["交付"] == "185(通过率 92.5%)"
    assert rows["平均质量分"] == 0.9311
    # 顶上的 Markdown:身份行 + 一句导航,不再复读任何数字
    md = overview_markdown(m)
    for n in ("200", "185", "92.5", "15", "31", "32"):
        assert n not in md, n


def test_overview_pending_row_counts_the_whole_adjudication_queue(full_delivery):
    """总览「其中 待人工裁决」= 人工裁决页队列的全部条数(成败弃权 × 标注分歧合并去重),
    括号里印构成(2026-09-09 用户定:此前只数成败弃权,与裁决页「全部(N)」对不上)。"""
    from curation.ui.manifest import merged_review_queue
    m = load_delivery(full_delivery)
    rows = dict(overview_rows(m))
    q = merged_review_queue(m)
    n_task = sum(1 for it in q if it["task"] is not None)
    n_audit = sum(1 for it in q if it["audit"] is not None)
    n_both = sum(1 for it in q if it["task"] and it["audit"])
    assert rows["　其中 待人工裁决"] == f"{len(q)}(成败弃权 {n_task},标注分歧 {n_audit},重叠 {n_both})"
    assert len(q) == n_task + n_audit - n_both


def test_overview_rows_add_up_input_equals_dropped_plus_delivered(full_delivery):
    """口径要能一眼验:输入 = 判废 + 交付,而判废逐项列出来的和 = 判废。

    旧表把「不合格拦截」写成"中途淘汰"(1),bullet 里那句却是"最终判废"(15),
    同一个词两个意思 —— 这条把新口径钉死。
    """
    rows = dict(overview_rows(load_delivery(full_delivery)))
    delivered = int(str(rows["交付"]).split("(")[0])
    assert rows["输入 episode"] == rows["判废"] + delivered
    detail = {k.strip("　├└ "): v for k, v in rows.items()
              if k.startswith("　") and ("├" in k or "└" in k)}
    assert detail == {"任务成败判定": 14, "时间戳检查": 1}
    assert sum(detail.values()) == rows["判废"]


def test_overview_rows_mark_the_within_delivery_flags(full_delivery):
    """带「其中」的行是交付内条目上的标记,不参与加减 —— 且**只有**待人工裁决一行。"""
    from curation.ui.manifest import merged_review_queue
    m = load_delivery(full_delivery)
    rows = dict(overview_rows(m))
    within = {k.replace("　", "").replace("其中 ", ""): v
              for k, v in rows.items() if "其中" in k}
    assert set(within) == {"待人工裁决"}
    # 2026-09-09 起数的是人工裁决页那张队列的全部条数,括号里印构成
    assert str(within["待人工裁决"]).startswith(f"{len(merged_review_queue(m))}(")


def test_overview_never_shows_the_label_rows(full_delivery):
    """★ 总览表**刻意不列**标注相关的行(2026-08-13 用户定,理由见 manifest 里
    那段 ⚠️):人工真值集里客户标注错 6/106,我们自产描述错 27/94 —— 把「分歧
    32 条」摆在总览首屏,实际是在展示自家打标不准,还会被读成"你的标注有 32 条
    有问题"。撤的只是这两行展示,分歧队列本身在「人工裁决」页照旧。
    """
    m = load_delivery(full_delivery)
    rows = overview_rows(m)
    blob = str([r[0] for r in rows]) + overview_markdown(m)
    assert AUDIT_TERM not in blob and "标注缺失" not in blob and "分歧" not in blob
    # 2026-09-09 用户定的唯一例外:「待人工裁决」那一行的括号里印人工队列的构成
    # (成败弃权 / 标注分歧 / 重叠),因为它就是裁决页队列的条数,不印构成对不上账。
    assert sum("分歧" in str(r[1]) for r in rows) == 1
    # 功能没被拆掉:队列还在,人工裁决页还照旧靠它
    assert len(m["audit_queue"]) == 32
    assert AUDIT_TERM in audit_note_md(m)


def test_overview_rows_degrade_row_by_row_on_a_legacy_delivery(tmp_path):
    """老交付缺哪行不显示哪行:不占位、不写「?」、不炸。"""
    d = tmp_path / "old"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps({
        "数据集": "old", "dataset": {"input_episodes": 7, "delivered": 5},
        "episodes": {}}, ensure_ascii=False))
    rows = dict(overview_rows(load_delivery(str(d))))
    assert rows == {"输入 episode": 7, "交付": 5}      # 无通过率就只有条数
    assert "?" not in str(rows)


def test_overview_never_shows_the_funnel_word(full_delivery):
    """「漏斗」是内部术语(用户:"用户看不懂啥意思"),总览页一个字不许剩。"""
    m = load_delivery(full_delivery)
    blob = overview_markdown(m) + str(overview_rows(m))
    assert "漏斗" not in blob and "硬门" not in blob


# ───────── 交付下拉:读不到就说读不到(2026-08-13)─────────

def test_load_delivery_flags_a_path_that_is_not_a_delivery(tmp_path):
    """交付下拉允许手输,用户打半截字("droid")再点选项时,输入框里留下的是那
    半截字 —— 当相对路径读,三个 JSON 全空,页面渲成一具壳子(机器人 None、
    交付 ?),看着像系统坏了。现在挂 load_error,渲染侧明说读不到。"""
    m = load_delivery(str(tmp_path / "droid"))
    assert m["load_error"] and "不是一份交付" in m["load_error"]
    assert "完整路径" in m["load_error"]        # 手输自定义路径的正确用法要讲清楚
    md = overview_markdown(m)
    assert "读不到" in md
    assert "机器人" not in md and "?" not in md   # 半空的壳子一个字不留
    assert overview_rows(m) == []


def test_resolve_delivery_recovers_a_typed_directory_name(tmp_path):
    """打半截字再点选项时输入框里留下的是那串目录名 —— 全库只有一份同名就还原成
    它。这不是猜:目录名精确相等,唯一性也验过。"""
    from curation.ui.manifest import resolve_delivery
    a = str(tmp_path / "a" / "droid-200-full")
    os.makedirs(a)
    open(os.path.join(a, "passed.json"), "w").write("{}")
    assert resolve_delivery("droid-200-full", [a]) == a
    assert resolve_delivery(a, [a]) == a                 # 完整路径原样通过
    assert resolve_delivery("  ", [a]) == ""


def test_resolve_delivery_refuses_to_pick_when_two_deliveries_share_a_name(tmp_path):
    """两份交付同名 → 一律不挑,交给上层明说读不到。挑一个"最像的"会让人看着
    别人的报告以为是自己的。"""
    from curation.ui.manifest import resolve_delivery
    one, two = str(tmp_path / "x" / "droid"), str(tmp_path / "y" / "droid")
    assert resolve_delivery("droid", [one, two]) == "droid"
    assert resolve_delivery("没这份", [one, two]) == "没这份"
    assert load_delivery(resolve_delivery("droid", [one, two]))["load_error"]


def test_load_delivery_flags_an_empty_picker_value(tmp_path):
    """空值/None 也不炸(下拉刚清空的一瞬间会走到这里)。"""
    for bad in ("", "   ", None):
        assert load_delivery(bad)["load_error"]


def test_load_delivery_has_no_error_for_a_real_delivery(delivery):
    """真交付一切照旧:load_error 是空串,老交付缺字段**不算**读不到。"""
    assert load_delivery(delivery)["load_error"] == ""


# ───────── Gradio 层:总览页 / 明细子页 / 页脚(2026-08-13)─────────


# ── 判废子项加不出总数时补一行(2026-08-14)────────────────────────────────
#
# 判废的子项来自 hard_fail_breakdown,那只统计**踩中硬门**的;而一条 episode 也可能
# 是综合加权分不达标被判废(见 pipeline/verdict.py),那种没有检查名、进不了这张表。
# 于是表上会出现「判废 16,子项 14+1」自己打自己脸。扫过 pod 上全部 49 份交付目前
# 都没踩到,但机制上迟早会。反方向也要防:一条同时踩中两个硬门,子项相加会大于总数。

def _with_drop(delivery_dir, drop, breakdown):
    """改一份现成交付的判废数字与子项分布(只动 passed.json 的那两个字段)。"""
    p = os.path.join(delivery_dir, "passed.json")
    doc = json.loads(open(p, encoding="utf-8").read())
    doc["dataset"]["verdict_drop"] = drop
    doc["dataset"]["hard_fail_breakdown"] = breakdown
    open(p, "w", encoding="utf-8").write(json.dumps(doc, ensure_ascii=False))
    return load_delivery(delivery_dir)


def _detail_rows(rows):
    return {k.strip("　├└ ").replace("其中 ", ""): v for k, v in rows
            if k.startswith("　") and "待人工裁决" not in k}


def test_overview_adds_a_row_when_the_hard_gates_do_not_add_up(full_delivery):
    """★ 判废 16、硬门子项只占 15 时,差额补一行「综合质量分不达标」。

    不补的话表上就是「判废 16,子项 14+1」——同一张表里两个数字对不上,读者第一
    反应是这张表算错了(而它只是漏掉了没有检查名的那一类)。
    """
    m = _with_drop(full_delivery, 16, {"task_success": 14, "timestamp_check": 1})
    rows = overview_rows(m)
    detail = _detail_rows(rows)
    assert detail == {"任务成败判定": 14, "时间戳检查": 1, "综合质量分不达标": 1}
    assert sum(detail.values()) == dict(rows)["判废"] == 16
    # 措辞用界面既有说法:「软分」是内部机制名,2026-08-11 就统一叫「质量分」了
    blob = str(rows)
    assert "软分" not in blob and "soft" not in blob.lower()
    # 仍然是分解式(能相加),所以最后一项挂 └
    assert [k for k, _ in rows if k.startswith("　└")]


def test_overview_stops_pretending_it_decomposes_when_items_overlap(full_delivery):
    """★ 一条同时踩中两个硬门 → 子项相加大于总数,这时不许再摆成 ├/└ 分解式。

    ├/└ 是在邀请读者把子项加起来,而这里加起来就是错的。改用「其中」的措辞,
    并在表下小字说明"同一条可能同时踩中多项"。
    """
    m = _with_drop(full_delivery, 15, {"task_success": 14, "timestamp_check": 2})
    rows = overview_rows(m)
    assert _detail_rows(rows) == {"任务成败判定": 14, "时间戳检查": 2}
    assert not [k for k, _ in rows if "├" in k or "└" in k]
    assert all("其中" in k for k, _ in rows if k.startswith("　"))
    assert "综合质量分不达标" not in str(rows)      # 相加已经多了,别再往上加


def test_overview_keeps_the_plain_decomposition_when_it_already_adds_up(full_delivery):
    """恰好加得出总数的老样子一个字不变(droid-200-full 就是这种)。"""
    m = load_delivery(full_delivery)
    rows = overview_rows(m)
    assert _detail_rows(rows) == {"任务成败判定": 14, "时间戳检查": 1}
    assert "综合质量分不达标" not in str(rows)
    # 表下解释小字已删(2026-08-23 用户),没有别的东西可断言


def test_overview_never_guesses_when_the_delivery_has_no_breakdown(full_delivery):
    """老交付没有 hard_fail_breakdown 字段 → 一行子项都不列,不猜、不占位。"""
    p = os.path.join(full_delivery, "passed.json")
    doc = json.loads(open(p, encoding="utf-8").read())
    doc["dataset"].pop("hard_fail_breakdown")
    open(p, "w", encoding="utf-8").write(json.dumps(doc, ensure_ascii=False))
    rows = overview_rows(load_delivery(full_delivery))
    assert _detail_rows(rows) == {}
    assert dict(rows)["判废"] == 15


def test_source_video_falls_back_to_data_root_when_path_not_recorded(tmp_path):
    """🔴 交付**只记数据集名、不记路径**(run.json 里就是 `数据集: droid_lerobot`),
    所以源数据集这条兜底路光靠交付自己解析不出目录 —— 不补这一手,它对绝大多数
    交付都是空的,等于没做(2026-08-19 实测:debug 交付的 source_dataset 就是 None)。

    用界面已知的「数据集根目录」把名字还原成路径:名字就是源目录名。
    """
    root = tmp_path / "datasets"
    src = root / "droid_lerobot"
    (src / "meta").mkdir(parents=True)
    (src / "meta" / "info.json").write_text('{"codebase_version": "v2.0"}',
                                            encoding="utf-8")
    d = src / "videos" / "chunk-000" / "observation.images.top"
    d.mkdir(parents=True)
    (d / "episode_000003.mp4").write_bytes(b"\x00" * 24 + b"ftyp" + b"\x00" * 9000)

    from curation.ui.manifest import source_video_paths
    # 交付没记路径(source_dataset 缺席),只有名字
    m = {"path": str(tmp_path / "deliv"), "name": "droid_lerobot",
         "episodes": {"ep000003": {"verdict": "拒绝"}}}
    assert source_video_paths(m, "ep000003") == [], "没给 data_root 时不该凭空猜路径"
    got = source_video_paths(m, "ep000003", str(root))
    assert len(got) == 1 and "episode_000003.mp4" in got[0], \
        f"给了数据集根目录还是找不到源视频:{got}"


# ───────── 人工裁决队列台账(2026-08-25 用户定):待办 + 已办结两堆合一 ─────────


def _ledger_delivery(tmp_path):
    """待办两条(ep0 成败弃权 / ep2 标注分歧)+ 台账两条(ep5 溯源派生 /
    ep9 存档直存)+ 复议台账一条(ep7,不进主队列)。"""
    d = tmp_path / "ledger"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps({
        "数据集": "x", "episodes": {
            "ep000000": {"判决": "通过", "checks": {
                "任务成败判定": {"结果": "弃权"}}},
            "ep000002": {"判决": "通过", "checks": {}},
            # 老交付形态:存档机制之前被 rejudge 搬走的条目,只有溯源块
            "ep000005": {"判决": "通过(人工裁决)", "checks": {},
                         "人工裁决": {"裁决": "判成功", "备注": "亲眼看的",
                                      "裁决时间": "t5"}},
            "ep000007": {"判决": "通过(人工复议)", "checks": {},
                         "人工复议": {"复议结论": "捞回", "复议时间": "t7"}}}},
        ensure_ascii=False))
    (d / "review.json").write_text(json.dumps({
        "待人工裁决总数": 1,
        "episodes": {"ep000000": {"当前判决": "通过",
                                  "待裁决项": ["任务成败判定"],
                                  "弃权原因": {"任务成败判定": "渐变问询不可判"}}},
        "标注-画面分歧复核队列": [{"id": "ep000002", "label": "open the door",
                                   "caption": "close the door"}],
        "已裁决存档": {"ep000009": {"线": "补判", "结论": "补判判失败",
                                    "补判判定": "failure", "去向": "进拒绝",
                                    "裁决时间": "t9", "应用时间": "t9"}}},
        ensure_ascii=False))
    (d / "reject.json").write_text(json.dumps({"被拒总数": 1, "episodes": {
        "ep000009": {"判决": "拒绝", "原因": "未通过「任务成败判定」(弃权补判)",
                     "checks": {}}}}, ensure_ascii=False))
    return load_delivery(str(d))


def test_adjudication_archive_merges_stored_and_derived(tmp_path):
    """新交付读「已裁决存档」堆;老交付从 passed/reject 溯源块派生兜底
    (droid-50 那批不重跑也能看全台账);同 id 存档为准。"""
    m = _ledger_delivery(tmp_path)
    arc = m["adjudication_archive"]
    assert arc["ep000009"]["线"] == "补判" and "_derived" not in arc["ep000009"]
    assert arc["ep000005"]["线"] == "成败" and arc["ep000005"]["_derived"] is True
    assert arc["ep000005"]["结论"] == "判成功"
    assert arc["ep000007"]["线"] == "复议" and arc["ep000007"]["结论"] == "捞回"
    assert "ep000000" not in arc                    # 待裁的不算台账


def test_merged_table_queue_orders_pending_then_archive(tmp_path):
    """全部档行序:待裁置顶(原队列序)→ 台账垫底(episode 序);台账行纯文字
    结论,复议线不进主队列(那是复议表的台账)。"""
    from curation.ui.manifest import merged_table_queue
    m = _ledger_delivery(tmp_path)
    items = merged_table_queue(m)
    assert [i["id"] for i in items] == ["ep000002", "ep000000",
                                        "ep000005", "ep000009"]
    assert [i["pending"] for i in items] == [True, True, False, False]
    by = {i["id"]: i for i in items}
    assert by["ep000005"]["result"] == "判成功"
    assert by["ep000005"]["kind"] == "成败弃权"
    assert by["ep000009"]["result"] == "补判判失败(failure)"
    assert "ep000007" not in by                     # 复议台账不进主队列


def test_merged_table_queue_status_filters(tmp_path):
    """「仅待裁决 / 仅已裁决」两档:一档只剩欠结论的,另一档只剩办结的。"""
    from curation.ui.manifest import (QUEUE_STATUS_DONE, QUEUE_STATUS_PENDING,
                                      merged_queue_rows, merged_table_queue)
    m = _ledger_delivery(tmp_path)
    pend = merged_table_queue(m, QUEUE_STATUS_PENDING)
    done = merged_table_queue(m, QUEUE_STATUS_DONE)
    assert [i["id"] for i in pend] == ["ep000002", "ep000000"]
    assert [i["id"] for i in done] == ["ep000005", "ep000009"]
    assert [r[0] for r in merged_queue_rows(m, QUEUE_STATUS_DONE)] == ["5", "9"]


def test_merged_table_queue_draft_gets_unapplied_suffix(tmp_path):
    """草稿(已裁未执行)与办结的文字区分 =「(未应用)」后缀(判据与顶部横幅
    同源 decisions_view;2026-08-25 用户否掉图标方案,只留文字)。裁完全部
    问题的草稿条目归"已裁决"档但仍在待办清单(卡片还能改判)。"""
    from curation.dataset_level.decisions import record_task_verdict
    from curation.ui.manifest import (QUEUE_STATUS_DONE, QUEUE_STATUS_PENDING,
                                      merged_table_queue)
    m = _ledger_delivery(tmp_path)
    record_task_verdict(m["path"], "ep000000", "判成功", note="")
    m = load_delivery(m["path"])
    it = [i for i in merged_table_queue(m) if i["id"] == "ep000000"][0]
    assert it["result"] == "判成功(未应用)" and it["pending"] is False
    assert "ep000000" not in [i["id"] for i in
                              merged_table_queue(m, QUEUE_STATUS_PENDING)]
    assert "ep000000" in [i["id"] for i in
                          merged_table_queue(m, QUEUE_STATUS_DONE)]


def test_appeal_rows_append_restored_ledger(tmp_path):
    """复议表尾补捞回台账:在案被拒条目在前,台账行(条目已回交付)垫底留痕。"""
    from curation.ui.manifest import appeal_rows
    m = _ledger_delivery(tmp_path)
    rows = appeal_rows(m)
    assert rows and rows[-1][1] == "ep000007" and rows[-1][-1] == "捞回"


def test_queue_filters_compose_and_counts_follow_status(tmp_path):
    """两组筛选联动(2026-08-25 用户点名):①类型档计数 = 当前状态档下的表行数
    (此前按待办卡片算,切「仅已裁决」还写 0);②表行 = 类型 × 状态交集;
    ③带计数的显示标签直接传回也认(界面上值就是带计数的)。"""
    from curation.ui.manifest import (QUEUE_STATUS_DONE, merged_filter_choices,
                                      merged_table_queue, queue_status_choices,
                                      queue_status_mode)
    m = _ledger_delivery(tmp_path)
    # 台账里 3 条成败线(ep5 派生/ep9 存档;ep7 复议不进主队列)+ 待办 2 条
    assert merged_filter_choices(m) == ["全部(4)", "只看标注问题(1)",
                                        "只看成败问题(3)"]
    assert merged_filter_choices(m, QUEUE_STATUS_DONE) == \
        ["全部(2)", "只看标注问题(0)", "只看成败问题(2)"]
    assert queue_status_choices(m) == ["全部(4)", "仅待裁决(2)", "仅已裁决(2)"]
    got = merged_table_queue(m, "仅已裁决(2)", "只看成败问题(2)")
    assert [i["id"] for i in got] == ["ep000005", "ep000009"]
    only_label = merged_table_queue(m, "全部(4)", "只看标注问题(1)")
    assert [i["id"] for i in only_label] == ["ep000002"]
    assert queue_status_mode("仅已裁决(2)") == QUEUE_STATUS_DONE
    assert queue_status_mode("看不懂的") == "全部"


def test_pending_bucket_reflects_recorded_conclusions(delivery):
    """轨迹 ⏳ 桶如实(2026-08-25 用户点名:名叫「待人工」就得真欠着人):
    给全结论(草稿即可)就离开 ⏳;「拿不准」不是结论;换成蓝条「已裁决
    (未应用)」说明,不许一声不吭。判据与人工裁决队列同源。"""
    from curation.ui.manifest import (BUCKET_PASSED, BUCKET_PENDING,
                                      bucket_counts, episode_bucket,
                                      manual_hint_html, question_pending_ids,
                                      record_label_decision,
                                      record_task_verdict)
    m = load_delivery(delivery)
    assert episode_bucket(m, "ep000000") == BUCKET_PENDING     # 成败弃权
    assert episode_bucket(m, "ep000002") == BUCKET_PENDING     # 标注分歧
    assert "待人工裁决" in manual_hint_html(m, "ep000000")
    record_task_verdict(m["path"], "ep000000", "拿不准")        # 不是结论
    assert episode_bucket(m, "ep000000") == BUCKET_PENDING
    record_task_verdict(m["path"], "ep000000", "判成功")
    record_label_decision(m["path"], "ep000002", "维持原标注")
    assert episode_bucket(m, "ep000000") == BUCKET_PASSED
    assert episode_bucket(m, "ep000002") == BUCKET_PASSED
    assert bucket_counts(m)[BUCKET_PENDING] == 0
    assert question_pending_ids(m) == set()
    h = manual_hint_html(m, "ep000000")
    assert "已裁决(未应用)" in h and "执行裁决" in h


def test_kill_seals_episode_and_other_dim_abstain_stays(tmp_path):
    """「弃用该条」封整条(② 被矛盾拦截,不再欠结论);其它维度的弃权
    (同步等)人工裁决页管不了,给了成败结论也照旧 ⏳。"""
    from curation.ui.manifest import (BUCKET_PASSED, BUCKET_PENDING,
                                      episode_bucket, record_label_decision)
    d = tmp_path / "seal"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps({
        "数据集": "x", "episodes": {
            "ep000001": {"判决": "通过", "checks": {
                "任务成败判定": {"结果": "弃权"}}},
            "ep000002": {"判决": "通过", "checks": {
                "视频-动作同步": {"结果": "弃权"}}}}}, ensure_ascii=False))
    (d / "review.json").write_text(json.dumps({
        "episodes": {
            "ep000001": {"当前判决": "通过", "待裁决项": ["任务成败判定"],
                         "弃权原因": {"任务成败判定": "渐变问询不可判"}},
            "ep000002": {"当前判决": "通过", "待裁决项": ["视频-动作同步"],
                         "弃权原因": {"视频-动作同步": "信号不足"}}},
        "标注-画面分歧复核队列": [{"id": "ep000001", "label": "a",
                                   "caption": "b"}]}, ensure_ascii=False))
    m = load_delivery(str(d))
    assert episode_bucket(m, "ep000001") == BUCKET_PENDING
    assert episode_bucket(m, "ep000002") == BUCKET_PENDING
    record_label_decision(m["path"], "ep000001", "弃用该条")
    assert episode_bucket(m, "ep000001") == BUCKET_PASSED      # 封条,不欠了
    assert episode_bucket(m, "ep000002") == BUCKET_PENDING     # 没人能替它答


def test_queue_kind_keeps_task_dimension_after_apply(tmp_path):
    """待裁问题列如实(2026-08-25 用户实报):两类问题都有的条目,成败线裁决
    执行(办结进台账)后名册行仍要标「标注+成败」,不许缩成「标注分歧」。"""
    from curation.ui.manifest import merged_table_queue
    m = _ledger_delivery(tmp_path)
    # ep000002 既在分歧名册又有成败台账 → 造一个这样的交付
    d = tmp_path / "both"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps({
        "数据集": "x", "episodes": {
            "ep000017": {"判决": "通过(人工裁决)", "checks": {},
                         "人工裁决": {"裁决": "判成功", "裁决时间": "t"}}}},
        ensure_ascii=False))
    (d / "review.json").write_text(json.dumps({
        "episodes": {},
        "标注-画面分歧复核队列": [{"id": "ep000017", "label": "a",
                                   "caption": "b"}]}, ensure_ascii=False))
    m2 = load_delivery(str(d))
    it = [i for i in merged_table_queue(m2) if i["id"] == "ep000017"][0]
    assert it["kind"] == "标注+成败"


def test_merged_card_deck_mirrors_table(tmp_path):
    """卡片清单与队列表同源同序(2026-08-25 用户定:点哪行显示哪条):
    台账条目也有卡(audit/task 皆 None + resolved 事件),下标一一对应。"""
    from curation.ui.manifest import merged_card_deck, merged_table_queue
    m = _ledger_delivery(tmp_path)
    deck = merged_card_deck(m)
    assert [d["id"] for d in deck] == [i["id"] for i in merged_table_queue(m)]
    by = {d["id"]: d for d in deck}
    assert by["ep000000"]["task"] is not None            # 待办条目原样
    # 台账卡:audit 无(标注线不裁),task 给最小条目 —— 方向 A(2026-08-25):
    # 执行后允许改判,② 成败问题照 required 渲染,按钮可点
    assert by["ep000005"]["audit"] is None
    assert by["ep000005"]["task"] == {"id": "ep000005", "current": "",
                                      "reason": "", "readings": {}, "state": ""}
    assert by["ep000005"]["resolved"]["结论"] == "判成功"  # 台账卡带当时结论
    # 类型档过滤与表一致
    only_label = merged_card_deck(m, "只看标注问题(1)")
    assert [d["id"] for d in only_label] == ["ep000002"]


def test_archived_row_result_prefers_unapplied_redecision(tmp_path):
    """执行后改判(方向 A):台账行有未应用的新草稿时,「裁决结果」列以草稿
    为准(带「(未应用)」后缀)——不然表里挂着旧结论,人以为没记上。"""
    from curation.dataset_level.decisions import record_task_verdict
    from curation.ui.manifest import merged_table_queue
    m = _ledger_delivery(tmp_path)
    it = [i for i in merged_table_queue(m) if i["id"] == "ep000005"][0]
    assert it["result"] == "判成功"                       # 无草稿:显示台账结论
    record_task_verdict(m["path"], "ep000005", "判失败", note="看错了,改判")
    m = load_delivery(m["path"])
    it = [i for i in merged_table_queue(m) if i["id"] == "ep000005"][0]
    assert it["result"] == "判失败(未应用)"


def test_banner_counts_episodes_and_items_separately(tmp_path):
    """条/项双报(2026-08-25 用户定):双问题条目裁完 = 1 条(2 项),
    横幅两级计数各归各,不再把项数冒充轨迹数。"""
    from curation.dataset_level.decisions import (record_label_decision,
                                                  record_task_verdict)
    from curation.ui.manifest import unapplied_banner_md
    d = tmp_path / "both2"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps({
        "数据集": "x", "episodes": {
            "ep000007": {"判决": "通过", "checks": {
                "任务成败判定": {"结果": "弃权"}}}}}, ensure_ascii=False))
    (d / "review.json").write_text(json.dumps({
        "episodes": {"ep000007": {"当前判决": "通过",
                                  "待裁决项": ["任务成败判定"],
                                  "弃权原因": {"任务成败判定": "r"}}},
        "标注-画面分歧复核队列": [{"id": "ep000007", "label": "a",
                                   "caption": "b"}]}, ensure_ascii=False))
    m = load_delivery(str(d))
    record_label_decision(m["path"], "ep000007", "采纳建议改标", new_label="b")
    record_task_verdict(m["path"], "ep000007", "判成功")
    banner = unapplied_banner_md(load_delivery(str(d)))
    assert "已裁 1 条(2 项)" in banner
    assert "待裁 0 条(0 项)" in banner
    assert "<b>1 条(2 项)</b>尚未应用于交付" in banner


# ── 批次自带片段(batch_clip_paths,2026-08-28 切片入交付)────────────────


def _clips_batch(tmp_path, with_files=(), origin_url=None):
    b = tmp_path / "deliv" / "20260828-000000"
    (b / "details").mkdir(parents=True)
    (b / "details" / "review_clips_index.json").write_text(
        json.dumps({"ep000001": ["cam_a", "cam_b"]}), encoding="utf-8")
    for f in with_files:
        (b / "review_clips").mkdir(exist_ok=True)
        (b / "review_clips" / f).write_bytes(b"x")
    if origin_url:
        from curation.tos_store import ORIGIN_NAME
        (tmp_path / "deliv" / ORIGIN_NAME).write_text(
            json.dumps({"delivery_url": origin_url}), encoding="utf-8")
    return b


def test_batch_clip_paths_prefers_local_files(tmp_path):
    from curation.ui.manifest import batch_clip_paths
    b = _clips_batch(tmp_path,
                     with_files=("ep000001__cam_a.mp4", "ep000001__cam_b.mp4"))
    got = batch_clip_paths({"path": str(b)}, "ep000001")
    assert [os.path.basename(p) for p in got] == \
        ["ep000001__cam_a.mp4", "ep000001__cam_b.mp4"]
    assert all(os.path.isabs(p) for p in got)
    assert batch_clip_paths({"path": str(b)}, "ep000099") == []


def test_batch_clip_paths_falls_back_to_origin_url(tmp_path):
    """直连交付:片段被轻镜像跳过,本地没有 → 按 .tos-origin.json 拼批次
    tos:// URL(播放端现签公网预签名);没有 origin → 空表,不乱猜。"""
    from curation.ui.manifest import batch_clip_paths
    b = _clips_batch(tmp_path, origin_url="tos://bkt/deliveries/x")
    got = batch_clip_paths({"path": str(b)}, "ep000001")
    assert got == [
        "tos://bkt/deliveries/x/20260828-000000/review_clips/ep000001__cam_a.mp4",
        "tos://bkt/deliveries/x/20260828-000000/review_clips/ep000001__cam_b.mp4"]
    b2 = _clips_batch(tmp_path / "n2")
    assert batch_clip_paths({"path": str(b2)}, "ep000001") == []


def test_batch_origin_url_registers_region_for_playback(tmp_path):
    """读 .tos-origin.json 顺手登记桶地区:异地(上海)交付镜像重开进程后,
    播放端签名照样拿对地区,不依赖本次会话谁先调过 make_store_for。"""
    from curation import tos_store
    from curation.ui.manifest import _batch_origin_url
    tos_store.clear_bucket_regions()
    b = tmp_path / "d" / "20260828-000000"
    b.mkdir(parents=True)
    (tmp_path / "d" / ".tos-origin.json").write_text(json.dumps(
        {"delivery_url": "tos://sh-deliv-bkt/so101/x",
         "region": "cn-shanghai"}), encoding="utf-8")
    url = _batch_origin_url({"path": str(b)})
    assert url == "tos://sh-deliv-bkt/so101/x/20260828-000000"
    assert tos_store.bucket_region("sh-deliv-bkt") == "cn-shanghai"
    tos_store.clear_bucket_regions()


