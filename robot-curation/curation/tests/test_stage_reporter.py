"""进度显示(_Progress)单测:计数/节流/并发安全/ETA 格式。

背景:抽帧与 VLM 两个慢阶段此前完全静默,DROID 200 条带 VLM 跑一小时零输出,
无法区分"在跑"和"卡死"。进度挂在 daft UDF 里逐条 tick,故必须线程安全。
"""
from __future__ import annotations

import threading

import pytest

from curation.pipeline.progress import (_fmt_dur, _PROGRESS, _progress_init,
                                        _progress_tick)


def test_counts_every_episode(capsys):
    """小数据集(total<20 → step=1)每条都报,末条必报。"""
    k = _progress_init("t1", 5, "测试阶段")
    for _ in range(5):
        _progress_tick(k)
    out = capsys.readouterr().out
    assert _PROGRESS[k]["n"] == 5
    assert "1/5" in out and "5/5" in out
    assert "测试阶段" in out
    assert "(100%)" in out                      # 末条百分比


def test_throttles_on_large_total(capsys):
    """大数据集不能每条刷屏:step=total//20,打印次数应远小于总数。"""
    k = _progress_init("t2", 400, "大批", min_interval_s=1e9)  # 关时间触发,只看条数
    for _ in range(400):
        _progress_tick(k)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert _PROGRESS[k]["n"] == 400
    assert len(lines) <= 25, f"打印 {len(lines)} 行,过多(应约 20)"
    assert any("400/400" in ln for ln in lines)   # 末条仍要报


def test_thread_safe_counting(capsys):
    """daft 可能并行执行行 → 并发 tick 计数不能丢。"""
    k = _progress_init("t3", 200, "并发", min_interval_s=1e9)

    def worker():
        for _ in range(50):
            _progress_tick(k)

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    capsys.readouterr()
    assert _PROGRESS[k]["n"] == 200, f"并发下计数丢失: {_PROGRESS[k]['n']}"


def test_zero_total_does_not_crash(capsys):
    """total=0(该阶段没有幸存者)不应除零。"""
    k = _progress_init("t4", 0, "空阶段")
    _progress_tick(k)
    assert "?" in capsys.readouterr().out       # 未知总数用 ? 占位


@pytest.mark.parametrize("sec,want", [(0, "0s"), (45, "45s"), (90, "1.5min"),
                                      (3600, "1.0h"), (-5, "0s")])
def test_fmt_dur(sec, want):
    assert _fmt_dur(sec) == want


def test_udf_closure_is_serializable():
    """★ 回归:daft 用 cloudpickle 序列化 UDF(为分发到 worker),而 threading.Lock
    不可 pickle。首版把锁放进对象让闭包捕获 → 真实运行直接崩
    "cannot pickle '_thread.lock' object"(单测抓不到,只有跑真数据才暴露)。
    现在 UDF 只捕获字符串 key,锁在执行侧惰性建。本测试用 daft 自己的序列化路径锁死该约束。
    (同一约束也是 RayRunner 的已知前置项:UDF 要能分发到 worker。)"""
    from daft.pickle import dumps as daft_dumps      # daft 内部就用它检查 UDF

    k = _progress_init("ser", 3, "序列化")

    def fake_udf(x):            # 模拟 UDF:只捕获 key(字符串)
        _progress_tick(k)
        return x

    daft_dumps(fake_udf)        # 不抛异常即通过


def test_lock_would_break_serialization():
    """反向验证:锁若被闭包捕获,daft 序列化必失败——证明上面那条测试确实有效。"""
    import threading

    import pytest as _pytest
    from daft.pickle import dumps as daft_dumps

    lock = threading.Lock()

    def bad_udf(x):             # 反例:直接捕获锁
        with lock:
            return x

    with _pytest.raises(Exception):
        daft_dumps(bad_udf)


# ───────── 静默期(2026-07-22:快阶段别刷屏)─────────

