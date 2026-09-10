"""并排对比视频:几路视频逐帧同步拼成一行,每格左上角写标签;评审页会把 *_compare.mp4 放在顶部。

    python3 -m augmentation.compare --out <评审目录>/<题目>_compare.mp4 original=<评审目录>/original.mp4 v3=<...>/v3.mp4 v4=<...>/v4.mp4
"""
from __future__ import annotations

import argparse

from . import video_ops


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--panel-w", type=int, default=640)
    p.add_argument("items", nargs="+", help="标签=视频路径,按给定顺序从左到右")
    a = p.parse_args(argv)
    items = [tuple(x.split("=", 1)) for x in a.items]
    n = video_ops.side_by_side(items, a.out, fps=a.fps, panel_w=a.panel_w)
    print(a.out, n, "frames")


if __name__ == "__main__":
    main()
