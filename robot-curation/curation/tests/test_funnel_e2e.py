"""P3.6 验收:混合注入集端到端——每种病灶被对应模块抓住;关掉 M4c 后 VLM 零调用;
VLM 只跑幸存者(计数验证);漏斗统计如实。"""
from __future__ import annotations

import copy
import json
import os

import numpy as np
import pytest

from curation.pipeline.config import load_config
from curation.pipeline.verdict import episode_verdict
from curation.registry.registry import EmbodimentRegistry
from curation.tests import corrupt

PUSHT = "/data03/hao/data/pusht"

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(PUSHT, "meta", "info.json")),
    reason="pusht 数据未下载")


def _cfg(**overrides) -> dict:
    cfg = load_config()
    # pusht 定制:相关性天花板 ~0.68(spike2),corr_min 放低;合成场景 spike 拐点低
    cfg["checks"]["video_action_sync"]["params"]["corr_min"] = 0.2
    cfg["checks"]["task_success"]["params"]["n_probe"] = 8
    for k, v in overrides.items():
        cfg["checks"][k].update(v)
    return cfg


@pytest.fixture(scope="module")
def mixed_rows():
    """10 条 pusht:7 干净 + 3 注入(超限/丢帧/视频错位),带真值。"""
    from curation.ingest.lerobot_reader import read_lerobot_rows

    clean = read_lerobot_rows(PUSHT, max_episodes=11, embodiment_id="pusht")[1:]
    rows = [copy.deepcopy(r) for r in clean]
    injected = {}
    rows[1], _ = corrupt.exceed_limits(rows[1], joint=0, frame=50, limit_value=512.0)
    injected[rows[1]["episode_id"]] = "kinematic_limits"
    rows[2], _ = corrupt.drop_frames(rows[2], start=40, n=12)
    injected[rows[2]["episode_id"]] = "timestamp_check"
    rows[3], _ = corrupt.shift_video(rows[3], shift_frames=10)
    injected[rows[3]["episode_id"]] = "video_action_sync"
    return rows, injected


def _run(rows, cfg, vlm=None):
    """→ (verdicts, stats, syncs);syncs = {episode_id: 同步检查 detail}(逐相机结构)。"""
    from curation.ingest.lerobot_reader import rows_to_daft
    from curation.pipeline.funnel import run_funnel

    df, stats = run_funnel(rows_to_daft(rows), cfg, EmbodimentRegistry(), vlm_completion=vlm)
    cols = ["episode_id", "verdict"]
    if "check_video_action_sync" in df.column_names:
        cols.append("check_video_action_sync")
    out = df.select(*cols).to_pydict()
    verdicts = {e: json.loads(v) for e, v in zip(out["episode_id"], out["verdict"])}
    syncs = {e: json.loads((c or {}).get("detail") or "{}")
             for e, c in zip(out["episode_id"],
                             out.get("check_video_action_sync", [None] * len(verdicts)))}
    return verdicts, stats, syncs


class SpyVlm:
    def __init__(self):
        self.calls = 0

    def __call__(self, reference, shuffled, instruction):
        self.calls += 1                                # 批式:每 episode 一次调用
        return [0.9] * len(shuffled)


def test_each_disease_caught_by_its_module(mixed_rows):
    """三种病各归各的模块。

    ⚠️ 2026-08-07 逐相机改造后,同步那一条的**期望变了**(不是削弱,是改判定层):
    pusht 是**单相机**数据集,而"单相机永不因同步判废"是用户拍板的红线(孤证不定罪,
    droid ep4 三路读数天差地别就是教训)。所以注入的视频错位现在不该被杀,而该
    **被如实标注**:该路读数 misaligned、进 flagged_cameras、verdict=annotated。
    证据一点没少,只是不再凭一路孤证定罪。"""
    rows, injected = mixed_rows
    vlm = SpyVlm()
    verdicts, stats, syncs = _run(rows, _cfg(), vlm=vlm)

    # 超限/丢帧仍在数值段被硬门 filter 掉(不在输出)
    assert stats["input"] == 10
    assert stats["after_numeric_gates"] == 8, f"数值硬门应杀 2 条,stats={stats}"
    surviving_ids = set(verdicts)
    for ep_id, module in injected.items():
        if module in ("kinematic_limits", "timestamp_check"):
            assert ep_id not in surviving_ids, f"{ep_id}({module})漏网"

    # 同步:单相机 → 不杀,但必须留下证据(测量层照旧读出错位)
    shifted = next(e for e, m in injected.items() if m == "video_action_sync")
    assert shifted in surviving_ids, "单相机数据集不得因同步判废(孤证不定罪)"
    assert stats["survivors_for_vlm"] == 8, f"抽帧段不该再杀人,stats={stats}"
    det = syncs.get(shifted, {})
    assert det.get("n_cameras") == 1
    cam = next(iter(det.get("per_camera") or {}), None)
    assert cam is not None, f"同步读数缺失:{det}"
    reading = det["per_camera"][cam]
    if reading["trusted"]:                      # 读数可信 → 必须被标注出来
        assert reading["code"] == "misaligned", f"注入 10 帧错位没读出来:{reading}"
        assert det["flagged_cameras"] == [cam] and det["verdict"] == "annotated"
    else:                                        # 读数不可信 → 如实说测不准,同样不杀
        assert det["verdict"] in ("undecidable", "annotated")

    # 干净 7 条全部 keep 或至多 1 条误杀(<5% 量级的小样本宽容)
    keeps = [e for e, v in verdicts.items() if v["verdict"] == "keep"]
    assert len(keeps) >= 6, f"干净集误杀过多: {verdicts}"


