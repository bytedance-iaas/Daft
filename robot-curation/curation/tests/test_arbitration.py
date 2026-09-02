"""取证仲裁链(C严 + 杀需≥2路)单元测试(2026-08-13)。

全部注入假 VLM,零网络请求。钉住规格第四节列出的每一格,尤其两条底线:
① "no 但只有 1 条有效路 → 降级弃权"(实验实锤:仅一路的杀 5 条全是冤杀,
   与 v7.3 杀人双签同源——这条护栏丢了,仲裁链就成了单签处决台);
② "只在 passed is None 时触发"(老算法判过的条目一个字段都不许动——
   仲裁链没有推翻 v7.3 决定表的权力)。

假件的身份约定:帧填充值=相机身份(ext_a=10 / ext_b=20 / wrist=30),judge 靠
imgs[0] 的像素值认出自己在哪路作证——并发投票下不依赖调用顺序,判词可复现。
"""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest

from curation.core.checks.task_success import (
    _arb_line_verdict,
    arbitration_review,
    gripper_event_time,
)
from curation.core.contract import CheckResult

# ───────── 假件 ─────────

VAL_A, VAL_B, VAL_W = 10, 20, 30      # 相机身份 = 帧填充值


def _frames(val, n=21):
    return [np.full((16, 16, 3), val, dtype=np.uint8) for _ in range(n)]


def _cams(n=21):
    """droid 形状的三路:两路外部 + 一路腕部(名字含 wrist 即为腕部线)。"""
    return ({"ext_a": _frames(VAL_A, n), "ext_b": _frames(VAL_B, n),
             "wrist_image": _frames(VAL_W, n)},
            {k: np.linspace(0.0, 10.0, n) for k in ("ext_a", "ext_b", "wrist_image")})


def _res(passed=None, verdict="uncertain"):
    return CheckResult(name="task_success", passed=passed,
                       detail={"verdict": verdict, "reason": "orig-reason",
                               "rules": ["gray_zone_final"]})


def _qw(task_type="persistent"):
    def writer(intent):
        return {"task_type": task_type, "target_location": "sink",
                "target_visual": "metal basin", "object": "cup",
                "verify_question": f"Is the cup in the sink? [{intent}]"}
    return writer


def _grounder(visible=(VAL_A, VAL_B)):
    """按相机身份决定可见性:不在 visible 里的路返回空框(该路跳过,不产票)。"""
    def g(img, target, visual, obj):
        return [(2, 2, 10, 10), (4, 4, 12, 12)] \
            if int(np.asarray(img).max()) in visible else []
    return g


class FakeJudge:
    """按相机身份逐票吐出预设判词;记录 (身份, scene) 供断言取证场景。"""

    def __init__(self, votes_by_cam):
        self.votes = {k: list(v) for k, v in votes_by_cam.items()}
        self.calls = []

    def __call__(self, imgs, *, target, question, scene):
        key = int(np.asarray(imgs[0]).max())
        self.calls.append((key, scene))
        seq = self.votes.get(key)
        if not seq:
            return "unclear"
        return seq.pop(0) if len(seq) > 1 else seq[0]


def _arb(res, judge, *, grounder=None, qw=None, caption="put the cup in the sink",
         annotation="", same_task=None, cams=None, gripper=None, gripper_ts=None,
         **kw):
    cam_frames, cam_ts = cams if cams is not None else _cams()
    return arbitration_review(
        res, caption=caption, annotation=annotation,
        cam_frames=cam_frames, cam_ts=cam_ts,
        gripper=gripper, gripper_ts=gripper_ts,
        question_writer=qw or _qw(), grounder=grounder or _grounder(),
        judge=judge, same_task=same_task, **kw)


# ───────── 底线①:只在 passed is None 时触发 ─────────

