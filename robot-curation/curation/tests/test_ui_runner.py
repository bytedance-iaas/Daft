"""UI 任务台执行层的测试(curation/ui/runner.py)。

三块最该被钉死的:
① **argv 构造**——面板参数拼错是这类界面最容易犯又最难发现的错(用户勾了个选项,
   静默没生效);
② **路径校验**——面板只收名字、路径由后端拼,这是安全边界不是易用性,越界的各种
   形态都要有用例;
③ **状态机**——跑批几小时,页面刷新/pod 重启都不能让状态说谎。

全部用 tmp_path + 注入的假 popen/假时钟,**不起真子进程、不跑真管道**。
"""
from __future__ import annotations

import datetime
import json
import os

import pytest

from curation.ui import runner


def _now(s="2026-08-13 14:25:30"):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


class _FakeProc:
    def __init__(self, pid=4321):
        self.pid = pid


def _fake_popen(pid=4321, sink=None):
    def popen(argv, **kw):
        if sink is not None:
            sink.append((argv, kw))
        return _FakeProc(pid)
    return popen


# ── ① argv 构造 ────────────────────────────────────────────────────────────

def test_argv_run_minimal():
    argv = runner.build_argv("run", input="/data/ds", output="/out/ds-0813")
    assert argv[:3] == [runner.sys.executable, "-m", "curation.cli"]
    assert argv[3] == "run"
    assert "--input" in argv and "/data/ds" in argv
    assert "--output" in argv and "/out/ds-0813" in argv


def test_argv_run_covers_every_cli_parameter():
    """run 的每个 CLI 参数都要能从面板表达(用户要的是全量等价物,不是简化版)。"""
    argv = runner.build_argv(
        "run", input="/d/x", output="/o/y", config="/c.yaml", embodiment_id="so101",
        max_episodes=30, episodes="3,10-12", only="visual_quality", overwrite=True,
        batch=True, lite=True, report_only=True, vlm_backend="ark",
        vlm_endpoint="http://h:8000/v1", vlm_model="m", vlm_api_key_env="ARK_API_KEY",
        set_overrides=["pipeline.sync_plots=all", "verdict.soft_threshold=0.6"])
    for flag in ("--input", "--output", "--config", "--embodiment-id", "--max-episodes",
                 "--episodes", "--only", "--overwrite", "--batch", "--lite",
                 "--report-only", "--vlm-backend", "--vlm-endpoint", "--vlm-model",
                 "--vlm-api-key-env"):
        assert flag in argv, flag
    assert argv.count("--set") == 2


def test_argv_omits_unset_and_false():
    argv = runner.build_argv("run", input="/d", output="/o", config=None,
                             max_episodes="", lite=False, overwrite=False)
    assert "--config" not in argv and "--max-episodes" not in argv
    assert "--lite" not in argv and "--overwrite" not in argv


def test_argv_only_and_skip_are_mutually_exclusive():
    with pytest.raises(ValueError, match="互斥"):
        runner.build_argv("run", input="/d", output="/o",
                          only="visual_quality", skip="task_success")


def test_argv_rejects_unknown_parameter():
    """面板与 CLI 的参数表迟早会漂;漂了要当场炸,不是静默丢掉用户勾的选项。"""
    with pytest.raises(ValueError, match="不认识"):
        runner.build_argv("run", input="/d", output="/o", turbo=True)


def test_argv_requires_mandatory_fields():
    with pytest.raises(ValueError, match="缺必填参数 output"):
        runner.build_argv("run", input="/d")
    with pytest.raises(ValueError, match="缺必填参数 input"):
        runner.build_argv("rejudge", delivery="/deliv")


def test_argv_unknown_command():
    with pytest.raises(ValueError, match="未知命令"):
        runner.build_argv("deploy", input="/d")


def test_argv_module_names_must_be_semantic_no_m_numbers():
    """M 编号不进用户界面(项目红线):m4a 这种在面板层就该被拒。"""
    with pytest.raises(ValueError, match="未知模块名"):
        runner.build_argv("run", input="/d", output="/o", only="m4a")
    ok = runner.build_argv("run", input="/d", output="/o",
                           skip="skill_profile,dedup")
    assert "--skip" in ok and "skill_profile,dedup" in ok


