"""VLM 超时对冲(hedge)测试(2026-08-15)。

防的是哪次事故:droid-200 交付里 6 次调用失败全是超时、其中 5 次耗时恰 600.1s
——旧默认 600s(llm 1800s)让慢调用卡在超时线上白等,任务判定探针一步本可
18.1 分钟干完被拖到 23.3 分钟。实测慢调用是服务端排队的随机抖动(按分钟稳定在
1.6%-7%),补发大概率落进快的那堆 ⇒ 分类型收紧超时 + 到线补发赛跑。

本文件钉死四件事(改回旧写法必红):
1. 对冲赢家是快的那发,总耗时 ≈ 补发耗时(不是两发之和、不是等首发死透再重试);
2. **一次逻辑调用最多 2×timeout**(用户拍板的唯一不变量,界面上要能承诺
   "最坏 120 秒出结果或作废");
3. 补发受同一并发闸门管,闸门满时必须等,在飞请求数绝不翻倍;
4. 快速报错的分类:5xx/连接错立即串行重发一次,4xx(429 在内)绝不重发。
"""
from __future__ import annotations

import inspect
import threading
import time

import pytest
import requests

from curation.adapters.vlm_client import (DEFAULT_TIMEOUTS_S, hedged_request,
                                          latency_record, latency_reset,
                                          latency_rows, latency_summary,
                                          timeout_for)


@pytest.fixture(autouse=True)
def _clean_latency():
    """每条测试前后清延时明细:对冲测试会留下落败发的迟到行,污染别的断言。"""
    latency_reset()
    yield
    # 落败的僵尸发最晚在 2T(本文件里 ≤1.2s)内退出;等它记完再清,
    # 否则它的 latency_record 会写进下一条测试的明细里
    time.sleep(0.05)
    latency_reset()


class _Resp:
    def __init__(self, status: int = 200, marker: str = ""):
        self.status_code = status
        self.ok = status < 400
        self.marker = marker


