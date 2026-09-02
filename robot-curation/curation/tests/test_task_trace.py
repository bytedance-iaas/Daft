"""判定痕迹落盘(details/task_details.json)测试。

背景:成败判定的中间读数一直算完即弃 —— 判"通过"的条目在交付里只剩一个 "pass",
弃权的把两个数字嵌在中文理由里。P5.1 校准要离线扫规则(改一条阈值会翻多少条),
重烧 VLM 既贵又不可复现(同配置两遍结论就不同),所以每次质检都得把草稿纸交上来。

本文件钉三件事:
① 痕迹全:pass/fail/abstain 三态都有记录,打分臂/复核臂/规则轨迹齐;
② 不编:拿不到的读数**不出现**,不是补 0 也不是补空串;
③ **判定行为一个比特都没变** —— 痕迹是纯旁路,拔掉它(把新增的键删掉)剩下的
   detail 必须与加痕迹之前逐字一致(下面用冻结的期望字典对照)。
"""
from __future__ import annotations

import json
import os

import numpy as np

from curation.core.checks.task_success import endstate_review, task_success
from curation.export.task_trace import build_task_trace, write_task_details

#: 痕迹功能新加的键。测"行为零变化"时把它们摘掉,剩下的必须是老样子。
TRACE_KEYS = ("rules", "raw", "init_verdict")


def _frames(prog):
    return [np.full((32, 32, 3), int(p * 255), dtype=np.uint8) for p in prog]


def _fake_vlm(reference, shuffled, instruction):
    return [float(f.mean() / 255.0) for f in shuffled]


def _voter(answer):
    return lambda starts, ends, label, desc: answer


def _strip(detail: dict) -> dict:
    return {k: v for k, v in detail.items() if k not in TRACE_KEYS}


# ───────── ① 规则轨迹:每条判据都留得下标识 ─────────

def test_rules_cover_scoring_arm_branches():
    """打分臂逐格:走了哪条判据要有机器可读的标识(reason 是中文句子,扫不了)。"""
    cases = {
        "success_candidate_strong": [0.05, 0.2, 0.5, 0.8, 0.9, 1.0, 1.0, 1.0],
        "fail_candidate_no_progress": [0.0, 0.05, 0.1, 0.1, 0.05, 0.1, 0.1, 0.1],
        "gap_violation_monotonicity": [0.05, 0.2, 0.9, 1.0, 1.0, 0.1, 0.05, 0.05],
        "gray_zone_final": [0.0, 0.1, 0.2, 0.3, 0.35, 0.35, 0.35, 0.35],
    }
    for rule, prog in cases.items():
        r = task_success(_frames(prog), "t", _fake_vlm)
        assert rule in r.detail["rules"], (rule, r.detail.get("rules"))

    # 全平低位 = 失败候选(2026-09-02 起),标记+候选规则两条都留痕
    r = task_success(_frames([0.0] * 8), "t", _fake_vlm)
    assert r.detail["rules"] == ["flat_low_scores", "fail_candidate_no_progress"]
    # 帧数不足 / VLM 抽风:没有读数可留,只留一条"为什么没读数"
    r = task_success(_frames([0.5]), "t", _fake_vlm)
    assert r.detail["rules"] == ["frames_insufficient"] and "scoring" not in r.detail

    def _boom(ref, fs, ins):
        raise RuntimeError("模型抽风")

    r = task_success(_frames([0.1, 0.9] * 4), "t", _boom)
    assert r.detail["rules"] == ["vlm_call_failed"]


