"""The task text each episode is judged and delivered with (design doc 02 §3.4, 06 §4.3).

Two sources, the human one first:

* the original annotation of the dataset (``原始标注``);
* a human relabel applied by ``adjudicate-apply`` replaces it (``人工改标``).

An episode with neither has no task text: task_success does not judge it (D72 - the
platform no longer writes a caption for it), its record says so and the episode goes on
through every other check.
"""
from __future__ import annotations

import json
import os

from .records import ADJUDICATION_DIR

LABELS_FILE = f"{ADJUDICATION_DIR}/labels.json"
SRC_ORIGINAL = "原始标注"
SRC_HUMAN = "人工改标"
SRC_NONE = "无"
#: what a record says instead of a judgement when the episode has no task text (D72)
NO_TASK_TEXT = "no_task_text"


def _labels(run_dir: str) -> dict[int, dict]:
    path = os.path.join(run_dir, LABELS_FILE)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return {int(k): v for k, v in (doc.get("labels") or {}).items()
            if isinstance(v, dict) and str(v.get("text") or "").strip()}


def load_relabels(run_dir: str) -> dict[int, str]:
    """``adjudication/labels.json``: the human label in force per episode."""
    return {i: str(v["text"]) for i, v in _labels(run_dir).items()}


class TaskText:
    """Resolves (text, source) per episode; ``missing`` names why there is none."""

    def __init__(self, run_dir: str, instructions: dict[int, str]):
        self.instructions = instructions
        self.relabels = load_relabels(run_dir)

    def resolve(self, episode_index: int) -> tuple[str, str, str | None]:
        """(text, source, problem): ``problem`` is set when the episode has no task text."""
        idx = int(episode_index)
        if idx in self.relabels:
            return self.relabels[idx], SRC_HUMAN, None
        instruction = self.instructions.get(idx, "")
        if str(instruction or "").strip():
            return str(instruction).strip(), SRC_ORIGINAL, None
        return "", SRC_NONE, "no task text: the dataset has no annotation for this episode"

    def delivered(self, episode_index: int) -> dict | None:
        """``task_text`` of a final-list entry: the text the delivery gets and its source."""
        text, src, problem = self.resolve(episode_index)
        if problem is not None:
            return None
        return {"text": text, "source": src}
