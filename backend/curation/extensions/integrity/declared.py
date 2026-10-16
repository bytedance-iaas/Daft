"""What an episode's data lacks, or holds against the dataset's declaration (design doc 29 §1-§2, D95-D96).

v1's readers stop at the first thing they cannot read: a column info.json declares that the parquet does not have
(``KeyError``), a frame without a value or of another width (``np.stack``'s ``ValueError``), a parquet or a LeRobot v3
slice whose length is not the episode table's (``ValueError`` / ``AssertionError``), an mcap without the topics its
mapping requires (``NotADatasetError``). Those were execution errors, held for a retry that never helps. Here the same
data is looked at against the declaration and what is wrong is said as problems - plain dicts (``code``, ``message``,
``args``) that data integrity reports as its findings; the codes are the registry's (8.4).
"""
from __future__ import annotations

import re
from collections import Counter

#: the columns v1's readers stack into arrays (``action`` always, ``observation.state`` when declared)
VECTOR_COLUMNS = ("action", "observation.state")
#: the columns v1's readers read whatever info.json says
READ_ALWAYS = ("action", "timestamp")


def problem(code: str, message: str, **args) -> dict:
    return {"code": code, "message": message, "args": args}


def features(info: dict | None) -> dict:
    feats = (info or {}).get("features")
    return feats if isinstance(feats, dict) else {}


def declared_columns(info: dict | None) -> list[str]:
    """The features info.json declares that live in the data parquet: every one but the videos."""
    return [k for k, v in features(info).items() if not (isinstance(v, dict) and v.get("dtype") == "video")]


def declared_width(info: dict | None, column: str) -> int | None:
    """The per-frame width info.json declares for a vector column (``shape: [7]``), else None."""
    spec = features(info).get(column)
    shape = spec.get("shape") if isinstance(spec, dict) else None
    if isinstance(shape, (list, tuple)) and len(shape) == 1 and isinstance(shape[0], int) and shape[0] > 0:
        return int(shape[0])
    return None


def missing_column(column: str, where: str, info: dict | None) -> dict:
    why = "info.json 声明了" if column in features(info) else "读数据要用它"
    code = "action_missing" if column == "action" else "column_missing"
    return problem(code, f"{where}里没有 {column} 列（{why}）", column=column)


def missing_columns(have, info: dict | None, where: str) -> list[dict]:
    """The columns the declaration (and v1's reader) needs that ``have`` lacks."""
    have = set(have)
    wanted = list(dict.fromkeys(list(READ_ALWAYS) + declared_columns(info)))
    return [missing_column(c, where, info) for c in wanted if c not in have]


def width_problem(column: str, width: int, info: dict | None) -> dict | None:
    """D96: every frame of ``column`` is ``width`` wide, info.json says otherwise."""
    want = declared_width(info, column)
    if want is None or width == want:
        return None
    return problem("values_invalid", f"{column} 每帧 {width} 维，info.json 声明 {want} 维",
                   column=column, width=width, declared=want)


def frames_text(index: list[int]) -> str:
    """``[3, 71, 72, …, 80]`` -> ``帧 3、71–80`` (three runs at most)."""
    runs: list[list[int]] = []
    for i in sorted(index):
        if runs and i == runs[-1][1] + 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    shown = [str(a) if a == b else f"{a}–{b}" for a, b in runs[:3]]
    return "帧 " + "、".join(shown) + ("…" if len(runs) > 3 else "")


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and v != v)


def _width(v) -> int | None:
    try:
        return len(v)
    except TypeError:                   # a scalar: v1's validation says what is wrong with it
        return None