def test_sync_never_pushes_episodes_into_manual_queue(mixed_rows):
    """红线回归:同步无论读成什么,都不得再产出 passed=None——那会把条目推进
    review.json 的人工裁决队列(用户明确否掉)。"""
    rows, _ = mixed_rows
    verdicts, _stats, syncs = _run(rows, _cfg())
    for e, v in verdicts.items():
        assert "video_action_sync" not in v.get("undecidable", []), f"{e} 进了人工队列"
    for e, det in syncs.items():
        assert det.get("verdict") in ("aligned", "annotated", "undecidable",
                                      "misaligned_all"), f"{e}: {det}"


def test_vlm_only_runs_on_survivors(mixed_rows):
    rows, _ = mixed_rows
    vlm = SpyVlm()
    _, stats, _syncs = _run(rows, _cfg(), vlm=vlm)
    expected = stats["survivors_for_vlm"]              # 批式协议:每幸存 episode 一次调用
    assert vlm.calls == expected, \
        f"VLM 调用 {vlm.calls} != 幸存者数 {stats['survivors_for_vlm']}(漏斗失效)"


def test_disabling_task_success_means_zero_vlm_calls(mixed_rows):
    """P3.6 验收:改配置关掉 M4c 后,VLM 确实一次没被调。"""
    rows, _ = mixed_rows
    vlm = SpyVlm()
    _run(rows, _cfg(task_success={"enable": False}), vlm=vlm)
    assert vlm.calls == 0


def test_soft_gate_affects_verdict_not_filter(mixed_rows):
    """软分不硬杀:低软分体现在 verdict=drop(reason=软分),行仍在输出里可审计。"""
    rows, _ = mixed_rows
    cfg = _cfg()
    cfg["verdict"]["soft_threshold"] = 0.99            # 极端阈值逼出软分 drop
    verdicts, _stats, _syncs = _run(rows[:2], cfg)
    assert any(v["verdict"] == "drop" and "质量分" in v["reason"] for v in verdicts.values())


# ---------- verdict 纯函数单测 ----------

def test_verdict_hard_short_circuit():
    cfg = load_config()
    checks = {"kinematic_limits": {"passed": False, "score": None},
              "visual_quality": {"passed": None, "score": 0.99}}
    v = episode_verdict(checks, cfg)
    assert v["verdict"] == "drop" and "kinematic_limits" in v["reason"]


def test_verdict_undecidable_not_counted_as_fail():
    cfg = load_config()
    checks = {"video_action_sync": {"passed": None, "score": None},
              "visual_quality": {"passed": None, "score": 0.9},
              "motion_quality": {"passed": None, "score": 0.8}}
    v = episode_verdict(checks, cfg)
    assert v["verdict"] == "keep"
    assert "video_action_sync" in v["undecidable"]


def test_verdict_weighted_soft():
    cfg = load_config()
    cfg["checks"]["visual_quality"]["weight"] = 3.0
    cfg["checks"]["motion_quality"]["weight"] = 1.0
    checks = {"visual_quality": {"passed": None, "score": 0.2},
              "motion_quality": {"passed": None, "score": 1.0}}
    v = episode_verdict(checks, cfg)
    assert v["soft_score"] == pytest.approx(0.4)       # (3×0.2+1×1)/4
    assert v["verdict"] == "drop"