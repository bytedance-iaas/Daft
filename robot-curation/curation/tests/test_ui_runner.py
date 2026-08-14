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
import itertools
import json
import os
import shutil
import time

import pytest

from curation.ui import runner


@pytest.fixture(autouse=True)
def _isolate_local_runs(tmp_path, monkeypatch):
    """把"容器本地盘"指到本用例自己的 tmp 下。

    2026-08-13 起活跃期日志写本地盘(FSX 挂载上一边追加一边读拿不到内容),而本地
    目录是按 run_id 平铺、跨用例共享的 —— 不隔离的话上一个用例留下的 run_id 会让
    下一个用例的 _new_run_id 加上 `-2` 后缀,用例之间隔空互相干扰。
    """
    monkeypatch.setattr(runner, "LOCAL_RUNS_ROOT", str(tmp_path / "local-runs"))


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
        max_episodes=30, episodes="3,10-12", only="visual_quality",
        run_name="20260814-074045",
        batch=True, lite=True, report_only=True, vlm_backend="ark",
        vlm_endpoint="http://h:8000/v1", vlm_model="m", vlm_api_key_env="ARK_API_KEY",
        set_overrides=["pipeline.sync_plots=all", "verdict.soft_threshold=0.6"])
    for flag in ("--input", "--output", "--config", "--embodiment-id", "--max-episodes",
                 "--episodes", "--only", "--run-name", "--batch", "--lite",
                 "--report-only", "--vlm-backend", "--vlm-endpoint", "--vlm-model",
                 "--vlm-api-key-env"):
        assert flag in argv, flag
    assert argv.count("--set") == 2


def test_argv_omits_unset_and_false():
    argv = runner.build_argv("run", input="/d", output="/o", config=None,
                             max_episodes="", lite=False, report_only=False)
    assert "--config" not in argv and "--max-episodes" not in argv
    assert "--lite" not in argv and "--report-only" not in argv


def test_argv_has_no_overwrite_flag():
    """`--overwrite` 2026-08-14 随布局改造删除:每次跑批各进各的时间戳子目录,
    没有可覆盖的东西 —— 而覆盖那条路曾把人工裁决连同旧结果一起 rmtree 掉。
    面板要是还发这个旗标,子进程会当场因未知参数退出。"""
    with pytest.raises(ValueError, match="不认识"):
        runner.build_argv("run", input="/d", output="/o", overwrite=True)


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


# ── 日志落地:活跃期写本地盘,结束归档回挂载 ───────────────────────────────

def test_active_log_goes_to_local_disk_not_the_mount(tmp_path):
    """防的是 2026-08-13 那次:任务跑了很久 run.log 仍是 0 字节,进度条全程空手。

    根因是交付根在 TOS 的 FSX 挂载上,那里"一边追加一边读"拿不到内容(手工
    `echo hi >> /mnt/tos/x && cat` 直接 Stale file handle)。所以 shell 的重定向
    必须指向**本地盘**那份,挂载上只放结束后的存档。
    """
    sink = []
    root, rid = _start(tmp_path, sink)
    shell = sink[0][0][2]
    local = os.path.join(runner.LOCAL_RUNS_ROOT, rid)
    mount = os.path.join(root, rid)
    assert f">> {local}/run.log" in shell
    assert f'echo "$1" > {local}/exit_code' in shell     # 退出码也落本地盘
    assert f">> {mount}/run.log" not in shell          # 挂载上一个字都不许追加着写


def test_finished_task_archives_log_back_to_the_mount(tmp_path):
    """本地盘随容器走:任务一结束就把日志与退出码**整份 cp** 回挂载(顺序整写,
    不是追加),否则 pod 重启后历史任务的日志就没了。"""
    sink = []
    root, rid = _start(tmp_path, sink)
    shell = sink[0][0][2]
    local = os.path.join(runner.LOCAL_RUNS_ROOT, rid)
    mount = os.path.join(root, rid)
    assert f"cp -f {local}/run.log {mount}/run.log" in shell
    assert f"cp -f {local}/exit_code {mount}/exit_code" in shell
    # 退出码文件是"任务已终结"的信号:它在挂载上出现时,日志必须已经完整
    assert shell.index(f"cp -f {local}/run.log") < shell.index(f"cp -f {local}/exit_code")


def test_tail_log_prefers_local_then_falls_back_to_the_mount(tmp_path):
    """读日志一律本地优先、挂载兜底:本地有=本机这轮起的(活跃期只有本地那份),
    本地没有=历史任务或换了 pod(读挂载上的存档),两边都有以本地为准(它是活的)。"""
    root = str(tmp_path / "runs")

    def _put(where, rid, text):
        d = os.path.join(where, rid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "run.log"), "w", encoding="utf-8") as f:
            f.write(text)

    _put(runner.LOCAL_RUNS_ROOT, "only-local", "本地这行")
    _put(root, "only-mount", "挂载那行")
    _put(runner.LOCAL_RUNS_ROOT, "both", "本地这行")
    _put(root, "both", "挂载那行")
    assert runner.tail_log(root, "only-local") == "本地这行"
    assert runner.tail_log(root, "only-mount") == "挂载那行"
    assert runner.tail_log(root, "both") == "本地这行"


def test_status_reads_the_exit_code_from_local_first(tmp_path):
    """退出码同样本地优先:归档要等任务结束,活跃期只有本地那份 —— 只看挂载会让
    刚跑完的任务在界面上继续"运行中"。"""
    root, rid = _start(tmp_path)
    with open(os.path.join(runner.LOCAL_RUNS_ROOT, rid, "exit_code"), "w") as f:
        f.write("0\n")
    assert not os.path.exists(os.path.join(root, rid, "exit_code"))
    assert runner.status(root, rid, alive=lambda p: True)["state"] == "done"


