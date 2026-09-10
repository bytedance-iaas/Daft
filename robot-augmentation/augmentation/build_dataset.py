"""把一条增广后的相机视频封回一份完整的 LeRobot v3 数据集(桶到桶复制,只换视频对象)。

    python3 -m augmentation.build_dataset --src tos://.../lerobot_curated --video /tmp/.../xxx_front.mp4 \
        --camera observation.images.front --dst tos://<bucket>/augment/<dataset>/datasets/<name>

前提:视频帧数、帧率、分辨率与原相机一致(assemble 阶段已保证),meta/episodes 的时间戳不用改。
"""
from __future__ import annotations

import argparse
import json

from .tos_io import Tos, TosUrl
from . import video_ops


def build(tos: Tos, src: TosUrl, dst: TosUrl, camera: str, video_path: str, *, expect_frames: int | None = None,
          match_spec: bool = True) -> dict:
    info = video_ops.probe(video_path)
    if expect_frames is not None and info["frames"] != expect_frames:
        raise SystemExit(f"帧数不对:{info['frames']} != {expect_frames}")
    # 与源视频同编码规格(codec / crf / 关键帧间隔 / 像素格式),规格从源 info.json 读,不写死
    spec_rec = None
    if match_spec:
        import os, tempfile
        src_info = json.loads(tos.get_bytes(src.join("meta", "info.json")))
        vinfo = (src_info.get("features", {}).get(camera, {}) or {}).get("info", {})
        spec = video_ops.encode_spec(vinfo)
        tmp = os.path.join(tempfile.gettempdir(), f"spec_{os.path.basename(video_path)}")
        spec_rec = video_ops.transcode(video_path, tmp, spec, fps=int(round(info["fps"])))
        if spec_rec["frames"] != info["frames"]:
            raise SystemExit(f"重编码后帧数变了:{spec_rec['frames']} != {info['frames']}")
        spec_rec["source_video_info"] = vinfo
        video_path = tmp
    prefix = src.key.rstrip("/") + "/"
    replaced, copied = [], 0
    cam_prefix = f"videos/{camera}/"
    for key, size in tos.list(TosUrl(src.bucket, prefix)):
        rel = key[len(prefix):]
        if not rel:
            continue
        out = dst.join(rel)
        if rel.startswith(cam_prefix):
            if rel.endswith(".mp4"):
                tos.upload(video_path, out)
                replaced.append(rel)
            continue
        tos.put_bytes(tos.get_bytes(TosUrl(src.bucket, key)), out)
        copied += 1
    if len(replaced) != 1:
        raise SystemExit(f"期望恰好一个 {camera} 视频文件,实际 {replaced}")
    # 留痕:这份数据集怎么来的
    manifest = {"source": str(src), "camera": camera, "video": video_path, "probe": info, "encode": spec_rec, "copied": copied, "replaced": replaced}
    tos.put_bytes(json.dumps(manifest, ensure_ascii=False, indent=1).encode(), dst.join("augment_manifest.json"))
    return manifest


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--src", required=True)
    p.add_argument("--dst", required=True)
    p.add_argument("--camera", default="observation.images.front")
    p.add_argument("--video", required=True)
    p.add_argument("--expect-frames", type=int)
    p.add_argument("--no-match-spec", action="store_true", help="不按源 info.json 的编码规格重编码,原样上传")
    a = p.parse_args(argv)
    m = build(Tos(), TosUrl.parse(a.src), TosUrl.parse(a.dst), a.camera, a.video, expect_frames=a.expect_frames, match_spec=not a.no_match_spec)
    print(json.dumps(m, ensure_ascii=False))


if __name__ == "__main__":
    main()
