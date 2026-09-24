"""The episode verdict of the EEF module: the CPU first, the model second (design 12 D-E12, appendix C.9).

Every episode ends as ``pass``, ``reject`` or ``human`` (a card in the adjudication queue):

* the model can judge position and orientation: per camera, a CPU ``suspect`` the model does not
  refute by majority (refute >= support, at least one vote) is a confirmed defect; the model
  supporting it by majority is a conflict; no vote at all (uncertain, not observable, failed windows)
  leaves the CPU without a second opinion. A CPU ``ok`` the model refutes by majority on the other
  windows is a conflict too. Uncertain / not observable answers never vote;
* the model saying, by majority on a camera, that the green cross tracks something else makes that
  camera's CPU reading untrustworthy;
* the model cannot judge time alignment, record jitter (state motion) or camera motion from a few
  crops: a CPU ``suspect`` there goes to a person;
* an episode the file does not declare, or whose position no camera could assess, cannot be judged.

Any confirmed defect rejects (a sure reject needs nobody); otherwise any of the above goes to a person;
otherwise the episode passes. Sub-items the CPU could not assess (other than position everywhere) do
not change the outcome and are listed as not checked.
"""
from __future__ import annotations

from typing import Any

from . import contracts as C

PASS, REJECT, HUMAN = "pass", "reject", "human"
SEEN = (C.POSITION, C.ORIENTATION)                      # the model votes on these
UNSEEN_CAMERA = (C.TEMPORAL, C.CAMERA_MOTION)           # per camera, the model cannot judge
NAMES = {C.POSITION: "位置", C.ORIENTATION: "朝向", C.TEMPORAL: "时间对齐", C.STATE_MOTION: "状态运动",
         C.CAMERA_MOTION: "相机运动"}
FIELD = {C.POSITION: "position_support", C.ORIENTATION: "orientation_support"}


def _count(windows: list[dict], sub: str) -> dict[str, int]:
    out = {"support": 0, "refute": 0}
    for w in windows:
        said = (w.get("answer") or {}).get(FIELD[sub])
        if said in out:
            out[said] += 1
    return out


def _item(code: str, text: str, **kw) -> dict:
    return {"code": code, "text": text, **{k: v for k, v in kw.items() if v is not None}}


def decide(cpu: dict | None, review: dict | None) -> dict[str, Any]:
    """``cpu``: the module's CPU ``detail`` (None: the file does not declare the episode); ``review``:
    ``{"cameras": {camera_id: {"windows": [...]}}}`` with each window's ``kind``, ``subitem`` and,
    when answered, ``status == "answered"`` and ``answer``."""
    confirmed: list[dict] = []
    human: list[dict] = []
    unchecked: list[dict] = []
    if cpu is None:
        human.append(_item("not_in_file", "trajectory.json 里没有这一条"))
        return _outcome(confirmed, human, unchecked)
    cams = cpu.get("cameras") or {}
    positions = {cid: ((cam.get("subitems") or {}).get(C.POSITION) or {}) for cid, cam in cams.items()}
    if not any(p.get("status") in (C.OK, C.SUSPECT) for p in positions.values()):
        why = sorted({r for p in positions.values() for r in p.get("reasons") or []})
        human.append(_item("not_assessable", "位置在所有相机上都无法评估" + (f"（{'、'.join(why)}）" if why else ""),
                           reasons=why))
    rcams = (review or {}).get("cameras") or {}
    for cid, cam in cams.items():
        cells = cam.get("subitems") or {}
        answered = [w for w in (rcams.get(cid) or {}).get("windows") or [] if w.get("status") == "answered"]
        track = {"support": 0, "refute": 0}
        for w in answered:
            said = (w.get("answer") or {}).get("tracking_target_correct")
            if said in track:
                track[said] += 1
        trusted = not track["refute"] > track["support"]
        if not trusted and any((cells.get(s) or {}).get("status") in (C.OK, C.SUSPECT) for s in SEEN):
            human.append(_item("tracking_suspect", f"相机 {cid}：模型多数认为绿十字（独立观测）跟错了目标，CPU 的读数不可信",
                               camera_id=cid, votes=track))
        for sub in SEEN:
            st = (cells.get(sub) or {}).get("status")
            name = NAMES[sub]
            if st == C.SUSPECT:
                v = _count([w for w in answered if w.get("kind") == "candidate" and w.get("subitem") == sub], sub)
                if not v["support"] and not v["refute"]:
                    human.append(_item("no_model_opinion", f"「{name}」（相机 {cid}）CPU 判为可疑，模型没有给出意见",
                                       subitem=sub, camera_id=cid, votes=v))
                elif v["refute"] >= v["support"]:
                    if trusted:
                        confirmed.append(_item("confirmed", f"「{name}」（相机 {cid}）CPU 与模型都认为不一致"
                                                            f"（模型反对 {v['refute']}、支持 {v['support']}）",
                                               subitem=sub, camera_id=cid, votes=v))
                else:
                    human.append(_item("conflict", f"「{name}」（相机 {cid}）CPU 判为可疑，模型多数认为一致"
                                                   f"（支持 {v['support']}、反对 {v['refute']}）",
                                       subitem=sub, camera_id=cid, cpu=C.SUSPECT, votes=v))
            elif st == C.OK:
                v = _count([w for w in answered if not (w.get("kind") == "candidate" and w.get("subitem") == sub)], sub)
                if v["refute"] > v["support"]:
                    human.append(_item("conflict", f"「{name}」（相机 {cid}）CPU 判为正常，模型多数认为不一致"
                                                   f"（反对 {v['refute']}、支持 {v['support']}）",
                                       subitem=sub, camera_id=cid, cpu=C.OK, votes=v))
            elif st is not None:
                unchecked.append({"subitem": sub, "camera_id": cid, "status": st})
        for sub in UNSEEN_CAMERA:
            st = (cells.get(sub) or {}).get("status")
            if st == C.SUSPECT:
                human.append(_item("model_cannot_see", f"「{NAMES[sub]}」（相机 {cid}）CPU 判为可疑，模型看不了这一项",
                                   subitem=sub, camera_id=cid))
            elif st not in (None, C.OK):
                unchecked.append({"subitem": sub, "camera_id": cid, "status": st})
    st = (cpu.get("state_motion") or {}).get("status")
    if st == C.SUSPECT:
        human.append(_item("model_cannot_see", f"「{NAMES[C.STATE_MOTION]}」CPU 判为可疑，模型看不了这一项",
                           subitem=C.STATE_MOTION))
    elif st not in (None, C.OK):
        unchecked.append({"subitem": C.STATE_MOTION, "status": st})
    return _outcome(confirmed, human, unchecked)


def _outcome(confirmed: list[dict], human: list[dict], unchecked: list[dict]) -> dict[str, Any]:
    if confirmed:
        outcome, passed, reason = REJECT, False, "；".join(c["text"] for c in confirmed)
    elif human:
        outcome, passed, reason = HUMAN, None, "需要人工裁决：" + "；".join(h["text"] for h in human)
    else:
        outcome, passed, reason = PASS, True, ""
    return {"outcome": outcome, "passed": passed, "reason": reason, "confirmed": confirmed, "human": human,
            "unchecked": unchecked}
