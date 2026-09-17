"""轨迹页「检查明细」的单条视图(2026-09-16 用户定)。

此前折叠区只有一张四列表 + 同步读数,任务成败一行写的是打分层的 voc/末态数字,运动质量
一行要点是空的;子项、逐相机、卡顿时间线、运动学、打标都只在整批的「明细」页或根本没有入口。
现在按检查分块,每块只放**这一条**的东西;表里的「要点」精炼成一句判定链,并多一行「打标」。

纯函数 + 只读交付文件;不 import 管道(与 manifest 同一红线)。
"""
from __future__ import annotations

import csv
import os

from ..export.detail_labels import MOTION_COLS, MOTION_NA_REASON_KEY, MOTION_SUBDIMS
from . import manifest as _m

# ── 任务成败:三层判定链 ──────────────────────────────────────────────────

_INIT_TEXT = {
    "success": "打分层成功候选", "recovery": "打分层判恢复成功", "failure": "打分层失败候选",
    "uncertain": "打分层拿不准", "gap_violation": "打分层峰值后回落", "voc_tripwire": "打分层时序绊线",
    "score_blind": "打分层无信息", "no_progress": "打分层全程无进展",
}
_TALLY_TEXT = {"yes": "复核一致判完成", "no": "复核一致判未完成", "split": "复核分歧",
               "abstain": "复核全体弃权"}
_FINAL_TEXT = {"pass": "通过", "拒绝": "判废", "弃权": "转人工"}
_SOURCE_TEXT = {"原始标注": "意图取原始标注", "人工改标": "意图取人工改标"}


def _task_record(m: dict, eid: str) -> dict:
    path = (m or {}).get("path") or ""
    return ((_m._details_json(path, "task_details.json").get("episodes") or {}).get(eid)
            or {})


def task_chain_gist(m: dict, eid: str) -> str:
    """一句判定链:按实际走过的层写,没走的不写。例:
    「打分层成功候选(弱) → 两路复核判未完成 → 仲裁两路一致未完成 → 判废;意图取原始标注」"""
    rec = _task_record(m, eid)
    ep = (m.get("episodes") or {}).get(eid) or {}
    chk = (ep.get("checks") or {}).get(_m.TASK_CHECK_CN) or {}
    det = chk.get("detail") or {}
    if not rec and not det:
        return ""
    init = str(rec.get("init_verdict") or det.get("init_verdict") or "")
    scoring = rec.get("scoring") or det.get("raw") or {}
    strong = scoring.get("strong_score", det.get("strong_score"))
    parts = []
    if init:
        t = _INIT_TEXT.get(init, f"打分层 {init}")
        if init == "success" and strong is not None:
            t += "(强)" if strong else "(弱)"
        parts.append(t)
    review = rec.get("review") or {}
    votes = review.get("cam_votes") or det.get("cam_votes") or {}
    tally = str(review.get("tally") or det.get("review") or "")
    if votes or tally:
        n_no = sum(1 for v in votes.values() if v == "no")
        n_yes = sum(1 for v in votes.values() if v == "yes")
        head = ""
        if tally == "no" and n_no:
            head = f"{n_no} 路"
        elif tally == "yes" and n_yes:
            head = f"{n_yes} 路"
        parts.append(head + _TALLY_TEXT.get(tally, f"复核 {tally}" if tally else "复核"))
    arb = rec.get("arbitration") or det.get("arbitration") or {}
    if isinstance(arb, dict) and arb.get("applied"):
        n = arb.get("n_effective")
        cons = str(arb.get("consensus") or "")
        cons_t = {"no": "一致未完成", "yes": "一致完成"}.get(cons, "拿不准")
        parts.append(f"仲裁{'' if n is None else f' {n} 路'}{cons_t}")
    verdict = str(rec.get("verdict") or det.get("verdict") or "")
    if verdict in ("label_conflict_suspect",):
        parts.append("标注与自产描述不是同一任务,护栏拦下")
    # 结论以**当前**检查态为准(rejudge 后 checks 已改写;判定痕迹记的是当时的结论),
    # 人工裁决过的条目把"转人工 → 人工判成功/失败"一起写出来
    machine = _FINAL_TEXT.get(str(rec.get("result") or ""), "")
    now = _FINAL_TEXT.get(str(chk.get("state") or ""), "")
    verdict_txt = str(ep.get("verdict") or "")
    if machine == "转人工" and "人工" in verdict_txt:
        parts.append("转人工")
        parts.append("人工判成功" if now == "通过" else "人工判失败" if now == "判废" else "人工裁决")
    else:
        final = now or machine
        if final:
            parts.append(final)
    gist = " → ".join(parts)
    final = parts[-1] if parts else ""
    src = _m._task_source_label(rec.get("instruction_source") or det.get("task_desc_source"))
    if src in _SOURCE_TEXT:
        gist += ";" + _SOURCE_TEXT[src]
    reason = str(rec.get("reason") or det.get("reason") or "")
    if reason and (final == "转人工" or len(parts) <= 1):
        # 转人工要说清停在哪一层为什么;老交付没有判定痕迹时链条只剩结论,补上理由
        gist += f";{reason}" if gist else reason
    return gist


