"""Episode-level video judgement. No frame probes or VOC-based thresholds."""
from __future__ import annotations

from ..adapters.video_input import prepare_videos
from ..adapters.video_vlm import PROTOCOL
from ..core.contract import CheckResult


class VideoPreparationError(RuntimeError):
    """Media preparation failed, rather than the model being unable to judge."""


def judge_video_episode(cfg, video, instruction, scorer, reviewer, arb_deps=None,
                        *, task_src="原始标注", hints="") -> CheckResult:
    from ..adapters.vlm_client import _map_concurrent
    from ..core.checks.task_success import hold_kill_on_label_conflict

    opts = (cfg.get("checks", {}).get("task_success", {}).get("vlm") or {}).get("video") or {}
    detail = {"input_mode": "video", "protocol": PROTOCOL, "task_desc": str(instruction),
              "task_desc_source": task_src, "rules": [], "verdict": "uncertain"}
    result = CheckResult("task_success", detail=detail)
    try:
        clips = prepare_videos(video, max_side=int(opts.get("max_side", 720)),
                               max_bytes=int(opts.get("max_bytes", 32 * 1024 * 1024)),
                               max_cams=int(cfg.get("pipeline", {}).get("max_endstate_cams", 4)),
                               fps=float(opts.get("fps", 5)))
    except Exception as exc:
        raise VideoPreparationError(f"视频准备失败: {type(exc).__name__}: {exc}") from exc
    detail["video_inputs"] = [c.metadata() for c in clips]
    detail["cams"] = [c.camera for c in clips]
    try:
        primary = scorer(clips, str(instruction), hints=hints)
    except Exception as exc:
        detail.update(reason=f"视频打分失败: {type(exc).__name__}: {exc}",
                      rules=["video_score_failed"])
        return result
    detail.update(video_assessment=primary, init_verdict=primary["verdict"],
                  task_type=primary["task_type"], task_type_source="video",
                  video_completion=primary["completion"], reason=primary["reason"],
                  video_evidence=list(primary["evidence"]))
    # task_success is a hard verdict. Keep the model's estimate in detail;
    # a non-null CheckResult.score would turn an abstention into "scored".

    def review(clip):
        if reviewer is None:
            return {"error": "video reviewer unavailable"}
        try:
            return reviewer([clip], str(instruction), hints=hints)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    reviews = dict(zip([c.camera for c in clips], _map_concurrent(review, clips, len(clips))))
    detail["video_reviews"] = reviews
    votes = {cam: {"success": "yes", "failure": "no", "uncertain": "unclear"}.get(
        answer.get("verdict"), "unavail") for cam, answer in reviews.items()}
    detail["cam_votes"] = votes
    yes, no = list(votes.values()).count("yes"), list(votes.values()).count("no")
    detail["review"] = "split" if yes and no else "yes" if yes else "no" if no else "abstain"
    for answer in reviews.values():
        detail["video_evidence"].extend(answer.get("evidence", []))
    verdict = primary["verdict"]
    if yes and not no and verdict != "failure":
        result.passed = True
        detail.update(verdict="success", rules=["video_review_confirms_success"])
    elif no and not yes and verdict == "failure":
        result.passed = False
        detail.update(verdict="failure", rules=["video_double_signed_failure"])
    else:
        detail["rules"].append("video_review_uncertain_or_conflicting")
        detail["reason"] = f"视频主判={verdict}，逐机位复核={detail['review']}，证据不足或结论冲突"

    if result.passed is None and arb_deps and arb_deps.get("video_judge"):
        try:
            answer = arb_deps["video_judge"](clips, str(instruction), hints=hints)
            detail["video_arbitration"] = answer
            detail["video_evidence"].extend(answer["evidence"])
            if answer["verdict"] == "success" and yes and not no:
                result.passed = True
                detail.update(verdict="arbitration_success", reason=answer["reason"])
                detail["rules"].append("video_arbitration_success")
            elif answer["verdict"] == "failure" and no >= 2 and not yes:
                result.passed = False
                detail.update(verdict="arbitration_failure", reason=answer["reason"])
                detail["rules"].append("video_arbitration_failure_two_cameras")
        except Exception as exc:
            detail["video_arbitration"] = {"error": f"{type(exc).__name__}: {exc}"}

    if result.passed is False and task_src == "原始标注" and arb_deps:
        try:
            caption = str(arb_deps["captioner"](clips)).strip()
            hold_kill_on_label_conflict(result, annotation=str(instruction), caption=caption,
                                       same_task=arb_deps["same_task"])
        except Exception as exc:
            detail["label_check"] = {"outcome": f"error:{type(exc).__name__}"}
    return result