def test_mutex_and_status_agree_when_only_the_local_copy_survives(tmp_path):
    """互斥与状态读走同一套路径解析。

    "本地看没有、挂载看有"(或反过来)各判各的,轻则界面显示空闲、一点开始却被拒,
    重则两个批同时开跑砸穿方舟配额。这里把挂载那份连同进程内缓存一起抹掉,只剩
    本地盘作数 —— 模拟 FSX 目录可见延迟那一段窗口。
    """
    root, rid = _start(tmp_path)
    shutil.rmtree(os.path.join(root, rid))
    runner._STARTED.pop(os.path.abspath(root), None)
    for key in [k for k in runner._WRITE_CACHE if k.startswith(os.path.abspath(root))]:
        runner._WRITE_CACHE.pop(key)

    assert runner.active_run(root, alive=lambda p: True)["run_id"] == rid
    with pytest.raises(runner.RunBusyError):
        runner.start(root, "backends", ["x"], now=_now("2026-08-13 15:00:00"),
                     popen=_fake_popen(), alive=lambda p: True)


def test_local_runs_of_another_delivery_do_not_leak_into_this_one(tmp_path):
    """本地目录按 run_id 平铺,同一个 pod 上开两份交付时靠 cmd.json 里的 runs_root
    认领主 —— 认目录名的话,甲交付的任务会出现在乙交付的历史里。"""
    root_a, rid_a = _start(tmp_path)
    root_b = str(tmp_path / "other" / ".runs")
    assert [r["run_id"] for r in runner.list_runs(root_a)] == [rid_a]
    assert runner.list_runs(root_b) == []


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


def _site_backends(tmp_path, body: str) -> str:
    """写一份站点配置。IP 一律用 RFC 5737 文档段,fixture 里不出现任何真实地址。"""
    site = tmp_path / "site.yaml"
    site.write_text("vlm_backends:\n" + body, encoding="utf-8")
    return str(site)


def test_vlm_backend_labels_always_show_declared_hardware(tmp_path):
    """配置声明了 hardware 就**一律**拼进标签,哪怕这台是独一份。

    2026-08-13 用户点名:两台 8B 因为标签重名带上了卡型,独一份的 32B 反而不带,
    同一排下拉里一个有一个没有,看着像漏了。
    """
    site = _site_backends(tmp_path,
                          "  house-32b:\n"
                          "    endpoint: http://192.0.2.10:8000/v1\n"
                          "    model: nvidia/Cosmos-Reason2-32B\n"
                          "    service_type: 自托管 vLLM\n"
                          "    hardware: H20\n")
    labels = runner.vlm_backend_labels(site)
    assert labels.get("自托管 vLLM · Cosmos-Reason2-32B · H20") == "house-32b"


def test_vlm_backend_labels_keep_two_machines_of_the_same_model_apart(tmp_path):
    """同类型同模型两台机器(实测:8B 同时跑在两种卡上)靠 hardware 区分,互不覆盖;
    两台都没声明硬件时仍各占一行(补空格保序),绝不静默丢掉一个预设。"""
    site = _site_backends(tmp_path,
                          "  gpu-a:\n"
                          "    endpoint: http://192.0.2.11:8000/v1\n"
                          "    model: Cosmos-Reason1-8B\n"
                          "    service_type: 自托管 vLLM\n"
                          "    hardware: A30\n"
                          "  gpu-b:\n"
                          "    endpoint: http://192.0.2.12:8000/v1\n"
                          "    model: Cosmos-Reason1-8B\n"
                          "    service_type: 自托管 vLLM\n"
                          "    hardware: H20\n"
                          "  nameless-1:\n"
                          "    endpoint: http://192.0.2.13:8000/v1\n"
                          "    model: Qwen2.5-VL-7B\n"
                          "    service_type: 自托管 vLLM\n"
                          "  nameless-2:\n"
                          "    endpoint: http://192.0.2.14:8000/v1\n"
                          "    model: Qwen2.5-VL-7B\n"
                          "    service_type: 自托管 vLLM\n")
    labels = runner.vlm_backend_labels(site)
    for code in ("gpu-a", "gpu-b", "nameless-1", "nameless-2"):
        assert code in labels.values(), code
    assert labels["自托管 vLLM · Cosmos-Reason1-8B · A30"] == "gpu-a"
    assert labels["自托管 vLLM · Cosmos-Reason1-8B · H20"] == "gpu-b"
    assert not [k for k in labels if k in labels.values()]      # 代号仍然不是标签


# ── 并发默认值:只为显示,读不到就弃权 ────────────────────────────────────

def test_concurrency_defaults_read_the_shipped_config():
    """界面上三个并发框的占位符要显示**生效配置里的**默认值(出厂是 32/16/32)。"""
    assert runner.concurrency_defaults() == {"ep": 32, "fr": 16, "cap": 32}


def test_concurrency_defaults_site_file_overrides_only_what_it_declares(tmp_path):
    """站点文件覆盖它写了的那一项,没写的仍取出厂值 —— 整层覆盖会让另外两个变 None,
    界面上就成了"一个有默认值两个没有"。"""
    site = tmp_path / "site.yaml"
    site.write_text("pipeline:\n  vlm_episode_concurrency: 8\n", encoding="utf-8")
    assert runner.concurrency_defaults(str(site)) == {"ep": 8, "fr": 16, "cap": 32}


def test_concurrency_defaults_ignore_a_missing_site_file(tmp_path):
    assert runner.concurrency_defaults(str(tmp_path / "nope.yaml")) \
        == {"ep": 32, "fr": 16, "cap": 32}


