"""Damaged datasets for the data integrity module (design doc 14 §8, F7.5).

Each sample is a copy of a clean fixture with one kind of damage per episode, and a
``damage.json`` next to it saying which episode got what (the truth the tests check the
module against). The same functions serve the tests (``test_integrity.py``) and people:

    cd backend
    PYTHONPATH=../tools ../.venv/bin/python -m tests.cli.integrity_samples --out /tmp/integrity-samples

writes ``lerobot_v2/``, ``lerobot_v3/`` and ``mcap/`` (each with its ``damage.json``); point
``curation check --modules data_integrity --input <sample>`` or the console at them.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil

import numpy as np

from curation.extensions.integrity import mp4

CAM_V3 = "observation.images.exterior"


# ---------------------------------------------------------------- building blocks


def video(root: str, cam: str, ep: int) -> str:
    return os.path.join(root, "videos", "chunk-000", f"observation.images.{cam}", f"episode_{ep:06d}.mp4")


def parquet(root: str, ep: int) -> str:
    return os.path.join(root, "data", "chunk-000", f"episode_{ep:06d}.parquet")


def samples_of(path: str) -> mp4.Samples:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        def rr(s, n):
            fh.seek(s)
            return fh.read(n)
        return mp4.samples(mp4.read_moov(rr, mp4.walk(rr, size)))


def faststart(path: str) -> None:
    """Remux in place with the moov in front (no re-encoding)."""
    import av

    tmp = path + ".fs.mp4"
    with av.open(path) as src, av.open(tmp, "w", options={"movflags": "faststart"}) as dst:
        stream = dst.add_stream_from_template(src.streams.video[0])
        for pkt in src.demux(src.streams.video[0]):
            if pkt.dts is None:
                continue
            pkt.stream = stream
            dst.mux(pkt)
    os.replace(tmp, path)


def garble_sample(path: str, k: int) -> None:
    """Random bytes over frame ``k``'s data: the structure stays intact, the picture does not."""
    s = samples_of(path)
    random.seed(k)
    with open(path, "r+b") as fh:
        fh.seek(s.offsets[k])
        fh.write(bytes(random.getrandbits(8) for _ in range(s.sizes[k])))


def truncate(path: str, fraction: float | None = None, *, drop: int = 0, at: int | None = None) -> None:
    size = os.path.getsize(path)
    os.truncate(path, at if at is not None else (int(size * fraction) if fraction is not None else size - drop))


def flip_byte(path: str, offset: int, mask: int = 0xFF) -> None:
    with open(path, "r+b") as fh:
        fh.seek(offset)
        b = fh.read(1)
        fh.seek(offset)
        fh.write(bytes([b[0] ^ mask]))


def rewrite_parquet(path: str, fn) -> None:
    import pandas as pd

    df = pd.read_parquet(path)
    fn(df)
    df.to_parquet(path)


def nan_action(df, row: int = 10) -> None:
    actions = [np.asarray(a, dtype=np.float32) for a in df["action"]]
    actions[row] = np.full_like(actions[row], np.nan)
    df["action"] = actions


def reversed_time(df) -> None:
    df["timestamp"] = df["timestamp"].to_numpy()[::-1].copy()


def thin_topic(path: str, topic: str) -> None:
    """Rewrite an mcap episode without every second message of ``topic`` (a camera at half rate)."""
    from mcap.reader import make_reader
    from mcap.writer import Writer

    tmp = path + ".thin"
    with open(path, "rb") as src, open(tmp, "wb") as out:
        reader = make_reader(src)
        w = Writer(out)
        w.start(profile="ros2", library="integrity-samples")
        schemas, channels, seen = {}, {}, 0
        for rec in reader.iter_metadata():
            w.add_metadata(rec.name, rec.metadata)
        for schema, channel, message in reader.iter_messages(log_time_order=True):
            if schema.id not in schemas:
                schemas[schema.id] = w.register_schema(schema.name, schema.encoding, schema.data)
            if channel.id not in channels:
                channels[channel.id] = w.register_channel(channel.topic, channel.message_encoding,
                                                          schemas[schema.id], channel.metadata)
            if channel.topic == topic:
                seen += 1
                if seen % 2 == 0:
                    continue
            w.add_message(channels[channel.id], message.log_time, message.data, message.publish_time,
                          message.sequence)
        w.finish()
    os.replace(tmp, path)