def test_argv_set_needs_key_equals_value():
    with pytest.raises(ValueError, match="路径=值"):
        runner.build_argv("run", input="/d", output="/o", set_overrides=["sync_plots"])


def test_argv_rejudge_and_review_page_and_backends():
    rj = runner.build_argv("rejudge", delivery="/deliv", input="/src",
                           vlm_backend="ark")
    assert rj[3] == "rejudge" and "--delivery" in rj and "--vlm-backend" in rj
    rp = runner.build_argv("review-page", input="/src", output="/rev/site",
                           rrd_fps=30, title="so101")
    assert rp[3] == "review-page" and "--rrd-fps" in rp and "30" in rp
    be = runner.build_argv("backends", timeout=5)
    assert be[3] == "backends" and "--timeout" in be


def test_argv_is_deterministic():
    """同样的输入永远得到同样的 argv(参数按名排序)——否则日志/历史对不上账。"""
    kw = dict(input="/d", output="/o", lite=True, max_episodes=5)
    assert runner.build_argv("run", **kw) == runner.build_argv("run", **kw)


# ── ② 路径校验(安全边界)──────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "", "   ", ".", "..", "../etc", "a/b", "a\\b", "/abs", ".hidden",
    "名字带中文", "has space", "x" * 81, "-leading-dash",
])
def test_safe_name_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        runner.safe_name(bad)


@pytest.mark.parametrize("good", ["droid-200", "ds_0813", "a", "A1.b-c", "x" * 80])
def test_safe_name_accepts_plain_names(good):
    assert runner.safe_name(good) == good


def test_resolve_under_builds_path_and_blocks_escape(tmp_path):
    root = str(tmp_path / "deliveries")
    os.makedirs(root)
    assert runner.resolve_under(root, "ds-0813") == os.path.realpath(
        os.path.join(root, "ds-0813"))
    with pytest.raises(ValueError):
        runner.resolve_under(root, "../../etc")


def test_resolve_under_blocks_symlink_escape(tmp_path):
    """safe_name 挡得住 ../,挡不住 root 下**已存在的符号链接**指向别处。"""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    os.symlink(outside, root / "sneaky")
    with pytest.raises(ValueError, match="越界"):
        runner.resolve_under(str(root), "sneaky")


def test_list_datasets_finds_lerobot_and_rrd(tmp_path):
    (tmp_path / "lerobot_ds" / "meta").mkdir(parents=True)
    (tmp_path / "lerobot_ds" / "meta" / "info.json").write_text("{}")
    (tmp_path / "rrd_ds").mkdir()
    (tmp_path / "rrd_ds" / "episode_000.rrd").write_bytes(b"x")
    (tmp_path / "not_a_dataset").mkdir()
    (tmp_path / "loose.txt").write_text("x")
    assert runner.list_datasets(str(tmp_path)) == ["lerobot_ds", "rrd_ds"]


def test_list_datasets_missing_root_is_empty_not_crash():
    assert runner.list_datasets("/no/such/root") == []


def test_suggest_delivery_name():
    assert runner.suggest_delivery_name("droid-200", _now()) == "droid-200-0813"
    assert runner.suggest_delivery_name("a b/c", _now()) == "a-b-c-0813"


# ── ③ 生命周期与状态机 ────────────────────────────────────────────────────

def _start(tmp_path, sink=None, pid=4321, command="run", alive=lambda p: True):
    """起一个假任务。alive 默认"活着"——本机不存在 pid 4321,不注入的话每个刚起的
    任务都会被判成 interrupted,互斥类用例就测不出东西了。"""
    root = str(tmp_path / "runs")
    argv = runner.build_argv(command, input="/d", output="/o") if command == "run" \
        else runner.build_argv("backends")
    rid = runner.start(root, command, argv, label="测试任务", now=_now(),
                       popen=_fake_popen(pid, sink), alive=alive)
    return root, rid