def test_concurrency_defaults_abstain_when_the_config_says_nothing(monkeypatch):
    """读不到就给 None,界面退回"留空 = 用配置里的值"。**绝不硬编码一个数字充数**:
    界面印着 32 而配置其实是 8,用户会照着那个错数做判断(同"fps 猜 30"那条禁令)。"""
    monkeypatch.setattr(runner, "_config_layers", lambda config_path=None: [])
    assert runner.concurrency_defaults() == {"ep": None, "fr": None, "cap": None}
    monkeypatch.setattr(runner, "_config_layers",
                        lambda config_path=None: [{"pipeline": {"别的": 1}}])
    assert runner.concurrency_defaults() == {"ep": None, "fr": None, "cap": None}


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
    # 「未完成」不是「失败」(2026-08-13 用户定):质检只有跑完/没跑完,
    # "失败"会被读成"数据判坏了/系统崩了"。颜色仍是红档。
    assert "未完成" in runner.status_html({"state": "failed", "exit_code": 2})
    assert "失败" not in runner.status_html({"state": "failed", "exit_code": 2})
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
    assert "未完成" in rows[0][2] and "退出码 3" in rows[0][2]
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
    assert "TERM INT; { " in shell and "_fin $?" in shell   # 单 job 仍走老路径
    cmd = json.loads(open(os.path.join(root, rid, "cmd.json")).read())
    assert "review-page" in " ".join(cmd["then_argv"])       # 历史里查得出跑了两步


# ── 多数据集:一次点击顺序跑几个(2026-08-13)──────────────────────────────

def test_picked_datasets_normalises_the_dropdown_value():
    """multiselect 给 list、单选给字符串、没选给空 —— 回调只认一种形状。

    防的是"选了却没跑":下拉从单选改成多选那天,任何一处还按字符串取值的回调都会
    静默把整个列表当成一个数据集名(或者干脆取到空),而界面上看不出任何异常。
    """
    assert runner.picked_datasets(["a", "b"]) == ["a", "b"]
    assert runner.picked_datasets("a") == ["a"]
    assert runner.picked_datasets([" a ", "", None]) == ["a"]
    assert runner.picked_datasets(None) == [] and runner.picked_datasets([]) == []


def test_multi_dataset_jobs_go_into_one_parent_folder():
    """选多个时交付名当**父文件夹**,每个数据集一份子交付 <交付名>/<数据集名>/。

    形状与 CLI `--batch` 一致是有意的:报告页的递归发现本来就找得到这种目录,
    不必为多选再造一套约定(造了就得再改一遍报告页那套已交付给客户的代码)。
    """
    jobs = runner.build_dataset_jobs("/data", "/deliv", ["so101", "bridge"],
                                     "0813-batch", lite=True)
    assert [j["title"] for j in jobs] == ["so101", "bridge"]
    argv = jobs[0]["steps"][0]
    assert "/data/so101" in argv and "/deliv/0813-batch/so101" in argv
    assert "/deliv/0813-batch/bridge" in jobs[1]["steps"][0]
    assert "--lite" in argv                      # 公共参数每个数据集都带上


def test_multi_dataset_jobs_still_validate_every_name():
    """路径仍然是"只收名字、由后端拼":任何一个名字越界,整批直接拒。"""
    with pytest.raises(ValueError):
        runner.build_dataset_jobs("/data", "/deliv", ["ok", "../etc"], "out")
    with pytest.raises(ValueError):
        runner.build_dataset_jobs("/data", "/deliv", [], "out")


def test_single_job_script_is_unchanged_and_uses_and():
    """单数据集(含"质检+切片")一个字没变:仍是 `a && b`,退出码就是命令自己的。

    多数据集的"一个失败不挡后面"绝不能倒灌回这里 —— 切片依赖质检的产出,质检没成
    还去切片,只会在日志里多一堆看不懂的报错。
    """
    s = runner.build_run_script([{"title": "", "steps": [["a", "1"], ["b"]]}])
    assert s == "a 1 && b"
    assert "_failed" not in s


def test_multi_dataset_script_semantics_end_to_end():
    """真拿 bash 跑一遍,把三件事一次钉死(纯 true/false,不碰管道):

    ① 一个数据集失败**不挡**后面的(与 `--batch` 的"单集失败不拖垮整批"对齐);
    ② 一个数据集**内部**的两步仍是 `&&`(第一步没成,第二步绝不执行);
    ③ 退出码 = 没跑完的数据集**个数**,全成功 = 0。
    这三条只在真 shell 里才算数,断字符串断不出来。
    """
    import subprocess

    script = runner.build_run_script([
        {"title": "good1", "steps": [["echo", "RAN-good1"]]},
        {"title": "bad", "steps": [["false"], ["echo", "MUST-NOT-RUN"]]},
        {"title": "good2", "steps": [["echo", "RAN-good2"]]},
        {"title": "bad2", "steps": [["false"]]}])
    p = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)
    assert "RAN-good1" in p.stdout and "RAN-good2" in p.stdout   # ①坏的没挡住后面
    assert "MUST-NOT-RUN" not in p.stdout                        # ②job 内部仍掐断
    assert p.returncode == 2                                     # ③两个没跑完

    ok = runner.build_run_script([{"title": "a", "steps": [["true"]]},
                                  {"title": "b", "steps": [["true"]]}])
    assert subprocess.run(["/bin/bash", "-c", ok]).returncode == 0


