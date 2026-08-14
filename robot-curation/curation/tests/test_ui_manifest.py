"""UI 数据层测试(2026-07-27 U1)。

契约:manifest.py 是"交付目录 → UI"的唯一读端,纯函数无副作用;
fixture 按 U0 定型的真实交付 schema 造(passed/reject/review 三 JSON +
details/evidence 帧 + details/plots 同步图),UI 逻辑全部在此测,
Gradio 层只剩摆组件(构造冒烟测试放最后,无 gradio 环境自动跳过)。
"""
from __future__ import annotations

import json
import os
import signal

import pytest

from curation.ui.manifest import (AUDIT_TERM, audit_note_md, audit_rows,
                                  check_rows,
                                  discover_deliveries, load_delivery,
                                  overview_markdown, overview_note_md,
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
    assert ts[1] == "拒绝" and "voc=0.87" in ts[3]


def test_skill_audit_overview(delivery):
    m = load_delivery(delivery)
    sk = skill_rows(m)
    assert sk[0][0] == "Arrange" and sk[0][1] == "Arrange soft goods" and sk[0][2] == 2
    au = audit_rows(m)
    # v3 行结构:[操作, 档位, episode, 原标注, 自产描述, 成败线判定, 分歧说明, 裁决]
    assert au[0][0] == "裁决 ▶"                       # 可点性操作列
    assert au[0][2] == "ep000002" and au[0][6] == "跨族"
    assert au[0][1] in ("重点", "参考")
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
    assert overview_note_md(m) == ""                            # 没有表就没有口径小字


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


def test_app_builds_without_server(delivery):
    """Gradio 冒烟:四 tab 构造成功即可(不 launch)。无 gradio 环境自动跳过。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    app = build_app(delivery)
    assert app is not None


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


# ───────── U3/U4 终端工作区(双层导航 + 内嵌网页终端)─────────
#
# U3(2026-07-28)是 ttyd 旁挂进程 + iframe;U4(2026-07-29)换成内嵌式:终端是 UI
# 自己的 /ws/term(forkpty bash),前端 xterm.js 从 /term-static/ 取。开关语义也从
# 「--terminal-url 给不给地址」变成布尔 --terminal(env CURATION_TERMINAL 等价)。


def test_cli_parses_terminal_flag(monkeypatch):
    """`ui --terminal` 是布尔开关;env CURATION_TERMINAL 提供缺省值。"""
    from curation.cli import build_parser
    monkeypatch.delenv("CURATION_TERMINAL", raising=False)   # 缺省值取自 env,先清干净
    assert build_parser().parse_args(["ui", "--delivery", "/d", "--terminal"]).terminal is True
    assert build_parser().parse_args(["ui", "--delivery", "/d"]).terminal is False
    monkeypatch.setenv("CURATION_TERMINAL", "1")
    assert build_parser().parse_args(["ui", "--delivery", "/d"]).terminal is True   # env 生效
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("CURATION_TERMINAL", falsy)
        assert build_parser().parse_args(["ui", "--delivery", "/d"]).terminal is False, falsy
    # 老开关必须真的没了(别留个被忽略的参数骗人)
    monkeypatch.delenv("CURATION_TERMINAL", raising=False)
    with pytest.raises(SystemExit):
        build_parser().parse_args(["ui", "--delivery", "/d", "--terminal-url", "http://x"])


def _config_text(app) -> str:
    """Blocks 配置(含全部组件的 label/value)拍平成字符串,便于断言与定序。"""
    return json.dumps(app.get_config_file(), ensure_ascii=False, default=str)


def _report_section(app) -> str:
    """配置里「质检报告」之后的那一段。

    2026-08-13 起顶层多了「任务台」,它的模块多选框用的是同一批语义名
    (技能画像/精确去重/任务成败判定…),整份配置里 str.index() 会先命中那边。
    报告页子页签的**顺序**断言因此必须限定在报告段内比——守的还是原来那件事,
    只是定位更精确了。
    """
    cfg = _config_text(app)
    return cfg[cfg.index("质检报告"):]


def test_app_with_terminal_tab(delivery):
    """terminal=True:有「终端」页签 + xterm 容器 div,且它排在**最右**。

    顺序 2026-08-13 用户定:任务台 / 质检报告 / 终端,默认落地页是任务台 ——
    终端是我们排障用的,不该占客户第一眼的位置。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = _config_text(build_app(delivery, terminal=True))
    assert "终端" in cfg and "curation-term-screen" in cfg
    assert "iframe" not in cfg and "7681" not in cfg      # ttyd 时代的痕迹一点不留
    assert "质检报告" in cfg
    assert cfg.index("质检报告") < cfg.index("终端")   # 终端在最右
    assert cfg.index("任务台") < cfg.index("质检报告")  # 任务台在最左
    # 六个子 tab 一个不少
    for t in ("质检总览", "Episodes", "技能分布", "卡顿动作时间线", "明细"):
        assert t in cfg


def test_app_without_terminal_leaves_no_terminal_trace(delivery):
    """terminal=False:配置里连「终端」二字都没有,报告页那套子页签照旧。

    断言口径 2026-08-13 收窄过:顶层导航此前**只在开终端时**渲染,所以老断言连
    「质检报告」四个字都不许出现。现在「任务台」与「质检报告」是常驻的顶层两页
    (用户定:面板是面向客户的那张脸),顶层标题必然出现 —— 本测试真正要守的是
    "客户部署里看不到终端入口",那一条一个字没松:终端页签、xterm 容器、
    /ws/term 路由与资产仍然全都不存在(后者见 test_asgi_app_without_terminal)。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = _config_text(build_app(delivery))
    assert "终端" not in cfg and "curation-term-screen" not in cfg
    assert "质检报告" in cfg and "任务台" in cfg
    for t in ("质检总览", "Episodes", "技能分布", "卡顿动作时间线", "明细"):
        assert t in cfg


def test_task_console_is_top_level_and_report_tabs_untouched(delivery):
    """任务台与质检报告并列在顶层;报告页那套子页签一个不少、顺序不变。

    用户红线(2026-08-13):UI 改动绝不能影响质检报告那套页签。这条把它钉死。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = _config_text(build_app(delivery))
    assert cfg.index("任务台") < cfg.index("质检报告")
    # 报告页九个子页签一个不少。**顺序**由既有的 test_app_has_manual_decision_tab
    # 等几条守,这里不重复钉 —— 这些词在正文里也出现(技能画像页有一行指向人工
    # 裁决),硬排序只会造出一条脆测试。
    rep = _report_section(build_app(delivery))
    for t in ("质检总览", "Episodes", "人工裁决", "技能分布", "视频-动作同步",
              "卡顿动作时间线", "明细", "性能剖析"):
        assert t in rep, t
    for t in ("跑质检", "执行人工裁决", "任务与日志"):   # 生成视频片段/模型服务已并入别处
        assert t in cfg, t


def test_probe_buttons_are_wired(delivery):
    """「检测可用性」必须真接了事件。

    2026-08-13 实测:这两个按钮建出来了却谁也没接,客户点了毫无反应,还以为
    是服务挂了。按钮不接线在界面上看不出来(截图里它长得跟能用的一样),只能
    靠这条钉住。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    app = build_app(delivery)
    probes = {i for i, c in app.blocks.items()
              if getattr(c, "value", None) == "检测可用性"}
    assert probes, "界面上找不到「检测可用性」按钮"
    wired = {t[0] for fn in app.fns.values() for t in getattr(fn, "targets", [])}
    assert probes <= wired, "有「检测可用性」按钮没接事件"


def test_console_knobs_to_set_overrides():
    """面板上的旋钮 → --set 列表:**只发用户动过的**,留空一律不发。

    界面不许复制一份默认值 —— 复制了就会出现"改了 default.yaml,界面还在按老值
    发"的静默不一致。手写的排最后:同键后到者赢,人手写的优先级最高。
    """
    from curation.ui.app import CONC_KEYS, PLOT_MODES, _sets

    assert _sets(PLOT_MODES["flagged"], None, None, None, "") == []   # 全默认 = 一条不发
    assert _sets(PLOT_MODES["all"], None, None, None, "") == ["pipeline.sync_plots=all"]
    assert _sets(PLOT_MODES["off"], None, None, None, "") == ["pipeline.sync_plots=off"]
    got = _sets(PLOT_MODES["flagged"], 8, 4, 16, "")
    assert got == [f"{CONC_KEYS['ep']}=8", f"{CONC_KEYS['fr']}=4",
                   f"{CONC_KEYS['cap']}=16"]
    assert _sets(PLOT_MODES["flagged"], 0, "", None, "") == []        # 0/空当没填
    assert _sets(PLOT_MODES["flagged"], "八", "-3", "  ", "") == []   # 填错也不拦着开跑
    assert _sets(PLOT_MODES["flagged"], " 12 ", None, None, "") == [f"{CONC_KEYS['ep']}=12"]
    tail = _sets(PLOT_MODES["all"], 8, None, None, " a.b=1 \n\n c.d=2 ")
    assert tail[-2:] == ["a.b=1", "c.d=2"]                            # 手写的在最后


def test_concurrency_defaults_are_shown_as_placeholders_not_prefilled():
    """并发框把生效配置里的默认值说给用户听,但只放**占位符**。

    2026-08-13 用户要"界面上看得到默认值",而预填 value 等于在界面里存第二份默认
    值:以后改了 default.yaml,界面还会按老值发 `--set`,静默地把配置改回去。
    """
    from curation.ui.app import _conc_placeholder

    assert _conc_placeholder(32) == "默认 32,留空即用它"
    assert _conc_placeholder(None) == "留空 = 用配置里的值"   # 读不到就不编数字


def test_concurrency_boxes_carry_the_default_in_the_placeholder(delivery):
    """界面这一侧钉死同一件事:占位符里带出厂默认值(32/16/32),value 仍是空。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = json.loads(json.dumps(build_app(delivery).get_config_file(), default=str))
    boxes = [c for c in cfg["components"]
             if "conc-num" in (c.get("props", {}).get("elem_classes") or [])]
    assert len(boxes) == 3
    for c in boxes:
        assert c["props"]["placeholder"].startswith("默认 ")
        assert not c["props"].get("value")            # 一个数都没预填


def test_probe_rescan_picks_up_a_backend_added_after_startup():
    """探活时重读配置:启动之后才加进站点 YAML 的服务也要出现在下拉里,
    而用户已经选中的那一项不能被刷掉(他可能正要拿它开跑)。"""
    from curation.ui.app import _reprobe_options

    old = {"方舟 MaaS · doubao-seed": "ark"}
    new = dict(old, **{"自托管 vLLM · Cosmos-Reason2-32B · H20": "house-32b"})
    ch, vals, msg = _reprobe_options(old, new, {"ark": True, "house-32b": True},
                                     ["方舟 MaaS · doubao-seed", None])
    assert any("Cosmos-Reason2-32B" in c for c in ch)
    assert vals[0] == "方舟 MaaS · doubao-seed · 可用"     # 原选中项仍在
    assert vals[1] is None
    assert "新增" in msg


def test_probe_rescan_clears_a_selection_that_left_the_config():
    """选中的预设从配置里删掉了 → 回到未选状态并说清楚,不留一个指向不存在服务的
    选项(选了它开跑,要等到连接失败才知道)。提示里只报数目,不露预设代号。"""
    from curation.ui.app import _reprobe_options

    ch, vals, msg = _reprobe_options({"甲服务": "old-8b"}, {"乙服务": "new-8b"},
                                     {"new-8b": False}, ["甲服务"])
    assert ch == ["乙服务 · 暂不可用"] and vals == [None]
    assert "请重新选" in msg and "一个都连不上" in msg
    assert "old-8b" not in msg and "new-8b" not in msg     # 代号不进界面


def test_probe_button_rereads_the_config_without_restarting_the_ui(delivery, monkeypatch):
    """客户在站点 YAML 里新加一台自托管服务,不该为了看见它去重启 UI ——
    重启会杀掉正在跑的批(那些任务是 UI 进程的子进程)。"""
    pytest.importorskip("gradio")
    from curation.ui import app as ui_app
    from curation.ui import runner as ui_runner

    live = {"方舟 MaaS · doubao-seed": "ark"}
    monkeypatch.setattr(ui_runner, "vlm_backend_labels", lambda cfg=None: dict(live))
    monkeypatch.setattr(ui_app, "_probe_backends",
                        lambda cfg, t: [[c, "✅在线", ""] for c in live.values()])
    app = ui_app.build_app(delivery)
    live["自托管 vLLM · Cosmos-Reason2-32B · H20"] = "house-32b"   # 启动之后才加的

    probes = {i for i, c in app.blocks.items()
              if getattr(c, "value", None) == "检测可用性"}
    fn = next(f for f in app.fns.values()
              if probes & {t[0] for t in getattr(f, "targets", [])})
    out = fn.fn("方舟 MaaS · doubao-seed", None)
    assert any("Cosmos-Reason2-32B" in str(c) for c in out[0]["choices"])
    assert "方舟" in str(out[0]["value"])


def test_vlm_involved_decides_concurrency_greying():
    """并发旋钮只在真调 VLM 时可用;自选模块要按两种语义分别算。"""
    from curation.ui.app import CUSTOM_SCAN, FULL_SCAN, QUICK_SCAN, _vlm_involved

    assert _vlm_involved(FULL_SCAN, [], "只跑选中") is True
    assert _vlm_involved(QUICK_SCAN, [], "只跑选中") is False
    # 只跑选中:选了才算跑
    assert _vlm_involved(CUSTOM_SCAN, ["task_success"], "只跑选中") is True
    assert _vlm_involved(CUSTOM_SCAN, ["motion_quality"], "只跑选中") is False
    # 跳过选中:把两个 VLM 步都跳了才算不跑(只跳一个仍然要调模型)
    assert _vlm_involved(CUSTOM_SCAN, ["task_success", "skill_profile"],
                         "跳过选中") is False
    assert _vlm_involved(CUSTOM_SCAN, ["task_success"], "跳过选中") is True


# ───────── U4 内嵌终端:ASGI 应用装配 / 鉴权 / PTY 往返 ─────────

def _paths(app) -> set:
    return {getattr(r, "path", None) for r in app.routes}


@pytest.fixture
def clean_ui_env(monkeypatch):
    """UI 的开关全从 env 读缺省值——每个用例先把它们清干净,免得互相串味。"""
    for k in ("CURATION_TERMINAL", "CURATION_UI_USER", "CURATION_UI_PASSWORD",
              "CURATION_TERMINAL_WORKDIR", "CURATION_TERMINAL_SHELL"):
        monkeypatch.delenv(k, raising=False)


def test_terminal_off_registers_no_routes(delivery, clean_ui_env):
    """不开终端:/ws/term 与静态资产整个不存在(404),与「不传就没有」的老行为对齐。"""
    pytest.importorskip("gradio")
    from starlette.testclient import TestClient

    from curation.ui.app import create_asgi_app
    app = create_asgi_app(delivery, terminal=False)
    assert "/ws/term" not in _paths(app) and "/term-static" not in _paths(app)
    with TestClient(app) as c:
        assert c.get("/").status_code == 200          # 报告页照常
        assert c.get("/healthz").status_code == 200   # 探针端点始终在
        assert c.get("/term-static/xterm.js").status_code == 404


def test_terminal_on_registers_routes_and_assets(delivery, clean_ui_env):
    """开终端:/ws/term 路由在,vendored 的 xterm.js/css/addon-fit/term.js 都取得到。"""
    pytest.importorskip("gradio")
    from starlette.testclient import TestClient

    from curation.ui.app import create_asgi_app
    app = create_asgi_app(delivery, terminal=True)
    assert "/ws/term" in _paths(app)
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        for asset in ("xterm.js", "xterm.css", "addon-fit.js", "term.js"):
            r = c.get(f"/term-static/{asset}")
            assert r.status_code == 200 and r.content, asset
        # 前端装配注入到了首页 head(不注入 = 页签打开是块黑板)
        html = c.get("/").text
        for asset in ("xterm.css", "xterm.js", "addon-fit.js", "term.js"):
            assert f"/term-static/{asset}" in html, asset


def test_basic_auth_401_then_200(delivery, monkeypatch, clean_ui_env):
    """配了 CURATION_UI_USER + PASSWORD:全路由 401,凭证对了才 200;/healthz 永远豁免。"""
    pytest.importorskip("gradio")
    from starlette.testclient import TestClient

    from curation.ui.app import create_asgi_app
    monkeypatch.setenv("CURATION_UI_USER", "demo")
    monkeypatch.setenv("CURATION_UI_PASSWORD", "s3cret")
    app = create_asgi_app(delivery, terminal=True)
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 401 and "Basic" in r.headers.get("www-authenticate", "")
        assert c.get("/term-static/xterm.js").status_code == 401
        assert c.get("/healthz").status_code == 200                   # 探针豁免
        assert c.get("/", auth=("demo", "wrong")).status_code == 401
        assert c.get("/", auth=("nobody", "s3cret")).status_code == 401
        assert c.get("/", auth=("demo", "s3cret")).status_code == 200
        assert c.get("/term-static/term.js", auth=("demo", "s3cret")).status_code == 200


def test_basic_auth_covers_websocket(delivery, monkeypatch, clean_ui_env):
    """鉴权必须**盖住 WS**(/ws/term 是真 shell;BaseHTTPMiddleware 会漏掉它,所以写的裸 ASGI)。"""
    pytest.importorskip("gradio")
    from starlette.testclient import TestClient

    from curation.ui.app import create_asgi_app
    monkeypatch.setenv("CURATION_UI_USER", "demo")
    monkeypatch.setenv("CURATION_UI_PASSWORD", "s3cret")
    app = create_asgi_app(delivery, terminal=True)
    with TestClient(app) as c:
        with pytest.raises(Exception):        # 无凭证握手被拒(断连或 401 denial response)
            with c.websocket_connect("/ws/term"):
                pass


def test_basic_auth_not_enabled_when_half_configured(delivery, monkeypatch, clean_ui_env):
    """只配用户名不配密码 = 不启用(半配的鉴权最坏:自以为锁了其实没锁)。"""
    pytest.importorskip("gradio")
    from starlette.testclient import TestClient

    from curation.ui.app import create_asgi_app
    monkeypatch.setenv("CURATION_UI_USER", "demo")
    app = create_asgi_app(delivery, terminal=False)
    with TestClient(app) as c:
        assert c.get("/").status_code == 200


def _read_until(ws, needle: bytes, times: int = 1, timeout: float = 20.0) -> bytes:
    """从 WS 上读二进制帧,直到 needle 累计出现 times 次,返回收到的全部字节。

    TestClient 的 receive 没有超时参数,一旦服务端不吐字节整条测试就挂死 → 用 SIGALRM
    兜底(pytest 用例跑在主线程,setitimer 可用;macOS 也没有 timeout(1) 可借)。
    """
    got = bytearray()

    def _boom(*_):
        raise TimeoutError(f"{timeout}s 内没等到 {needle!r}×{times},只收到 {bytes(got)[:400]!r}")

    previous = signal.signal(signal.SIGALRM, _boom)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        while got.count(needle) < times:
            got += ws.receive_bytes()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return bytes(got)


def test_ws_term_pty_roundtrip(delivery, tmp_path, monkeypatch, clean_ui_env):
    """真握手 + 真 PTY:发一条 `echo <token>` 的 input 帧,读回 PTY 吐出来的 token。

    顺带验证 CURATION_TERMINAL_WORKDIR:shell 的落脚目录就是它(pwd 打出来对得上)。
    """
    pytest.importorskip("gradio")
    from starlette.testclient import TestClient

    from curation.ui.app import create_asgi_app
    workdir = tmp_path / "落脚点"
    workdir.mkdir()
    monkeypatch.setenv("CURATION_TERMINAL_WORKDIR", str(workdir))
    app = create_asgi_app(delivery, terminal=True)
    with TestClient(app) as c, c.websocket_connect("/ws/term") as ws:
        # 协议原样复刻同事的实现:控制/输入都是 JSON **文本**帧,回程是二进制帧
        ws.send_text(json.dumps({"type": "resize", "cols": 100, "rows": 30}))
        ws.send_text(json.dumps({"type": "input", "data": "echo hello-ws-term-42\n"}))
        # 终端回显(命令行本身)+ 命令输出 = token 出现两次 → shell 真跑了,不只是回显
        _read_until(ws, b"hello-ws-term-42", times=2)
        ws.send_text(json.dumps({"type": "input", "data": "pwd\n"}))
        assert workdir.name.encode() in _read_until(ws, workdir.name.encode())


def test_ws_term_closes_when_shell_exits(delivery, clean_ui_env):
    """shell 自己 `exit` 之后服务端要主动关连接(不关 = 前端一直转,PTY fd 也漏着)。"""
    pytest.importorskip("gradio")
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from curation.ui.app import create_asgi_app
    app = create_asgi_app(delivery, terminal=True)
    with TestClient(app) as c, c.websocket_connect("/ws/term") as ws:
        ws.send_text(json.dumps({"type": "input", "data": "exit\n"}))
        with pytest.raises(WebSocketDisconnect):
            _read_until(ws, "\x00此串永不出现\x00".encode())   # 只会以「对端关闭」告终


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
    """延时表:四桶语义化中文标签、固定顺序、五个统计列齐全(实现标签不露头)。"""
    from curation.ui.manifest import LATENCY_HEADERS, latency_rows, load_perf
    rows = latency_rows(load_perf(_with_perf(delivery)))
    assert [r[0] for r in rows] == ["任务判定探针", "终态复核", "技能打标", "体系归纳"]
    # 表头写全"响应时间",别指望客户认得裸 P50/P90(第二轮反馈)
    assert LATENCY_HEADERS == ["调用类型", "调用次数", "平均响应时间(秒)",
                               "P50 响应时间(秒)", "P90 响应时间(秒)",
                               "P99 响应时间(秒)"]
    probe = rows[0]
    assert probe[1] == 1583 and probe[2] == "20.01" and probe[5] == "63.26"
    flat = json.dumps(rows, ensure_ascii=False)
    for impl_tag in ("probe", "endstate", "caption", "llm", "arbitration"):
        assert impl_tag not in flat, impl_tag        # 埋点标签只是字典键,不是界面词


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
    root = pathlib.Path(__file__).resolve().parent.parent
    tags = set()
    for py in root.rglob("*.py"):
        if "tests" in py.parts:
            continue
        tags |= set(_re.findall(r'latency_record\(\s*["\']([a-z_]+)["\']', py.read_text(encoding="utf-8")))
    assert tags, "一个埋点都没扫到 —— 正则该跟着 latency_record 的写法改"
    missing = sorted(t for t in tags if t not in LATENCY_LABELS)
    assert not missing, f"这些埋点没有中文名,会在界面上露英文:{missing}"


def test_latency_bar_chart_is_wall_clock_only(delivery):
    """横条图:四条、按**墙钟**降序、最长的那条 100%,时长人性化,失败次数带出来。"""
    from curation.ui.manifest import latency_bar_html, load_perf
    html = latency_bar_html(load_perf(_with_perf(delivery)))
    assert html.count("<div style=\"margin:8px 0\">") == 4
    # probe 墙钟 2530s 最大 → 排第一、宽度 100%、念作 42 分 10 秒
    assert html.index("任务判定探针") < html.index("技能打标") < html.index("终态复核")
    assert "width:100.00%" in html
    assert "墙钟 <b>42 分 10 秒</b>(1583 次调用并发执行)" in html
    assert "失败 2" in html                                  # endstate 的 errors
    assert "忙碌区间并集" in html          # 口径文案(2026-08-06 随 wall_s 并集口径更新)
    assert "各条墙钟相加 ≠ 整次运行总时长" in html


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


def test_latency_kind_notes_explain_all_four_call_types():
    """四类调用各配一句人话说明——语义化名字不解释,客户仍读不懂 1583 次是什么。"""
    from curation.ui.manifest import LATENCY_KIND_NOTE, LATENCY_PCTL_NOTE
    assert "一半的调用不超过此耗时" in LATENCY_PCTL_NOTE
    for label in ("任务判定探针", "终态复核", "技能打标", "体系归纳"):
        assert f"**{label}**" in LATENCY_KIND_NOTE, label
    assert "次数最多" in LATENCY_KIND_NOTE              # 探针次数为何最多(措辞 2026-08-13 精简)
    assert "只对没通过一审的数据跑" in LATENCY_KIND_NOTE
    for impl_tag in ("probe", "endstate", "caption", "llm"):
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


def test_app_has_perf_tab(delivery):
    """Gradio 层:「性能剖析」在,且报告页那几个页签一个不少。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = _config_text(build_app(_with_perf(delivery)["path"]))
    for t in ("质检总览", "Episodes", "技能分布", "卡顿动作时间线", "明细",
              "性能剖析"):
        assert t in cfg, t
    assert "延时剖析" in cfg
    for codename in ("h20-", "a30-8b"):
        assert codename not in cfg, codename


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
    assert "共 3 个技能族" in html and "覆盖 195 条数据" in html


def test_skill_chart_flat_shape_marks_degradation(tmp_path):
    """扁平形状:必须当面写清是「未经 VLM 审计的原始标注分组」,且一个 details 都没有。"""
    from curation.ui.manifest import SKILL_FALLBACK_NOTE, skill_bar_html, skill_chart_items
    m = _skills_delivery(tmp_path, FLAT_SKILLS)
    shape, items = skill_chart_items(m)
    assert shape == "flat" and [it["name"] for it in items][0] == "(无指令)"
    html = skill_bar_html(m)
    assert SKILL_FALLBACK_NOTE in html and "降级" in html and "仅供参考" in html
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
    assert html.count("样本偏少") == 2                # 1 个 chip + 1 句出处说明
    assert "画像自带的名单" in html and "不是本图现算的" in html
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


def test_app_skill_chart_wired_into_tab(delivery):
    """Gradio 层:技能画像页多了一块 HTML,且 _load 的返回数与输出组件数对得上。

    (输出数对不上是 Gradio 运行期才炸的接线错误,构造冒烟测不出来,这里直接调。)
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    app = build_app(delivery)
    loads = [f for f in app.fns.values() if getattr(f.fn, "__name__", "") == "_load"]
    assert loads, "找不到 _load 依赖"
    for f in loads:
        assert len(f.fn(delivery)) == len(f.outputs)
    html = [o for f in loads for o in f.outputs
            if type(o).__name__ == "HTML"]
    assert html, "技能画像页应有 HTML 组件承载分布图"


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
    assert audit_rows(load_delivery(str(old)))[0][2] == "ep1"       # 老交付打得开
    row = audit_rows(load_delivery(str(new)))[0]
    assert row[2] == "ep2" and "自产描述" in row[6] and "画面=" not in row[6]
    assert row[1] == "参考"                    # 老数据无 priority 字段 → 默认参考,不崩


def test_label_decision_roundtrip(delivery):
    from curation.ui.manifest import (load_label_decisions, record_label_decision,
                                      load_delivery)
    m = load_delivery(delivery)
    assert load_label_decisions(m) == {}                       # 初始无裁决
    msg = record_label_decision(m["path"], "ep000002", "采纳建议改标",
                                new_label="wipe the table", note="核对过视频")
    assert "已记录" in msg and "rejudge" in msg
    dec = load_label_decisions(m)
    assert dec["ep000002"]["decision"] == "采纳建议改标"
    assert dec["ep000002"]["new_label"] == "wipe the table"
    # 改判 = 追加,后写覆盖前写
    record_label_decision(m["path"], "ep000002", "维持原标注")
    assert load_label_decisions(m)["ep000002"]["decision"] == "维持原标注"
    # 裁决列回显进表格
    row = [r for r in audit_rows(m) if r[2] == "ep000002"][0]
    assert row[-1] == "维持原标注"


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
    from curation.ui.manifest import task_review_rows
    m = load_delivery(delivery)
    q = m["task_review"]
    assert [t["id"] for t in q] == ["ep000000"]
    assert q[0]["current"] == "通过" and q[0]["reason"] == "渐变问询不可判"
    assert q[0]["readings"] == {"voc": 0.87, "末态分": 0.3}   # 从 checks 的 detail 解出
    rows = task_review_rows(m)
    # 行结构:[操作, episode, 当前判决, 弃权原因, 关键读数, 裁决]
    assert rows[0][0] == "裁决 ▶" and rows[0][1] == "ep000000"
    assert rows[0][2] == "通过" and "渐变问询" in rows[0][3]
    assert "voc=0.87" in rows[0][4] and "末态分=0.3" in rows[0][4]
    assert rows[0][5] == ""                                   # 未裁决

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
    from curation.ui.manifest import (load_task_verdicts, record_task_verdict,
                                      task_review_rows)
    m = load_delivery(delivery)
    assert load_task_verdicts(m) == {}
    msg = record_task_verdict(m["path"], "ep000000", "判成功", note="看了视频,完成了")
    assert "已记录" in msg and "rejudge" in msg and "1 分钟" in msg
    got = load_task_verdicts(m)
    assert got["ep000000"]["verdict"] == "判成功"
    assert got["ep000000"]["note"] == "看了视频,完成了" and got["ep000000"]["at"]
    assert task_review_rows(m)[0][5] == "判成功"                # 回显进表格
    record_task_verdict(m["path"], "ep000000", "搁置")          # 改判=追加,后写覆盖
    assert load_task_verdicts(m)["ep000000"]["verdict"] == "搁置"


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
    (tmp_path / "human-decisions" / "task_verdicts.csv").write_text("")  # 装作还看不见
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
    """页面上的"还剩几条"与工序引导:裁过的不再催,裁完催办语消失;搁置算未裁。"""
    from curation.ui.manifest import (WORKFLOW_GUIDE, audit_note_md,
                                      audit_pending_count, record_label_decision,
                                      record_task_verdict, task_pending_count,
                                      task_review_hint_md)
    m = load_delivery(delivery)
    assert audit_pending_count(m) == 1 and task_pending_count(m) == 1
    assert "1" in audit_note_md(m) and "人工裁决" in audit_note_md(m)
    assert f"「{AUDIT_TERM}」没裁" in task_review_hint_md(m)  # 提示先清那一块
    record_label_decision(m["path"], "ep000002", "维持原标注")
    assert audit_pending_count(m) == 0
    assert f"「{AUDIT_TERM}」没裁" not in task_review_hint_md(m)   # 清完了就不再催
    assert "**1** 条待裁" in task_review_hint_md(m)          # 本块进度照报
    assert "已全部裁决" in audit_note_md(m)
    record_task_verdict(m["path"], "ep000000", "搁置")
    assert task_pending_count(m) == 1, "搁置是待定不是结论,仍算未裁"
    record_task_verdict(m["path"], "ep000000", "判成功")
    assert task_pending_count(m) == 0
    # 工序引导:先裁「标注与画面对不上」→ rejudge → 再成败 → 再 rejudge
    assert WORKFLOW_GUIDE.index(AUDIT_TERM) < WORKFLOW_GUIDE.index("任务成败")
    assert "执行人工裁决" in WORKFLOW_GUIDE                 # 指向任务台按钮,不再教敲命令行            # 两趟 rejudge,别只跑一次就收工
    assert WORKFLOW_GUIDE.count("执行") >= 2


def test_app_has_manual_decision_tab(delivery):
    """Gradio 层:新增「人工裁决」页签,排在 Episodes 与 技能画像 之间;
    两块裁决面板的文案都在,且技能画像页只剩一行指路(裁决卡片已搬走)。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = _config_text(build_app(delivery))
    for t in ("质检总览", "Episodes", "人工裁决", "技能分布", "卡顿动作时间线",
              "明细", "性能剖析"):
        assert t in cfg, t
    rep = _report_section(build_app(delivery))       # 只在报告段里比顺序(见 _report_section)
    assert rep.index("Episodes") < rep.index("人工裁决") < rep.index("技能分布")
    # 区块头 2026-08-07 改成自绘 HTML(色块序号 + 标题),不再是"① 标注分歧"这种
    # 字面前缀 —— 断言跟着渲染实况走(此前这里钉的是旧文案,一直红着)
    for txt in (AUDIT_TERM, "任务成败弃权", "✅ 判成功", "❌ 判失败", "⏸ 搁置",
                "建议按这个顺序做"):     # 2026-08-13 文案精简后的标题
        assert txt in cfg, txt
    assert cfg.index(AUDIT_TERM) < cfg.index("任务成败弃权")


def test_asgi_app_serves_manual_decision_tab(delivery, clean_ui_env):
    """整页起得来,且「人工裁决」的文案真出现在首页 HTML 里。"""
    pytest.importorskip("gradio")
    from starlette.testclient import TestClient

    from curation.ui.app import create_asgi_app
    app = create_asgi_app(delivery, terminal=False)
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "人工裁决" in r.text


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
    assert rows[0][0] == "复议 ▶" and rows[0][1] == "ep000001"
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
    assert "已记录" in msg and "rejudge" in msg
    got = load_reject_appeals(m)
    assert got["ep000001"]["appeal"] == "捞回"
    assert got["ep000001"]["note"] == "看了视频,完成了" and got["ep000001"]["at"]
    assert appeal_rows(m)[0][4] == "捞回" and appeal_pending_count(m) == 0
    record_reject_appeal(m["path"], "ep000001", "维持拒绝")      # 改判=追加,后写覆盖
    assert load_reject_appeals(m)["ep000001"]["appeal"] == "维持拒绝"
    assert "未记录" in record_reject_appeal(m["path"], "ep1", "放它一马")
    assert "未记录" in record_reject_appeal(m["path"], "", "捞回")


def test_appeal_hint_is_empty_when_nothing_to_appeal(delivery, tmp_path):
    """有条目时提示写清"能复议什么、不能复议什么";一条都没有时给空串
    (调用侧据此整区不渲染——空区块占位只会让人以为自己漏看了)。"""
    from curation.ui.manifest import appeal_hint_md
    m = load_delivery(delivery)
    hint = appeal_hint_md(m)
    assert "1" in hint and "捞回" in hint and "终局" in hint
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


def test_app_has_reject_appeal_section(delivery):
    """Gradio 层:「被拒复议」区在人工裁决页、排在成败弃权之后;两个按钮文案在位。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = _config_text(build_app(delivery))
    for txt in ("被拒复议", "🛟 捞回(判为可用)", "❌ 维持拒绝"):
        assert txt in cfg, txt
    assert cfg.index("任务成败弃权") < cfg.index("被拒复议")


def test_load_callback_wiring_stays_aligned(delivery):
    """`_load` 的返回值个数必须与它的输出槽位个数逐一对齐。

    这是运行期才炸的接线错误(加一个区块忘了加槽位 → 换交付时整页错位),
    构造期看不出来,只能靠这条断言钉住。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    app = build_app(delivery)
    fns = [f for f in app.fns.values() if getattr(f.fn, "__name__", "") == "_load"]
    assert fns, "找不到 _load 回调(gradio 内部结构变了,断言需跟着改)"
    for f in fns:
        assert len(f.fn(delivery)) == len(f.outputs)


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
    csv_path.write_text("")                      # 模拟 FSX 可见延迟:读回是空的
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
    assert "没有视频-动作同步读数" in sync_camera_html(m, "ep000000")


def test_episode_card_names_the_fatal_check(delivery):
    """判决卡:大徽章 + 判决 + 一句人话理由,理由里点名是哪一项没过。"""
    from curation.ui.manifest import (episode_card_html, episode_reason_text,
                                      episode_verdict_label, fatal_checks)
    m = load_delivery(delivery)
    assert fatal_checks(m, "ep000001") == ["任务成败判定"]
    assert episode_reason_text(m, "ep000001") == "渐变问询不可判"
    card = episode_card_html(m, "ep000001")
    assert "⛔ 拒绝" in card and "ep000001" in card
    # 检查名 + **该检查自己写的人话**:只报检查名会把人带偏(ep000018 教训)
    assert "未通过「任务成败判定」:渐变问询不可判" in card
    assert "硬门" not in card and "0.940" in card             # 黑话已清除 · 质量分
    # 待裁决优先于当前判决(系统还没定论时先叫人上)
    assert episode_verdict_label(m["episodes"]["ep000000"]) == "待裁决"
    card0 = episode_card_html(m, "ep000000")
    assert "⏳ 待裁决" in card0 and "「任务成败判定」弃权" in card0
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
    assert [r[0] for r in rows] == ["任务成败判定"]
    assert html.count("#FFECE8") == 1                    # 红底只给被拒那一行
    assert "voc=0.87" in html
    # 通过条目:一行红都没有
    assert "#FFECE8" not in check_table_html(m, "ep000002")
    assert "没有逐维检查读数" in check_table_html(m, "")   # 空态不崩


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
    assert "共 **1** 张曲线" in v["note"] and v["pos"] == ""      # 一页不显示页码
    # aligned 且无标注相机 → 不算"有标注/异常",筛选后为空并给出指路
    clean = _with_sync(delivery, detail={"verdict": "aligned", "per_camera": {},
                                         "flagged_cameras": []})
    assert sync_plot_items(clean, SYNC_FILTER_FLAGGED) == []
    assert "切到「全部」" in sync_view(clean, SYNC_FILTER_FLAGGED, 0)["note"]
    assert sync_view(clean, SYNC_FILTER_ALL, 0)["items"][0][1] == "ep000001 · 同步正常"


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


def test_app_has_sync_curve_tab_and_split_evidence(delivery):
    """Gradio 层:「同步曲线」页在(挨着 Stuck 时间线),曲线走独立的整幅宽度组件。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = _config_text(build_app(_with_sync(delivery)["path"]))
    for t in ("质检总览", "Episodes", "人工裁决", "技能分布", "视频-动作同步",
              "卡顿动作时间线", "明细", "性能剖析"):
        assert t in cfg, t
    # 「同步曲线」四个字在 Episodes 页的曲线组件标题里就出现过 → 页签定位用该页
    # 独有的筛选器文案(否则这条断言量的是 Episodes 页,永远不会红)
    rep = _report_section(build_app(delivery))
    assert (rep.index("技能分布") < rep.index("只看有标注/异常的")
            < rep.index("卡顿动作时间线"))
    # 老画廊标题("证据(probe 帧 + 同步曲线)")必须绝迹:那就是混排的证据
    assert "probe 帧 + 同步曲线" not in cfg
    # 曲线走独立的 Image 组件(整幅宽度),不再是画廊里的一格
    assert '"name": "image"' in cfg or '"type": "image"' in cfg


def test_asgi_app_serves_sync_curve_tab(delivery, clean_ui_env):
    """整页起得来,「同步曲线」页签文案真出现在首页 HTML 里。"""
    pytest.importorskip("gradio")
    from starlette.testclient import TestClient

    from curation.ui.app import create_asgi_app
    app = create_asgi_app(_with_sync(delivery)["path"], terminal=False)
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "视频-动作同步" in r.text and "只看有标注/异常的" in r.text


def test_app_load_returns_match_outputs_after_rework(delivery):
    """接线闸门:_load 的返回数 = outputs 组件数(错位是运行期才炸的接线错误)。

    本轮 Episodes 页与同步曲线页新增了一批输出槽,这条断言是它们的唯一保险。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    app = build_app(_with_sync(delivery)["path"])
    loads = [f for f in app.fns.values() if getattr(f.fn, "__name__", "") == "_load"]
    assert loads
    for f in loads:
        assert len(f.fn(delivery)) == len(f.outputs)


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


def test_sync_coverage_note_explains_partial_plotting():
    """只给问题条目画图时,「全部」必须说破 = 全部**已出图**,不是全部 episode。"""
    from curation.ui.manifest import sync_coverage_note, sync_view

    def _ep(has_plot):
        e = {"checks": {"视频-动作同步": {"state": "pass",
                                          "detail": {"verdict": "aligned"}}}}
        if has_plot:
            e["plot"] = "/tmp/p.png"
        return e

    m = {"config_effective": {"pipeline": {"sync_plots": "flagged"}},
         "episodes": {f"ep{i:06d}": _ep(i < 2) for i in range(7)}}
    note = sync_coverage_note(m, 2)
    assert "7 条" in note and "2 条有图" in note and "sync_plots=all" in note
    assert note in sync_view(m, "全部")["note"]

    # 全画了 → 不啰嗦
    m2 = {"config_effective": {"pipeline": {"sync_plots": "all"}},
          "episodes": {f"ep{i:06d}": _ep(True) for i in range(3)}}
    assert sync_coverage_note(m2, 3) == ""
    assert "只为需要留意的条目" not in sync_view(m2, "全部")["note"]


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
    assert labels == ["✅ 通过 1", "❌ 拒绝 2", "⏳ 待人工 2", "全部 5"]
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
    assert by_id["ep000001"]["label"] == "ep000001 ⏳ 渐变问询不可判"
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
    assert "✅ 通过" in card and "ep000000" in card
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
    assert "review-page" in episode_video_html(m, "ep000000", None)


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
    assert "currentTime=0" in html and ".pause()" in html
    assert "ep-video-zone" in html
    assert "&" not in html                 # 属性里的 & 会被当实体开头,踩过一次


def test_manual_hint_only_on_pending_and_points_at_the_decision_page(ep_delivery):
    """待人工条目在明细上方给醒目提示 + 去「人工裁决」页的指引;别的桶不占位。"""
    from curation.ui.manifest import manual_hint_html
    m = load_delivery(ep_delivery)
    hint = manual_hint_html(m, "ep000001")
    assert "人工裁决" in hint and "执行人工裁决" in hint
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


def test_app_episodes_tab_is_buckets_plus_list_and_detail(ep_delivery, tmp_path):
    """Gradio 层:三桶单选 + 左清单 + 折叠的检查明细都在,证据帧画廊已撤。

    桶与清单的选项是 `_load` 现算的(交付一换就得换),所以这里既看页面骨架,
    也直接跑一遍 `_load` 看它真发出了带计数的三桶。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    site = _review_site(str(tmp_path / "review"), "droid_buckets", ["ep000000"])
    app = build_app(ep_delivery, review_dir=site)
    cfg = _config_text(app)
    assert "检查明细" in cfg
    assert "证据帧" not in cfg                     # 静态证据帧整块撤掉(用户原话)
    assert "只看被拒" not in cfg                   # 旧筛选档已被三桶取代
    loads = [f for f in app.fns.values() if getattr(f.fn, "__name__", "") == "_load"]
    blob = str(loads[0].fn(ep_delivery))
    assert "✅ 通过 1" in blob and "❌ 拒绝 2" in blob and "⏳ 待人工 2" in blob
    assert "ep000002 ❌ 末态未完成" in blob                 # 左清单那一行


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


def test_detail_subtabs_order_and_the_new_video_page(delivery):
    """明细页五个子页的顺序钉死,新的「视频打分明细」排在「动作打分明细」之后。

    顺序是用户 2026-08-13 定的:两张打分明细相邻,后面才是三个诊断/排障页。
    子页顺序在截图里一眼能看出对不对,但改坏了没人会想起来重看 —— 故用测试守。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    rep = _report_section(build_app(delivery))
    # 从第一个子页处往后切:「视频-动作同步」这几个词在 Episodes 页的正文里也出现
    # (逐相机同步那块),整份配置里 index() 会先命中那边 —— 与 _report_section
    # 当初要限定范围是同一个坑,只是又深了一层。
    det = rep[rep.index("动作打分明细"):]
    order = ["动作打分明细", "视频打分明细", "视频-动作同步",
             "卡顿动作时间线", "本次运行配置"]
    at = [det.index(t) for t in order]
    assert at == sorted(at), f"明细子页顺序不对:{order} → {at}"


def test_perf_tab_is_top_level_right_of_detail(delivery):
    """「性能剖析」必须是**顶层页签**,且排在「明细」右边(2026-08-13 用户点名)。

    它曾被收进「明细」当子页 —— 结果客户以为这一页没了。它回答的是"这批为什么慢、
    用的什么服务",是**跑批本身的账**,不是某一维的明细,不该藏在二级里。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    rep = _report_section(build_app(delivery))
    assert rep.index("明细") < rep.index("性能剖析")
    # 不在明细的子页里:子页从「动作打分明细」起,到「本次运行配置」止
    det = rep[rep.index("动作打分明细"):rep.index("本次运行配置")]
    assert "性能剖析" not in det


# ───────── 数据集多选(2026-08-13)─────────

def test_dataset_dropdown_is_multiselect(delivery):
    """「数据集」下拉必须是多选 —— 一次点击顺序跑几个就靠它。

    此前只有"一个"和"父目录下全部"两档,想跑其中三个得排三轮队(任务台同一时刻
    只许一个任务在跑)。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = json.loads(json.dumps(build_app(delivery).get_config_file(), default=str))
    picks = [c for c in cfg["components"]
             if c.get("props", {}).get("label") == "数据集"]
    assert picks and all(c["props"].get("multiselect") for c in picks)


def test_delivery_name_hint_switches_when_several_datasets_are_picked():
    """选多个时「交付名」的说明要换成"父文件夹"那句。

    不说明白的话,客户会以为三个数据集的结果互相覆盖 —— 而实际是
    `<交付名>/<数据集名>/` 各一份(与 CLI --batch 同款)。
    """
    from curation.ui.app import OUT_NAME_HINT_MANY, OUT_NAME_HINT_ONE
    assert "父文件夹" in OUT_NAME_HINT_MANY and "父文件夹" not in OUT_NAME_HINT_ONE
    assert "M4" not in OUT_NAME_HINT_MANY and "batch" not in OUT_NAME_HINT_MANY


# ───────── 下拉浮层跟着页面滚(2026-08-13)─────────

def test_dropdown_overlay_follows_the_page_scroll():
    """选项浮层要跟手:纯 JS 进不了 pytest,故按字符串钉住那几处要害。

    现象(用户实见,有照片):点开「数据集」下拉、不选、直接滚鼠标,选项表留在原地,
    和输入框错开老远。根因是 Gradio 6.9 的 `ul.options` 是 position:fixed 且坐标只
    在打开那一刻算一次,而输入框随页面滚。
    """
    from curation.ui.app import _DROPDOWN_JS

    js = _DROPDOWN_JS
    assert "ul.options" in js and "getBoundingClientRect" in js
    # 捕获阶段:滚动事件不冒泡到 window,内层容器滚动只有捕获阶段收得到
    assert "'scroll', reflow, true" in js and "'resize', reflow, true" in js
    # 列表自己滚动时短路 —— 不短路会自己跟自己打架(重算甚至抖动)
    assert "closest('ul.options')) return" in js
    # 保住原来的展开方向:一律按"下方"会把上开的浮层甩到输入框头上盖住它
    assert "above" in js and "u.height" in js
    # 锚点是 div.wrap 而不是里面的 input(2026-08-13 真机量的:gradio 自己按 wrap
    # 算,input 比它窄 60px、右偏 16px)—— 按 input 重排会让浮层横向跳一下
    assert "closest('.wrap')" in js
    # 锚点滚出视野就收起:挂在半空的浮层比关掉更糟
    assert "input.blur()" in js


def test_dropdown_overlay_script_is_actually_injected():
    """脚本得真进 head —— 常量写好了却没接进 presentation(),界面上一点看不出来。"""
    pytest.importorskip("gradio")
    from curation.ui.app import _DROPDOWN_JS, presentation

    assert _DROPDOWN_JS in presentation()["head"]


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
    rows = dict(overview_rows(load_delivery(full_delivery)))
    within = {k.replace("　", "").replace("其中 ", ""): v
              for k, v in rows.items() if "其中" in k}
    assert within == {"待人工裁决": 31}


def test_overview_never_shows_the_label_rows(full_delivery):
    """★ 总览表**刻意不列**标注相关的行(2026-08-13 用户定,理由见 manifest 里
    那段 ⚠️):人工真值集里客户标注错 6/106,我们自产描述错 27/94 —— 把「分歧
    32 条」摆在总览首屏,实际是在展示自家打标不准,还会被读成"你的标注有 32 条
    有问题"。撤的只是这两行展示,分歧队列本身在「人工裁决」页照旧。
    """
    m = load_delivery(full_delivery)
    blob = str(overview_rows(m)) + overview_note_md(m) + overview_markdown(m)
    assert AUDIT_TERM not in blob and "标注缺失" not in blob and "分歧" not in blob
    # 功能没被拆掉:队列还在,人工裁决页还照旧靠它
    assert len(m["audit_queue"]) == 32
    assert AUDIT_TERM in audit_note_md(m)


def test_overview_note_states_the_arithmetic(full_delivery):
    """表下小字要把两件事说清:哪几行能相加、「其中」不参与加减。"""
    note = overview_note_md(load_delivery(full_delivery))
    assert "输入 = 判废 + 交付" in note
    assert "不参与加减" in note


def test_overview_note_names_dedup_in_the_identity_when_it_removed_anything(full_delivery):
    """去重真删了东西时,等式必须把它写进去 —— 否则读者一加发现对不上。"""
    import json as _json
    p = os.path.join(full_delivery, "passed.json")
    doc = _json.loads(open(p, encoding="utf-8").read())
    doc["dataset"]["dedup_removed"] = 4
    open(p, "w", encoding="utf-8").write(_json.dumps(doc, ensure_ascii=False))
    note = overview_note_md(load_delivery(full_delivery))
    assert "输入 = 判废 + 精确去重删除 + 交付" in note


def test_overview_note_skips_the_within_clause_when_there_is_no_such_row():
    """没有「其中」行的交付(全通过、无待裁)不该去解释一行不存在的东西 ——
    真机上 bridge-rrd-200 就是这样,读者会回头去找那一行在哪。"""
    m = {"path": "", "load_error": "", "episodes": {}, "audit_queue": [],
         "dataset": {"input_episodes": 200, "verdict_drop": 0, "delivered": 200,
                     "dedup_removed": 0,
                     "summary_stats": {"pass_rate_pct": 100.0}}}
    assert not any("其中" in r[0] for r in overview_rows(m))
    note = overview_note_md(m)
    assert "输入 = 判废 + 交付" in note and "其中" not in note


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
    assert overview_note_md(load_delivery(str(d))) == ""   # 缺「判废」→ 口径无从谈起


def test_overview_never_shows_the_funnel_word(full_delivery):
    """「漏斗」是内部术语(用户:"用户看不懂啥意思"),总览页一个字不许剩。"""
    m = load_delivery(full_delivery)
    blob = overview_markdown(m) + str(overview_rows(m)) + overview_note_md(m)
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
    assert overview_rows(m) == [] and overview_note_md(m) == ""


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

def test_report_overview_tab_is_one_table_without_the_funnel_word(full_delivery):
    """总览页:表不再顶「漏斗」这个标题,整份界面配置里也不剩这两个字。

    页签本来就写着「质检总览」,表上再顶一个标题既重复又是行话(用户:
    "用户看不懂啥意思")。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = _config_text(build_app(full_delivery))
    assert "漏斗" not in cfg
    assert "质检总览" in cfg


def test_effective_config_moved_under_detail_as_the_last_subtab(full_delivery):
    """「本次运行配置」从质检总览底部搬到「明细」下,是它的最后一个子页。

    总览要收敛成一张表,而这份快照是"日后复核这份报告按什么标准出的"的底稿。
    页签名不许写文件里的键名 config_effective —— 那是我们的字段名。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    rep = _report_section(build_app(full_delivery))
    assert "本次运行配置" in rep
    det = rep[rep.index("动作打分明细"):]
    assert det.index("卡顿动作时间线") < det.index("本次运行配置")
    assert "config_effective" not in rep          # 键名不进界面
    assert "本次运行生效配置" not in rep          # 旧标题(挂在总览底部那个)已撤


def test_load_feeds_the_overview_table_and_the_config_page(full_delivery):
    """_load 的返回值与 outs 一一对齐,且总览那一格发的是新表、不是老的漏斗表。"""
    pytest.importorskip("gradio")
    from curation.ui import app as ui_app
    app = ui_app.build_app(full_delivery)
    fn = next(f for f in app.fns.values()
              if getattr(f, "fn", None) and getattr(f.fn, "__name__", "") == "_load")
    out = fn.fn(full_delivery)
    assert len(out) == len(fn.outputs)
    assert out[2] == overview_rows(load_delivery(full_delivery))   # 表
    assert out[3] == overview_note_md(load_delivery(full_delivery))  # 表下小字
    assert "checks" in out[4] or "(此交付无" in out[4]              # 生效配置 YAML


def test_footer_links_are_off(full_delivery, clean_ui_env):
    """页脚那排 Use via API / Built with Gradio / Settings 整排去掉。

    第一个会把本服务的接口文档摆给任何打开页面的人;另两个对客户没有用处。
    走 gradio 自己的 footer_links=[] 而不是 CSS 藏 —— 藏起来的链接照样在 DOM 里、
    照样可点。
    """
    pytest.importorskip("gradio")
    import re as _re

    from starlette.testclient import TestClient

    from curation.ui.app import create_asgi_app
    with TestClient(create_asgi_app(full_delivery, terminal=False)) as c:
        html = c.get("/").text
    hit = _re.search(r'"footer_links"\s*:\s*(\[[^\]]*\])', html)
    assert hit, "页面配置里找不到 footer_links(gradio 版本变了?去看 mount_gradio_app)"
    assert json.loads(hit.group(1)) == []


def test_start_button_refuses_an_empty_dataset_selection(full_delivery, tmp_path):
    """数据集多选默认空:点「开始质检」要给一句明确提示,不静默、不抛红框。"""
    pytest.importorskip("gradio")
    from curation.ui import app as ui_app
    data_root = tmp_path / "data"
    (data_root / "so101" / "meta").mkdir(parents=True)
    (data_root / "so101" / "meta" / "info.json").write_text("{}")
    app = ui_app.build_app(full_delivery, data_root=str(data_root))
    go = {i for i, c in app.blocks.items()
          if getattr(c, "value", None) == "开始质检"}
    assert go, "界面上找不到「开始质检」按钮"
    fn = next(f for f in app.fns.values()
              if go & {t[0] for t in getattr(f, "targets", [])})
    # 参数顺序 = rn_go.click 的 inputs;这里只关心第一个(数据集)为空
    # 参数少了一个:「覆盖同名结果」2026-08-14 随布局改造撤掉(每次跑批各进各的
    # 时间戳子目录,没有可覆盖的东西)
    out = fn.fn([], "out", ui_app.FULL_SCAN, [], "只跑选中", None, "", None,
                "", "", ui_app.PLOT_MODES["flagged"], None, None, None, "",
                False, False)
    msg = str(out[2])
    assert "数据集" in msg and "跑全部" in msg


def test_terminal_screen_clips_its_overflow(delivery, clean_ui_env):
    """终端容器必须 overflow:hidden —— 2026-08-13 用户截图里面板底部那条深色带,
    就是 xterm 行区(行数 × 行高)比 78vh 容器高出几像素、画到圆角之外形成的。
    ⚠️ 滚动区 .xterm-viewport 不许跟着加 overflow(那是终端自己的滚动)。"""
    pytest.importorskip("gradio")
    from curation.ui.app import presentation
    css = presentation(terminal=True)["css"]
    block = css[css.index("#curation-term-screen {"):]
    assert "overflow: hidden" in block[:block.index("}")]
    vp = css[css.index("#curation-term-screen .xterm-viewport {"):]
    assert "overflow" not in vp[:vp.index("}")]


def test_polling_does_not_wipe_the_validation_message(full_delivery, tmp_path):
    """两秒一次的轮询必须把当前那句提示**带回去**,不是清空。

    2026-08-13 实测:清空的话,「还没选数据集」这类校验提示活不过一次 tick ——
    用户点了按钮什么也没看见,和静默失败没有区别。钉法 = 提示那个组件既是这一跳
    的输入也是它的输出(带回去了),而不是零输入(那就是清空)。
    """
    pytest.importorskip("gradio")
    from curation.ui import app as ui_app
    app = ui_app.build_app(full_delivery, data_root=str(tmp_path))
    fn = next(f for f in app.fns.values()
              if getattr(getattr(f, "fn", None), "__name__", "") == "_tk_tick")
    assert len(fn.inputs) == 1
    assert fn.inputs[0] in fn.outputs


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
    blob = str(rows) + overview_note_md(m)
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
    note = overview_note_md(m)
    assert "同一条可能同时踩中多项" in note
    assert "综合质量分不达标" not in str(rows)      # 相加已经多了,别再往上加


def test_overview_keeps_the_plain_decomposition_when_it_already_adds_up(full_delivery):
    """恰好加得出总数的老样子一个字不变(droid-200-full 就是这种)。"""
    m = load_delivery(full_delivery)
    rows = overview_rows(m)
    assert _detail_rows(rows) == {"任务成败判定": 14, "时间戳检查": 1}
    assert "综合质量分不达标" not in str(rows)
    assert "同一条可能同时踩中多项" not in overview_note_md(m)
    # 「其中」那句仍然只为交付内标记那一行印
    assert "不参与加减" in overview_note_md(m)


def test_overview_never_guesses_when_the_delivery_has_no_breakdown(full_delivery):
    """老交付没有 hard_fail_breakdown 字段 → 一行子项都不列,不猜、不占位。"""
    p = os.path.join(full_delivery, "passed.json")
    doc = json.loads(open(p, encoding="utf-8").read())
    doc["dataset"].pop("hard_fail_breakdown")
    open(p, "w", encoding="utf-8").write(json.dumps(doc, ensure_ascii=False))
    rows = overview_rows(load_delivery(full_delivery))
    assert _detail_rows(rows) == {}
    assert dict(rows)["判废"] == 15