def test_decided_entries_returned_untouched():
    """老算法判过的条目逐字段原样返回:仲裁链没有翻案权(不动 v7.3 决定表的
    任何一格)。哪怕注入的判官一律喊 no,也一个字节不许改。"""
    judge = FakeJudge({VAL_A: ["no"] * 3, VAL_B: ["no"] * 3, VAL_W: ["no"] * 3})
    for passed, verdict in ((True, "success"), (False, "failure")):
        r0 = _res(passed, verdict)
        snapshot = copy.deepcopy(r0.detail)
        r = _arb(r0, judge)
        assert r is r0 and r.passed is passed
        assert r.detail == snapshot          # 连 arbitration 留痕键都不许出现
        assert not judge.calls               # 零 VLM 调用:决过的条目不烧钱


# ───────── 共识矩阵 ─────────

def test_unanimous_yes_rescues():
    """全部有效路一致 yes → 救回(救人一签,与 v7.3 同源;三路取证齐全)。"""
    judge = FakeJudge({VAL_A: ["yes"] * 3, VAL_B: ["yes"] * 3, VAL_W: ["yes"] * 3})
    r = _arb(_res(), judge)
    assert r.passed is True
    assert r.detail["verdict"] == "arbitration_success"
    arb = r.detail["arbitration"]
    assert arb["applied"] is True and arb["final"] == "yes"
    assert arb["n_effective"] == 3 and arb["consensus"] == "yes"


def test_unanimous_no_with_two_lines_kills():
    """全票 no 且有效路 ≥2 → 杀(双签成立);verdict 写 arbitration_failure,
    复议轨道靠它归因到任务成败判定。"""
    judge = FakeJudge({VAL_A: ["no"] * 3, VAL_B: ["no"] * 3, VAL_W: ["no"] * 3})
    r = _arb(_res(), judge)
    assert r.passed is False
    assert r.detail["verdict"] == "arbitration_failure"
    assert r.detail["arbitration"]["applied"] is True


def test_no_with_single_line_downgrades_to_abstain():
    """🔴 核心:结论 no 但只有 1 条有效路 → 降级弃权。droid-200 实测仅一路的杀
    5 条全是冤杀 —— 这一格丢了,C严就退化成孤证处决。原 verdict/reason 必须
    原样保留(维持弃权=不覆盖打分层/复核层写的话)。"""
    judge = FakeJudge({VAL_A: ["no"] * 3, VAL_W: ["unclear"] * 3})
    r = _arb(_res(), judge, grounder=_grounder(visible=(VAL_A,)))   # ext_b 无框
    assert r.passed is None
    assert r.detail["verdict"] == "uncertain" and r.detail["reason"] == "orig-reason"
    arb = r.detail["arbitration"]
    assert arb["applied"] is False and arb["final"] == "abstain"
    assert arb["consensus"] == "no" and arb["n_effective"] == 1
    assert arb["kill_downgraded"] is True
    assert "arbitration_kill_needs_two_lines" in r.detail["rules"]


def test_yes_with_single_line_still_rescues():
    """救人一签:yes 不受 ≥2 路门槛限制(门槛只挡杀,对称加严会白丢救回率)。"""
    judge = FakeJudge({VAL_A: ["yes"] * 3, VAL_W: ["unclear"] * 3})
    r = _arb(_res(), judge, grounder=_grounder(visible=(VAL_A,)))
    assert r.passed is True and r.detail["arbitration"]["n_effective"] == 1


def test_split_lines_abstain():
    """strict 共识:有效路结论不一致 → 弃权(不做多数决——实验里 majority 口径
    的错放更多,拍板只落 strict)。"""
    judge = FakeJudge({VAL_A: ["yes"] * 3, VAL_B: ["no"] * 3, VAL_W: ["unclear"] * 3})
    r = _arb(_res(), judge)
    assert r.passed is None
    assert r.detail["arbitration"]["consensus"] == "split"
    assert r.detail["verdict"] == "uncertain"