def test_multi_dataset_script_prints_a_separator_per_dataset():
    """每个数据集开跑前打一行醒目分隔(与 cli 的 batch 输出同款)。

    没有它,三个数据集的日志糊成一片 —— 出了错连"是哪一份炸的"都看不出来;
    进度解析(parse_progress_all)也靠这行断句。
    """
    s = runner.build_run_script([{"title": "so101", "steps": [["true"]]},
                                 {"title": "bridge", "steps": [["true"]]}])
    assert "===== so101 =====" in s and "===== bridge =====" in s


def test_multi_dataset_failure_count_is_capped_below_the_reserved_codes():
    """失败个数封顶 125:126/127/128+n 是 shell 的保留语义(不可执行/找不到/被信号
    杀),越过去就会跟"人点了停止"(143)混在一起,而那两件事界面上必须分得开。"""
    s = runner.build_run_script([{"title": f"d{i}", "steps": [["true"]]}
                                 for i in range(3)])
    assert f"> {runner.MULTI_RUN_MAX_RC} ? {runner.MULTI_RUN_MAX_RC}" in s


def test_start_records_the_whole_job_table(tmp_path):
    """多数据集任务的 cmd.json 要能看出跑了哪几个 —— 只记第一条 argv 的话,
    历史里那行任务永远只认得出第一个数据集。"""
    sink = []
    root = str(tmp_path / "runs")
    jobs = runner.build_dataset_jobs("/data", "/deliv", ["so101", "bridge"], "out")
    rid = runner.start(root, "run", jobs[0]["steps"][0], label="质检 2 个数据集",
                       now=_now(), popen=_fake_popen(sink=sink),
                       alive=lambda p: True, jobs=jobs)
    cmd = json.loads(open(os.path.join(root, rid, "cmd.json")).read())
    assert [j["title"] for j in cmd["jobs"]] == ["so101", "bridge"]
    shell = sink[0][0][2]
    assert "_fin $?" in shell and "exit_code" in shell        # 退出码照旧落盘
    assert "===== bridge =====" in shell


# ── 累积进度(2026-08-13):阶段一换不再归零重来 ────────────────────────────

_MULTI_STAGE_LOG = (
    "[curation] 数据集: droid | 机器人: Franka | 199 条\n"
    "[curation] 数值检查 199/199 (100%) | 已用 12s\n"
    "[curation] 视觉质量 + 视频动作同步(共用一次解码) 199/199 (100%) | 已用 3.0min\n"
    "[curation] VLM 任务成败判定 12/199 (6%) | 已用 3.2min | 剩余 ~48min\n")


def test_parse_progress_all_keeps_every_stage():
    """三个阶段三条,顺序照日志;跑满的标完成,当前的带百分比与剩余。

    防的是 2026-08-13 用户提的那件事:只画最后一条进度时,阶段一换进度条就归零
    重来,等了半小时的人看不出"已经过了几关",判断不了还值不值得等。
    """
    got = runner.parse_progress_all(_MULTI_STAGE_LOG)
    assert [p["stage"] for p in got] == [
        "数值检查", "视觉质量 + 视频动作同步(共用一次解码)", "VLM 任务成败判定"]
    assert [p["done"] for p in got] == [True, True, False]
    assert got[-1]["pct"] == 6 and got[-1]["eta"] == "48min"
    assert got[0]["elapsed"] == "12s"          # 完成态要显示这个阶段最终用了多久


def test_parse_progress_all_same_stage_keeps_the_last_reading():
    """同名阶段只占一行,读数取最后一次 —— 一个阶段刷二十行,不该变成二十根条。"""
    got = runner.parse_progress_all(
        "[curation] 数值检查 10/199 (5%) | 已用 1s\n"
        "[curation] 数值检查 40/199 (20%) | 已用 4s\n")
    assert len(got) == 1 and got[0]["n"] == 40 and got[0]["done"] is False


def test_parse_progress_all_marks_earlier_stages_done_without_a_full_count():
    """没数到满就换了阶段(被跳过/提前收):后面开打就说明前面收工了。

    管道是顺序跑的,这是"完成"的第二个信号;硬信号(n==total)优先,但不能只认它
    —— 只认它的话,前一个阶段会永远停在半截,看着像卡住了。
    """
    got = runner.parse_progress_all(
        "[curation] 抽帧 30/199 (15%) | 已用 20s\n"
        "[curation] VLM 任务成败判定 3/40 (7%) | 已用 30s\n")
    assert got[0]["done"] is True and got[1]["done"] is False


def test_parse_progress_all_never_invents_a_percentage():
    """阶段式(一步 = 一次 LLM 大调用)占一行,但**不许编百分比**。

    3/5 步反推 60% 等于给它配一条匀速前进的假条:五步的耗时天差地别,卡在第三步时
    用户还以为在动(progress.py 顶上那条纪律)。
    """
    got = runner.parse_progress_all(
        "[curation] 技能画像 3/5 归纳技能体系(LLM)… | 已用 1.2min")
    assert len(got) == 1 and got[0]["pct"] is None
    assert (got[0]["n"], got[0]["total"]) == (3, 5)     # n/total 照样显示


def test_parse_progress_all_splits_on_the_dataset_separator():
    """多数据集顺序跑:分隔行断句,第二份的同名阶段另起一行。

    不断句的话,第二个数据集的「数值检查」会把第一个那行改回 5% —— 看着像已经跑完
    的活又倒回去了。
    """
    got = runner.parse_progress_all(
        "===== so101 =====\n"
        "[curation] 数值检查 199/199 (100%) | 已用 12s\n"
        "===== bridge =====\n"
        "[curation] 数值检查 10/199 (5%) | 已用 1s\n")
    assert len(got) == 2
    assert [p["section"] for p in got] == ["so101", "bridge"]
    assert got[0]["done"] is True and got[1]["n"] == 10


