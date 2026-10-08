"""Episode-level video judgement. No frame probes or VOC-based thresholds.

One protocol, ``video-task/2`` (D71, 2026-10-07): ONE request over all cameras, and that one
answer speaks for the episode and for every camera. The per-camera answers come out of the same
reasoning pass, so they are not an independent second signature and are not treated as one: a pass
needs the episode verdict and no camera contradicting it, a rejection needs the episode verdict,
a camera backing it and no camera contradicting it, a definite verdict must cite evidence, and
anything less than that goes to a person rather than to another request. There is no review
request per camera, no arbitration and, since D73, no label guard: a rejection is the one answer
and its evidence - at temperature 0 a re-ask of the same model on the same video is the same
opinion, not a second one. The two-pass protocol this replaced (``video-task/1``: a scoring pass,
a review per camera, a double-signed rejection, an arbitration request) is in the history before
D71; the label guard (a caption of the video compared with the annotation before a rejection) in
the history before D73.
"""
from __future__ import annotations

from ..adapters.video_input import prepare_videos
from ..adapters.video_vlm import PROTOCOL
from ..core.contract import CheckResult

#: a camera's answer -> its vote, the word the console and the records have always used
_VOTE = {"success": "yes", "failure": "no", "uncertain": "unclear"}


class VideoPreparationError(RuntimeError):
    """Media preparation failed, rather than the model being unable to judge."""


def prepare_clips(cfg, video) -> list:
    """The episode's cameras as the clips one request takes (the configured size, byte budget,
    camera cap and sampling rate); raises :class:`VideoPreparationError`."""
    opts = (cfg.get("checks", {}).get("task_success", {}).get("vlm") or {}).get("video") or {}
    try:
        return prepare_videos(video, max_side=int(opts.get("max_side", 720)),
                              max_bytes=int(opts.get("max_bytes", 32 * 1024 * 1024)),
                              max_cams=int(cfg.get("pipeline", {}).get("max_endstate_cams", 4)),
                              fps=float(opts.get("fps", 5)))
    except Exception as exc:
        raise VideoPreparationError(f"视频准备失败: {type(exc).__name__}: {exc}") from exc


def judge_video_episode(cfg, video, instruction, scorer, *, task_src="原始标注", hints="") -> CheckResult:
    """One request, one answer: the verdict, with every camera's own answer and defect report."""
    detail = {"input_mode": "video", "protocol": PROTOCOL,
              "task_desc": str(instruction),
              "task_desc_source": task_src, "rules": [], "verdict": "uncertain"}
    result = CheckResult("task_success", detail=detail)
    clips = prepare_clips(cfg, video)
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

    cameras = primary.get("cameras") or {}
    detail["cameras"] = cameras
    votes = {cam: _VOTE.get(entry.get("verdict"), "unavail") for cam, entry in cameras.items()}
    detail["cam_votes"] = votes
    yes, no = list(votes.values()).count("yes"), list(votes.values()).count("no")
    detail["review"] = "split" if yes and no else "yes" if yes else "no" if no else "abstain"
    verdict = primary["verdict"]
    # One answer, so the cameras are not a second signature: the episode's own verdict decides,
    # the cameras only have to not contradict it, and a definite verdict must cite evidence
    # (parse_assessment already refuses one that does not). Everything else goes to a person.
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
    return result