def _wait_rows(n: int, timeout: float = 3.0) -> list:
    """等落败那发把自己的行记完(它在 helper 返回后才死透)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        rows = latency_rows()
        if len(rows) >= n:
            return rows
        time.sleep(0.02)
    return latency_rows()


def test_hedge_winner_is_fast_second_attempt():
    """核心:首发慢 ⇒ 超时线上补发,拿快的那发;总耗时 ≈ 线 + 补发耗时。

    若退化成"等首发死透(2T)再重试",总耗时 ≥ 0.85s,下面的 <0.75 断言变红;
    若退化成"两发串行相加"同理。sleep 全部可控,不依赖真实网络。
    """
    T = 0.4
    calls: list[dict] = []
    lock = threading.Lock()

    def send(hard):
        with lock:
            i = len(calls)
            calls.append({"hard": hard, "t": time.time()})
        if i == 0:                       # 首发:慢到底,被自己的硬超时杀掉
            time.sleep(min(9.9, hard))
            raise requests.exceptions.Timeout("首发慢")
        time.sleep(0.05)                 # 补发:落进快的那堆
        return _Resp(200, marker="hedge")

    t0 = time.time()
    resp = hedged_request(send, tag="probe", timeout_s=T,
                          gate=threading.Semaphore(4))
    dt = time.time() - t0
    assert resp.marker == "hedge"
    assert dt < 0.75, f"总耗时 {dt:.2f}s:应 ≈ 超时线0.4 + 补发0.05,不是等首发死透"
    assert dt >= T - 0.02, "补发不该在超时线之前就发出"
    # 首发硬超时 = 2T;补发从超时线起,自己的死线不越过绝对 2T
    assert abs(calls[0]["hard"] - 2 * T) < 0.05
    assert calls[1]["t"] - t0 >= T - 0.02
    assert (calls[1]["t"] - t0) + calls[1]["hard"] <= 2 * T + 0.1
    # 明细:两发同 call_id,首发 attempt=0 记超时,补发 attempt=1 记成功
    rows = _wait_rows(2)
    mine = [r for r in rows if r[0] == "probe"]
    assert len(mine) == 2
    assert mine[0][4] == mine[1][4] and mine[0][4] is not None
    by_attempt = {r[5]: r for r in mine}
    assert by_attempt[0][2] is False and by_attempt[0][6] == "timeout"
    assert by_attempt[1][2] is True and by_attempt[1][6] == ""
    s = latency_summary()["probe"]
    assert s["attempts"] == 2 and s["hedged"] == 1 and s["unanswered"] == 0


def test_logical_call_hard_capped_at_twice_timeout():
    """★唯一不变量:从首发算起,整次逻辑调用最多 2×timeout 内返回或作废。

    两发都挂 ⇒ 首发在 2T 被硬杀、补发的死线也钉在同一绝对时刻 2T,调用方在
    ~2T 拿到异常走既有 except 分支(少一票)。旧写法(600s 硬等)在这里会把
    测试拖到天荒地老;"补发再给整 T"的写法会把 calls[1] 的死线推过 2T,变红。
    """
    T = 0.3
    calls: list[dict] = []
    lock = threading.Lock()

    def send(hard):
        with lock:
            calls.append({"hard": hard, "t": time.time()})
        time.sleep(min(9.9, hard))
        raise requests.exceptions.Timeout("挂死")

    t0 = time.time()
    with pytest.raises(requests.exceptions.Timeout):
        hedged_request(send, tag="probe", timeout_s=T,
                       gate=threading.Semaphore(4))
    dt = time.time() - t0
    assert len(calls) == 2, "共两次机会:首发 + 补发,一次不多"
    assert abs(calls[0]["hard"] - 2 * T) < 0.05, "首发硬超时应为 2T"
    for c in calls:                       # 两发各自的死线都不越过绝对 2T
        assert (c["t"] - t0) + c["hard"] <= 2 * T + 0.1
    assert dt <= 2 * T + 0.5, f"整次逻辑调用耗时 {dt:.2f}s,超出 2T 承诺"
    assert dt >= 2 * T - 0.1, "两发都挂时不该提前放弃(少等即多冤)"
    s = latency_summary()["probe"]
    assert s["unanswered"] == 1 and s["unanswered_timeout"] == 1
    assert s["hedged"] == 1 and s["attempts"] == 2


def test_hedge_waits_for_concurrency_gate():
    """补发不突破并发闸门:闸门满员时补发必须排队等许可,在飞数绝不翻倍。

    布景:闸门容量 2,一个许可被"别的在飞请求"占着到 t=0.7,首发占着另一个
    (挂死到 2T)。补发在超时线 t=0.5 就想发,但必须等到 0.7 才拿得到许可。
    若有人删掉闸门(或只给首发上闸),补发会在 ~0.5 起飞,下面断言变红。
    """
    T = 0.5
    gate = threading.Semaphore(2)
    assert gate.acquire(timeout=1)        # 模拟别的在飞请求占走一个许可
    released_at: list[float] = []

    def occupant():
        time.sleep(0.7)
        released_at.append(time.time())
        gate.release()

    threading.Thread(target=occupant, daemon=True).start()
    calls: list[dict] = []
    lock = threading.Lock()

    def send(hard):
        with lock:
            i = len(calls)
            calls.append({"hard": hard, "t": time.time()})
        if i == 0:
            time.sleep(min(9.9, hard))
            raise requests.exceptions.Timeout("首发挂死占着许可")
        time.sleep(0.05)
        return _Resp(200, marker="hedge")

    resp = hedged_request(send, tag="probe", timeout_s=T, gate=gate)
    assert resp.marker == "hedge"
    assert released_at, "布景线程没跑完,测试本身有问题"
    assert calls[1]["t"] >= released_at[0] - 0.05, \
        "补发在闸门许可释放前就发出去了 —— 在飞请求数被翻倍"


def test_attempt_threads_are_daemon():
    """两发必须跑在守护线程上(2026-08-15 主会话实测抓到的缺陷)。

    此前用 per-call ThreadPoolExecutor:工作线程非守护,CPython 退出时把它们
    全部 join —— 首发 sleep 8s、T=1s 时逻辑调用 1.14s 就返回,进程却要 8.26s
    才退出;生产 T=60 ⇒ 跑批干完后进程多挂最多 120s,任务台只认退出码文件,
    界面平白多显示两分钟「运行中」。改回 ThreadPoolExecutor,这条当场红。
    """
    T = 0.4
    seen: list = []

    def send(hard):
        seen.append(threading.current_thread())
        if len(seen) == 1:
            time.sleep(min(9.9, hard))
            raise requests.exceptions.Timeout("首发慢")
        time.sleep(0.05)
        return _Resp(200, marker="hedge")

    caller = threading.current_thread()
    resp = hedged_request(send, tag="probe", timeout_s=T,
                          gate=threading.Semaphore(4))
    assert resp.marker == "hedge"
    spawned = [t for t in seen if t is not caller]
    assert len(spawned) == 2, "两发都该在自己的工作线程里跑"
    assert all(t.daemon for t in spawned), \
        "对冲线程必须是守护线程 —— 否则落败那发会在解释器退出时被强制 join,卡住进程"


def test_losing_attempt_does_not_block_process_exit():
    """端到端钉死:落败发还挂着时,**进程**照样立即退出(任务台判终结的依据)。

    子进程里首发裸 sleep 4s(故意不理硬超时,模拟最恶劣的挂法),T=0.3,
    补发 0.05s 拿到结果。守护线程语义下子进程 ~0.4s 内退出;若回退成非守护
    (ThreadPoolExecutor),解释器退出被 join 拖到 ~4s,下面的墙钟上限变红。
    """
    import os
    import subprocess
    import sys

    code = """