def test_zero_effective_lines_abstain():
    """一条有效路都没有(全路无框/全 unclear)→ 弃权,留痕说明每路为何缺席。"""
    judge = FakeJudge({VAL_W: ["unclear"] * 3})
    r = _arb(_res(), judge, grounder=_grounder(visible=()))
    assert r.passed is None
    arb = r.detail["arbitration"]
    assert arb["n_effective"] == 0 and arb["consensus"] == "abstain"
    assert arb["lines"]["ext_a"].get("skipped")


def test_line_verdict_plurality():
    """路内三票的多数逻辑:unclear 参与计票(两票 unclear 压一票 yes = 这路大体
    看不清,不算有效证词);三方平票=实验版 Counter 插入序的未定义行为,生产
    改为诚实 unclear;判官调用失败(error)不成为任何方向的证据。"""
    assert _arb_line_verdict(["yes", "yes", "unclear"]) == "yes"
    assert _arb_line_verdict(["yes", "unclear", "unclear"]) == "unclear"
    assert _arb_line_verdict(["yes", "no", "unclear"]) == "unclear"
    assert _arb_line_verdict(["no", "no", "yes"]) == "no"
    assert _arb_line_verdict(["error", "error", "yes"]) == "unclear"


# ───────── 底线②:意图打架护栏 ─────────

def test_intent_conflict_both_agree_adopts():
    """标注与 caption 语义不同 → 双意图各跑一遍,两遍结论相同才自动判。
    (护栏实测价值:去掉它冤杀 17→26。)"""
    judge = FakeJudge({VAL_A: ["yes"] * 9, VAL_B: ["yes"] * 9, VAL_W: ["yes"] * 9})
    r = _arb(_res(), judge, annotation="wipe the counter",
             same_task=lambda a, b: False)
    assert r.passed is True
    arb = r.detail["arbitration"]
    assert arb["intent_conflict"] is True
    assert arb["annotation_run"]["intent"] == "wipe the counter"


def test_intent_conflict_disagree_abstains():
    """双意图结论不同 → 维持弃权:判官对 caption 问题喊 yes、对标注问题喊 no,
    说明两种意图下这条数据成败对立,自动判必错一头。judge 靠 verify_question
    里带的意图原文区分两轮(问题生成器把意图写进问句)。"""
    class SplitJudge:
        def __call__(self, imgs, *, target, question, scene):
            return "yes" if "put the cup in the sink" in question else "no"

    r = _arb(_res(), SplitJudge(), annotation="wipe the counter",
             same_task=lambda a, b: False)
    assert r.passed is None and r.detail["verdict"] == "uncertain"
    assert r.detail["arbitration"]["applied"] is False


def test_intent_same_runs_single_chain():
    """语义比对判 SAME → 单链(措辞不同不算打架,别为同一个任务烧两倍钱)。"""
    calls = []

    def qw(intent):
        calls.append(intent)
        return _qw()(intent)

    judge = FakeJudge({VAL_A: ["yes"] * 3, VAL_B: ["yes"] * 3, VAL_W: ["yes"] * 3})
    r = _arb(_res(), judge, qw=qw, annotation="place the cup into the sink",
             same_task=lambda a, b: True)
    assert r.passed is True
    assert len(calls) == 1                 # 只按 caption 跑了一遍
    assert r.detail["arbitration"]["intent_conflict"] is False


def test_comparer_failure_treated_as_conflict():
    """语义比对器失败/不可用 → 按打架从严(宁可双跑,不省护栏)。"""
    def broken(a, b):
        raise RuntimeError("boom")

    judge = FakeJudge({VAL_A: ["yes"] * 9, VAL_B: ["yes"] * 9, VAL_W: ["yes"] * 9})
    r = _arb(_res(), judge, annotation="wipe the counter", same_task=broken)
    assert r.detail["arbitration"]["intent_conflict"] is True
    assert "annotation_run" in r.detail["arbitration"]


# ───────── 意图缺失 / 依赖缺失:诚实弃权 ─────────