def test_start_writes_cmd_and_status(tmp_path):
    sink = []
    root, rid = _start(tmp_path, sink)
    assert rid == "20260813-142530-run"
    cmd = json.loads(open(os.path.join(root, rid, "cmd.json")).read())
    assert cmd["command"] == "run" and cmd["label"] == "测试任务"
    st = json.loads(open(os.path.join(root, rid, "status.json")).read())
    assert st["state"] == "running" and st["pid"] == 4321


def test_start_spawns_bash_with_redirect_and_exit_code(tmp_path):
    """真正 exec 的是 bash -c '<命令> >> log 2>&1; echo $? > exit_code':
    日志重定向给 shell(UI 是常驻进程,守管道等于自找阻塞),退出码落盘
    (重启后任务终结与否依然作数)。"""
    sink = []
    root, rid = _start(tmp_path, sink)
    argv, kw = sink[0]
    assert argv[0] == "/bin/bash" and argv[1] == "-c"
    assert "run.log" in argv[2] and "exit_code" in argv[2]
    assert kw["start_new_session"] is True          # 自成进程组 → 停止时能整组 kill


def test_run_id_collision_gets_suffix(tmp_path):
    root = str(tmp_path / "runs")
    os.makedirs(os.path.join(root, "20260813-142530-backends"))
    rid = runner.start(root, "backends", ["x"], now=_now(),
                       popen=_fake_popen())
    assert rid == "20260813-142530-backends-2"


def test_only_one_task_at_a_time(tmp_path):
    """VLM 并发已按单跑批调到 32×16,两个批叠加会砸穿方舟配额。"""
    root, rid = _start(tmp_path)
    with pytest.raises(runner.RunBusyError) as e:
        runner.start(root, "backends", ["x"], now=_now(), popen=_fake_popen(),
                     alive=lambda p: True)
    assert rid in str(e.value)


def test_interrupted_task_does_not_block_forever(tmp_path):
    """被 pod 重启带走的任务不该永远占着唯一的位子。"""
    root, _ = _start(tmp_path)
    rid2 = runner.start(root, "backends", ["x"], now=_now("2026-08-13 15:00:00"),
                        popen=_fake_popen(), alive=lambda p: False)
    assert rid2.endswith("-backends")


def test_finished_task_frees_the_slot(tmp_path):
    root, rid = _start(tmp_path)
    open(os.path.join(root, rid, "exit_code"), "w").write("0\n")
    assert runner.status(root, rid, alive=lambda p: False)["state"] == "done"
    rid2 = runner.start(root, "backends", ["x"], now=_now("2026-08-13 15:00:00"),
                        popen=_fake_popen())
    assert rid2 != rid


def test_status_done_and_failed_from_exit_code(tmp_path):
    root, rid = _start(tmp_path)
    open(os.path.join(root, rid, "exit_code"), "w").write("2\n")
    st = runner.status(root, rid, alive=lambda p: True)   # 僵尸也算"活着"
    assert st["state"] == "failed" and st["exit_code"] == 2


def test_exit_code_wins_over_liveness(tmp_path):
    """没被 reap 的子进程是僵尸,kill(pid,0) 照样成功 —— 只看进程死活会让任务
    永远显示"运行中"。退出码文件必须优先。"""
    root, rid = _start(tmp_path)
    open(os.path.join(root, rid, "exit_code"), "w").write("0")
    assert runner.status(root, rid, alive=lambda p: True)["state"] == "done"


def test_status_interrupted_when_process_vanished(tmp_path):
    """pod 重启(PID 1 就是 UI)会把跑批带走:如实叫 interrupted,不假装在跑。"""
    root, rid = _start(tmp_path)
    st = runner.status(root, rid, alive=lambda p: False)
    assert st["state"] == "interrupted" and "重启" in st["note"]


def test_terminal_state_is_frozen(tmp_path):
    root, rid = _start(tmp_path)
    runner.status(root, rid, alive=lambda p: False)       # → interrupted 并固化
    again = runner.status(root, rid, alive=lambda p: True)
    assert again["state"] == "interrupted"


