"""Does each video have the resolution it says it has? (FILE-7, design doc 14 §4.6)

Two comparisons, both discrete - a size matches or it does not, no thresholds:

1. **what the dataset declares against what the frames are**: LeRobot's ``meta/info.json`` gives
   every camera's picture size in its video feature's ``shape``, and the decoded frames give the
   size the camera actually recorded. They differ when a capture silently fell back (USB bandwidth
   downshifted, a camera lost its configuration on reconnect) and nobody updated the metadata.
2. **what the container header claims against what the frames are**: a stream header can lie -
   capture stacks have been seen writing one size into the header and another into the bitstream.
   The player believes the header, so a reader that trusts it gets a different picture than a
   reader that decodes.

FILE-7's other half, "the dataset's episodes are not all one resolution", needs no check of its
own here: a LeRobot dataset declares one size per camera, so the episodes that differ from the rest
are exactly the ones comparison 1 reports.

Nothing here reads a file: :func:`declared` reads metadata the module already loaded and
:func:`mismatches` is a pure comparison, so every branch is testable without a video. The measuring
rides the decode the module already does for FILE-4 - no second pass over the bytes.
"""
from __future__ import annotations

from typing import Any

#: channel counts a leading dimension may have in a [c, h, w] shape
_CHANNELS = (1, 3, 4)


def size_of_feature(feature: dict[str, Any] | None) -> tuple[int | None, int | None]:
    """``(height, width)`` out of a video feature's ``shape``, whatever order the axes are in.

    Datasets in the wild write both ``[h, w, c]`` and ``[c, h, w]``. The feature's own ``names``
    decide when they label the axes (LeRobot also writes ``names`` as a dict of groups, the shape
    the reader handles for state and action); otherwise a leading dimension that looks like a
    channel count while the trailing one does not means channel-first. Assuming ``[h, w, c]`` would
    turn every episode of a channel-first dataset into a false mismatch.
    """
    shape = (feature or {}).get("shape")
    if not isinstance(shape, (list, tuple)) or len(shape) < 2:
        return None, None
    try:
        dims = [int(x) for x in shape]
    except (TypeError, ValueError):
        return None, None
    names = (feature or {}).get("names")
    if isinstance(names, dict):
        names = next(iter(names.values()), [])
    if isinstance(names, (list, tuple)) and len(names) == len(dims):
        lowered = [str(n).lower() for n in names]
        if "height" in lowered and "width" in lowered:
            return dims[lowered.index("height")], dims[lowered.index("width")]
    if len(dims) == 3 and dims[0] in _CHANNELS and dims[2] not in _CHANNELS:
        return dims[1], dims[2]                      # channel-first
    return dims[0], dims[1]                          # LeRobot's own order: [h, w, c]


def declared(info: dict[str, Any] | None) -> dict[str, tuple[int, int]]:
    """Per camera key, the ``(height, width)`` the dataset declares; cameras it says nothing
    usable about are left out, so nothing is compared against a guess."""
    features = ((info or {}).get("features") or {})
    out: dict[str, tuple[int, int]] = {}
    for key, feature in features.items() if isinstance(features, dict) else []:
        if not isinstance(feature, dict) or feature.get("dtype") != "video":
            continue
        height, width = size_of_feature(feature)
        if height and width:
            out[str(key)] = (int(height), int(width))
    return out


def mismatches(frame: tuple[int | None, int | None], header: tuple[int | None, int | None],
               said: tuple[int, int] | None) -> list[dict]:
    """What this camera's sizes disagree about: ``[{kind, expected, actual}]``, empty when they
    agree or when there is nothing to compare. ``frame`` / ``header`` / ``said`` are (h, w)."""
    f_h, f_w = frame
    if not f_h or not f_w:
        return []                                    # nothing was measured: no verdict
    out: list[dict] = []
    if said and (int(said[0]), int(said[1])) != (int(f_h), int(f_w)):
        out.append({"kind": "declared", "expected": [int(said[1]), int(said[0])],
                    "actual": [int(f_w), int(f_h)]})
    h_h, h_w = header
    if h_h and h_w and (int(h_h), int(h_w)) != (int(f_h), int(f_w)):
        out.append({"kind": "header", "expected": [int(h_w), int(h_h)],
                    "actual": [int(f_w), int(f_h)]})
    return out


def text(camera: str, found: list[dict]) -> str:
    """One Chinese line naming what disagrees - the message the report and the console show."""
    said = {"declared": "元数据声明", "header": "视频容器头写"}
    parts = [f"{said[m['kind']]} {m['expected'][0]}×{m['expected'][1]}" for m in found
             if m.get("kind") in said]
    actual = found[0]["actual"]
    return f"{camera} 相机实际是 {actual[0]}×{actual[1]}，" + "、".join(parts) + "，对不上"
