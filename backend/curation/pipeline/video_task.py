"""Episode-level video judgement. No frame probes or VOC-based thresholds.

Two protocols live here.

``video-task/1`` (two passes) asks the episode once over all cameras, then once per camera on its
own, and wants the two to agree: a pass needs every voting camera to say success, a rejection needs
the episode verdict AND the cameras to say failure (the "double signature"), and a disagreement
goes to arbitration - a third request. The per-camera pass is what makes the second signature
independent: the model judges one camera knowing nothing of the others.

``video-task/2`` (single pass) asks once and has that one answer speak for the episode and for
every camera. The per-camera answers come out of the same reasoning pass, so they are NOT a second
signature and are not treated as one: what guards a rejection here is that it must cite evidence,
that it still passes the label guard, and that anything less than a clear answer goes to a person
rather than to another request. Arbitration is gone with the review - at temperature 0 a re-ask of
the same model on the same video is the same opinion, not a second one.
"""
from __future__ import annotations

from ..adapters.video_input import prepare_videos
from ..adapters.video_vlm import PROTOCOL, PROTOCOL_SINGLE
from ..core.contract import CheckResult

#: a camera's answer -> its vote, the word the console and the records have always used
_VOTE = {"success": "yes", "failure": "no", "uncertain": "unclear"}


class VideoPreparationError(RuntimeError):
    """Media preparation failed, rather than the model being unable to judge."""


def judge_video_episode(cfg, video, instruction, scorer, reviewer, arb_deps=None,
                        *, task_src="原始标注", hints="", single_pass: bool = False) -> CheckResult:
    from ..adapters.vlm_client import _map_concurrent
    from ..core.checks.task_success import hold_kill_on_label_conflict

    opts = (cfg.get("checks", {}).get("task_success", {}).get("vlm") or {}).get("video") or {}
    detail = {"input_mode": "video", "protocol": PROTOCOL_SINGLE if single_pass else PROTOCOL,
              "task_desc": str(instruction),
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

    if single_pass:
        cameras = primary.get("cameras") or {}
        detail["cameras"] = cameras
        votes = {cam: _VOTE.get(entry.get("verdict"), "unavail") for cam, entry in cameras.items()}
    else:
        def review(clip):
            if reviewer is None:
                return {"error": "video reviewer unavailable"}
            try:
                return reviewer([clip], str(instruction), hints=hints)
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}

        reviews = dict(zip([c.camera for c in clips], _map_concurrent(review, clips, len(clips))))
        detail["video_reviews"] = reviews
        votes = {cam: _VOTE.get(answer.get("verdict"), "unavail") for cam, answer in reviews.items()}
        for answer in reviews.values():
            detail["video_evidence"].extend(answer.get("evidence", []))
    detail["cam_votes"] = votes
    yes, no = list(votes.values()).count("yes"), list(votes.values()).count("no")
    detail["review"] = "split" if yes and no else "yes" if yes else "no" if no else "abstain"
    verdict = primary["verdict"]
    if single_pass:
        # One answer, so the cameras are not a second signature: the episode's own verdict decides,
        # the cameras only have to not contradict it, and a definite verdict must cite evidence
        # (parse_assessment already refuses one that does not). Everything else goes to a person -
        # which is what the second and third requests used to be spent avoiding.
        if verdict == "success" and not no:
            result.passed = True
            detail.update(verdict="success", rules=["video_single_pass_success"])
        elif verdict == "failure" and no and not yes:
            result.passed = False
            detail.update(verdict="failure", rules=["video_single_pass_failure"])
        else:
            detail["rules"].append("video_single_pass_undecided")
            detail["reason"] = (f"视频判定={verdict}，逐机位={detail['review']}，"
                                f"结论不够确定，转人工")
    elif yes and not no and verdict != "failure":
        result.passed = True
        detail.update(verdict="success", rules=["video_review_confirms_success"])
    elif no and not yes and verdict == "failure":
        result.passed = False
        detail.update(verdict="failure", rules=["video_double_signed_failure"])
    else:
        detail["rules"].append("video_review_uncertain_or_conflicting")
        detail["reason"] = f"视频主判={verdict}，逐机位复核={detail['review']}，证据不足或结论冲突"

    # Single pass has no arbitration: the same model, the same video and temperature 0 give the
    # same answer back, so the request buys a wording change, not a second opinion. An undecided
    # episode goes to a person instead.
    if not single_pass and result.passed is None and arb_deps and arb_deps.get("video_judge"):
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
