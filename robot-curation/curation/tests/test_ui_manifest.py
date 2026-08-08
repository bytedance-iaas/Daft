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
    # v3 行结构:[操作, 档位, episode, 原标注, 自产描述, 成败线判定, 分歧说明, 裁决]
    assert au[0][0] == "裁决 ▶"                       # 可点性操作列
    assert au[0][2] == "ep000002" and au[0][6] == "跨族"
    assert au[0][1] in ("重点", "参考")
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
    for impl_tag in ("probe", "endstate", "caption", "llm"):
        assert impl_tag not in flat, impl_tag        # 埋点标签只是字典键,不是界面词


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
    assert "每帧一次调用" in LATENCY_KIND_NOTE          # 探针次数为何最多
    assert "仅一审未通过的数据触发" in LATENCY_KIND_NOTE
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
    """Gradio 层:「性能剖析」子 tab 在,且七个子 tab 一个不少。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = _config_text(build_app(_with_perf(delivery)["path"]))
    for t in ("漏斗总览", "Episodes", "技能画像", "Stuck 时间线", "明细",
              "性能剖析", "后端状态"):
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
    assert SKILL_BAR_COLOR == "#2a78d6"


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
    (tmp_path / "details" / "task_verdicts.csv").write_text("")   # 装作还看不见
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
    assert "标注分歧未裁" in task_review_hint_md(m)          # 提示先清标注分歧
    record_label_decision(m["path"], "ep000002", "维持原标注")
    assert audit_pending_count(m) == 0
    assert "标注分歧未裁" not in task_review_hint_md(m)      # 清完了就不再催
    assert "**1** 条待裁" in task_review_hint_md(m)          # 本块进度照报
    assert "已全部裁决" in audit_note_md(m)
    record_task_verdict(m["path"], "ep000000", "搁置")
    assert task_pending_count(m) == 1, "搁置是待定不是结论,仍算未裁"
    record_task_verdict(m["path"], "ep000000", "判成功")
    assert task_pending_count(m) == 0
    # 工序引导:先标注分歧 → rejudge → 再成败 → 再 rejudge
    assert WORKFLOW_GUIDE.index("标注分歧") < WORKFLOW_GUIDE.index("任务成败")
    assert "再跑一次" in WORKFLOW_GUIDE            # 两趟 rejudge,别只跑一次就收工
    assert WORKFLOW_GUIDE.count("curation rejudge") >= 2


def test_app_has_manual_decision_tab(delivery):
    """Gradio 层:新增「人工裁决」页签,排在 Episodes 与 技能画像 之间;
    两块裁决面板的文案都在,且技能画像页只剩一行指路(裁决卡片已搬走)。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = _config_text(build_app(delivery))
    for t in ("漏斗总览", "Episodes", "人工裁决", "技能画像", "Stuck 时间线",
              "明细", "性能剖析", "后端状态"):
        assert t in cfg, t
    assert cfg.index("Episodes") < cfg.index("人工裁决") < cfg.index("技能画像")
    # 区块头 2026-08-07 改成自绘 HTML(色块序号 + 标题),不再是"① 标注分歧"这种
    # 字面前缀 —— 断言跟着渲染实况走(此前这里钉的是旧文案,一直红着)
    for txt in ("标注分歧", "任务成败弃权", "✅ 判成功", "❌ 判失败", "⏸ 搁置",
                "建议工作顺序"):
        assert txt in cfg, txt
    assert cfg.index("标注分歧") < cfg.index("任务成败弃权")


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
    csv_path = tmp_path / "details" / "label_decisions.csv"
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
    """展示文案必须与用户拍板的语义一致:标注不判废、弃权不进裁决队列不进软分。"""
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
    assert "不进人工裁决队列" in h3 and "不参与综合软分" in h3


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
    """判决卡:判决 + 致命项是哪个检查 + 一句人话原因 + 综合软分,四件事都在。"""
    from curation.ui.manifest import (episode_card_html, episode_reason_text,
                                      episode_verdict_label, fatal_checks)
    m = load_delivery(delivery)
    assert fatal_checks(m, "ep000001") == ["任务成败判定"]
    assert episode_reason_text(m, "ep000001") == "渐变问询不可判"
    card = episode_card_html(m, "ep000001")
    assert "⛔ 拒绝" in card and "ep000001" in card
    assert "致命项" in card and "任务成败判定" in card
    assert "渐变问询不可判" in card and "0.940" in card       # 软分
    # 待裁决优先于当前判决(系统还没定论时先叫人上)
    assert episode_verdict_label(m["episodes"]["ep000000"]) == "待裁决"
    card0 = episode_card_html(m, "ep000000")
    assert "⏳ 待裁决" in card0 and "待裁决项" in card0
    assert "任务成败判定" in card0
    # 通过条目:明说没有任何一维投拒绝
    card2 = episode_card_html(m, "ep000002")
    assert "✅ 通过" in card2 and "没有任何一维投拒绝" in card2
    # 没选中 / 老交付无软分:不崩,给引导语
    assert "选一条 episode" in episode_card_html(m, "")
    assert "选一条 episode" in episode_card_html(m, "查无此条")


