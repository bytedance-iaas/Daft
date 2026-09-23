"""Synthetic LeRobot v2 dataset for offline parity and integration tests.

The dataset is small (8 episodes, two cameras, a few seconds each) but walks
the paths the parity tools must see:

* ep 2 has a 0.6 s timestamp jump      -> the timestamp hard gate drops it;
* ep 5 is a 0.4 s fragment             -> dropped as a fragment;
* ep 7 is a byte-for-byte copy of ep 3 -> removed by dedup;
* eps 4 and 6 have no task text        -> captioned by the model first.

Videos are real H.264 files, so decoding, optical flow and frame sampling run
exactly as on customer data. The square in each frame moves with joint 0, so
the video-action sync check has a signal to lock onto.
"""
from __future__ import annotations

import json
import os
import shutil

import numpy as np

FPS = 15
CAMERAS = ("observation.images.exterior", "observation.images.wrist")
WIDTH, HEIGHT = 128, 96
DOF = 7
TASKS = ["pick up the red block and place it in the bin", ""]
#: episode -> (length in frames, task index)
EPISODES = {0: (75, 0), 1: (90, 0), 2: (75, 0), 3: (60, 0), 4: (81, 1),
            5: (6, 0), 6: (69, 1)}
DUPLICATE_OF = {7: 3}


def _joint_track(length: int, ep: int) -> np.ndarray:
    """Smooth joint trajectory inside Franka limits; varies per episode."""
    t = np.arange(length) / FPS
    phase = 0.7 * ep
    base = np.array([0.0, 0.2, 0.0, -1.8, 0.0, 1.6, 0.0])
    amp = np.array([0.6, 0.25, 0.3, 0.3, 0.3, 0.3, 0.4])
    freq = np.array([0.35, 0.5, 0.4, 0.45, 0.3, 0.55, 0.25])
    q = base + amp * np.sin(2 * np.pi * freq * t[:, None] + phase)
    return q.astype(np.float32)