def test_no_caption_skips_chain():
    """拿不到自产 caption → 不跑仲裁链,维持弃权(拿标注顶会退回"标注优先",
    违背规格的意图定义);零 VLM 调用。"""
    judge = FakeJudge({VAL_A: ["yes"] * 3})
    r = _arb(_res(), judge, caption="")
    assert r.passed is None and not judge.calls
    arb = r.detail["arbitration"]
    assert arb["applied"] is False and arb.get("skipped")
    assert "arbitration_skipped_no_caption" in r.detail["rules"]


def test_question_writer_failure_abstains():
    """问题生成失败 = 整条意图链无题可验 → 弃权并留痕,不硬凑检查单。"""
    def broken_qw(intent):
        raise ValueError("bad json")

    judge = FakeJudge({VAL_A: ["yes"] * 3})
    r = _arb(_res(), judge, qw=broken_qw)
    assert r.passed is None
    assert r.detail["arbitration"]["n_effective"] == 0
    assert "问题生成失败" in r.detail["arbitration"]["error"]


# ───────── 取证时刻:transient / persistent ─────────

def test_persistent_uses_final_frame():
    """持久任务验末帧(撤手后仍须成立),外部线场景=exterior_final。"""
    judge = FakeJudge({VAL_A: ["yes"] * 3, VAL_B: ["yes"] * 3, VAL_W: ["yes"] * 3})
    r = _arb(_res(), judge)
    arb = r.detail["arbitration"]
    assert arb["lines"]["ext_a"]["frame"] == 20          # 21 帧的末帧
    assert ("wrist_release" in {s for _, s in judge.calls}
            and "exterior_final" in {s for _, s in judge.calls})


def test_transient_uses_grasp_moment_plus_offset():
    """瞬态任务(抓/举)不验末帧——"松爪即失败"是被明令纠正过的谬误(ep181/35)。
    取证时刻=最大闭爪时刻+1.0s:夹爪 0→1 的最大跳变在 t=5.0,锚点 6.0,
    cam_ts linspace(0,10,21) 上最近帧=下标 12。"""
    judge = FakeJudge({VAL_A: ["yes"] * 3, VAL_B: ["yes"] * 3, VAL_W: ["yes"] * 3})
    ts = np.linspace(0.0, 10.0, 21)
    grip = (ts >= 5.0).astype(float)                     # t=5.0 闭合(droid 约定 1=闭)
    r = _arb(_res(), judge, qw=_qw("transient"), gripper=grip, gripper_ts=ts)
    arb = r.detail["arbitration"]
    assert arb["lines"]["ext_a"]["frame"] == 12
    scenes = {s for _, s in judge.calls}
    assert "exterior_post_grasp" in scenes and "wrist_grasp" in scenes


def test_gripper_event_time_matrix():
    """夹爪事件检测:①闭爪=归一化信号最大正跳变的**后一帧**时刻;②开爪反向;
    ③全程不动 → None(不猜);④多爪(aloha 双臂)取事件幅度最大的那列——
    夹爪列下标来自 registry 的 gripper_dims,这里只管信号本身,不硬编码数据集。"""
    ts = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
    close = np.array([0.0, 0.0, 1.0, 1.0, 1.0])
    assert gripper_event_time(close, ts, closing=True) == 2.0
    assert gripper_event_time(close, ts, closing=False) is None
    assert gripper_event_time(np.ones(5), ts, closing=True) is None
    two = np.stack([np.array([0.0, 1.0, 1.0, 1.0, 1.0]),      # 满幅事件在 t=1
                    np.array([0.0, 0.0, 0.4, 0.4, 1.0])], 1)  # 最大跳变仅 0.6
    assert gripper_event_time(two, ts, closing=True) == 1.0


# ───────── 杀的拒因必须进复议轨道 ─────────