# ── 各检查一句要点 ────────────────────────────────────────────────────────

def _f(v, nd: int = 2) -> str:
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def motion_gist(det: dict) -> str:
    scored = [(zh, det.get(k)) for k, zh in MOTION_COLS if k in MOTION_SUBDIMS
              and k not in ("path_efficiency", "joint_stability", "stuck")]
    used = [f"{zh} {_f(v)}" for zh, v in scored if v is not None]
    na = [zh for zh, v in scored if v is None]
    out = "总分=" + "、".join(used) + " 的均值" if used else ""
    if na:
        out += ";不适用:" + "、".join(na)
    if det.get("same_source"):
        out += "(指令与读数同源)"
    return out


def _per_camera(det: dict) -> dict:
    """逐相机读数归一成 {相机: {score, ...}}:老交付里 per_camera 的值是一个数(总分),新交付是字典。"""
    per = det.get("per_camera") or {}
    if not isinstance(per, dict):
        return {}
    out = {}
    for cam, v in per.items():
        if isinstance(v, dict):
            out[cam] = v
        elif isinstance(v, (int, float)):
            out[cam] = {"score": float(v)}
    return out


def visual_gist(det: dict) -> str:
    per = _per_camera(det)
    if not per:
        return ""
    cam, d = min(per.items(), key=lambda kv: float(kv[1].get("score") if kv[1].get("score") is not None else 1.0))
    bits = [f"{len(per)} 路相机;最低 {_f(d.get('score'))}({_m._camera_label(cam)}"]
    if d.get("sharpness") is not None:
        bits.append(f":清晰度 {_f(d.get('sharpness'))} 曝光 {_f(d.get('exposure'))}")
    return "".join(bits) + ")"


def sync_gist(det: dict) -> str:
    per = det.get("per_camera") or {}
    v = str(det.get("verdict") or "")
    txt = _m.SYNC_VERDICT_TEXT.get(v, ("", "", "", ""))[0] if hasattr(_m, "SYNC_VERDICT_TEXT") else v
    if not isinstance(per, dict) or not per:
        return txt or v
    cams = [c for c in per.values() if isinstance(c, dict)]
    if not cams:
        return txt or v
    trusted = sum(1 for c in cams if c.get("trusted"))
    lags = "/".join(f"{float(c.get('lag_s') or 0):+.2f}" for c in cams)
    return f"{txt or v} · {trusted}/{len(cams)} 路可信 · 滞后 {lags}s"


def timestamp_gist(det: dict) -> str:
    if det.get("reason"):
        return str(det["reason"])
    n, dur = det.get("n"), det.get("duration_s")
    if n and dur:
        return f"{int(n)} 帧 {float(dur):.1f} 秒,帧间隔稳定"
    return ""