import sys, threading, time
import requests
from curation.adapters.vlm_client import hedged_request

class R:
    def __init__(s): s.ok, s.status_code = True, 200

calls = []
def send(hard):
    calls.append(1)
    if len(calls) == 1:
        time.sleep(4.0)                      # 裸睡,故意无视硬超时
        raise requests.exceptions.Timeout()
    time.sleep(0.05)
    return R()

t0 = time.time()
r = hedged_request(send, tag="probe", timeout_s=0.3,
                   gate=threading.Semaphore(4))
print("RET", round(time.time() - t0, 2), r.ok)
"""
    t0 = time.time()
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=30, env=dict(os.environ))
    wall = time.time() - t0
    assert p.returncode == 0, p.stderr[-500:]
    assert "RET" in p.stdout and "True" in p.stdout
    assert wall < 2.5, (f"进程退出用了 {wall:.2f}s:落败发把解释器退出卡住了"
                        f"(非守护线程被 atexit join)")


def test_fast_5xx_gets_one_serial_resend():
    """首发秒回 5xx ⇒ 立即串行重发一次抢救该拿的那票(这不是对冲是重发)。"""
    seq = [_Resp(500), _Resp(200, marker="retry")]
    calls = []

    def send(hard):
        calls.append(time.time())
        return seq.pop(0)

    resp = hedged_request(send, tag="endstate", timeout_s=0.5,
                          gate=threading.Semaphore(2))
    assert resp.marker == "retry" and len(calls) == 2
    s = latency_summary()["endstate"]
    assert s["retried"] == 1 and s["hedged"] == 0 and s["unanswered"] == 0
    rows = [r for r in latency_rows() if r[0] == "endstate"]
    assert rows[0][6] == "http_error" and rows[0][5] == 0 and rows[1][5] == 1


def test_4xx_never_resent_429_included():
    """4xx 一律不重发:是我们自己的问题(请求/凭证/配额),429 尤其是服务端
    明说"你太快了",再发就是火上浇油。原响应交回,调用方 raise_for_status 走原路。"""
    for status in (400, 401, 404, 429):
        latency_reset()
        calls = []

        def send(hard, _s=status):
            calls.append(1)
            return _Resp(_s)

        resp = hedged_request(send, tag="caption", timeout_s=0.5,
                              gate=threading.Semaphore(2))
        assert resp.status_code == status and len(calls) == 1, status


def test_fast_connect_error_gets_one_serial_resend():
    """连接类网络错(非读超时)⇒ 立即串行重发一次;重发成功则票没丢。"""
    state = {"n": 0}

    def send(hard):
        state["n"] += 1
        if state["n"] == 1:
            raise requests.exceptions.ConnectionError("connection reset")
        return _Resp(200, marker="retry")

    resp = hedged_request(send, tag="arbitration", timeout_s=0.5,
                          gate=threading.Semaphore(2))
    assert resp.marker == "retry" and state["n"] == 2
    s = latency_summary()["arbitration"]
    assert s["retried"] == 1 and s["unanswered"] == 0


def test_read_timeout_not_resent():
    """读超时(Timeout 异常)不重发:它意味着 2T 预算已经烧完,再发没有空间。"""
    calls = []

    def send(hard):
        calls.append(1)
        raise requests.exceptions.Timeout("synthetic read timeout")

    with pytest.raises(requests.exceptions.Timeout):
        hedged_request(send, tag="llm", timeout_s=0.5,
                       gate=threading.Semaphore(2))
    assert len(calls) == 1


def test_both_attempts_dead_walks_existing_except_path(monkeypatch):
    """工厂级:两发都超时 ⇒ 工厂抛异常,调用方既有 except 分支(少一票)不变。

    走真实 make_llm_ask(而非直接调 helper):若有人把调用点改回裸
    requests.post(不过 hedged_request),这里只会记到一行、无 attempt=1,变红。
    """
    from curation.adapters.vlm_client import make_llm_ask

    def fake_post(url, json=None, headers=None, timeout=None):
        time.sleep(min(9.9, timeout))
        raise requests.exceptions.Timeout("服务端排队")

    monkeypatch.setattr("requests.post", fake_post)
    llm_ask = make_llm_ask("http://198.51.100.7:8000/v1", "m", timeout_s=0.2)
    t0 = time.time()
    with pytest.raises(Exception):
        llm_ask("归纳一下")
    assert time.time() - t0 <= 0.9      # 2T=0.4 + 松余量,绝不是旧写法的硬等
    rows = _wait_rows(2)
    mine = [r for r in rows if r[0] == "llm"]
    assert {r[5] for r in mine} == {0, 1}, "没有补发 = 调用点绕过了 hedged_request"
    assert all(r[6] == "timeout" and not r[2] for r in mine)
    s = latency_summary()["llm"]
    assert s["unanswered"] == 1 and s["errors"] == 2


def test_default_timeouts_per_kind():
    """分类型默认超时(2026-08-15 用户定):投票四类 60s、技能归纳 120s。

    同时钉工厂签名默认值——有人把某个工厂改回 600 而忘了别处,这里先红。
    """
    from curation.adapters.vlm_client import (make_endstate_voter,
                                              make_evidence_judge,
                                              make_grounder,
                                              make_intent_comparer,
                                              make_llm_ask,
                                              make_multiview_completion,
                                              make_question_writer,
                                              make_vlm_completion)
    from curation.dataset_level.caption import make_vlm_captioner

    assert DEFAULT_TIMEOUTS_S == {"probe": 60.0, "endstate": 60.0,
                                  "arbitration": 60.0, "caption": 60.0,
                                  "llm": 120.0}

    def _default(fn):
        return inspect.signature(fn).parameters["timeout_s"].default

    assert _default(make_vlm_completion) == 60.0
    assert _default(make_multiview_completion) == 60.0
    assert _default(make_endstate_voter) == 60.0
    for factory in (make_question_writer, make_grounder,
                    make_evidence_judge, make_intent_comparer):
        assert _default(factory) == 60.0, factory.__name__
    assert _default(make_llm_ask) == 120.0
    # captioner 默认 None = 构造期取 DEFAULT_TIMEOUTS_S["caption"](与其余同源)
    assert _default(make_vlm_captioner) is None


def test_timeouts_site_config_override(monkeypatch):
    """站点配置 checks.task_success.vlm.timeouts_s 能覆盖,且走既有配置深合并路。"""
    vcfg = {"timeouts_s": {"probe": 30, "llm": 300}}
    assert timeout_for("probe", vcfg) == 30.0
    assert timeout_for("llm", vcfg) == 300.0
    assert timeout_for("endstate", vcfg) == 60.0      # 没覆盖的走默认
    assert timeout_for("caption", None) == 60.0
    # vlm_completion_from_config 必须把覆盖值传进打分工厂
    import curation.adapters.vlm_client as vc
    captured = {}

    def fake_factory(endpoint, model, **kw):
        captured.update(kw)
        return lambda *a, **k: []

    monkeypatch.setattr(vc, "make_multiview_completion", fake_factory)
    cfg = {"checks": {"task_success": {"vlm": {
        "endpoint": "http://198.51.100.7:8000/v1", "model": "m",
        "timeouts_s": {"probe": 33}}}}}
    vc.vlm_completion_from_config(cfg)
    assert captured["timeout_s"] == 33.0


def test_default_yaml_timeouts_match_code_defaults():
    """default.yaml 里的 timeouts_s 与代码默认值必须一致 —— 两处各改一处会让
    "出厂默认"变成薛定谔的值(配置在场用 A、配置缺段用 B)。"""
    import os

    import yaml

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "pipeline", "default.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    ts = cfg["checks"]["task_success"]["vlm"]["timeouts_s"]
    assert {k: float(v) for k, v in ts.items()} == DEFAULT_TIMEOUTS_S


def test_latency_csv_roundtrip_with_hedge_columns(tmp_path):
    """新七列 CSV 读回后,对冲口径与内存明细一致(事后复算不丢补发信息);
    UI 独立实现与管道实现对拍(UI 不 import 管道的红线下,两份算法必须同数)。"""
    import csv

    from curation.adapters.vlm_client import (LATENCY_CSV_HEADER,
                                              read_latency_csv)
    from curation.ui.manifest import _recompute_latency

    latency_reset()
    latency_record("probe", 1.0, False, 10.0, call_id="a", attempt=0,
                   fail_kind="timeout")
    latency_record("probe", 0.2, True, 10.6, call_id="a", attempt=1)
    latency_record("probe", 0.3, True, 12.0, call_id="b")
    p = tmp_path / "vlm_latency.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(LATENCY_CSV_HEADER)
        for tag, dt, ok, st, cid, att, fk in latency_rows():
            w.writerow([tag, dt, int(ok), "" if st is None else st,
                        cid or "", att, fk or ""])
    mem = latency_summary()["probe"]
    assert mem["hedged"] == 1 and mem["attempts"] == 3
    back = latency_summary(read_latency_csv(str(p)))["probe"]
    assert back == mem
    assert _recompute_latency(str(p))["probe"] == mem


def test_summary_hedge_stats_from_rows():
    """汇总的对冲口径:hedged/retried/unanswered 按 call_id 分组算,attempts 数
    每一次真实发起。这是界面「N 次超时后补发,均已拿到结果」那句话的数据源。"""
    latency_reset()
    # a: 首发超时,补发救回(hedged)
    latency_record("probe", 1.0, False, 10.0, call_id="a", attempt=0, fail_kind="timeout")
    latency_record("probe", 0.2, True, 10.5, call_id="a", attempt=1)
    # b: 首发 5xx,串行重发救回(retried)
    latency_record("probe", 0.1, False, 12.0, call_id="b", attempt=0, fail_kind="http_error")
    latency_record("probe", 0.2, True, 12.1, call_id="b", attempt=1)
    # c: 两发都超时(unanswered,且全超时 → 界面说「没等到回应」)
    latency_record("probe", 1.0, False, 14.0, call_id="c", attempt=0, fail_kind="timeout")
    latency_record("probe", 0.5, False, 14.5, call_id="c", attempt=1, fail_kind="timeout")
    # d: 普通一发成功
    latency_record("probe", 0.3, True, 16.0, call_id="d")
    s = latency_summary()["probe"]
    assert s["attempts"] == 7 and s["n"] == 3 and s["errors"] == 4
    assert s["hedged"] == 2 and s["retried"] == 1
    assert s["unanswered"] == 1 and s["unanswered_timeout"] == 1
    # 老四列数据(无 call_id):每行自成一组 → unanswered 退化为 errors,数字不变
    latency_reset()
    latency_record("caption", 1.0, False, 20.0)
    latency_record("caption", 0.5, True, 21.0)
    s_old = latency_summary()["caption"]
    assert s_old["attempts"] == 2 and s_old["unanswered"] == 1
    assert s_old["hedged"] == 0 and s_old["retried"] == 0
    assert s_old["unanswered_timeout"] == 0     # 原因未知,不硬说是超时