def test_arbitration_kill_reason_enters_appeal_track():
    """被本链杀掉的条目必须能进「被拒复议」区(规格成立前提):拒因经
    episode_verdict → hard_fail_reason 拼装后,is_task_success_reject 要认得出。"""
    from curation.dataset_level.decisions import is_task_success_reject
    from curation.pipeline.config import load_config
    from curation.pipeline.verdict import episode_verdict

    judge = FakeJudge({VAL_A: ["no"] * 3, VAL_B: ["no"] * 3, VAL_W: ["no"] * 3})
    r = _arb(_res(), judge)
    assert r.passed is False
    checks = {"task_success": {"passed": r.passed, "score": None,
                               "detail": json.dumps(r.detail, ensure_ascii=False,
                                                    default=str)}}
    v = episode_verdict(checks, load_config())
    assert v["verdict"] == "drop"
    assert is_task_success_reject(v["reason"]), v["reason"]


# ───────── 留痕装配与配置 ─────────

def test_trace_carries_arbitration_verbatim():
    """task_trace 纯装配:detail.arbitration 原样搬进记录,一个数不重算;
    没触发仲裁的条目不得无中生有出这个键。"""
    from curation.export.task_trace import build_task_trace

    judge = FakeJudge({VAL_A: ["yes"] * 3, VAL_B: ["yes"] * 3, VAL_W: ["yes"] * 3})
    r = _arb(_res(), judge)
    rec = build_task_trace("ep000001", r.passed, r.detail)
    assert rec["arbitration"] == r.detail["arbitration"]
    rec2 = build_task_trace("ep000002", True, {"verdict": "success"})
    assert "arbitration" not in rec2


def test_detail_is_json_serializable():
    """留痕要落 details/task_details.json:整个 detail(含 numpy 下标、票列表)
    必须能直接 json.dumps —— 序列化炸在导出层等于白跑一遍 VLM。"""
    judge = FakeJudge({VAL_A: ["no"] * 3, VAL_B: ["yes"] * 3, VAL_W: ["unclear"] * 3})
    r = _arb(_res(), judge)
    json.dumps(r.detail, ensure_ascii=False)             # 不抛即过


def test_default_config_declares_arbitration():
    """default.yaml 出厂即带 arbitration 段(enable 默认 true,杀门槛 2,strict):
    配置漂移会让"关掉=逐字节退回"的性质失去锚点。"""
    from curation.pipeline.config import load_config

    acfg = load_config()["checks"]["task_success"]["arbitration"]
    assert acfg["enable"] is True
    assert acfg["consensus"] == "strict"
    assert acfg["kill_min_lines"] == 2
    assert acfg["n_votes"] == 3


def test_builder_disabled_returns_none():
    """enable:false → build_arbitration_deps 返回 None,管线一行仲裁代码都不执行
    (逐字节等价的实现方式);非 strict 共识配置要炸,不许静默换口径。"""
    from curation.pipeline.config import load_config
    from curation.pipeline.funnel import build_arbitration_deps

    cfg = load_config()
    cfg["checks"]["task_success"]["arbitration"]["enable"] = False
    assert build_arbitration_deps(cfg) is None
    cfg["checks"]["task_success"]["arbitration"].update(enable=True,
                                                        consensus="majority")
    with pytest.raises(ValueError):
        build_arbitration_deps(cfg)


# ───────── 解析器(adapter 纯函数,不发网络) ─────────

def test_parse_arbitration_boxes_coordinate_autoscale():
    """定位回答的坐标制自适应:模型不总按提示词的千分制答——0-1 归一化也要认;
    NOT VISIBLE 行与 <3px 噪声框丢弃。"""
    from curation.adapters.vlm_client import parse_arbitration_boxes

    txt = ("TARGET: <bbox>100 200 500 600</bbox>\n"
           "OBJECT: NOT VISIBLE")
    assert parse_arbitration_boxes(txt, 1000, 1000) == [(100.0, 200.0, 500.0, 600.0)]
    txt2 = "TARGET: 0.1 0.2 0.5 0.6\nOBJECT: 0.5 0.5 0.500 0.500"
    assert parse_arbitration_boxes(txt2, 100, 100) == [(10.0, 20.0, 50.0, 60.0)]