def test_parse_progress_all_empty_and_garbage():
    """认不出就是空列表(界面只滚日志),绝不给不可预测的步骤配假进度条。"""
    assert runner.parse_progress_all("") == []
    assert runner.parse_progress_all("Traceback (most recent call last):") == []


def test_status_html_draws_one_bar_per_stage():
    """一列条:跑完的留在原地(满条 + 阶段名 + 最终用时,颜色淡一档),当前的照旧。"""
    html = runner.status_html({"state": "running", "label": "质检 droid"},
                              runner.parse_progress_all(_MULTI_STAGE_LOG))
    assert html.count("border-radius:999px;overflow:hidden") == 3   # 三个阶段三根条
    assert "数值检查" in html and "用时 12s" in html
    assert "6%" in html and "剩余 ~48min" in html
    assert runner._BAR_DONE in html and runner._BAR_LIVE in html    # 完成态淡一档


def test_status_html_still_takes_a_single_progress_dict():
    """老形状(parse_progress 的单条 dict)照收 —— 那个签名与行为一个字没动。"""
    html = runner.status_html({"state": "running"},
                              {"stage": "抽帧", "n": 5, "total": 10, "pct": 50,
                               "elapsed": "3s", "eta": "3s"})
    assert "50%" in html and "抽帧" in html


# ── 模型服务标签:硬件只在它真是"型号"时才拼(2026-08-13)────────────────

def test_hardware_suffix_keeps_a_real_gpu_model():
    """`NVIDIA H20` / `NVIDIA A30` 必须继续拼上 —— 两台自托管服务靠卡型区分,
    不拼的话下拉里两条长得一模一样,用户选不出想要的那台。"""
    assert runner.hardware_suffix("自托管 vLLM 服务", "NVIDIA H20") == "NVIDIA H20"
    assert runner.hardware_suffix("自托管 vLLM 服务", " NVIDIA A30 ") == "NVIDIA A30"


def test_hardware_suffix_drops_a_whole_sentence():
    """防的是 2026-08-13 验收时看到的那条:方舟的标签渲染成

        方舟 MaaS(托管服务) · doubao-seed-2-0-pro-260215 · 托管服务,硬件不可见(由服务商调度)

    「托管服务」说了两遍,后半句还是整句描述而不是型号。带句读/过长的一律不拼。"""
    kind = "方舟 MaaS(托管服务)"
    assert runner.hardware_suffix(kind, "托管服务,硬件不可见(由服务商调度)") == ""
    assert runner.hardware_suffix(kind, "由服务商统一调度不对外暴露具体机型") == ""


def test_hardware_suffix_drops_a_restatement_of_the_service_type():
    """硬件字段只是把服务类型重说一遍时也不拼(短到不触发长度判据的那种)。"""
    assert runner.hardware_suffix("方舟 MaaS(托管服务)", "托管服务") == ""
    assert runner.hardware_suffix("自托管推理服务(集群内)", "") == ""
    assert runner.hardware_suffix("自托管推理服务(集群内)", None) == ""


def test_backend_labels_keep_hardware_only_when_it_is_a_model(monkeypatch):
    """整条链路走一遍:托管服务那条标签到「模型名」为止,自托管那条带着卡型。"""
    layer = {"vlm_backends": {
        "ark": {"endpoint": "https://ark.cn-beijing.volces.com/api/v3",
                "model": "doubao-seed-2-0-pro-260215",
                "service_type": "方舟 MaaS(托管服务)",
                "hardware": "托管服务,硬件不可见(由服务商调度)"},
        "house": {"endpoint": "http://vllm-cosmos-8b:8000/v1",
                  "model": "Cosmos-Reason1-8B",
                  "service_type": "自托管 vLLM", "hardware": "NVIDIA A30"}}}
    monkeypatch.setattr(runner, "_config_layers", lambda config_path=None: [layer])
    labels = runner.vlm_backend_labels(None)
    assert "方舟 MaaS(托管服务) · doubao-seed-2-0-pro-260215" in labels
    assert "自托管 vLLM · Cosmos-Reason1-8B · NVIDIA A30" in labels
    assert not any("硬件不可见" in k for k in labels)


# ── 状态条:多数据集时退出码要说人话(2026-08-13)────────────────────────

def test_failed_note_translates_the_exit_code_for_multi_dataset_runs():
    """防的是 2026-08-13 验收看到的那句:多数据集串跑的退出码**就是**没跑完的
    数据集个数(build_run_script 定的),状态条却照旧写「没跑到底(退出码 3)」——
    读起来像个神秘错误码,而它其实是能直接看懂的信息。"""
    note = runner.failed_note({"exit_code": 3, "n_jobs": 5})
    assert note == "3 个数据集没跑完;是哪几个见下方日志"
    assert "退出码" not in note


def test_failed_note_keeps_the_exit_code_for_a_single_dataset():
    """单数据集维持原样:那里的退出码是 CLI 自己的返回值,只有排障价值。"""
    assert "退出码 2" in runner.failed_note({"exit_code": 2, "n_jobs": 1})
    assert "退出码 2" in runner.failed_note({"exit_code": 2})


def test_failed_note_does_not_invent_a_count_when_the_code_is_out_of_range():
    """整串被信号打断时退出码不再是"个数"(143 > 作业数),不硬翻成"143 个没跑完"。"""
    note = runner.failed_note({"exit_code": 143, "n_jobs": 5})
    assert "143" not in note and "没跑到底" in note