def _vector(column: str, values: list, info: dict | None) -> dict | None:
    empty = [i for i, v in enumerate(values) if _blank(v) or _width(v) == 0]
    if empty:
        return problem("values_invalid", f"{column} 有 {len(empty)} 帧为空（{frames_text(empty)}）",
                       column=column, empty_frames=len(empty))
    widths = Counter(w for w in (_width(v) for v in values) if w is not None)
    if len(widths) > 1:
        parts = "、".join(f"{w} 维 {n} 帧" for w, n in sorted(widths.items(), key=lambda x: (-x[1], x[0])))
        return problem("values_invalid", f"{column} 各帧维度不一：{parts}", column=column,
                       widths={str(w): n for w, n in sorted(widths.items())})
    if widths:
        return width_problem(column, next(iter(widths)), info)
    return None


def frame_problems(frames, info: dict | None, length: int | None) -> list[dict]:
    """One episode's rows (a DataFrame: a LeRobot v2 parquet, or the episode's slice of a v3 one) against the
    declaration and the episode table's ``length``."""
    out = missing_columns(frames.columns, info, "数据")
    have = set(frames.columns)
    if length is not None and len(frames) != int(length):
        out.append(problem("length_mismatch", f"数据有 {len(frames)} 帧，episode 表记 {int(length)} 帧",
                           rows=len(frames), length=int(length)))
    for col in VECTOR_COLUMNS:
        if col in have and (col == "action" or col in features(info)):
            found = _vector(col, frames[col].tolist(), info)
            if found:
                out.append(found)
    if "timestamp" in have:
        empty = [i for i, v in enumerate(frames["timestamp"].tolist()) if _blank(v)]
        if empty:
            out.append(problem("timestamps_invalid", f"timestamp 有 {len(empty)} 帧为空（{frames_text(empty)}）",
                               empty_frames=len(empty)))
    return out


def v3_episode_problems(df, ep, info: dict | None) -> list[dict]:
    """One episode of a LeRobot v3 data file, sliced the way v1 slices it (``_rows_v3_group``: by the file's own
    ``index`` and the episode table's ``dataset_from_index`` / ``dataset_to_index``)."""
    if "index" not in df.columns:
        return [missing_column("index", "数据文件", info)]
    base = int(df["index"].iloc[0]) if len(df) else 0
    lo, hi = int(ep["dataset_from_index"]), int(ep["dataset_to_index"])
    rows = df.iloc[max(lo - base, 0):max(hi - base, 0)]
    if not len(rows) and "episode_index" in df.columns:
        # D101 (design doc 30 §4): the table's range misses the file's index while the file holds the episode's rows -
        # the index and the reference disagree (FILE-9), the episode is not short
        own = df.loc[df["episode_index"] == int(ep["episode_index"]), "index"]
        if len(own):
            first, last = int(own.min()), int(own.max())
            return [problem("index_range_invalid",
                            f"episode 表写的行区间是 [{lo}, {hi})，数据文件里这条的 index 是 {first}–{last}，对不上",
                            table_from=lo, table_to=hi, file_first=first, file_last=last)]
    return frame_problems(rows, info, int(ep["length"]))


#: v1's mcap reader (``mcap_reader._require_topics``) says which topics an episode lacks; its words are A-class
_REQUIRED = "mcap 里找不到必需 topic"


def mcap_problems(error: BaseException) -> list[dict]:
    """The topics an mcap episode lacks, from v1's reader's own words; [] for anything else - also for a topic that
    is there but whose fields the mapping cannot read (the mapping is wrong, for every episode alike)."""
    text = str(error).split("\n", 1)[0]
    if _REQUIRED not in text:
        return []
    items = text.split(_REQUIRED, 1)[1]
    if "topic 存在但" in items or re.search(r"动作 '", items):
        return []
    out = [problem("action_missing", f"缺动作 topic {t}（这条 mcap 里没有它）", topic=t)
           for t in re.findall(r"动作来源 '([^']*)'", items)]
    m = re.search(r"视频 '([^']*)'\*", items)
    if m:
        out.append(problem("video_missing", "没有一路相机的视频 topic", prefix=m.group(1)))
    return out