def test_parse_evidence_verdict():
    """判官判词解析:找 VERDICT 行;没按协议答(缺行)= unclear,不硬猜。"""
    from curation.adapters.vlm_client import parse_evidence_verdict

    assert parse_evidence_verdict("blah...\nVERDICT: YES") == "yes"
    assert parse_evidence_verdict("desc\nverdict: unclear") == "unclear"
    assert parse_evidence_verdict("I think it worked") == "unclear"


# ═════ 漏斗接线(真数据 + 假 VLM;pusht 未下载则跳过)═════════════════════════

import os as _os  # noqa: E402

# H200 开发机与 pod 的数据落点不同,哪个在就用哪个(都不在才跳过)
PUSHT = next((p for p in ("/data03/hao/data/pusht", "/mnt/tos/datasets/pusht")
              if _os.path.exists(_os.path.join(p, "meta", "info.json"))),
             "/data03/hao/data/pusht")

_pusht_missing = not _os.path.exists(_os.path.join(PUSHT, "meta", "info.json"))


class FlatLowVlm:
    """打分层全平低位(每帧 0)→ 失败候选;配合恒 unclear 的复核器 → 缺第二签
    弃权:制造"老算法弃权"的入口条件(2026-09-02 前是 score_blind 弃权)。"""

    def __call__(self, reference, shuffled, instruction):
        return [0.0] * len(shuffled)


def _patch_vlm_factories(monkeypatch, judge_answer="yes"):
    """把所有会发 HTTP 的工厂换成确定性假件(单测零网络请求)。"""
    import curation.adapters.vlm_client as vc
    import curation.dataset_level.caption as cap

    monkeypatch.setattr(vc, "make_endstate_voter",
                        lambda *a, **k: (lambda s, e, label, d: "unclear"))
    monkeypatch.setattr(vc, "make_question_writer", lambda *a, **k: _qw())
    monkeypatch.setattr(vc, "make_grounder",
                        lambda *a, **k: (lambda img, t, v, o: [(2, 2, 30, 30)]))
    monkeypatch.setattr(vc, "make_evidence_judge",
                        lambda *a, **k: (lambda imgs, *, target, question, scene:
                                         judge_answer))
    monkeypatch.setattr(vc, "make_intent_comparer",
                        lambda *a, **k: (lambda a_, b_: True))
    monkeypatch.setattr(cap, "make_vlm_captioner",
                        lambda *a, **k: (lambda groups:
                                         "push the t-shaped block onto the target"))


@pytest.mark.skipif(_pusht_missing, reason="pusht 数据未下载")
def test_funnel_label_guard_holds_annotation_kill(monkeypatch):
    """判废护栏端到端(ep000029 原型):全零打分 + 复核三路 no = 双签判废;指令来自原始
    标注且 caption 被比对器判为另一任务 → 不杀转人工,再进仲裁(双意图链)且 caption
    复用不重打;比对器判同一任务 → 照杀,留痕 label_agrees_kill_kept。"""
    import curation.adapters.vlm_client as vc
    import curation.dataset_level.caption as cap
    calls = {"caption": 0}

    def _captioner(groups):
        calls["caption"] += 1
        return "pour rice into the green bowl"
    for same in (False, True):
        calls["caption"] = 0
        # 判官 unclear:让仲裁弃权,才看得见"护栏拦下→进人工"这一步(判官 yes 时双意图
        # 两遍都判完成会被仲裁救回,那是正确行为但不是本测试要钉的)
        _patch_vlm_factories(monkeypatch, judge_answer="unclear")
        monkeypatch.setattr(vc, "make_endstate_voter",
                            lambda *a, **k: (lambda s, e, label, d: "no"))
        monkeypatch.setattr(vc, "make_intent_comparer",
                            lambda *a, **k: (lambda a_, b_, _s=same: _s))
        monkeypatch.setattr(cap, "make_vlm_captioner", lambda *a, **k: _captioner)
        results, _ = _run_task_check(_funnel_cfg(arbitration_enable=True))
        for e, r in results.items():
            d = r["detail"]
            if d.get("task_desc_source") != "原始标注":
                pytest.skip("pusht 条目非原始标注,护栏不适用")
            if same:
                assert r["passed"] is False, e
                assert "label_agrees_kill_kept" in d["rules"]
                assert "arbitration" not in d                # 判废不进仲裁
            else:
                assert r["passed"] is None, e
                assert d["verdict"] == "label_conflict_suspect"
                assert "kill_held_label_conflict" in d["rules"]
                assert d["label_check"]["outcome"] == "different"
                assert d["arbitration"]["intent_conflict"] is True   # 双意图链跑了
                assert d["arbitration"]["final"] == "abstain"
        # caption 每条只打一次(护栏打的,仲裁复用不重打)
        assert calls["caption"] == len(results)