def test_status_carries_the_job_count_so_the_bar_can_speak_plainly(tmp_path):
    """状态条拿得到作业数才翻得动那句话 —— status() 必须把它从 cmd.json 带出来。"""
    root = str(tmp_path / "runs")
    jobs = runner.build_dataset_jobs("/data", "/deliv", ["so101", "bridge", "droid"], "out")
    rid = runner.start(root, "run", jobs[0]["steps"][0], label="质检 3 个数据集",
                       now=_now(), popen=_fake_popen(), alive=lambda p: False,
                       jobs=jobs)
    with open(os.path.join(root, rid, "exit_code"), "w") as f:
        f.write("2\n")
    st = runner.status(root, rid, alive=lambda p: False)
    assert st["state"] == "failed" and st["n_jobs"] == 3
    assert "2 个数据集没跑完" in runner.status_html(st)


# ── 开跑前的数据集选择校验(2026-08-13:多选之后默认一个都没选)──────────

def test_dataset_selection_error_blocks_an_empty_multiselect():
    """多选下拉默认空,点「开始质检」必须被拦住并说清楚。

    不拦的后果不是"没反应":空选会一路走到 resolve_under(data_root, ""),
    那解析出来正好是数据集根目录本身 —— 等于悄悄拿整个根当一份数据集去跑。
    """
    msg = runner.dataset_selection_error([], batch=False)
    assert msg and "数据集" in msg and "跑全部" in msg
    assert runner.dataset_selection_error(None, batch=False) == msg
    assert runner.dataset_selection_error(["  "], batch=False) == msg


def test_dataset_selection_error_lets_real_selections_and_batch_through():
    """选了就放行;勾了「跑全部」时下拉本来就被忽略,也不该被拦。"""
    assert runner.dataset_selection_error(["so101"], batch=False) == ""
    assert runner.dataset_selection_error("so101", batch=False) == ""
    assert runner.dataset_selection_error([], batch=True) == ""


# ── 「停止」按下去永远卡在「正在停止」(2026-08-14 用户实见)───────────────
#
# 现场(.runs/20260814-043019-run):用户点了停止 → 状态 stopping,**七分钟后仍是
# stopping**;两个进程都是 Z(僵尸)且 ppid=1;`exit_code` 文件**根本不存在**;
# 日志时间戳定格在点停止的那一刻 —— 进程其实早就死了。根因两条缺一不可:
#   ① killpg 把外层那个 bash 也砍了,它还没走到 `echo $? > exit_code` 就没了;
#   ② 本 pod 的 PID 1 就是这个 UI(Python 进程),不会 wait() 领养来的子进程 ⇒
#      僵尸永远不被回收,而僵尸的 kill(pid,0) 照样成功。
# 最后是人工 `echo 143 > exit_code` 才让状态翻成「已停止(人工)」。

_PROC_SEQ = itertools.count()


def _fake_proc(tmp_path, entries: dict) -> str:
    """造一个假的 /proc:{pid: (状态位, pgrp)}。用例里造不出真僵尸,只能造 /proc。

    comm 字段故意写成带空格**和右括号**的样子:真实现场里它就是 `(bash -c 'x)y')`
    这种鬼东西,而 `split()[2]` 那种取法会当场读错位(所以实现从最后一个 ')' 切)。
    每次调用另起一个目录:同一个用例里造两版 /proc 时,上一版残留的进程会串进来。
    """
    root = tmp_path / f"proc-{next(_PROC_SEQ)}"
    for pid, (state, pgrp) in entries.items():
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "stat").write_text(
            f"{pid} (bash -c 'x)y') {state} 1 {pgrp} 0 -1 4194304 0 0",
            encoding="utf-8")
    return str(root)


def test_alive_counts_a_zombie_as_dead(tmp_path, monkeypatch):
    """僵尸必须算死的,否则「正在停止」永远翻不过去。

    这条不是理论洁癖:本 pod 的 PID 1 就是 UI 自己,Python 不 wait() 领养来的子
    进程,所以 2026-08-14 那两个僵尸**重起 7861 都清不掉**,而 kill(pid,0) 一直
    返回成功 —— _alive 只看 kill 的话这个任务到今天还在"运行中"。
    """
    monkeypatch.setattr(runner, "PROC_ROOT",
                        _fake_proc(tmp_path, {1035487: ("Z", 1035487),
                                              1035488: ("S", 1035487)}))
    assert runner._alive(1035487) is False        # 僵尸 = 死
    assert runner._alive(1035488) is True         # 睡着的还活着
    assert runner._alive(999999) is False         # /proc 里没有这一项


def test_alive_falls_back_to_kill_when_there_is_no_proc(tmp_path, monkeypatch):
    """取不到 /proc(非 Linux / 没挂 proc)时退回原来的 kill(pid, 0),不能一律判死。"""
    monkeypatch.setattr(runner, "PROC_ROOT", str(tmp_path / "no-such-proc"))
    assert runner._alive(os.getpid()) is True
    assert runner._alive(0) is False


def test_group_alive_ignores_zombie_members(tmp_path, monkeypatch):
    """进程组里只剩僵尸 = 已经停干净了;还有活的成员就不算停干净。

    只看组长是不够的:跑批 fork 出来的解码/VLM 子进程还在烧方舟的钱时,组长可能
    已经先变成僵尸了。
    """
    monkeypatch.setattr(runner, "PROC_ROOT",
                        _fake_proc(tmp_path, {1035487: ("Z", 1035487),
                                              1035488: ("Z", 1035487)}))
    assert runner._group_alive(1035487) is False
    monkeypatch.setattr(runner, "PROC_ROOT",
                        _fake_proc(tmp_path, {1035487: ("Z", 1035487),
                                              1035489: ("R", 1035487)}))
    assert runner._group_alive(1035487) is True