def test_quiet_period_suppresses_intermediate_but_not_completion(capsys):
    """又快又多的阶段:头几秒不打中间行,但**完成行必须打**。

    这是数值检查的场景——10 条 episode 毫秒级跑完,不设静默就瞬间刷 10 行噪音;
    但完全不吭声又会让人以为这段没跑。故设计成"只留一行完成汇总"。
    """
    k = _progress_init("q1", 10, "数值检查", quiet_before_s=60.0)   # 静默期远长于本测试
    for _ in range(10):
        _progress_tick(k)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1, f"静默期内应只剩完成行,实得 {len(lines)} 行:{lines}"
    assert "10/10" in lines[0] and "数值检查" in lines[0]


def test_quiet_period_zero_keeps_old_behavior(capsys):
    """默认(quiet_before_s=0)行为不变——不能因为加了参数就改了既有阶段的表现。"""
    k = _progress_init("q2", 5, "抽帧")
    for _ in range(5):
        _progress_tick(k)
    # 2026-08-30 起开跑先亮 0/N 行(首条完成前进度条也得在)⇒ 5 tick + 1 初始行
    out = capsys.readouterr().out
    assert out.startswith("[curation] 抽帧 0/5")
    assert len(out.strip().splitlines()) == 6


def test_quiet_period_still_reports_when_stage_is_slow(capsys):
    """慢阶段不能被静默期吞掉:超过静默期后中间行照常出。

    否则十万条的数值检查会长时间零输出,又回到"分不清在跑还是卡死"的老问题。
    """
    k = _progress_init("q3", 100, "数值检查", quiet_before_s=0.05)
    _PROGRESS[k]["t0"] -= 1.0          # 假装这阶段已经跑了 1 秒 → 静默期已过
    for _ in range(100):
        _progress_tick(k)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) > 1, "静默期已过却只有完成行 → 慢阶段没进度可看"


# ───────── 阶段式(不可数的 LLM 步骤)─────────

def test_phase_step_reports_position_and_action_without_fake_percent(capsys):
    """阶段式只报'第几步/在做什么',**绝不编百分比**。

    这些步骤各是一次 LLM 大调用,耗时不可预测。假进度条比没有更糟:卡住时
    用户还以为在动。本测试钉住"不出现 %"这条。
    """
    from curation.pipeline.progress import phase_step

    phase_step("技能画像", 2, 5, "归纳技能体系(LLM)…")
    out = capsys.readouterr().out
    assert "技能画像 2/5 归纳技能体系(LLM)…" in out
    assert "%" not in out


def test_phase_step_shows_elapsed_when_given_t0(capsys):
    """给了起点就报累计用时——这是不可预测步骤里唯一能诚实给出的时间信息。"""
    import time

    from curation.pipeline.progress import phase_step

    phase_step("技能画像", 5, 5, "汇总", time.time() - 90)
    assert "已用 1.5min" in capsys.readouterr().out


# ───────── caption 逐条回调 ─────────

def test_caption_progress_counts_failures_and_cache_hits():
    """on_progress 必须**每条都调**,含失败条与命中缓存的条。

    否则有失败时进度永远到不了 100%,看着像卡死——而 caption 单条失败是设计内的
    (空串=未获 caption,不崩批)。
    """
    from curation.dataset_level.caption import caption_episodes

    rows = [
        {"episode_id": "ok_cached"},                       # 命中缓存 → continue 分支
        {"episode_id": "broken"},                          # 无 video 键 → 异常分支
        {"episode_id": "also_cached"},
    ]
    seen = []
    caps = caption_episodes(rows, lambda frames: "x",
                            precomputed={"ok_cached": "c1", "also_cached": "c2"},
                            on_progress=lambda: seen.append(1))
    assert len(seen) == 3, f"应每条各调一次,实得 {len(seen)}"
    assert caps == ["c1", "", "c2"]


# ───────── 剩余时间:近段速率而不是全程平均(2026-08-13)─────────

def _replay(samples, total, t0=0.0):
    """把同一串 (时刻, 已完成数) 采样点喂给新算法,返回最后一次给出的剩余秒数。

    t0 是**阶段起点**,在第一个采样点之前 —— 全程平均那条兜底路径要靠它。
    """
    from curation.pipeline.progress import _eta_seconds

    st = {"t0": t0, "hist": []}
    eta = None
    for t, n in samples:
        eta = _eta_seconds(st, t, n, total)
    return eta


