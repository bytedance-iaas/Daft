"""进度显示(_Progress)单测:计数/节流/并发安全/ETA 格式。

背景:抽帧与 VLM 两个慢阶段此前完全静默,DROID 200 条带 VLM 跑一小时零输出,
无法区分"在跑"和"卡死"。进度挂在 daft UDF 里逐条 tick,故必须线程安全。
"""
from __future__ import annotations

import threading

import pytest

from curation.pipeline.funnel import (_fmt_dur, _PROGRESS, _progress_init,
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
