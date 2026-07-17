"""通用导出:episode 级 parquet(daft 原生 writer)。

⚠️ write_parquet 到已有目录是追加非覆盖(smoke test 实测)→ 非空目录拒绝。
"""
from __future__ import annotations

import os


def write_episodes_parquet(rows: list[dict], out_dir: str) -> str:
    """幸存 episode 行 → parquet(action/proprio 为 tensor 列,daft 读回无损)。"""
    from ..ingest.lerobot_reader import rows_to_daft

    if os.path.exists(out_dir) and os.listdir(out_dir):
        raise FileExistsError(f"输出目录非空: {out_dir}(daft 追加语义,拒绝)")
    # video struct 列含绝对路径指针,原样保留(交付时指向交付包内相对路径由 lerobot_writer 负责)
    rows_to_daft(rows).write_parquet(out_dir)
    return out_dir