def test_status_running_when_alive_and_no_exit_code(tmp_path):
    root, rid = _start(tmp_path)
    assert runner.status(root, rid, alive=lambda p: True)["state"] == "running"


def test_status_unknown_run(tmp_path):
    assert runner.status(str(tmp_path), "no-such-run")["state"] == "unknown"


def test_stop_kills_process_group_and_marks_stopped(tmp_path):
    killed = []
    root, rid = _start(tmp_path)
    runner.stop(root, rid, killer=lambda pid, sig: killed.append((pid, sig)))
    assert killed == [(4321, runner.signal.SIGTERM)]
    open(os.path.join(root, rid, "exit_code"), "w").write("143")
    st = runner.status(root, rid, alive=lambda p: False)
    assert st["state"] == "stopped"        # 人停的 ≠ 跑失败,界面上必须分得开


def test_stop_survives_already_dead_process(tmp_path):
    """进程已经自己退了也要能"停":用户按了停止键,结局就该叫 stopped,
    不能因为 kill 抛异常就把状态卡在 stopping 上。"""
    def _boom(pid, sig):
        raise ProcessLookupError()
    root, rid = _start(tmp_path)
    assert runner.stop(root, rid, killer=_boom)["state"] == "stopped"


def test_list_runs_newest_first(tmp_path):
    root = str(tmp_path / "runs")
    for t in ("2026-08-13 10:00:00", "2026-08-13 12:00:00"):
        rid = runner.start(root, "backends", ["x"], now=_now(t), popen=_fake_popen())
        open(os.path.join(root, rid, "exit_code"), "w").write("0")
        runner.status(root, rid, alive=lambda p: False)
    ids = [r["run_id"] for r in runner.list_runs(root)]
    assert ids == sorted(ids, reverse=True)


def test_active_run_none_when_all_finished(tmp_path):
    root, rid = _start(tmp_path)
    open(os.path.join(root, rid, "exit_code"), "w").write("0")
    assert runner.active_run(root, alive=lambda p: False) is None


def test_tail_log_reads_only_the_tail(tmp_path):
    root, rid = _start(tmp_path)
    path = os.path.join(root, rid, "run.log")
    with open(path, "w") as f:
        for i in range(5000):
            f.write(f"line {i} " + "x" * 80 + "\n")
    out = runner.tail_log(root, rid, max_bytes=2000)
    assert len(out.encode()) <= 2000
    assert "line 4999" in out and "line 0 " not in out


def test_tail_log_missing_file_is_empty(tmp_path):
    assert runner.tail_log(str(tmp_path), "nope") == ""


# ── 进度解析 ──────────────────────────────────────────────────────────────

def test_parse_progress_item_style():
    p = runner.parse_progress(
        "[curation] 数据集: droid | 机器人: Franka | 199 条\n"
        "[curation] VLM 任务成败判定 12/199 (6%) | 已用 3.2min | 剩余 ~48min\n")
    assert p["stage"] == "VLM 任务成败判定"
    assert (p["n"], p["total"], p["pct"]) == (12, 199, 6)
    assert p["elapsed"] == "3.2min" and p["eta"] == "48min"


def test_parse_progress_phase_style_has_no_percentage():
    """一步 = 一次 LLM 大调用,既没有可数单位也不可预测耗时 —— 不编百分比。"""
    p = runner.parse_progress("[curation] 技能画像 3/5 归纳技能体系(LLM)… | 已用 1.2min")
    assert p["stage"] == "技能画像" and (p["n"], p["total"]) == (3, 5)
    assert "归纳技能体系" in p["detail"]


def test_parse_progress_takes_the_last_one():
    p = runner.parse_progress(
        "[curation] 数值检查 10/199 (5%) | 已用 1s\n"
        "[curation] 视觉质量 + 视频动作同步(共用一次解码) 40/199 (20%) | 已用 30s\n")
    assert p["n"] == 40


def test_parse_progress_other_prefixes():
    assert runner.parse_progress("[rejudge] 重判 3/12:ep000011 → success | 已用 42s")["n"] == 3
    assert runner.parse_progress("[review-page] 120/199")["n"] == 120