def _funnel_cfg(arbitration_enable: bool) -> dict:
    from curation.pipeline.config import load_config

    cfg = load_config()
    for name in cfg["checks"]:
        if name != "task_success":
            cfg["checks"][name]["enable"] = False       # 只留 VLM 段,别的检查不陪跑
    cfg["checks"]["task_success"]["arbitration"]["enable"] = arbitration_enable
    return cfg


def _run_task_check(cfg):
    from curation.ingest.lerobot_reader import read_lerobot_rows, rows_to_daft
    from curation.pipeline.funnel import run_funnel
    from curation.registry.registry import EmbodimentRegistry

    rows = read_lerobot_rows(PUSHT, max_episodes=2, embodiment_id="pusht")
    df, stats = run_funnel(rows_to_daft(rows), cfg, EmbodimentRegistry(),
                           vlm_completion=FlatLowVlm())
    out = df.select("episode_id", "check_task_success").to_pydict()
    return {e: {"passed": c["passed"],
                "detail": json.loads(c.get("detail") or "{}")}
            for e, c in zip(out["episode_id"], out["check_task_success"])}, stats


@pytest.mark.skipif(_pusht_missing, reason="pusht 数据未下载")
def test_funnel_disabled_keeps_todays_behavior(monkeypatch):
    """enable:false = 完全退回今天的行为:弃权条目维持弃权(失败候选缺第二签),
    detail 里不出现任何 arbitration 痕迹,stats 也没有 arbitration 键——
    仲裁代码一行都没执行(与改动前逐字节等价的可回归锚点)。"""
    _patch_vlm_factories(monkeypatch)
    results, stats = _run_task_check(_funnel_cfg(arbitration_enable=False))
    assert "arbitration" not in stats
    for e, r in results.items():
        assert r["passed"] is None, e                   # 失败候选×复核 unclear=缺第二签
        assert r["detail"].get("verdict") == "failure"
        assert "kill_missing_second_signature" in r["detail"].get("rules", [])
        assert "arbitration" not in r["detail"]
        assert not any(x.startswith("arbitration") for x in r["detail"].get("rules", []))


@pytest.mark.skipif(_pusht_missing, reason="pusht 数据未下载")
def test_funnel_enabled_arbitrates_abstentions(monkeypatch):
    """enable:true(出厂默认):只有弃权条目走仲裁链,取证一致 yes → 救回;
    留痕(意图来源=仲裁时自产 caption)与触发计数(stats.arbitration)都要在。
    这是"接线通"的进程内证明,真 VLM 的端到端另行在 droid 上验。"""
    _patch_vlm_factories(monkeypatch, judge_answer="yes")
    results, stats = _run_task_check(_funnel_cfg(arbitration_enable=True))
    n = len(results)
    assert stats["arbitration"]["triggered"] == n
    assert stats["arbitration"]["adopted_success"] == n
    for e, r in results.items():
        assert r["passed"] is True, e
        assert r["detail"]["verdict"] == "arbitration_success"
        arb = r["detail"]["arbitration"]
        assert arb["applied"] is True
        assert arb["intent_source"] == "自产caption(仲裁时)"   # pusht 有原始标注
        assert arb["intent"] == "push the t-shaped block onto the target"
        assert arb["n_effective"] >= 1 and arb["lines"]
