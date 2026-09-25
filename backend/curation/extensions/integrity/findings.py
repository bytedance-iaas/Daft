"""What the data integrity module finds, and what each finding means (design doc 14 §4).

A finding is ``reject`` (the episode is broken: the gate fails it), ``suspect`` (a person
looks: the episode stays in passed and gets an 完整性存疑 card) or ``dataset`` (a fact
about the dataset that concerns no single episode; it goes to ``dataset.json`` and the
report only). Codes are the contract with the report and the console, which translate
them; ``message`` is Chinese and already names the file and the camera.

A file problem with a position (a truncation, a zeroed block) carries the time span it
covers (``span``, seconds on the file's own time axis): in LeRobot v3 one file holds
several episodes, and only those whose window overlaps the span are affected (§4.3).
"""
from __future__ import annotations

from dataclasses import dataclass, field

REJECT, SUSPECT, DATASET = "reject", "suspect", "dataset"

#: code -> (level, 中文名); the order is the report's
CODES: dict[str, tuple[str, str]] = {
    "file_empty": (REJECT, "文件为空或过小"),
    "file_truncated": (REJECT, "文件被截断"),
    "zero_filled": (REJECT, "文件里有成块的零填充"),
    "structure_invalid": (REJECT, "文件结构损坏"),
    "crc_mismatch": (REJECT, "CRC 校验不符"),
    "row_invalid": (REJECT, "数据不合规"),
    "decode_failed": (REJECT, "视频解码失败"),
    "count_mismatch": (SUSPECT, "帧数与记录的长度对不上"),
    "cut_off": (SUSPECT, "录制中断"),
    "duplicate_content": (SUSPECT, "与另一条的文件完全相同"),
    "stream_missing": (SUSPECT, "缺一路其他条都有的流"),
    "rate_outlier": (SUSPECT, "某路流的频率明显偏低"),
    "decode_concealed": (SUSPECT, "解码器掩盖了错误"),
    "table_inconsistent": (SUSPECT, "episode 表前后不一致"),
    "orphan_files": (DATASET, "不属于任何 episode 的文件"),
    "dark_camera": (DATASET, "近乎全黑的相机"),
    "table_overlap": (DATASET, "episode 表的帧区间重叠或不连续"),
}

_RANK = {REJECT: 0, SUSPECT: 1, DATASET: 2}


@dataclass
class Finding:
    code: str
    message: str
    tier: str                               # dataset | L1 | L2 | L3 | peers
    file: str | None = None
    camera: str | None = None
    args: dict = field(default_factory=dict)
    span: tuple[float, float] | None = None  # seconds in the file; None: the whole file
    level: str = ""

    def __post_init__(self) -> None:
        if not self.level:
            self.level = CODES[self.code][0]

    def to_json(self) -> dict:
        out = {"level": self.level, "code": self.code, "tier": self.tier,
               "message": self.message}
        if self.file:
            out["file"] = self.file
        if self.camera:
            out["camera"] = self.camera
        if self.args:
            out["args"] = dict(self.args)
        if self.span is not None:
            out["span_s"] = [round(self.span[0], 3),
                             None if self.span[1] == float("inf") else round(self.span[1], 3)]
        return out


def affects(f: Finding, window: tuple[float, float] | None) -> bool:
    """Whether a file finding concerns the episode whose part of the file is ``window``."""
    if f.span is None or window is None:
        return True
    return f.span[0] < window[1] and window[0] < f.span[1]


def ordered(findings: list[Finding]) -> list[Finding]:
    order = list(CODES)
    return sorted(findings, key=lambda f: (_RANK[f.level], order.index(f.code)))


def outcome(findings: list[Finding]) -> tuple[bool | None, str]:
    """(passed, reason): any reject -> False; only suspects -> None; nothing -> True."""
    per_episode = [f for f in findings if f.level in (REJECT, SUSPECT)]
    if not per_episode:
        return True, ""
    first = ordered(per_episode)[0]
    passed = False if first.level == REJECT else None
    more = len(per_episode) - 1
    reason = first.message + (f"（另有 {more} 项发现）" if more else "")
    if passed is None:
        reason = "需要人工裁决：" + reason
    return passed, reason