def test_parse_progress_returns_none_when_unparseable():
    """认不出就返回 None,界面只滚日志 —— 给不可预测的步骤配假进度条是骗人。"""
    assert runner.parse_progress("Traceback (most recent call last):\n  File ...") is None
    assert runner.parse_progress("") is None


# ── 界面要用的读端与渲染(纯函数)────────────────────────────────────────

def test_deliveries_root_of_handles_both_shapes(tmp_path):
    """--delivery 收的可能是一份交付,也可能是装着多份交付的父目录。"""
    parent = tmp_path / "deliveries"
    one = parent / "droid-200"
    one.mkdir(parents=True)
    (one / "passed.json").write_text("{}")
    assert runner.deliveries_root_of(str(one)) == str(parent)
    assert runner.deliveries_root_of(str(parent)) == str(parent)


def test_source_dataset_of_reads_new_field(tmp_path):
    src = tmp_path / "datasets" / "droid"
    src.mkdir(parents=True)
    deliv = tmp_path / "d1"
    deliv.mkdir()
    (deliv / "passed.json").write_text(
        json.dumps({"数据集": "droid", "源数据集路径": str(src)}), encoding="utf-8")
    assert runner.source_dataset_of(str(deliv)) == str(src)


def test_source_dataset_of_old_delivery_returns_none(tmp_path):
    """老交付没这个字段 → None,界面退回让用户自己选,绝不猜一个路径。"""
    deliv = tmp_path / "d2"
    deliv.mkdir()
    (deliv / "passed.json").write_text(json.dumps({"数据集": "droid"}), encoding="utf-8")
    assert runner.source_dataset_of(str(deliv)) is None
    assert runner.source_dataset_of(str(tmp_path / "nope")) is None


def test_source_dataset_of_ignores_vanished_path(tmp_path):
    deliv = tmp_path / "d3"
    deliv.mkdir()
    (deliv / "passed.json").write_text(
        json.dumps({"源数据集路径": "/gone/dataset"}), encoding="utf-8")
    assert runner.source_dataset_of(str(deliv)) is None


def test_vlm_backend_labels_hide_the_codename():
    """预设名(ark / h20-8b …)是内部代号,**不进界面**:下拉只放标签,代号后台映射。
    出厂 default.yaml 里的 ark 预设应当以「方舟 MaaS…」这类人话出现。"""
    labels = runner.vlm_backend_labels()
    assert "ark" in labels.values()
    assert "ark" not in labels                       # 代号不能是标签
    assert not [k for k in labels if k in labels.values()]
    assert any("方舟" in k for k in labels), labels


def test_vlm_backend_labels_skip_placeholder_preset():
    """出厂的 self-hosted-example 端点是 YOUR-VLLM-HOST,注定不可达,不列出来。"""
    assert "self-hosted-example" not in runner.vlm_backend_labels().values()


def test_status_html_no_task():
    assert "当前没有任务" in runner.status_html(None)


def test_status_html_shows_state_and_progress():
    html = runner.status_html(
        {"state": "running", "label": "质检 droid", "run_id": "20260813-1-run"},
        {"stage": "VLM 任务成败判定", "n": 12, "total": 199, "pct": 6,
         "elapsed": "3.2min", "eta": "48min", "detail": ""})
    assert "运行中" in html and "6%" in html and "剩余 ~48min" in html


def test_status_html_distinguishes_stopped_from_failed():
    assert "已停止(人工)" in runner.status_html({"state": "stopped"})
    assert "失败" in runner.status_html({"state": "failed", "exit_code": 2})
    assert "被中断" in runner.status_html({"state": "interrupted"})


def test_status_html_escapes_user_text():
    html = runner.status_html({"state": "running", "label": "<script>x</script>"})
    assert "<script>" not in html and "&lt;script&gt;" in html


# ── 手填路径:只许落在 TOS ────────────────────────────────────────────────

