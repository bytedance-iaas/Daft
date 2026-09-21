"""同步证据图的文字必须是纯 ASCII(2026-07-22)。

背景:sync_plots.py 从 2026-07-15 起就写着"标签全英文,不赌 CJK 字体",但静态标签
英文化之后,标题里又拼进了检查产出的**中文 reason**。容器镜像里一个 CJK 字体都没有
(matplotlib 只见 19 个字体,零中文),于是交付给客户的证据图标题变成:

    ep000004  [abstain]  □□ 0.60s □□□□□□□□(corr0 0.33≈peak 0.44),□□□□ → □□□

而且**只 warn 不报错**,图照常生成、照常交付。
教训:"全英文"这条纪律靠人记是记不住的——它管得住字面量,管不住拼进去的数据。
故本文件把它变成断言:凡进图的文字,一律 ASCII。
"""
from __future__ import annotations

import pytest

from curation.core.checks.video_action_sync import global_lag
from curation.export.sync_plots import _CODE_EN, plot_title


def test_chinese_reason_never_reaches_the_plot():
    """就算 detail 里塞满中文,标题也必须是纯 ASCII——这是当初出事的那条路径。"""
    det = {"code": "ambiguous_peak", "lag_s": 0.6, "corr_peak": 0.44, "n_samples": 95,
           "reason": "滞后 0.60s 超容差但峰不突出(corr0 0.33≈peak 0.44),证据含糊 → 不可判"}
    title = plot_title("ep000004", "abstain", det)
    assert title.isascii(), f"标题混进非 ASCII:{title!r}"
    # 信息没丢,只是换了语言:判读结论 + 关键数字都还在
    assert _CODE_EN["ambiguous_peak"] in title
    assert "lag=0.60s" in title and "peak=0.44" in title and "n=95" in title


def test_unknown_code_degrades_to_ascii_not_tofu():
    """将来新增分支忘了补映射表:显示 code 本身即可,但绝不能变成方框。"""
    title = plot_title("ep1", "abstain", {"code": "some_new_branch"})
    assert title.isascii() and "some_new_branch" in title


def test_missing_or_chinese_code_still_ascii():
    """code 缺失、或某天有人手滑塞了中文 code,兜底闸门都要挡住。"""
    assert plot_title("ep1", "pass", {}).isascii()
    assert plot_title("ep1", "pass", {"code": "峰不突出"}).isascii()


@pytest.mark.parametrize("code", sorted(_CODE_EN))
def test_all_english_labels_are_ascii(code):
    """映射表本身不能混进中文(否则兜底闸会把它剥成半截)。"""
    assert _CODE_EN[code].isascii()


def test_every_check_branch_emits_a_code():
    """检查的**每条**返回路径都要带 code,否则图上就没得可显示。

    直接驱动真实函数走完各分支,而不是读源码数 return——后者改代码就失效。
    """
    import numpy as np

    seen = set()

    def run(flow, ft, speed, st, **kw):
        r = global_lag(flow, ft, speed, st, **kw)
        assert "code" in r.detail, f"该分支没有 code:{r.detail.get('reason')!r}"
        seen.add(r.detail["code"])
        return r

    rng = np.random.default_rng(0)
    # ① 信号过短
    run([1, 2, 3], [0, 1, 2], [1, 2, 3], [0, 1, 2])
    # ② 静止段无信号
    t = np.arange(100) * 0.1
    run(np.ones(100), t, np.ones(100), t)
    # ③ 序列过短(n<60):有信号但样本不足
    t2 = np.arange(30) * 0.1
    run(rng.normal(size=30), t2, rng.normal(size=30), t2)
    # ④ 对齐(pass):两条同源信号
    t3 = np.arange(200) * 0.1
    base = np.sin(t3 * 2.0) + rng.normal(scale=0.05, size=200)
    run(base, t3, base.copy(), t3)

    assert {"short_signal", "no_motion", "short_sequence", "aligned"} <= seen
    assert seen <= set(_CODE_EN), f"出现了映射表里没有的 code:{seen - set(_CODE_EN)}"
