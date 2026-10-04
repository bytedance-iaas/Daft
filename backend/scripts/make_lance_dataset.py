"""A local LeRobot dataset (v2.1 or v3.0) in one of the lerobot-lancedb layouts, for the visualizer's Lance
checks (design doc 19 §4.2). Not a production converter - that is lerobot-lance-convert; this one writes the
same tables the three releases do, from a dataset on disk, without lerobot installed.

    ../.venv/bin/python scripts/make_lance_dataset.py $L/inputs/drift_3x720p $L/inputs/lance_drift --layout 0.3

A v2.1 source gets the v3.0 metadata the converters expect (an episode table with each episode's row window
and, per camera, its own mp4 as file ``episode_index`` of chunk 0); a v3.0 source keeps its own.
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import shutil


def _v3_meta(src: str, out: str) -> dict:
    """``out/meta`` as LeRobot v3.0, from a v2.1 or v3.0 ``src``; the info.json."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    with open(os.path.join(src, "meta", "info.json")) as fh:
        info = json.load(fh)
    if str(info.get("codebase_version", "")).startswith("v3"):
        shutil.copytree(os.path.join(src, "meta"), os.path.join(out, "meta"))
        return info
    os.makedirs(os.path.join(out, "meta", "episodes", "chunk-000"), exist_ok=True)
    fps = float(info["fps"])
    cams = [k for k, f in info["features"].items() if f.get("dtype") == "video"]
    rows, cursor = [], 0
    with open(os.path.join(src, "meta", "episodes.jsonl")) as fh:
        eps = [json.loads(line) for line in fh if line.strip()]
    for e in sorted(eps, key=lambda r: r["episode_index"]):
        n = int(e["length"])
        row = {"episode_index": e["episode_index"], "tasks": e.get("tasks") or [], "length": n,
               "data/chunk_index": 0, "data/file_index": 0,
               "dataset_from_index": cursor, "dataset_to_index": cursor + n}
        for k in cams:
            row.update({f"videos/{k}/chunk_index": 0, f"videos/{k}/file_index": e["episode_index"],
                        f"videos/{k}/from_timestamp": 0.0, f"videos/{k}/to_timestamp": round(n / fps, 6)})
        rows.append(row)
        cursor += n
    pq.write_table(pa.Table.from_pylist(rows), os.path.join(out, "meta", "episodes", "chunk-000", "file-000.parquet"))
    tasks = [json.loads(line) for line in open(os.path.join(src, "meta", "tasks.jsonl")) if line.strip()]
    pq.write_table(pa.table({"task_index": [t["task_index"] for t in tasks], "task": [t["task"] for t in tasks]}),
                   os.path.join(out, "meta", "tasks.parquet"))
    info = {**info, "codebase_version": "v3.0",
            "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
            "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"}
    with open(os.path.join(out, "meta", "info.json"), "w") as fh:
        json.dump(info, fh, indent=1)
    return info