def test_resolve_tos_path_accepts_config_under_root(tmp_path):
    root = tmp_path / "tos"
    root.mkdir()
    cfg = root / "site.yaml"
    cfg.write_text("checks: {}")
    assert runner.resolve_tos_path(str(cfg), root=str(root)) == os.path.realpath(str(cfg))


@pytest.mark.parametrize("bad,msg", [
    ("", "不能为空"),
    ("site.yaml", "绝对路径"),
    ("/etc/passwd", "安全边界"),
    ("/app/curation/pipeline/default.yaml", "安全边界"),
])
def test_resolve_tos_path_rejects_outside(tmp_path, bad, msg):
    root = tmp_path / "tos"
    root.mkdir()
    with pytest.raises(ValueError, match=msg):
        runner.resolve_tos_path(bad, root=str(root))


def test_resolve_tos_path_rejects_missing_and_wrong_suffix(tmp_path):
    root = tmp_path / "tos"
    root.mkdir()
    with pytest.raises(ValueError, match="文件不存在"):
        runner.resolve_tos_path(str(root / "nope.yaml"), root=str(root))
    other = root / "data.parquet"
    other.write_text("x")
    with pytest.raises(ValueError, match="扩展名"):
        runner.resolve_tos_path(str(other), root=str(root))


def test_resolve_tos_path_blocks_symlink_out_of_tos(tmp_path):
    """TOS 里放一个指向容器系统盘的软链,realpath 之后照样挡住。"""
    root = tmp_path / "tos"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "evil.yaml").write_text("x")
    os.symlink(outside / "evil.yaml", root / "innocent.yaml")
    with pytest.raises(ValueError, match="安全边界"):
        runner.resolve_tos_path(str(root / "innocent.yaml"), root=str(root))


# ── 跑批历史 ──────────────────────────────────────────────────────────────

def test_duration_text():
    assert runner.duration_text("2026-08-13 10:00:00", "2026-08-13 10:00:42") == "42 秒"
    assert runner.duration_text("2026-08-13 10:00:00", "2026-08-13 10:03:12") == "3 分 12 秒"
    assert runner.duration_text("2026-08-13 10:00:00", "2026-08-13 12:30:00") == "2 小时 30 分"
    assert runner.duration_text(None, None) == "—"


def test_duration_text_running_counts_to_now():
    """还在跑的没有结束时间,算到此刻——不能显示"—",那看着像没跑。"""
    assert runner.duration_text("2026-08-13 10:00:00", None,
                                now=_now("2026-08-13 10:01:30")) == "1 分 30 秒"


def test_history_rows_uses_same_words_as_the_status_bar(tmp_path):
    root, rid = _start(tmp_path)
    open(os.path.join(root, rid, "exit_code"), "w").write("3")
    runner.status(root, rid, alive=lambda p: False)
    rows = runner.history_rows(runner.list_runs(root, alive=lambda p: False))
    assert rows and rows[0][1] == "测试任务"
    assert "失败" in rows[0][2] and "退出码 3" in rows[0][2]
    assert rows[0][4] == rid
    assert len(rows[0]) == len(runner.HISTORY_HEADERS)


def test_check_labels_have_no_m_numbers():
    import re as _re
    assert not [k for k in runner.CHECK_LABELS if _re.match(r"^m\d", k)]
    assert runner.CHECK_LABELS["visual_quality"] == "视觉质量"


# ── FSX 可见延迟(交付根在 TOS 挂载上时的真实现场)────────────────────────

def test_status_survives_fsx_visibility_delay(tmp_path):
    """刚写完的 status.json 读回来是空的(FSX 可见延迟约 20-60s)时,状态照样对。

    2026-08-13 真机第一次点按钮就撞上了:任务其实跑得好好的,界面却报"找不到该
    任务的状态文件"。读不到就退回本进程的写缓存,窗口期内自愈。
    """
    root, rid = _start(tmp_path)
    os.remove(os.path.join(root, rid, "status.json"))      # 模拟"写了但读不回来"
    st = runner.status(root, rid, alive=lambda p: True)
    assert st["state"] == "running" and st["pid"] == 4321