def test_episode_card_notes_sync_abstention_is_only_an_annotation(delivery):
    """同步测不准出现在卡片上时,必须当面写清它不进裁决队列、不进软分。"""
    from curation.ui.manifest import episode_card_html
    m = _with_sync(delivery, detail={**SYNC_DETAIL_NEW, "verdict": "undecidable"})
    card = episode_card_html(m, "ep000002")
    assert "同步测不准仅作标注" in card
    assert "不进人工裁决队列" in card and "不参与综合软分" in card
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
    assert html.count("#fee2e2") == 1                    # 红底只给被拒那一行
    assert "voc=0.87" in html
    # 通过条目:一行红都没有
    assert "#fee2e2" not in check_table_html(m, "ep000002")
    assert "没有逐维检查读数" in check_table_html(m, "")   # 空态不崩


def test_episode_filter_narrows_list_and_ids(delivery):
    """筛选:只看被拒 / 只看待裁决 / 全部;未知档位退回全部(前端能塞任意值)。"""
    from curation.ui.manifest import (EPISODE_FILTER_ALL, EPISODE_FILTER_PENDING,
                                      EPISODE_FILTER_REJECTED, EPISODE_FILTERS,
                                      episode_rows, filter_episode_ids)
    m = load_delivery(delivery)
    assert EPISODE_FILTERS[0] == EPISODE_FILTER_ALL       # 默认档排最前
    assert filter_episode_ids(m) == ["ep000000", "ep000001", "ep000002"]
    assert filter_episode_ids(m, EPISODE_FILTER_REJECTED) == ["ep000001"]
    assert filter_episode_ids(m, EPISODE_FILTER_PENDING) == ["ep000000"]
    assert filter_episode_ids(m, "乱传的档位") == filter_episode_ids(m)
    rows = episode_rows(m, EPISODE_FILTER_REJECTED)
    assert [r[0] for r in rows] == ["ep000001"] and rows[0][1] == "拒绝"
    assert len(episode_rows(m)) == 3                      # 默认参数行为不变


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


def test_sync_view_empty_state_points_at_the_switch(tmp_path):
    """交付里没有 plots → 一句友好说明,并点名 pipeline.sync_plots 开关。"""
    from curation.ui.manifest import NO_PLOTS_NOTE, sync_view
    d = tmp_path / "noplots"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps(
        {"数据集": "x", "episodes": {"ep0": {"判决": "通过", "checks": {}}}},
        ensure_ascii=False), encoding="utf-8")
    v = sync_view(load_delivery(str(d)))
    assert v["items"] == [] and v["pages"] == 1
    assert v["note"] == NO_PLOTS_NOTE
    assert "pipeline.sync_plots" in NO_PLOTS_NOTE
    for mode in ("flagged", "all", "off"):
        assert f"`{mode}`" in NO_PLOTS_NOTE


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
    """Gradio 层:新增「同步曲线」页(挨着 Stuck 时间线),且 Episodes 页的
    证据帧画廊与同步曲线是**两个组件**——混排正是"显示很差"的根因。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg = _config_text(build_app(_with_sync(delivery)["path"]))
    for t in ("漏斗总览", "Episodes", "人工裁决", "技能画像", "同步曲线",
              "Stuck 时间线", "明细", "性能剖析", "后端状态"):
        assert t in cfg, t
    # 「同步曲线」四个字在 Episodes 页的曲线组件标题里就出现过 → 页签定位用该页
    # 独有的筛选器文案(否则这条断言量的是 Episodes 页,永远不会红)
    assert (cfg.index("技能画像") < cfg.index("只看有标注/异常的")
            < cfg.index("Stuck 时间线"))
    assert "证据帧" in cfg and "只看被拒" in cfg
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
        assert "同步曲线" in r.text and "只看被拒" in r.text


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
                             "color": "#166534", "flagged": False,
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
