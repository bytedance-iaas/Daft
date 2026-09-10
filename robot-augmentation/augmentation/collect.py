"""把一次跑批的产物收进评审目录(本地工作目录或 TOS 都行),再生成评审页。

    python3 -m augmentation.collect --run <AUG_WORK>/final/<name> --dir <评审目录>
    python3 -m augmentation.collect --run tos://<bucket>/augment/<dataset>/final/<name> --dir <评审目录>

收的东西:<name>_<cam>.mp4 → <name>.mp4,<name>_triptych.mp4,run.json → <name>_run.json;
评审目录里还没有 original.mp4 时顺带把源视频(run.json 里记的 lerobot_src + camera)放进去。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil

from . import config
from .tos_io import Tos, TosUrl


_TOS: Tos | None = None


def _get(src: str, dst: str) -> None:
    """本地路径直接拷;tos:// 才建 TOS 客户端(本地评审不需要 TOS 凭证)。"""
    global _TOS
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if src.startswith("tos://"):
        if _TOS is None:
            _TOS = Tos()
        _TOS.download(TosUrl.parse(src), dst)
    else:
        shutil.copyfile(src, dst)


def collect(run: str, d: str, *, page: bool = True) -> dict:
    j = lambda *p: run.rstrip("/") + "/" + "/".join(p) if run.startswith("tos://") else os.path.join(run, *p)
    rj = os.path.join(d, "_run.json.tmp")
    _get(j("run.json"), rj)
    rec = json.load(open(rj))
    name = rec["name"]
    cam = rec.get("camera", config.camera()).split(".")[-1]
    os.replace(rj, os.path.join(d, f"{name}_run.json"))
    _get(j(f"{name}_{cam}.mp4"), os.path.join(d, f"{name}.mp4"))
    try:
        _get(j(f"{name}_triptych.mp4"), os.path.join(d, f"{name}_triptych.mp4"))
    except Exception:
        pass
    orig = os.path.join(d, "original.mp4")
    if not os.path.exists(orig) and rec.get("lerobot_src"):
        src_root = rec["lerobot_src"]
        rel = config.video_rel_path(rec.get("camera", config.camera()))
        _get(str(TosUrl.parse(src_root).join(rel)) if src_root.startswith("tos://") else os.path.join(src_root, rel), orig)
    out = {"name": name, "dir": d}
    if page:
        from .review_page import build
        out["versions"] = build(d, os.path.join(d, "review.html"))
    return out


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True, help="跑批产物目录:本地 <AUG_WORK>/final/<name> 或 tos://.../final/<name>")
    p.add_argument("--dir", required=True, help="评审目录")
    p.add_argument("--no-page", action="store_true")
    a = p.parse_args(argv)
    print(json.dumps(collect(a.run, a.dir, page=not a.no_page), ensure_ascii=False))


if __name__ == "__main__":
    main()