def test_mutex_holds_even_if_run_dir_not_visible_yet(tmp_path):
    """任务目录本身也可能还没可见 —— 那时列表若查无此人,互斥就形同虚设
    (界面上能连点两次发起,两个跑批一起烧方舟的钱)。"""
    import shutil
    root, rid = _start(tmp_path)
    shutil.rmtree(os.path.join(root, rid))                 # 模拟目录尚未可见
    assert (runner.active_run(root, alive=lambda p: True) or {}).get("run_id") == rid
    with pytest.raises(runner.RunBusyError):
        runner.start(root, "backends", ["x"], now=_now("2026-08-13 15:00:00"),
                     popen=_fake_popen(), alive=lambda p: True)


def test_finished_at_comes_from_the_exit_code_file_not_poll_time(tmp_path):
    """耗时要按任务**真正**结束的时刻算,不是"谁先来轮询谁算数"。

    2026-08-13 真机实测:一个几秒跑完的探活,因为隔了一会儿才有人刷新页面,
    历史里写着"5 分 54 秒"。观众什么时候到场,不该改变这条数据。
    """
    import time
    root, rid = _start(tmp_path)
    rc = os.path.join(root, rid, "exit_code")
    open(rc, "w").write("0")
    real_end = _now("2026-08-13 14:26:12").timestamp()
    os.utime(rc, (real_end, real_end))
    st = runner.status(root, rid, alive=lambda p: False,
                       now=_now("2026-08-13 18:00:00"))     # 四小时后才有人来看
    assert st["finished_at"] == "2026-08-13 14:26:12"
    assert runner.duration_text(st["started_at"], st["finished_at"]) == "42 秒"


# ── 数据集格式识别 + 串命令(2026-08-13:v3 才需要先切片)────────────────────

def test_dataset_format_v3_needs_clips(tmp_path):
    d = tmp_path / "droid"
    (d / "meta").mkdir(parents=True)
    (d / "meta" / "info.json").write_text('{"codebase_version": "v3.0"}')
    f = runner.dataset_format(str(d))
    assert f["kind"] == "lerobot" and f["version"] == "v3.0" and f["needs_clips"] is True


def test_dataset_format_v2_does_not_need_clips(tmp_path):
    """v2 每条本来就是独立 mp4,交付集里直接能播 —— 不该拿这个去烦用户。"""
    d = tmp_path / "bridge"
    (d / "meta").mkdir(parents=True)
    (d / "meta" / "info.json").write_text('{"codebase_version": "v2.1"}')
    assert runner.dataset_format(str(d))["needs_clips"] is False


def test_dataset_format_rrd_needs_clips(tmp_path):
    d = tmp_path / "so101"
    d.mkdir()
    (d / "episode_000.rrd").write_bytes(b"x")
    f = runner.dataset_format(str(d))
    assert f["kind"] == "rrd" and f["needs_clips"] is True


def test_dataset_format_unknown_never_asks(tmp_path):
    """认不出格式就别弹询问框——问一个我们自己都没把握的问题只会让人犹豫。"""
    d = tmp_path / "mystery"
    d.mkdir()
    assert runner.dataset_format(str(d)) == {"kind": "unknown", "version": "",
                                            "needs_clips": False}


def test_start_chains_two_commands_as_one_task(tmp_path):
    """质检 + 切片 = 一个任务、一条日志、一个退出码;前一步失败不做后一步。"""
    sink = []
    root = str(tmp_path / "runs")
    a = runner.build_argv("run", input="/d", output="/o")
    b = runner.build_argv("review-page", input="/d", output="/rev/x")
    rid = runner.start(root, "run", a, label="质检+切片", now=_now(),
                       popen=_fake_popen(sink=sink), alive=lambda p: True,
                       then_argv=b)
    shell = sink[0][0][2]
    assert " && " in shell and shell.count("curation.cli") == 2
    assert shell.startswith("{ ") and "echo $? >" in shell
    cmd = json.loads(open(os.path.join(root, rid, "cmd.json")).read())
    assert "review-page" in " ".join(cmd["then_argv"])       # 历史里查得出跑了两步