def test_rules_cover_review_arm_and_keep_scoring_arm_conclusion():
    """复核臂逐格 + init_verdict:复核推翻初判时,打分臂原本判了什么必须还查得到
    (verdict 是就地改写的,不存一份就永远丢了 —— 校准的第一手输入)。"""
    weak = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.5, 0.5]      # 弱成功候选
    strong = [0.05, 0.2, 0.5, 0.8, 0.9, 1.0, 1.0, 1.0]   # 强成功候选
    fail = [0.0, 0.05, 0.1, 0.1, 0.05, 0.1, 0.1, 0.1]    # 失败候选

    def run(prog, vote):
        fr = _frames(prog)
        return endstate_review(task_success(fr, "t", _fake_vlm), "t",
                               _voter(vote), {"cam": fr})

    r = run(weak, "no")
    assert "weak_success_vetoed_by_review" in r.detail["rules"]
    assert r.detail["init_verdict"] == "success"          # 打分臂原判还在
    assert r.detail["verdict"] == "endstate_failure_suspect"

    assert "review_overruled_by_strong_score" in run(strong, "no").detail["rules"]
    assert "review_confirms_success" in run(strong, "yes").detail["rules"]
    assert "double_signed_kill" in run(fail, "no").detail["rules"]
    assert "arms_conflict_fail_vs_done" in run(fail, "yes").detail["rules"]
    assert "kill_missing_second_signature" in run(fail, "unclear").detail["rules"]
    assert "weak_success_uncorroborated" in run(weak, "unclear").detail["rules"]

    # 投票器不可用 = 基础设施缺席(与"证据缺席"刻意区分,两条标识不许混)
    fr = _frames(fail)
    r = endstate_review(task_success(fr, "t", _fake_vlm), "t", None, {"cam": fr})
    assert r.detail["rules"][-2:] == ["voter_unavailable",
                                      "kill_downgraded_no_second_signature"]
    assert "cam_votes" not in r.detail                    # 没投过票就不许有票


# ───────── ② 不编:拿不到的不出现 ─────────

def test_trace_record_omits_what_it_cannot_get():
    """老交付/降级路径下缺什么就少什么键,绝不补 0/空串充数。"""
    rec = build_task_trace("ep000001", None, {"reason": "帧数不足",
                                              "rules": ["frames_insufficient"]})
    assert rec == {"episode_id": "ep000001", "result": "弃权",
                   "reason": "帧数不足", "rules": ["frames_insufficient"]}
    assert "scoring" not in rec and "review" not in rec and "instruction" not in rec
    # 老 detail(没有 raw)照样装配得出来,取那份四舍五入过的读数
    rec = build_task_trace("ep2", True, {"voc": 0.5, "completion_final": 0.9,
                                         "completions": [0.1, 0.9], "verdict": "success"})
    assert rec["scoring"] == {"voc": 0.5, "completion_final": 0.9,
                              "completions": [0.1, 0.9]}
    assert "probe_frames" not in rec["scoring"]


def test_trace_keeps_full_precision_and_full_instruction():
    """两处会丢信息的地方各钉一遍:
    ① 展示用读数四舍五入到 3~4 位,校准按阈值重扫会翻边 → 原始精度另存一份;
    ② detail 里的标注为了界面截到 80 字,痕迹要全文(校准得跟原始标注对得上)。"""
    def third(ref, shuffled, ins):
        return [float(f.mean() / 255.0) / 3.0 for f in shuffled]

    r = task_success(_frames([0.3, 0.6, 0.9, 1.0, 1.0, 1.0, 1.0, 1.0]), "t", third)
    assert r.detail["completions"] != r.detail["raw"]["completions"]
    assert any(len(repr(v)) > 5 for v in r.detail["raw"]["completions"])
    long_ins = "把" + "很长的任务描述" * 20
    rec = build_task_trace("ep3", True, dict(r.detail, task_desc=long_ins[:80]),
                           instruction=long_ins, instruction_source="原始标注")
    assert rec["instruction"] == long_ins and len(rec["instruction"]) > 80
    assert rec["instruction_source"] == "原始标注"
    assert rec["scoring"]["completions"] == r.detail["raw"]["completions"]


# ───────── ③ 行为零变化:痕迹是纯旁路 ─────────