def test_polling_no_longer_hangs_on_stopping_when_only_zombies_are_left(tmp_path,
                                                                       monkeypatch):
    """★ 七分钟卡在「正在停止」的那个现场:进程是僵尸、退出码文件不存在。

    轮询必须能自己走到终态 —— 用户面前只有这一条路(他当时是手工 echo 143 才解开的)。
    """
    root, rid = _start(tmp_path, pid=1035487)
    p = runner._paths(root, rid)
    st = runner._read_state(p, "status")
    st["state"] = "stopping"                       # 用户点过停止,信号也发出去了
    runner._write_state(p, "status", st)
    monkeypatch.setattr(runner, "PROC_ROOT",
                        _fake_proc(tmp_path, {1035487: ("Z", 1035487)}))
    assert not os.path.exists(p["rc"])             # 退出码文件根本不会出现
    out = runner.status(root, rid)
    assert out["state"] == "stopped"
    assert out["finished_at"]


def test_stop_writes_the_terminal_state_itself_when_no_exit_code_will_come(
        tmp_path, monkeypatch):
    """★ stop() 不许干等一个注定不出现的文件。

    进程组里已经没有活着的成员时就自己落终态;退出码拿不到就记 None 并在 note 里
    说清楚 —— 补一个 143 上去是伪造证据(界面对「已停止(人工)」本来也不显示退出码)。
    """
    monkeypatch.setattr(runner, "PROC_ROOT",
                        _fake_proc(tmp_path, {1035487: ("Z", 1035487)}))
    root, rid = _start(tmp_path, pid=1035487)
    killed = []
    st = runner.stop(root, rid, killer=lambda pid, sig: killed.append((pid, sig)),
                     sleep=lambda s: None, now=_now("2026-08-14 04:37:00"))
    assert killed == [(1035487, runner.signal.SIGTERM)]
    assert st["state"] == "stopped"
    assert st["exit_code"] is None and "没留下退出码" in st["note"]
    assert st["finished_at"] == "2026-08-14 04:37:00"


def test_stop_leaves_it_to_polling_while_the_group_is_still_alive(tmp_path,
                                                                 monkeypatch):
    """组里还有活着的成员 → 不许急着宣布"已停止":那是把还在烧钱的跑批说成停了。"""
    monkeypatch.setattr(runner, "PROC_ROOT",
                        _fake_proc(tmp_path, {4321: ("S", 4321)}))
    root, rid = _start(tmp_path)
    st = runner.stop(root, rid, killer=lambda pid, sig: None,
                     sleep=lambda s: None, grace_s=0.3)
    assert st["state"] == "stopping"


def test_the_outer_shell_traps_the_stop_signal(tmp_path):
    """外层 shell 必须装 trap:停止时杀的是整个进程组,它自己也在组里。

    没有 trap 的后果就是 2026-08-14 那次 —— bash 收到 TERM 当场退,
    `echo $? > exit_code` 永远执行不到,而那正是本模块判定"任务已终结"的唯一依据。
    """
    sink = []
    _start(tmp_path, sink)
    shell = sink[0][0][2]
    assert "trap '_fin 143; exit 143' TERM INT" in shell
    assert shell.index("trap ") < shell.index("2>&1")   # 先装 trap 再开跑


def test_stopping_a_real_long_command_settles_within_ten_seconds(tmp_path):
    """★ 真跑一遍:起一个假的长命令,点停止,十秒内必须变成终态。

    这条是上面那堆假 /proc 用例的兜底 —— 停止这件事的真相只在真 bash + 真信号里,
    断字符串断不出来。用 `echo GO; sleep 300` 而不是光 sleep:要等到 trap 装好、
    作业真的跑起来了再发信号,否则测的是一个还没来得及装 trap 的 shell。
    """
    root = str(tmp_path / "runs")
    rid = runner.start(root, "backends",
                       ["/bin/bash", "-c", "echo GO; sleep 300"],
                       label="假的长命令", now=_now(), alive=lambda p: False)
    pid = runner.status(root, rid)["pid"]
    try:
        for _ in range(100):                       # 最多等 10 秒等它真的跑起来
            if "GO" in runner.tail_log(root, rid):
                break
            time.sleep(0.1)
        assert "GO" in runner.tail_log(root, rid), "作业没起来,后面测的就不是停止了"

        t0 = time.time()
        st = runner.stop(root, rid)
        while st["state"] not in ("stopped", "done", "failed", "interrupted"):
            assert time.time() - t0 < 10, f"十秒还没变终态:{st}"
            time.sleep(0.2)
            st = runner.status(root, rid)
        assert st["state"] == "stopped"
        # trap 真的跑到了:退出码文件在,而且是"被 TERM 打断"的 143
        assert st["exit_code"] == 143, st
        if os.path.isdir(runner.PROC_ROOT):        # 只有 Linux 有 /proc(生产就在那)
            assert runner._group_alive(pid) is False   # 组里没有活着的成员了
    finally:
        try:
            os.killpg(pid, runner.signal.SIGKILL)
        except OSError:
            pass


def test_archived_status_is_read_back_and_rewritten_when_it_comes_back_broken(
        tmp_path, monkeypatch, capsys):
    """★ 归档回挂载的 status.json 要回读校验。

    2026-08-14 实测:挂载上那份变成了 **164 字节全 \\0** 的坏文件(和 savefig 静默
    产出零填充坏 PNG 同一家族)。它是 pod 重启之后唯一的历史,静默坏掉最不能忍 ——
    读不回就重写一次,并在 stderr 留一行。
    """
    root, rid = _start(tmp_path)
    archived = os.path.join(root, rid, "status.json")
    real_copy = runner._copy_text
    broke = []

    def flaky(path, blob):
        if path == archived and not broke:
            broke.append(path)
            with open(path, "wb") as f:
                f.write(b"\0" * 164)               # 现场就是这个样子
            return
        real_copy(path, blob)

    monkeypatch.setattr(runner, "_copy_text", flaky)
    open(os.path.join(root, rid, "exit_code"), "w").write("0")
    st = runner.status(root, rid, alive=lambda p: False)
    assert st["state"] == "done"
    assert broke, "这条用例没触发到归档写,白测了"
    assert json.loads(open(archived, encoding="utf-8").read())["state"] == "done"
    assert "读不回来" in capsys.readouterr().err


