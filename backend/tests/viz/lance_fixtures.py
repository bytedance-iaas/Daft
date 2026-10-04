"""The ``make_v3`` dataset converted to Lance the way each lerobot-lancedb release does (design doc 19 §4.2).

``0.3`` - ``lerobot-lance-convert`` (0.3.x): ``frames.lance`` (one row per frame in ``index`` order, dots in the
feature keys turned into underscores, vectors as fixed-size lists, language columns kept nested), ``videos.lance``
(one row per source mp4: ``video_key`` / ``chunk_index`` / ``file_index``, the byte-index columns and the bytes in a
blob v2 column), ``meta.lance`` (``path`` / ``data`` of every ``meta/`` file) and ``meta/`` with
``storage_format: lance`` stamped in ``info.json``.

``0.2-video`` - ``lerobot-convert-to-lance-video`` (0.1-0.2): ``<name>.lance`` (numeric features only, as float32
fixed-size lists, no camera columns) + ``<name>_videos.lance`` (blob v1: ``large_binary`` with
``lance-encoding:blob``) + ``meta/``.

``0.2-frames`` - ``lerobot-convert-to-lance`` (0.1-0.2): ``<name>.lance`` with one JPEG per frame in a ``binary``
column per camera + ``meta/``.
"""
from __future__ import annotations

import io
import json
import os
import shutil

from .fixtures import make_v3

NAME = "lift_cup"
CAMERA = "observation.images.top"


def _moov(path: str) -> tuple[int, int]:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        offset = 0
        while offset < size:
            fh.seek(offset)
            head = fh.read(16)
            box, kind = int.from_bytes(head[:4], "big"), head[4:8]
            if box == 1:
                box = int.from_bytes(head[8:16], "big")
            elif box == 0:
                box = size - offset
            if kind == b"moov":
                return offset, box
            offset += box
    raise ValueError(f"no moov box in {path}")


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
    moov_offset, moov_size = _moov(path)
    return {"file_size": os.path.getsize(path), "moov_offset": moov_offset, "moov_size": moov_size,
            "kf_indices": [i for i, _ in kf], "kf_positions": [p for _, p in kf]}


def _jpegs(path: str) -> list[bytes]:
    import av

    out = []
    with av.open(path) as container:
        for frame in container.decode(video=0):
            buf = io.BytesIO()
            frame.to_image().save(buf, format="JPEG", quality=90)
            out.append(buf.getvalue())
    return out


def _numeric_frames(table, with_images: dict | None = None):
    """0.1-0.2 frames schema: the five index columns, then every numeric feature as a float32 fixed-size list."""
    import numpy as np
    import pyarrow as pa

    n = table.num_rows
    cols = {
        "episode_index": pa.array(table.column("episode_index").to_numpy().astype(np.int32)),
        "frame_index": pa.array(table.column("frame_index").to_numpy().astype(np.int32)),
        "index": table.column("index").combine_chunks(),
        "timestamp": table.column("timestamp").combine_chunks(),
        "task_index": pa.array(table.column("task_index").to_numpy().astype(np.int32)),
        "subtask_index": pa.array(table.column("subtask_index").to_numpy().astype(np.int32)),
    }
    for key in ("observation.state", "action"):
        values = np.asarray(table.column(key).to_pylist(), np.float32)
        cols[key.replace(".", "_")] = pa.FixedSizeListArray.from_arrays(pa.array(values.reshape(-1)), values.shape[1])
    for key, frames in (with_images or {}).items():
        assert len(frames) == n, (len(frames), n)
        cols[key.replace(".", "_")] = pa.array(frames, pa.binary())
    return pa.table(cols)


def make_lance(root: str, layout: str = "0.3") -> str:
    import lance
    import pyarrow as pa
    import pyarrow.parquet as pq

    src = make_v3(root.rstrip("/") + "_src")
    os.makedirs(root, exist_ok=True)
    shutil.copytree(os.path.join(src, "meta"), os.path.join(root, "meta"))
    data = pq.read_table(os.path.join(src, "data", "chunk-000", "file-000.parquet"))
    mp4 = os.path.join(src, "videos", CAMERA, "chunk-000", "file-000.mp4")
    if layout == "0.3":
        info_path = os.path.join(root, "meta", "info.json")
        with open(info_path) as fh:
            info = json.load(fh)
        info["storage_format"] = "lance"
        with open(info_path, "w") as fh:
            json.dump(info, fh, indent=4)
        lance.write_dataset(data.rename_columns([c.replace(".", "_") for c in data.column_names]),
                            os.path.join(root, "frames.lance"))
        with open(mp4, "rb") as fh:
            blob = fh.read()
        schema = pa.schema([pa.field("video_key", pa.string()), pa.field("chunk_index", pa.int64()),
                            pa.field("file_index", pa.int64()), pa.field("file_size", pa.int64()),
                            pa.field("moov_offset", pa.int64()), pa.field("moov_size", pa.int64()),
                            pa.field("kf_indices", pa.list_(pa.int64())), pa.field("kf_positions", pa.list_(pa.int64())),
                            lance.blob_field("video_bytes")])
        index = _byte_index(mp4)
        row = {"video_key": [CAMERA], "chunk_index": [0], "file_index": [0],
               **{k: [v] for k, v in index.items()}, "video_bytes": lance.blob_array([blob])}
        lance.write_dataset(pa.table(row, schema=schema), os.path.join(root, "videos.lance"), data_storage_version="2.2")
        meta_files = sorted(os.path.relpath(os.path.join(d, f), os.path.join(root, "meta"))
                            for d, _, fs in os.walk(os.path.join(root, "meta")) for f in fs)
        payloads = []
        for rel in meta_files:
            with open(os.path.join(root, "meta", rel), "rb") as fh:
                payloads.append(fh.read())
        lance.write_dataset(pa.table({"path": pa.array(meta_files, pa.string()), "data": pa.array(payloads, pa.large_binary())}),
                            os.path.join(root, "meta.lance"))
    elif layout == "0.2-video":
        lance.write_dataset(_numeric_frames(data), os.path.join(root, f"{NAME}.lance"))
        with open(mp4, "rb") as fh:
            blob = fh.read()
        schema = pa.schema([pa.field("video_key", pa.string()), pa.field("chunk_index", pa.int32()),
                            pa.field("file_index", pa.int32()),
                            pa.field("video_bytes", pa.large_binary(), metadata={b"lance-encoding:blob": b"true"})])
        lance.write_dataset(pa.table({"video_key": [CAMERA], "chunk_index": [0], "file_index": [0], "video_bytes": [blob]},
                                     schema=schema), os.path.join(root, f"{NAME}_videos.lance"))
    elif layout == "0.2-frames":
        lance.write_dataset(_numeric_frames(data, {CAMERA: _jpegs(mp4)}), os.path.join(root, f"{NAME}.lance"))
    else:
        raise ValueError(layout)
    shutil.rmtree(src)
    return root
