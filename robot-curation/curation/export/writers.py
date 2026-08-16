"""通用导出:episode 级 parquet(daft 原生 writer)。

⚠️ write_parquet 到已有目录是追加非覆盖(smoke test 实测)→ 非空目录拒绝。
"""
from __future__ import annotations

import os


def write_episodes_parquet(rows: list[dict], out_dir: str) -> str:
    """幸存 episode 行 → parquet(action/proprio 为 tensor 列,daft 读回无损)。"""
    from ..ingest.lerobot_reader import rows_to_daft
    from .safe_write import delivery_dir

    # 非空拒绝的行为保留:重跑同一输出目录该显式清理,不许静默清掉上一份交付。
    # (daft 追加语义那个坑现在由 staging 挡掉 —— 它永远是新建的空目录。)
    if os.path.exists(out_dir) and os.listdir(out_dir):
        raise FileExistsError(f"输出目录非空: {out_dir}(不覆盖已有交付,拒绝)")
    # video struct 列含绝对路径指针,原样保留(交付时指向交付包内相对路径由 lerobot_writer 负责)
    # 先写同级 staging 再原子换位:交付路径上不再存在"目录已经在那儿、数据还不全"
    # 的中间态(daft 多文件写目录,写一半被打断就是这种形状)。
    with delivery_dir(out_dir) as staging:
        rows_to_daft(rows).write_parquet(staging)
    return out_dir