def test_eta_uses_the_recent_rate_not_the_lifetime_average():
    """★ 回归:先快后慢时,剩余估计不能再照着全程平均速率外推。

    用户 2026-08-13 实见「已用 41s | 剩余 ~6s」之后又跑了 40 秒。根因是 VLM 段并发:
    开头一批同时冲进去、完成得又快又密,把全程平均抬得很高;尾巴上只剩零星几条排队,
    真实速率掉下来,于是估计一路偏乐观。同一串采样点喂进来,新算法必须明显更保守。
    """
    # 前 80 条每条 0.5 秒(并发段冲进去的那一批),之后每条 6 秒(尾巴上零星排队)
    samples = [(0.5 * n, n) for n in range(5, 81, 5)]
    samples += [(40.0 + 6.0 * (n - 80), n) for n in (85, 90)]
    new = _replay(samples, 100)
    t_last, n_last = samples[-1]
    old = (t_last - samples[0][0]) / n_last * (100 - n_last)   # 旧公式:全程平均外推
    assert old < 15, "样本没造出'旧公式偏乐观'的场景,这条测试就白测了"
    assert new > 3 * old, f"近段速率没生效:新 {new:.1f}s vs 旧 {old:.1f}s"


def test_eta_falls_back_to_the_lifetime_average_before_the_window_fills():
    """窗口里只有一个采样点(刚开跑)→ 退回全程平均,而不是拒答。

    刚起步时给不出近段速率是常态,那时哪怕粗糙的估计也比一片空白有用。
    """
    assert _replay([(10.0, 10)], 100) == pytest.approx(90.0, rel=0.01)


def test_eta_history_is_bounded():
    """历史有上限:十万条的阶段也不该在进度状态里攒出一条无限长的列表。"""
    from curation.pipeline.progress import _RATE_HISTORY_MAX, _eta_seconds

    st = {"t0": 0.0, "hist": []}
    for n in range(1, 500):
        _eta_seconds(st, float(n), n, 100000)
    assert len(st["hist"]) == _RATE_HISTORY_MAX


def test_no_zero_eta_before_the_stage_is_actually_done(capsys, monkeypatch):
    """★ 没结束就不许显示 `~0s`,一律说「收尾中」。

    `~0s` 是"马上就好"的承诺,而并发段的最后几条常常还要磨很久 —— 承诺完再跑 40 秒
    比不给数字更伤信任。完成行(n==total)则干脆不提剩余:都跑完了还报剩余是废话。
    """
    import time

    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    k = _progress_init("eta-floor", 10, "抽帧")
    for _ in range(10):
        clock["t"] += 0.1                       # 又快又稳 → 剩余算出来远小于 5 秒
        _progress_tick(k)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert "~0s" not in "\n".join(lines)
    assert "收尾中" in lines[-2] and "剩余" not in lines[-2]     # 9/10:说收尾中
    assert "10/10" in lines[-1]
    assert "收尾中" not in lines[-1] and "剩余" not in lines[-1]  # 完成行:什么都不说


def test_eta_is_still_reported_when_there_is_real_time_left(capsys, monkeypatch):
    """反向:确实还要跑很久时,剩余照报 —— 别把「收尾中」变成万能挡箭牌。"""
    import time

    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    k = _progress_init("eta-real", 100, "VLM 任务成败判定")
    for _ in range(10):
        clock["t"] += 6.0
        _progress_tick(k)
    last = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()][-1]
    assert "10/100" in last and "剩余 ~" in last and "收尾中" not in last


