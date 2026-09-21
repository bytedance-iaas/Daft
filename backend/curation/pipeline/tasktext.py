"""The task text each episode is judged and delivered with (design doc 02 §3.4, 06 §4.3).

Three sources, v1's policy (``run.judge_text_and_source``, annotation first):

* the original annotation of the dataset (``原始标注``);
* else the caption ``autolabel`` wrote for an episode without one (``自产caption``);
* a human relabel applied by ``adjudicate-apply`` replaces both (``人工改标``).

An unlabeled episode whose autolabel caption failed (or was never made) has no
task text: task_success cannot judge it, so it is held until autolabel is
retried (D33) - never judged against an empty text. An honest ``unclear``
caption is not a failure: v1 judges such an episode with the empty text, and so
does v2.
"""
from __future__ import annotations

import json
import os

from .records import ADJUDICATION_DIR, AUTOLABEL_FILE, read_jsonl
from .run import judge_text_and_source

LABELS_FILE = f"{ADJUDICATION_DIR}/labels.json"
SRC_ORIGINAL = "原始标注"
SRC_CAPTION = "自产caption"
SRC_HUMAN = "人工改标"
SRC_NONE = "无"


def load_autolabel(run_dir: str) -> dict[int, dict]:
    """``autolabel/captions.jsonl``: the latest line per episode."""
    out: dict[int, dict] = {}
    for line in read_jsonl(os.path.join(run_dir, AUTOLABEL_FILE)):
        out[int(line["episode_index"])] = line
    return out


def autolabel_ran(run_dir: str) -> bool:
    return os.path.isfile(os.path.join(run_dir, AUTOLABEL_FILE))


def load_relabels(run_dir: str) -> dict[int, str]:
    """``adjudication/labels.json``: the human label in force per episode."""
    path = os.path.join(run_dir, LABELS_FILE)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return {int(k): str(v["text"]) for k, v in (doc.get("labels") or {}).items()
            if isinstance(v, dict) and str(v.get("text") or "").strip()}


def precomputed_captions(autolabel: dict[int, dict]) -> dict[int, str]:
    """Captions skill_profile reuses: only the ones autolabel actually got (v1's
    ``auto_caps`` keeps non-empty captions only)."""
    return {i: str(line["caption"]) for i, line in autolabel.items()
            if line.get("status") == "ok" and str(line.get("caption") or "").strip()}


class TaskText:
    """Resolves (text, source) per episode; ``missing`` names why there is none."""

    def __init__(self, run_dir: str, instructions: dict[int, str]):
        self.instructions = instructions
        self.autolabel = load_autolabel(run_dir)
        self.relabels = load_relabels(run_dir)

    def resolve(self, episode_index: int) -> tuple[str, str, str | None]:
        """(text, source, problem): ``problem`` is set when the episode has no usable text."""
        idx = int(episode_index)
        if idx in self.relabels:
            return self.relabels[idx], SRC_HUMAN, None
        instruction = self.instructions.get(idx, "")
        if str(instruction or "").strip():
            return judge_text_and_source(instruction, "") + (None,)
        line = self.autolabel.get(idx)
        if line is None:
            return "", SRC_NONE, "no task text: autolabel has not captioned this episode"
        if line.get("status") == "error":
            return "", SRC_NONE, "no task text: the autolabel caption failed"
        caption = str(line.get("caption") or "") if line.get("status") == "ok" else ""
        text, src = judge_text_and_source(instruction, caption)
        return text, src, None

    def delivered(self, episode_index: int) -> dict | None:
        """``task_text`` of a final-list entry: the text the delivery gets and its source."""
        text, src, problem = self.resolve(episode_index)
        if problem is not None:
            return None
        return {"text": text, "source": src}
