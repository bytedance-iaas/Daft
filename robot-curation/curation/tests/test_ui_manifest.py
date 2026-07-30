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

from curation.ui.manifest import (audit_rows, check_rows, discover_deliveries,
                                  episode_rows, funnel_rows, load_delivery,
                                  overview_markdown, parse_detail, skill_rows)

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


def test_episode_rows_shape(delivery):
    rows = episode_rows(load_delivery(delivery))
    assert [r[0] for r in rows] == ["ep000000", "ep000001", "ep000002"]
    r0 = rows[0]
    assert r0[1] == "通过" and r0[3] == "任务成败判定" and r0[5] == 2   # 2 张证据帧
    assert rows[1][6] == "有"                                          # 同步图


def test_funnel_and_check_rows(delivery):
    m = load_delivery(delivery)
    fr = dict(funnel_rows(m))
    assert fr["输入 episode"] == 3 and fr["交付"] == 2
    cr = check_rows(m, "ep000001")
    ts = [r for r in cr if r[0] == "任务成败判定"][0]
    assert ts[1] == "拒绝" and "voc=0.87" in ts[3]


def test_skill_audit_overview(delivery):
    m = load_delivery(delivery)
    sk = skill_rows(m)
    assert sk[0][0] == "Arrange" and sk[0][1] == "Arrange soft goods" and sk[0][2] == 2
    au = audit_rows(m)
    assert au[0][0] == "ep000002" and au[0][3] == "跨族"
    md = overview_markdown(m)
    assert "droid_fake" in md and "franka" in md and "待人工裁决 **1** 条" in md


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
    assert funnel_rows(m) == []                                 # 无统计=空表,不炸


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


def test_app_with_terminal_tab(delivery):
    """terminal=True:配置里有「终端」页签 + xterm 容器 div,且「终端」排在「质检报告」前。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = _config_text(build_app(delivery, terminal=True))
    assert "终端" in cfg and "curation-term-screen" in cfg
    assert "iframe" not in cfg and "7681" not in cfg      # ttyd 时代的痕迹一点不留
    assert "质检报告" in cfg
    assert cfg.index("终端") < cfg.index("质检报告")   # 左终端、右报告
    # 六个子 tab 一个不少
    for t in ("漏斗总览", "Episodes", "技能画像", "Stuck 时间线", "明细", "后端状态"):
        assert t in cfg


def test_app_without_terminal_tab_is_unchanged(delivery):
    """terminal=False:配置里连「终端」二字都没有,六 tab 照旧。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = _config_text(build_app(delivery))
    assert "终端" not in cfg and "质检报告" not in cfg and "curation-term-screen" not in cfg
    for t in ("漏斗总览", "Episodes", "技能画像", "Stuck 时间线", "明细", "后端状态"):
        assert t in cfg


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