def _frames(src: str):
    """Every frame of ``src`` in ``index`` order, as one Arrow table."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    files = sorted(glob.glob(os.path.join(src, "data", "**", "*.parquet"), recursive=True))
    table = pa.concat_tables([pq.read_table(f) for f in files], promote_options="default")
    return table.sort_by("index")


def _videos(src: str, info: dict) -> list[tuple[str, int, int, str]]:
    """(video_key, chunk, file, path) of every mp4 the episode table points at."""
    v3 = str(info.get("codebase_version", "")).startswith("v3")
    out = []
    for path in sorted(glob.glob(os.path.join(src, "videos", "**", "*.mp4"), recursive=True)):
        parts = os.path.relpath(path, os.path.join(src, "videos")).split(os.sep)
        if v3:                                   # videos/<key>/chunk-NNN/file-MMM.mp4
            key, chunk, file = parts[0], int(parts[1].split("-")[1]), int(parts[2].split("-")[1].split(".")[0])
        else:                                    # videos/chunk-NNN/<key>/episode_NNNNNN.mp4
            key, chunk, file = parts[1], 0, int(parts[2].split("_")[1].split(".")[0])
        out.append((key, chunk, file, path))
    return out


def _byte_index(path: str) -> dict:
    import av

    kf = []
    with av.open(path) as container:
        stream = container.streams.video[0]
        fps = float(stream.average_rate)
        for packet in container.demux(stream):
            if packet.pts is not None and packet.is_keyframe and packet.pos is not None:
                kf.append((round(float(packet.pts * packet.time_base) * fps), packet.pos))
    kf.sort()
    size = os.path.getsize(path)
    moov = (0, 0)
    with open(path, "rb") as fh:
        off = 0
        while off < size:
            fh.seek(off)
            head = fh.read(16)
            box, kind = int.from_bytes(head[:4], "big"), head[4:8]
            box = int.from_bytes(head[8:16], "big") if box == 1 else (size - off if box == 0 else box)
            if kind == b"moov":
                moov = (off, box)
                break
            off += box
    return {"file_size": size, "moov_offset": moov[0], "moov_size": moov[1],
            "kf_indices": [i for i, _ in kf], "kf_positions": [p for _, p in kf]}


def _jpegs(path: str) -> list[bytes]:
    import av

    out = []
    with av.open(path) as container:
        for frame in container.decode(video=0):
            buf = io.BytesIO()
            frame.to_image().save(buf, format="JPEG", quality=85)
            out.append(buf.getvalue())
    return out


def main() -> None:
    import lance
    import numpy as np
    import pyarrow as pa

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--layout", choices=["0.3", "0.2-video", "0.2-frames"], default="0.3")
    ap.add_argument("--name", default="dataset", help="the 0.1-0.2 table name (<name>.lance)")
    a = ap.parse_args()
    if os.path.exists(a.out):
        raise SystemExit(f"{a.out} exists")
    with open(os.path.join(a.src, "meta", "info.json")) as fh:
        info_src = json.load(fh)
    os.makedirs(a.out)
    info = _v3_meta(a.src, a.out)
    frames = _frames(a.src)
    videos = _videos(a.src, info_src)
    cams = [k for k, f in info["features"].items() if f.get("dtype") == "video"]
    if a.layout == "0.3":
        info["storage_format"] = "lance"
        with open(os.path.join(a.out, "meta", "info.json"), "w") as fh:
            json.dump(info, fh, indent=1)
        lance.write_dataset(frames.rename_columns([c.replace(".", "_") for c in frames.column_names]),
                            os.path.join(a.out, "frames.lance"))
        schema = pa.schema([pa.field("video_key", pa.string()), pa.field("chunk_index", pa.int64()),
                            pa.field("file_index", pa.int64()), pa.field("file_size", pa.int64()),
                            pa.field("moov_offset", pa.int64()), pa.field("moov_size", pa.int64()),
                            pa.field("kf_indices", pa.list_(pa.int64())), pa.field("kf_positions", pa.list_(pa.int64())),
                            lance.blob_field("video_bytes")])
        rows = {f.name: [] for f in schema}
        blobs = []
        for key, chunk, file, path in videos:
            idx = _byte_index(path)
            for k, v in {"video_key": key, "chunk_index": chunk, "file_index": file, **idx}.items():
                rows[k].append(v)
            with open(path, "rb") as fh:
                blobs.append(fh.read())
        rows["video_bytes"] = lance.blob_array(blobs)
        lance.write_dataset(pa.table(rows, schema=schema), os.path.join(a.out, "videos.lance"), data_storage_version="2.2")
        meta = sorted(os.path.relpath(os.path.join(d, f), os.path.join(a.out, "meta"))
                      for d, _, fs in os.walk(os.path.join(a.out, "meta")) for f in fs)
        data = [open(os.path.join(a.out, "meta", m), "rb").read() for m in meta]
        lance.write_dataset(pa.table({"path": pa.array(meta, pa.string()), "data": pa.array(data, pa.large_binary())}),
                            os.path.join(a.out, "meta.lance"))
    else:
        cols = {"episode_index": pa.array(frames.column("episode_index").to_numpy().astype(np.int32)),
                "frame_index": pa.array(frames.column("frame_index").to_numpy().astype(np.int32)),
                "index": frames.column("index").combine_chunks(),
                "timestamp": pa.array(frames.column("timestamp").to_numpy().astype(np.float32)),
                "task_index": pa.array(frames.column("task_index").to_numpy().astype(np.int32))}
        for key, f in info["features"].items():
            name = key.replace(".", "_")
            if name in cols or f.get("dtype") in ("video", "image", "string", "language") or key not in frames.column_names:
                continue
            values = np.asarray(frames.column(key).to_pylist(), np.float32).reshape(frames.num_rows, -1)
            cols[name] = pa.FixedSizeListArray.from_arrays(pa.array(values.reshape(-1)), values.shape[1])
        if a.layout == "0.2-frames":
            for key in cams:
                pics = []
                for _, _, _, path in sorted((v for v in videos if v[0] == key), key=lambda v: (v[1], v[2])):
                    pics += _jpegs(path)
                cols[key.replace(".", "_")] = pa.array(pics[:frames.num_rows], pa.binary())
        lance.write_dataset(pa.table(cols), os.path.join(a.out, f"{a.name}.lance"))
        if a.layout == "0.2-video":
            schema = pa.schema([pa.field("video_key", pa.string()), pa.field("chunk_index", pa.int32()),
                                pa.field("file_index", pa.int32()),
                                pa.field("video_bytes", pa.large_binary(), metadata={b"lance-encoding:blob": b"true"})])
            data = []
            for _, _, _, path in videos:
                with open(path, "rb") as fh:
                    data.append(fh.read())
            lance.write_dataset(pa.table({"video_key": [v[0] for v in videos], "chunk_index": [v[1] for v in videos],
                                          "file_index": [v[2] for v in videos], "video_bytes": data}, schema=schema),
                                os.path.join(a.out, f"{a.name}_videos.lance"))
    print(a.out)


if __name__ == "__main__":
    main()