def kinematics_gist(det: dict) -> str:
    if det.get("reason"):
        return str(det["reason"])
    v = det.get("violations") or []
    return f"{len(v)} 处超限" if v else "关节/末端全程在规格内"


def check_gist(m: dict, eid: str, name: str, det: dict) -> str:
    if name == _m.TASK_CHECK_CN:
        return task_chain_gist(m, eid) or str(det.get("reason") or "")
    if name == "运动质量":
        return motion_gist(det)
    if name == "视觉质量":
        return visual_gist(det)
    if name == "视频-动作同步":
        return sync_gist(det)
    if name == "时间戳检查":
        return timestamp_gist(det)
    if name == "运动学极限":
        return kinematics_gist(det)
    return str(det.get("reason") or det.get("verdict") or "")


# ── 打标行 ────────────────────────────────────────────────────────────────

LABEL_ROW_NAME = "打标"
LABEL_STATE_AGREE = "一致"
LABEL_STATE_DISAGREE = "分歧(待人工)"
LABEL_STATE_DECIDED = "分歧(已裁)"
LABEL_STATE_NO_LABEL = "无标注(用自产描述)"


def _skill_of(m: dict, eid: str) -> tuple[str, str, str]:
    """skill_assignment.csv → (技能族, 子技能, 自产描述);中文名从画像表里对号。"""
    path = os.path.join(str((m or {}).get("path") or ""), "details", "skill_assignment.csv")
    try:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("episode_id") == eid:
                    fam, sub = r.get("family", ""), r.get("subskill", "")
                    fams = (m.get("skills") or {}).get("families") or {}
                    fam_zh = _m._skill_name(fams.get(fam), fam)
                    sub_zh = _m._skill_name(((fams.get(fam) or {}).get("subskills") or {}).get(sub), sub)
                    return fam_zh, sub_zh, r.get("caption", "")
    except OSError:
        pass
    return "", "", ""


def label_row(m: dict, eid: str) -> list:
    """[打标, 状态, "", 要点]:分数列留空(用户定)。"""
    fam, sub, cap = _skill_of(m, eid)
    if not cap:
        cap = str(_m._details_json(str(m.get("path") or ""), "captions.json").get(eid) or "")
    audit = next((a for a in (m.get("audit_queue") or []) if a.get("id") == eid), None)
    text, src = _m.episode_task_text(m, eid)
    bits = []
    if fam or sub:
        bits.append(f"{fam} › {sub}" if sub else fam)
    if cap:
        bits.append(f"自产描述:{cap}")
    if audit:
        decision = str(audit.get("decision") or "")
        human = _m.load_label_decisions(m).get(eid) if hasattr(_m, "load_label_decisions") else None
        if isinstance(human, dict) and human.get("decision"):
            decision = str(human["decision"])
        state = LABEL_STATE_DECIDED if decision else LABEL_STATE_DISAGREE
        if audit.get("reason"):
            bits.append(str(audit["reason"]))
        if decision:
            bits.append(f"人工:{decision}")
    elif src == _m.TASK_SOURCE_CAPTION or not text:
        state = LABEL_STATE_NO_LABEL
    else:
        state = LABEL_STATE_AGREE
    return [LABEL_ROW_NAME, state, "", " ｜ ".join(bits)]


# ── 分块 HTML ─────────────────────────────────────────────────────────────

def _block(title: str, body: str) -> str:
    if not body:
        return ""
    return (f'<div style="margin-top:14px"><div style="font:13px/1.6 system-ui;font-weight:700;'
            f'color:#4E5969;margin-bottom:4px">{_m._esc(title)}</div>{body}</div>')


def _check_detail(m: dict, eid: str, name: str) -> tuple[dict, dict]:
    ep = (m.get("episodes") or {}).get(eid) or {}
    chk = (ep.get("checks") or {}).get(name) or {}
    return chk, (chk.get("detail") or {})


_IN_TOTAL = ("smoothness", "spike", "gripper_jitter", "actuator_saturation")