# ── 历史表跟状态条说同一种话(2026-08-14)────────────────────────────────

def test_history_row_says_how_many_datasets_did_not_finish():
    """多数据集时历史表不能还写「未完成(退出码 3)」。

    状态条早就改口说「3 个数据集没跑完」了,而多数据集下退出码**就是**这个个数
    (build_run_script 定的语义)—— 同一件事两处两种说法,其中一种还是读不懂的
    神秘错误码。退出码落在个数区间之外时两边都不硬翻。
    """
    assert runner.failed_suffix({"exit_code": 3, "n_jobs": 5}) == "(3 个数据集没跑完)"
    assert runner.failed_suffix({"exit_code": 2, "n_jobs": 1}) == "(退出码 2)"
    assert runner.failed_suffix({"exit_code": 2}) == "(退出码 2)"
    assert runner.failed_suffix({"exit_code": 143, "n_jobs": 5}) == ""
    assert runner.failed_suffix({"exit_code": None, "n_jobs": 5}) == ""

    rows = runner.history_rows([
        {"state": "failed", "exit_code": 3, "n_jobs": 5, "label": "质检 5 个数据集",
         "started_at": "2026-08-14 04:30:19", "finished_at": "2026-08-14 05:00:19",
         "run_id": "r1"},
        {"state": "failed", "exit_code": 2, "n_jobs": 1, "label": "质检 droid",
         "started_at": "2026-08-14 04:30:19", "finished_at": "2026-08-14 04:31:19",
         "run_id": "r2"}])
    assert rows[0][2] == "未完成(3 个数据集没跑完)"
    assert rows[1][2] == "未完成(退出码 2)"
    # 两处措辞同源:状态条那句里也是这个说法
    assert "3 个数据集没跑完" in runner.failed_note({"exit_code": 3, "n_jobs": 5})


# ── 多数据集的切片追问:问一次,覆盖全部(2026-08-14)────────────────────

def _ds(root, name, kind):
    d = root / name
    if kind == "rrd":
        d.mkdir(parents=True)
        (d / "episode_000.rrd").write_bytes(b"x")
    else:
        (d / "meta").mkdir(parents=True)
        (d / "meta" / "info.json").write_text(f'{{"codebase_version": "{kind}"}}')
    return name


def test_datasets_needing_clips_picks_only_the_ones_that_need_it(tmp_path):
    """选中的一批里挑出真要切片的那几个(v3 / rrd),v2 与认不出的都不算。"""
    root = tmp_path / "data"
    names = [_ds(root, "droid", "v3.0"), _ds(root, "bridge", "v2.1"),
             _ds(root, "so101", "rrd")]
    (root / "mystery").mkdir()
    assert runner.datasets_needing_clips(str(root), names + ["mystery"]) == \
        ["droid", "so101"]
    assert runner.datasets_needing_clips(str(root), []) == []


def test_clips_prompt_asks_once_for_the_whole_selection():
    """多选时问一句「这 N 个数据集…」并点名是哪几个 —— 绝不逐个弹窗。

    2026-08-14 之前多选**直接跳过不问**,于是多选跑出来的 v3/rrd 交付在 Episodes
    页全是"没有画面",而用户压根没被问过。
    """
    md = runner.clips_prompt(["droid", "so101"])
    assert "这 2 个数据集" in md and "droid" in md and "so101" in md
    assert "一起生成" in md or "要在质检之后一起生成" in md
    assert "这几份" in md


def test_clips_prompt_keeps_the_format_explanation_for_a_single_dataset():
    """单数据集仍按格式说清为什么要切 —— 那句话本来就在,别为了统一把它删了。"""
    v3 = runner.clips_prompt(["droid"], {"kind": "lerobot", "needs_clips": True})
    assert "LeRobot v3" in v3
    rrd = runner.clips_prompt(["so101"], {"kind": "rrd", "needs_clips": True})
    assert "rerun" in rrd
    assert runner.clips_prompt([]) == ""


def test_multi_dataset_jobs_chain_clips_only_for_the_ones_that_need_them():
    """答"一起生成"就给**每个需要的**数据集串上切片,不需要的一个字不加。

    片段站按数据集名建目录:站点认领看的是 site.json 里的源数据集路径
    (manifest.review_clip_paths),按数据集名建的话同一份数据换个交付还能直接复用。
    """
    jobs = runner.build_dataset_jobs(
        "/data", "/deliv", ["droid", "bridge", "so101"], "0814-batch",
        clips_root="/review", clips_for=["droid", "so101"], lite=True)
    assert [len(j["steps"]) for j in jobs] == [2, 1, 2]
    clip = jobs[0]["steps"][1]
    assert "review-page" in clip and "/data/droid" in clip and "/review/droid" in clip
    assert "/review/so101" in jobs[2]["steps"][1]


def test_multi_dataset_clips_need_a_review_dir():
    """没配片段目录就别假装能切:说清楚,不要拼出一个 None 路径去跑。"""
    with pytest.raises(ValueError):
        runner.build_dataset_jobs("/data", "/deliv", ["droid"], "out",
                                  clips_for=["droid"])