def _write_truth(root: str, damage: dict[int, str]) -> None:
    with open(os.path.join(root, "damage.json"), "w", encoding="utf-8") as fh:
        json.dump({str(k): v for k, v in sorted(damage.items())}, fh, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- the samples


def damage_lerobot_v2(root: str) -> dict[int, str]:
    """One kind of damage per episode of the 8-episode v2 fixture (3 and 7 stay byte copies)."""
    open(video(root, "wrist", 0), "wb").close()
    truncate(video(root, "exterior", 1), 0.6)                       # the moov is at the end: gone
    truncate(parquet(root, 2), drop=20)
    rewrite_parquet(parquet(root, 3), reversed_time)
    with open(video(root, "exterior", 4), "r+b") as fh:
        fh.write(bytes(4096))
    rewrite_parquet(parquet(root, 6), nan_action)
    damage = {0: "reject file_empty (wrist video emptied)",
              1: "reject file_truncated (exterior video cut at 60%, moov lost)",
              2: "reject file_truncated (parquet footer cut)",
              3: "reject row_invalid (timestamps reversed) + suspect duplicate_content",
              4: "reject zero_filled (exterior video head zeroed)",
              5: "pass",
              6: "reject row_invalid (NaN in the action)",
              7: "suspect duplicate_content (byte copy of 3)"}
    _write_truth(root, damage)
    return damage


def damage_lerobot_v3(root: str) -> dict[int, str]:
    """The exterior camera's shared file-000.mp4 (episodes 0-3), moov first, cut in episode 2."""
    import pandas as pd

    table = pd.read_parquet(os.path.join(root, "meta", "episodes", "chunk-000", "file-000.parquet"))
    path = os.path.join(root, "videos", CAM_V3, "chunk-000", "file-000.mp4")
    faststart(path)
    s = samples_of(path)
    row = table[table["episode_index"] == 2].iloc[0]
    mid = (float(row[f"videos/{CAM_V3}/from_timestamp"]) + float(row[f"videos/{CAM_V3}/to_timestamp"])) / 2
    k = next(i for i, t in enumerate(s.times) if t >= mid)
    truncate(path, at=s.offsets[k] + 1)
    damage = {0: "pass", 1: "pass", 2: "reject file_truncated (its window crosses the cut)",
              3: "reject file_truncated (after the cut)", 4: "pass", 5: "pass"}
    _write_truth(root, damage)
    return damage


def damage_mcap(root: str) -> dict[int, str]:
    from curation.cli import containers

    truncate(os.path.join(root, "episode_4.mcap"), 0.6)
    path = os.path.join(root, "episode_2.mcap")
    with open(path, "rb") as fh:
        footer = containers.read_footer(fh)
    flip_byte(path, footer.summary_start + 20, 0x5A)
    path = os.path.join(root, "episode_6.mcap")
    flip_byte(path, os.path.getsize(path) // 3)
    thin_topic(os.path.join(root, "episode_1.mcap"), "/observation.images.exterior")
    damage = {0: "pass", 1: "suspect rate_outlier (exterior camera at half rate)",
              2: "reject structure_invalid (summary CRC)", 3: "suspect duplicate_content",
              4: "reject file_truncated or suspect cut_off (recording cut at 60%)", 5: "pass",
              6: "reject crc_mismatch (a chunk's byte flipped)", 7: "suspect duplicate_content"}
    _write_truth(root, damage)
    return damage


def main(argv: list[str] | None = None) -> int:
    from parity.fixtures import make_mini_lerobot, make_mini_mcap
    from tests.export.v3_fixture import make_mini_lerobot_v3

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    for name, make, damage in (("lerobot_v2", make_mini_lerobot, damage_lerobot_v2),
                               ("lerobot_v3", make_mini_lerobot_v3, damage_lerobot_v3),
                               ("mcap", make_mini_mcap, damage_mcap)):
        root = os.path.join(args.out, name)
        if os.path.exists(root):
            shutil.rmtree(root)
        make(root)
        for ep, what in damage(root).items():
            print(f"{name} ep {ep}: {what}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
