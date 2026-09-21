"""Daft 适配器 —— 项目里唯一允许把 core.checks 包成 daft UDF 的桥(DESIGN.md §11.2)。

职责:把 core.contract 注册表里的检查逐个包成 daft UDF;行 ←→ Episode 互转;
CheckSpec.gpus 转译成 daft 资源声明。只用新 API @daft.func(旧 @daft.udf 将废弃)。
客户/自研检查永远写在 contract 上,从不见到本文件。
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np

from ..core import contract

# 检查结果的统一列类型:硬门看 passed,软分看 score,detail 序列化成 JSON 字符串
# (嵌套自由字典无法静态定 schema,JSON 字符串最稳,报告层再解析)


def _result_dtype():
    from daft import DataType

    return DataType.struct({
        "passed": DataType.bool(),
        "score": DataType.float64(),
        "detail": DataType.string(),
    })


def episode_from_row(row: dict[str, Any]) -> contract.Episode:
    """DataFrame 行(dict 视图,列名=ingest.schema 约定)→ Episode。"""
    video = row.get("video") or {}
    return contract.Episode(
        episode_id=row.get("episode_id", ""),
        embodiment_id=row.get("embodiment_id", ""),
        instruction=row.get("instruction", ""),
        action=np.asarray(row["action"]) if row.get("action") is not None else None,
        proprio=(
            {"state": np.asarray(row["proprio_state"])}
            if row.get("proprio_state") is not None else {}
        ),
        video_paths={cam: v["path"] for cam, v in video.items()} if video else {},
        timestamps=(
            {"action": np.asarray(row["timestamps"])}
            if row.get("timestamps") is not None else {}
        ),
        fps=row.get("fps"),
        raw_extras={"video_bounds": video},  # v3 的 from_ts/to_ts 走 extras 带给需要的检查
    )


def result_to_struct(res: contract.CheckResult) -> dict[str, Any]:
    return {
        "passed": res.passed,
        "score": res.score,
        "detail": json.dumps(res.detail, ensure_ascii=False, default=str),
    }


# Episode 检查需要的行列(与 ingest reader 产出的列名一致)
EPISODE_COLUMNS = (
    "episode_id", "embodiment_id", "instruction",
    "action", "proprio_state", "video", "timestamps", "fps",
)


def wrap_check(spec: contract.CheckSpec):
    """CheckSpec → 可调用的 daft 表达式工厂。

    用法:df = df.with_column(f"check_{spec.name}", wrap_check(spec)(df))
    产出列类型 struct{passed, score, detail(json)}。
    """
    import daft
    from daft import col

    @daft.func(return_dtype=_result_dtype())
    def run_check(episode_id, embodiment_id, instruction,
                  action, proprio_state, video, timestamps, fps):
        ep = episode_from_row({
            "episode_id": episode_id, "embodiment_id": embodiment_id,
            "instruction": instruction, "action": action,
            "proprio_state": proprio_state, "video": video,
            "timestamps": timestamps, "fps": fps,
        })
        return result_to_struct(spec.fn(ep))

    def apply(df):
        available = set(df.column_names)
        args = [col(c) if c in available else daft.lit(None) for c in EPISODE_COLUMNS]
        return run_check(*args)

    return apply


def with_checks(df, specs: dict[str, contract.CheckSpec] | None = None):
    """把(默认全部)注册检查加为 check_<name> 列;返回新 DataFrame。"""
    specs = specs if specs is not None else contract.all_checks()
    for name, spec in specs.items():
        df = df.with_column(f"check_{name}", wrap_check(spec)(df))
    return df


def hard_gate_mask(df, specs: dict[str, contract.CheckSpec] | None = None):
    """所有硬门检查的 passed 合取表达式(用于 filter 短路)。"""
    from daft import col, lit

    specs = specs if specs is not None else contract.all_checks()
    mask = lit(True)
    for name, spec in specs.items():
        if spec.gate == "hard":
            mask = mask & col(f"check_{name}")["passed"]
    return mask