def motion_subdims_html(m: dict, eid: str) -> str:
    chk, det = _check_detail(m, eid, "运动质量")
    if not chk:
        return ""
    rows, marks = [], []
    for key, zh in MOTION_COLS:
        if key not in MOTION_SUBDIMS and key not in ("fluency", "active_ratio"):
            continue
        v = det.get(key)
        if v is None:
            why = det.get(MOTION_NA_REASON_KEY.get(key, ""), "") or "本条缺少计算它所需的读数"
            rows.append([zh, "不适用", "", str(why)])
            marks.append(False)
            continue
        rows.append([zh, _f(v, 4), "计入总分" if key in _IN_TOTAL else "只报不罚", ""])
        marks.append(False)
    note = ""
    if det.get("same_source"):
        note = ('<p style="margin:4px 0 0;font:12px/1.6 system-ui;color:#86909C">'
                '指令与读数同源(指令即下一帧读数):执行器饱和、卡顿无法评估,按不适用处理。</p>')
    return _block("运动质量子项", _m._table_html(["子项", "得分", "计分", "说明"], rows, marks) + note)


def visual_cameras_html(m: dict, eid: str) -> str:
    chk, det = _check_detail(m, eid, "视觉质量")
    per = _per_camera(det)
    if not per:
        return ""
    rows, marks = [], []
    for cam in sorted(per):
        d = per[cam]
        rows.append([_m._camera_label(cam), _f(d.get("score")), _f(d.get("sharpness")),
                     _f(d.get("exposure")), _f(d.get("integrity")),
                     _f(d.get("frozen_ratio"), 3), str(d.get("status") or "")])
        marks.append(float(d.get("score") or 1.0) < 0.8)
    return _block("视觉质量(逐相机)", _m._table_html(
        ["相机", "视觉总分", "清晰度", "曝光", "完整性", "冻结帧占比", "状态"], rows, marks))


def episode_timeline_html(m: dict, eid: str) -> str:
    tl = _m.load_timeline(m)
    e = (tl.get("episodes") or {}).get(eid)
    if not e:
        return ""
    one = {"episodes": {eid: e}, "dataset_note": tl.get("dataset_note", "")}
    return _block("卡顿与空闲时间线", _m.timeline_html(one, show="all", sort="episode"))


def kinematics_html(m: dict, eid: str) -> str:
    chk, det = _check_detail(m, eid, "运动学极限")
    if chk:
        v = det.get("violations") or []
        if not v:
            body = (f'<p style="margin:0;font:13px/1.6 system-ui;color:#4E5969">'
                    f'{_m._esc(kinematics_gist(det))}</p>')
        else:
            rows = [[str(x.get("type", "")), str(x.get("joint", "")), str(x.get("frame", "")),
                     _f(x.get("value"), 4), _f(x.get("limit"), 4)] for x in v[:50]]
            body = _m._table_html(["违规类型", "关节/轴", "帧", "实测值", "极限"], rows,
                                  [True] * len(rows), mark_color="#FFECE8")
        return _block("运动学极限", body)
    # 本次没跑:从数据包完整性里把原因说出来(型号不在规格库 / 未声明型号)
    cf = ((m.get("dataset") or {}).get("container") or {}).get("findings") or []
    why = next((str(f.get("说明") or "") for f in cf if f.get("项") == "机器人型号"), "")
    return _block("运动学极限", f'<p style="margin:0;font:13px/1.6 system-ui;color:#86909C">'
                                f'本次未跑运动学极限{"。" + _m._esc(why) if why else "。"}</p>')


def sync_caption_html() -> str:
    """同步曲线图上方的一行标题(图片组件不再带浮动 label:它压在图内标题上,2026-09-16 用户实见)。"""
    return ('<div style="font:13px/1.6 system-ui;font-weight:700;color:#4E5969;'
            'margin:14px 0 2px">视频-动作同步曲线(右上角可全屏放大)</div>')