def test_eta_suppressed_during_warmup(monkeypatch, capsys):
    """复盘 ⑧:完成量不足总数 10%(下限 3)时不报「剩余」——并发段头几条的
    全程平均速率严重失真(实测 1/49 报 ~57min,真实尾巴 4 分钟)。"""
    from curation.pipeline import progress as pg
    t = [1000.0]
    monkeypatch.setattr(pg.time, "time", lambda: t[0]) if hasattr(pg, "time") else None
    key = pg._progress_init("warmup-test", 49, "VLM 任务成败判定",
                            min_interval_s=0.0)
    st = pg._PROGRESS[key]
    st["step"] = 1                          # 每条都打,便于断言
    for n in range(1, 5):                   # 1..4 < ceil(49/10)=5 → 预热期
        pg._progress_tick(key)
    out = capsys.readouterr().out
    # 4 tick + 开跑的 0/N 初始行(2026-08-30 亮条)= 5 行,预热期仍不报剩余
    assert out.count("已用") == 5 and "剩余" not in out and "收尾中" not in out
    assert "0/49" in out
    # 过了预热线(第 5 条起)就该报剩余(速率可算的前提下)
    import time as _time
    st["t0"] = _time.time() - 60            # 制造已用时长,速率>0
    pg._progress_tick(key)
    out2 = capsys.readouterr().out
    assert "5/49" in out2 and ("剩余 ~" in out2 or "收尾中" in out2)


def test_eta_warmup_floor_for_small_totals(capsys):
    """总数小(10 条)时预热下限 3 条:1-2 条不报,第 3 条起报。"""
    from curation.pipeline import progress as pg
    import time as _time
    key = pg._progress_init("warmup-small", 10, "小批", min_interval_s=0.0)
    st = pg._PROGRESS[key]; st["step"] = 1
    pg._progress_tick(key); pg._progress_tick(key)
    assert "剩余" not in capsys.readouterr().out
    st["t0"] = _time.time() - 30
    pg._progress_tick(key)
    out = capsys.readouterr().out
    assert "剩余 ~" in out or "收尾中" in out   # 第 3 条起恢复报数(近段速率快时说收尾中)


# ── 心跳(2026-08-30 用户拍板:静默期看着像卡死)────────────────────────────

def test_progress_bar_appears_at_zero(capsys):
    """条目式阶段开跑立刻打 0/N 行(首条完成前进度条也得在);
    设了静默期的快阶段照旧不吭声。"""
    from curation.pipeline import progress as pg
    pg._progress_init("hb-t1", 8, "VLM 任务成败判定")
    out = capsys.readouterr().out
    assert "VLM 任务成败判定 0/8" in out
    pg._progress_init("hb-t2", 8, "数值检查", quiet_before_s=3.0)
    assert "数值检查 0/8" not in capsys.readouterr().out
    pg._PROGRESS.pop("hb-t1", None)
    pg._PROGRESS.pop("hb-t2", None)


def test_heartbeat_scan_reprints_stalled_bars_and_phases(monkeypatch):
    """扫描逻辑(线程只是按节奏调它):条目条停 15s+ 重报(0 条=首批在飞);
    阶段式在一步里停 15s+ 报"仍在这一步";phase_done 收账后闭嘴;完成的条
    不再吭声。状态字典按用例隔离(pod 实测:别家用例遗留的僵尸条会被扫进
    来,报「已用 496684.9h」这种鬼数字)。"""
    import time

    from curation.pipeline import progress as pg
    monkeypatch.setattr(pg, "_PROGRESS", {})
    monkeypatch.setattr(pg, "_PHASE", {})
    now = time.time()
    pg._PROGRESS["hb-s1"] = {"total": 5, "label": "VLM 任务成败判定", "n": 0,
                             "t0": now - 30, "last": 0.0, "min_interval_s": 20,
                             "quiet_before_s": 0.0, "step": 1, "lock": None,
                             "hist": []}
    lines = pg._hb_scan(now)
    assert any("VLM 任务成败判定 0/5" in x and "首批在飞" in x for x in lines)
    assert pg._hb_scan(now + 1) == []          # 刚报过:间隔内不重复
    pg._PROGRESS["hb-s1"]["n"] = 5
    assert pg._hb_scan(now + 100) == []        # 完成:闭嘴(扫描跳过 n>=total)
    pg._PROGRESS.pop("hb-s1", None)
    pg.phase_step("技能画像", 2, 5, "归纳技能体系(LLM)…", now - 40)
    pg._PHASE["技能画像"]["ts"] = now - 20
    lines = pg._hb_scan(now)
    assert any("技能画像 2/5" in x and "仍在这一步" in x for x in lines)
    pg.phase_done("技能画像")
    assert pg._hb_scan(now + 100) == []