def _frames(q: np.ndarray, camera: str, ep: int) -> np.ndarray:
    """Checkerboard background plus a square that follows joint 0."""
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    board = (((yy // 8) + (xx // 8)) % 2 * 60 + 90).astype(np.uint8)
    out = np.empty((len(q), HEIGHT, WIDTH, 3), dtype=np.uint8)
    lo, hi = float(q[:, 0].min()), float(q[:, 0].max())
    span = max(hi - lo, 1e-6)
    for i, row in enumerate(q):
        img = np.repeat(board[:, :, None], 3, axis=2).copy()
        pos = (float(row[0]) - lo) / span
        if camera.endswith("wrist"):
            cy, cx = int(12 + pos * (HEIGHT - 36)), WIDTH // 2 - 12
            color = (40, 160, 220)
        else:
            cy, cx = HEIGHT // 2 - 12, int(12 + pos * (WIDTH - 36))
            color = (220, 50, 40)
        img[cy:cy + 24, cx:cx + 24] = color
        img[2:6, 2 + (ep * 7) % 100: 10 + (ep * 7) % 100] = 255   # per-episode mark
        out[i] = img
    return out


def _write_video(path: str, frames: np.ndarray) -> None:
    import av

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with av.open(path, mode="w") as container:
        stream = container.add_stream("libx264", rate=FPS)
        stream.width, stream.height = WIDTH, HEIGHT
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": "18", "preset": "veryfast", "threads": "1"}
        for img in frames:
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def make_mini_lerobot(root: str, *, overwrite: bool = False) -> str:
    """Write the dataset under ``root`` and return the path."""
    import pandas as pd

    if os.path.exists(os.path.join(root, "meta", "info.json")) and not overwrite:
        return root
    if overwrite and os.path.exists(root):
        shutil.rmtree(root)
    n_eps = len(EPISODES) + len(DUPLICATE_OF)
    lengths = {ep: EPISODES[ep][0] for ep in EPISODES}
    lengths.update({dup: EPISODES[src][0] for dup, src in DUPLICATE_OF.items()})
    names = [f"joint_{i}" for i in range(DOF)]
    info = {
        "codebase_version": "v2.1",
        "robot_type": "franka",
        "total_episodes": n_eps,
        "total_frames": int(sum(lengths.values())),
        "total_tasks": len(TASKS),
        "total_videos": n_eps * len(CAMERAS),
        "total_chunks": 1,
        "chunks_size": 1000,
        "fps": FPS,
        "splits": {"train": f"0:{n_eps}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": ("videos/chunk-{episode_chunk:03d}/{video_key}/"
                       "episode_{episode_index:06d}.mp4"),
        "features": {
            "action": {"dtype": "float32", "shape": [DOF], "names": names},
            "observation.state": {"dtype": "float32", "shape": [DOF], "names": names},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
            **{cam: {"dtype": "video", "shape": [HEIGHT, WIDTH, 3],
                     "names": ["height", "width", "channel"],
                     "info": {"video.fps": FPS, "video.codec": "h264",
                              "video.pix_fmt": "yuv420p"}} for cam in CAMERAS},
        },
    }
    os.makedirs(os.path.join(root, "meta"), exist_ok=True)
    with open(os.path.join(root, "meta", "info.json"), "w") as fh:
        json.dump(info, fh, indent=1)

    episodes_meta, cursor = [], 0
    for ep in range(n_eps):
        src = DUPLICATE_OF.get(ep, ep)
        length, task_idx = EPISODES[src]
        q = _joint_track(length, src)
        state = np.vstack([q[:1], q[:-1]]).astype(np.float32)   # state lags action by 1 frame
        ts = (np.arange(length) / FPS).astype(np.float32)
        if src == 2:
            ts[length // 2:] += np.float32(0.6)                  # timestamp jump
        df = pd.DataFrame({
            "action": list(q), "observation.state": list(state),
            "timestamp": ts,
            "frame_index": np.arange(length, dtype=np.int64),
            "episode_index": np.full(length, ep, dtype=np.int64),
            "index": np.arange(cursor, cursor + length, dtype=np.int64),
            "task_index": np.full(length, task_idx, dtype=np.int64),
        })
        cursor += length
        pq = os.path.join(root, info["data_path"].format(episode_chunk=0, episode_index=ep))
        os.makedirs(os.path.dirname(pq), exist_ok=True)
        df.to_parquet(pq, index=False)
        for cam in CAMERAS:
            dst = os.path.join(root, info["video_path"].format(
                episode_chunk=0, video_key=cam, episode_index=ep))
            if ep in DUPLICATE_OF:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(os.path.join(root, info["video_path"].format(
                    episode_chunk=0, video_key=cam, episode_index=src)), dst)
            else:
                _write_video(dst, _frames(q, cam, src))
        episodes_meta.append({"episode_index": ep, "tasks": [TASKS[task_idx]],
                              "length": length})

    with open(os.path.join(root, "meta", "episodes.jsonl"), "w") as fh:
        for row in episodes_meta:
            fh.write(json.dumps(row) + "\n")
    with open(os.path.join(root, "meta", "tasks.jsonl"), "w") as fh:
        for i, task in enumerate(TASKS):
            fh.write(json.dumps({"task_index": i, "task": task}) + "\n")
    return root


# ---------------------------------------------------------------- mcap and lance (D44)
#
# The same eight episodes in the two container formats v1's PR #155 reads, so a check run
# on them sees the same actions, timestamps, tasks and pictures as on the LeRobot fixture:
#
# * mcap: one ``episode_<i>.mcap`` per episode, ROS2 cdr messages (mcap-ros2-support's
#   writer): ``/action`` and ``/observation.state`` (float64[] data), ``/task`` (only the
#   labelled episodes), one ``/observation.images.<camera>`` stream of JPEG frames per camera
#   (the camera keys come out as the LeRobot feature keys), log_time = t0 + timestamp, and
#   a metadata record ``robot_type: franka``. ep 7 is a byte copy of ep 3's file.
# * lance: lerobot-lance-convert's three-table layout - ``meta/`` (LeRobot v3.0 with
#   ``storage_format: "lance"``), ``frames.lance`` (one row per frame), ``videos.lance``
#   (one row per source mp4, the bytes in a blob column; here one file per episode and
#   camera) and ``meta.lance`` (the meta files as (path, bytes)).

MCAP_T0_NS = 1_700_000_000_000_000_000


def _episode_data(ep: int):
    """(source episode, length, task text, action, state, timestamps) of fixture episode ``ep``."""
    src = DUPLICATE_OF.get(ep, ep)
    length, task_idx = EPISODES[src]
    q = _joint_track(length, src)
    state = np.vstack([q[:1], q[:-1]]).astype(np.float32)
    ts = (np.arange(length) / FPS).astype(np.float32)
    if src == 2:
        ts[length // 2:] += np.float32(0.6)
    return src, length, TASKS[task_idx], q, state, ts


def _n_episodes() -> int:
    return len(EPISODES) + len(DUPLICATE_OF)


def _fresh(root: str, marker: str, overwrite: bool) -> bool:
    """True when ``root`` must be (re)written."""
    if os.path.exists(os.path.join(root, marker)) and not overwrite:
        return False
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    return True


def _write_episode_mcap(path: str, ep: int) -> None:
    import cv2
    from mcap_ros2.writer import Writer as Ros2Writer

    src, length, task, q, state, ts = _episode_data(ep)
    times = [MCAP_T0_NS + int(round(float(t) * 1e9)) for t in ts]
    frames = {cam: _frames(q, cam, src) for cam in CAMERAS}
    with open(path, "wb") as fh:
        w = Ros2Writer(fh)
        w._writer.add_metadata("curation", {"robot_type": "franka"})
        s_arr = w.register_msgdef("curation_msgs/msg/FloatArray", "float64[] data")
        s_txt = w.register_msgdef("std_msgs/msg/String", "string data")
        s_img = w.register_msgdef("sensor_msgs/msg/CompressedImage",
                                  "string format\nuint8[] data")
        if task:
            w.write_message("/task", s_txt, {"data": task}, log_time=times[0])
        for i, t in enumerate(times):
            w.write_message("/action", s_arr, {"data": [float(x) for x in q[i]]}, log_time=t)
            w.write_message("/observation.state", s_arr,
                            {"data": [float(x) for x in state[i]]}, log_time=t)
            for cam in CAMERAS:
                ok, buf = cv2.imencode(".jpg", cv2.cvtColor(frames[cam][i], cv2.COLOR_RGB2BGR))
                assert ok
                w.write_message("/" + cam, s_img, {"format": "jpeg", "data": buf.tobytes()},
                                log_time=t)
        w.finish()


def make_mini_mcap(root: str, *, overwrite: bool = False) -> str:
    """The fixture's eight episodes as an mcap dataset (``episode_<i>.mcap``) under ``root``."""
    if not _fresh(root, "episode_0.mcap", overwrite):
        return root
    for ep in range(_n_episodes()):
        dst = os.path.join(root, f"episode_{ep}.mcap")
        if ep in DUPLICATE_OF:
            shutil.copyfile(os.path.join(root, f"episode_{DUPLICATE_OF[ep]}.mcap"), dst)
        else:
            _write_episode_mcap(dst, ep)
    return root


def _video_bytes(frames: np.ndarray) -> bytes:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="fixture-mp4-") as tmp:
        path = os.path.join(tmp, "v.mp4")
        _write_video(path, frames)
        with open(path, "rb") as fh:
            return fh.read()


def make_mini_lance(root: str, *, overwrite: bool = False, meta_dir: bool = True) -> str:
    """The fixture's eight episodes in lerobot-lance-convert's three-table layout.

    ``meta_dir=False`` leaves ``meta/`` out: only the ``meta.lance`` mirror holds it (a
    root that someone copied the three tables of).
    """
    import io

    import lance
    import pandas as pd
    import pyarrow as pa

    if not _fresh(root, os.path.join("frames.lance", "_versions"), overwrite):
        return root
    n_eps = _n_episodes()
    names = [f"joint_{i}" for i in range(DOF)]
    data = {ep: _episode_data(ep) for ep in range(n_eps)}
    total = int(sum(d[1] for d in data.values()))
    info = {
        "codebase_version": "v3.0",
        "storage_format": "lance",
        "robot_type": "franka",
        "total_episodes": n_eps,
        "total_frames": total,
        "total_tasks": len(TASKS),
        "chunks_size": 1000,
        "data_files_size_in_mb": 100,
        "video_files_size_in_mb": 200,
        "fps": FPS,
        "splits": {"train": f"0:{n_eps}"},
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
        "features": {
            "action": {"dtype": "float32", "shape": [DOF], "names": names},
            "observation.state": {"dtype": "float32", "shape": [DOF], "names": names},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
            **{cam: {"dtype": "video", "shape": [HEIGHT, WIDTH, 3],
                     "names": ["height", "width", "channel"],
                     "info": {"video.fps": FPS, "video.codec": "h264",
                              "video.pix_fmt": "yuv420p"}} for cam in CAMERAS},
        },
    }
    task_list = list(dict.fromkeys(TASKS))
    episodes, frames_cols, cursor = [], {k: [] for k in (
        "index", "episode_index", "frame_index", "timestamp", "task_index", "action",
        "observation_state")}, 0
    for ep in range(n_eps):
        src, length, task, q, state, ts = data[ep]
        row = {"episode_index": ep, "length": length, "tasks": [task],
               "dataset_from_index": cursor, "dataset_to_index": cursor + length,
               "data/chunk_index": 0, "data/file_index": 0}
        for cam in CAMERAS:
            row.update({f"videos/{cam}/chunk_index": 0, f"videos/{cam}/file_index": ep,
                        f"videos/{cam}/from_timestamp": 0.0,
                        f"videos/{cam}/to_timestamp": length / FPS})
        episodes.append(row)
        frames_cols["index"] += list(range(cursor, cursor + length))
        frames_cols["episode_index"] += [ep] * length
        frames_cols["frame_index"] += list(range(length))
        frames_cols["timestamp"] += [float(t) for t in ts]
        frames_cols["task_index"] += [task_list.index(task)] * length
        frames_cols["action"] += [list(map(float, a)) for a in q]
        frames_cols["observation_state"] += [list(map(float, s)) for s in state]
        cursor += length

    def parquet_bytes(df) -> bytes:
        buf = io.BytesIO()
        df.to_parquet(buf)
        return buf.getvalue()

    meta_files = {
        "meta/info.json": json.dumps(info, indent=1).encode(),
        "meta/episodes/chunk-000/file-000.parquet": parquet_bytes(pd.DataFrame(episodes)),
        "meta/tasks.parquet": parquet_bytes(pd.DataFrame(
            {"task_index": list(range(len(task_list)))}, index=pd.Index(task_list, name="task"))),
    }
    if meta_dir:
        for rel, blob in meta_files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(blob)

    vec = pa.list_(pa.float32(), DOF)
    lance.write_dataset(pa.table({
        "index": pa.array(frames_cols["index"], pa.int64()),
        "episode_index": pa.array(frames_cols["episode_index"], pa.int64()),
        "frame_index": pa.array(frames_cols["frame_index"], pa.int64()),
        "timestamp": pa.array(frames_cols["timestamp"], pa.float32()),
        "task_index": pa.array(frames_cols["task_index"], pa.int64()),
        "action": pa.array(frames_cols["action"], vec),
        "observation_state": pa.array(frames_cols["observation_state"], vec),
    }), os.path.join(root, "frames.lance"))

    video_rows = {"video_key": [], "chunk_index": [], "file_index": [], "video_bytes": []}
    encoded: dict[tuple[int, str], bytes] = {}
    for ep in range(n_eps):
        src, _length, _task, q, _state, _ts = data[ep]
        for cam in CAMERAS:
            if (src, cam) not in encoded:
                encoded[(src, cam)] = _video_bytes(_frames(q, cam, src))
            video_rows["video_key"].append(cam)
            video_rows["chunk_index"].append(0)
            video_rows["file_index"].append(ep)
            video_rows["video_bytes"].append(encoded[(src, cam)])
    schema = pa.schema([pa.field("video_key", pa.string()), pa.field("chunk_index", pa.int64()),
                        pa.field("file_index", pa.int64()),
                        pa.field("video_bytes", pa.large_binary(),
                                 metadata={"lance-encoding:blob": "true"})])
    lance.write_dataset(pa.table(video_rows, schema=schema), os.path.join(root, "videos.lance"))
    lance.write_dataset(pa.table({"path": list(meta_files), "data": list(meta_files.values())}),
                        os.path.join(root, "meta.lance"))
    return root


MAKERS = {"lerobot": make_mini_lerobot, "mcap": make_mini_mcap, "lance": make_mini_lance}


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m parity make-fixture")
    p.add_argument("--out", required=True)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--format", choices=sorted(MAKERS), default="lerobot",
                   help="lerobot (v2.1, default), mcap (one cdr .mcap per episode) or lance "
                        "(lerobot-lance-convert's three tables); the same eight episodes")
    args = p.parse_args(argv)
    print(MAKERS[args.format](args.out, overwrite=args.overwrite))
    return 0
