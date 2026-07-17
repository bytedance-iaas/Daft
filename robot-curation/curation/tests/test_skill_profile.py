"""技能画像的降级路径测试:标签分组判定 + 单级画像统计。

(聚类/命名/embedding 模块 2026-07-16 已删——caption 路线 8 次迭代定稿后成为
唯一主路径,KMeans 路线从未接线;历史见 git log 7396e47 之前。)
"""
from __future__ import annotations

import numpy as np

from curation.dataset_level.profile import instruction_grouping_available, skill_profile


def _rows(instrs, t=50, fps=10.0):
    return [{"episode_id": f"ep{i}", "instruction": s,
             "action": np.zeros((t, 2), dtype=np.float32), "fps": fps}
            for i, s in enumerate(instrs)]


def test_instruction_grouping_preference():
    assert instruction_grouping_available(_rows(["a", "b", "c", ""]))       # 3/4 有标签
    assert not instruction_grouping_available(_rows(["a", "", "", ""]))     # 1/4


def test_profile_counts_and_undersampled():
    rows = _rows(["pick"] * 12 + ["place"] * 7 + ["rare"] * 1)
    prof = skill_profile(rows, [r["instruction"] for r in rows], undersampled_pct=10.0)
    assert prof["n_skills"] == 3
    assert prof["skills"]["pick"]["count"] == 12
    assert prof["skills"]["pick"]["pct"] == 60.0
    assert prof["skills"]["pick"]["avg_len_s"] == 5.0
    assert prof["undersampled"] == ["rare"]