def test_decision_is_bit_for_bit_unchanged_without_trace_keys():
    """把新增的三个键摘掉,detail 必须与加痕迹之前**逐字一致**。

    期望值不是手算的,是把加痕迹**之前**那版模块(git HEAD)与现版并排跑出来的:
    12 条曲线 × 3 种 VLM(含抽风)× 6 种复核票 + 多机位/无帧共 240 组场景,
    摘掉痕迹键后 0 组有差异,下面冻结的就是其中一组的原样输出。
    (⚠️ 做这种对照时,带 RNG 的假 VLM 必须给两边各造一个同种子实例,
      否则先跑的那边把随机数消耗掉,会看到 66 组"假差异"。)
    """
    fr = _frames([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.5, 0.5])
    res = task_success(fr, "t", _fake_vlm)
    assert res.passed is True
    assert _strip(res.detail) == {
        "voc": 0.9759, "completion_final": 0.498, "completion_peak": 0.498,
        "completion_gap": 0.0,
        "completions": [0.0, 0.098, 0.2, 0.298, 0.4, 0.498, 0.498, 0.498],
        "probe_frames": [0, 1, 2, 3, 4, 5, 6, 7],
        "strong_score": False, "verdict": "success"}

    out = endstate_review(res, "t", _voter("no"), {"cam": fr})
    assert out.passed is None
    assert _strip(out.detail) == {
        "voc": 0.9759, "completion_final": 0.498, "completion_peak": 0.498,
        "completion_gap": 0.0,
        "completions": [0.0, 0.098, 0.2, 0.298, 0.4, 0.498, 0.498, 0.498],
        "probe_frames": [0, 1, 2, 3, 4, 5, 6, 7],
        "strong_score": False, "verdict": "endstate_failure_suspect",
        "cam_votes": {"cam": "no"}, "review": "no",
        "reason": "进度证据较弱,且各机位复核一致不认可:转人工,不直接判废"}


def test_trace_never_touches_the_verdict_matrix():
    """决定表逐格重跑一遍,只看判定输出(痕迹在不在都该是这个结果)。"""
    weak = _frames([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.5, 0.5])
    strong = _frames([0.05, 0.2, 0.5, 0.8, 0.9, 1.0, 1.0, 1.0])
    fail = _frames([0.0, 0.05, 0.1, 0.1, 0.05, 0.1, 0.1, 0.1])
    table = [
        (strong, "yes", True, "success"), (strong, "no", True, "success"),
        (weak, "no", None, "endstate_failure_suspect"),
        (weak, "unclear", None, "endstate_unconfirmed"),
        (fail, "no", False, "failure"), (fail, "yes", None, "review_conflict"),
        (fail, "unclear", None, "failure"),
    ]
    for fr, vote, want_passed, want_verdict in table:
        r = endstate_review(task_success(fr, "t", _fake_vlm), "t",
                            _voter(vote), {"cam": fr})
        assert (r.passed, r.detail["verdict"]) == (want_passed, want_verdict), vote


# ───────── 落盘 ─────────

def test_write_task_details_file_shape(tmp_path):
    """pass 条目也必须有痕迹(此前它一个数字都不留);口径写在文件里。"""
    rows = [build_task_trace("ep000000", True,
                             {"verdict": "success", "rules": ["success_candidate_strong"],
                              "raw": {"voc": 0.87, "completion_final": 0.9},
                              "cam_votes": {"ext1": "yes", "wrist": "unclear"},
                              "review": "yes", "cams": ["ext1", "wrist"]},
                             instruction="pick up the cup",
                             instruction_source="原始标注"),
            build_task_trace("ep000009", False, {"verdict": "failure", "rules": []})]
    path = write_task_details(str(tmp_path / "details"), rows, dataset="droid")
    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc["数据集"] == "droid" and "只记录" in doc["口径"]
    assert set(doc["episodes"]) == {"ep000000", "ep000009"}
    ok = doc["episodes"]["ep000000"]
    assert ok["result"] == "pass" and ok["instruction"] == "pick up the cup"
    assert ok["scoring"]["voc"] == 0.87
    assert ok["review"]["cam_votes"]["wrist"] == "unclear"
    assert ok["review"]["tally"] == "yes" and ok["review"]["cameras"] == ["ext1", "wrist"]
    assert doc["episodes"]["ep000009"]["result"] == "拒绝"


def test_no_file_when_nothing_was_judged(tmp_path):
    """--lite / 该检查关闭 / 全员被前置硬门拒掉 → 不产文件(空文件会被误读成
    "判定跑了但什么都没测出来")。"""
    det = str(tmp_path / "details")
    assert write_task_details(det, [], dataset="x") is None
    assert not os.path.exists(os.path.join(det, "task_details.json"))
