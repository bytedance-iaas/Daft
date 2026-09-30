#!/usr/bin/env python
"""Synthetic fault injection for the regression sample set (design 16 §7 third batch, ledger F11.4).

Takes clean episodes of a LeRobot v2.x dataset and writes a new v2.x dataset whose episodes each carry ONE injected fault at
one of two severities (``obvious`` / ``borderline``), plus a few pristine copies as controls. The faults live in the bytes, the
tables or the pictures - the same places the real defects live - so the platform reads the result like any other dataset.

Every output episode is described in ``injection.json`` (the truth: item id of design 16 §3, severity, scope, frames,
parameters, base episode); ``make_truth.py`` turns that into expectation lines with ``basis: injected``.

    python inject.py --base <lerobot v2.x dir> --out <new dir> --episodes 3,7,12,... --plan droid --seed 16

Only the standard scientific stack plus PyAV / Pillow are needed; the mp4 sample table for the ``garble`` fault comes from the
platform package when it is importable (``curation.extensions.integrity.mp4``), otherwise the fault is skipped.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import time

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

try:
    import av
    from PIL import Image, ImageDraw, ImageFilter
except ImportError as ex:  # pragma: no cover
    raise SystemExit(f"needs PyAV and Pillow: {ex}")

ENCODER = {"av1": ("libsvtav1", {"preset": "8", "crf": "30", "svtav1-params": "verbose=0"}), "mpeg4": ("mpeg4", {}), "h264": ("libx264", {"crf": "23", "preset": "fast"}),
           "hevc": ("libx265", {"crf": "26"})}


# ---------------------------------------------------------------- dataset access ---------------------------------------------
class Base:
    def __init__(self, root):
        self.root = root
        self.info = json.load(open(os.path.join(root, "meta/info.json")))
        assert str(self.info["codebase_version"]).startswith("v2"), "LeRobot v2.x only"
        self.fps = float(self.info["fps"])
        self.chunk = int(self.info.get("chunks_size", 1000))
        self.cams = [k for k, v in self.info["features"].items() if v.get("dtype") == "video"]
        self.episodes = {int(r["episode_index"]): r for r in (json.loads(l) for l in open(os.path.join(root, "meta/episodes.jsonl")) if l.strip())}
        self.tasks = [json.loads(l) for l in open(os.path.join(root, "meta/tasks.jsonl")) if l.strip()] if os.path.exists(os.path.join(root, "meta/tasks.jsonl")) else []
        self.stats = {}
        sp = os.path.join(root, "meta/episodes_stats.jsonl")
        if os.path.exists(sp):
            self.stats = {int(r["episode_index"]): r for r in (json.loads(l) for l in open(sp) if l.strip())}
        names = self.info["features"]["action"].get("names") or []
        if isinstance(names, dict):
            names = [x for v in names.values() for x in (v if isinstance(v, list) else [v])]
        self.action_names = names

    def parquet(self, ep):
        return os.path.join(self.root, self.info["data_path"].format(episode_chunk=ep // self.chunk, episode_index=ep))

    def video(self, ep, cam):
        return os.path.join(self.root, self.info["video_path"].format(episode_chunk=ep // self.chunk, video_key=cam, episode_index=ep))

    def codec(self, cam):
        return ((self.info["features"][cam].get("info") or {}).get("video.codec") or "h264").lower()


class Out:
    """the output dataset: same features / fps as the base, episodes numbered from 0 in the order they are added"""

    def __init__(self, base: Base, root):
        if os.path.exists(root):
            shutil.rmtree(root)
        os.makedirs(os.path.join(root, "meta"))
        self.base, self.root, self.n, self.frames, self.rows = base, root, 0, 0, []
        self.records = []

    def paths(self, ep):
        b = self.base
        return (os.path.join(self.root, b.info["data_path"].format(episode_chunk=ep // b.chunk, episode_index=ep)),
                {cam: os.path.join(self.root, b.info["video_path"].format(episode_chunk=ep // b.chunk, video_key=cam, episode_index=ep)) for cam in b.cams})

    def add(self, src_ep, record):
        """copy base episode src_ep as the next output episode (files are copied verbatim; faults are applied afterwards)"""
        ep = self.n; self.n += 1
        pq_path, vids = self.paths(ep)
        os.makedirs(os.path.dirname(pq_path), exist_ok=True)
        df = pd.read_parquet(self.base.parquet(src_ep))
        df["episode_index"] = ep
        df["index"] = np.arange(self.frames, self.frames + len(df))
        df.to_parquet(pq_path)
        for cam, dst in vids.items():
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(self.base.video(src_ep, cam), dst)
        row = dict(self.base.episodes[src_ep]); row["episode_index"] = ep
        self.rows.append(row)
        self.frames += len(df)
        rec = {"episode_index": ep, "base_episode": src_ep, "lineage": f"{os.path.basename(self.base.root.rstrip('/'))}:{src_ep}", **record}
        self.records.append(rec)
        return ep, pq_path, vids, df

    def finish(self, note):
        b = self.base
        info = json.loads(json.dumps(b.info))
        info["total_episodes"] = self.n; info["total_frames"] = self.frames; info["total_chunks"] = (self.n - 1) // b.chunk + 1
        info["total_videos"] = self.n * len(b.cams)
        info["splits"] = {"train": f"0:{self.n}"}
        json.dump(info, open(os.path.join(self.root, "meta/info.json"), "w"), indent=2)
        with open(os.path.join(self.root, "meta/episodes.jsonl"), "w") as f:
            for r in self.rows:
                f.write(json.dumps(r) + "\n")
        if b.tasks:
            with open(os.path.join(self.root, "meta/tasks.jsonl"), "w") as f:
                for t in b.tasks:
                    f.write(json.dumps(t, ensure_ascii=False) + "\n")
        if b.stats:
            with open(os.path.join(self.root, "meta/episodes_stats.jsonl"), "w") as f:
                for rec in self.records:
                    s = b.stats.get(rec["base_episode"])
                    if s:
                        s = dict(s); s["episode_index"] = rec["episode_index"]; f.write(json.dumps(s) + "\n")
        for extra in ("stats.json",):
            if os.path.exists(os.path.join(b.root, "meta", extra)):
                shutil.copyfile(os.path.join(b.root, "meta", extra), os.path.join(self.root, "meta", extra))
        json.dump({"schema_version": "0.1", "base": os.path.basename(b.root.rstrip("/")), "note": note, "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "episodes": self.records}, open(os.path.join(self.root, "injection.json"), "w"), ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- video operations -------------------------------------------
def transcode(src, dst, codec, fps, frame_fn=None, seq_fn=None, size=None):
    """decode src, apply frame_fn(i, ndarray HxWx3) -> ndarray (or seq_fn(list) -> list), encode to dst with the base codec"""
    frames = []
    with av.open(src) as c:
        st = c.streams.video[0]
        for i, fr in enumerate(c.decode(st)):
            img = fr.to_ndarray(format="rgb24")
            if frame_fn is not None:
                img = frame_fn(i, img)
            frames.append(img)
    if seq_fn is not None:
        frames = seq_fn(frames)
    enc, opts = ENCODER.get(codec, ENCODER["h264"])
    h, w = frames[0].shape[:2]
    if size:
        w, h = size
    with av.open(dst, "w", format="mp4") as o:  # the temp name has no .mp4 suffix
        s = o.add_stream(enc, rate=int(round(fps)), options=opts)
        s.width, s.height, s.pix_fmt = w, h, "yuv420p"
        for img in frames:
            if size:
                img = np.asarray(Image.fromarray(img).resize(size))
            f = av.VideoFrame.from_ndarray(np.ascontiguousarray(img), format="rgb24")
            for pkt in s.encode(f):
                o.mux(pkt)
        for pkt in s.encode(None):
            o.mux(pkt)
    return len(frames)


def truncate(path, fraction):
    os.truncate(path, int(os.path.getsize(path) * fraction))


def zero_fill(path, offset, n):
    with open(path, "r+b") as fh:
        fh.seek(offset); fh.write(bytes(n))


def garble(path, frame_ids, seed):
    """random bytes over whole compressed frames: the container is intact, the pictures are not (blocky / grey / decode errors)"""
    try:
        from curation.extensions.integrity import mp4
    except ImportError:
        return False
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        def rr(s, n):
            fh.seek(s); return fh.read(n)
        s = mp4.samples(mp4.read_moov(rr, mp4.walk(rr, size)))
    rnd = random.Random(seed)
    with open(path, "r+b") as fh:
        for k in frame_ids:
            if k < len(s.offsets):
                fh.seek(s.offsets[k]); fh.write(bytes(rnd.getrandbits(8) for _ in range(s.sizes[k])))
    return True


# ---------------------------------------------------------------- faults -----------------------------------------------------
# each fault: name -> (item, applies(base, out_ep_ctx, severity, rnd) -> record fields)
def seg(n, fps, length_s, rnd, start_frac=(0.3, 0.6)):
    L = min(n - 1, max(2, int(round(length_s * fps))))
    a = int(n * rnd.uniform(*start_frac)); a = max(0, min(a, n - L - 1))
    return a, a + L - 1


def fault_frozen(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    a, z = seg(n, b.fps, 3.0 if sev == "obvious" else 0.8, rnd)
    hold = {}
    def fn(i, img):
        if a <= i <= z:
            hold.setdefault("f", img); return hold["f"]
        return img
    transcode(vids[cam], vids[cam] + ".tmp", b.codec(cam), b.fps, fn); os.replace(vids[cam] + ".tmp", vids[cam])
    return {"item": "IMG-1", "scope": {"stream": cam}, "frames": [a, z], "params": {"held_seconds": round((z - a + 1) / b.fps, 2)}}


def fault_dark(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    factor = 0.12 if sev == "obvious" else 0.45
    a, z = (0, n - 1) if sev == "obvious" else seg(n, b.fps, 4.0, rnd)
    def fn(i, img):
        return (img.astype(np.float32) * factor).clip(0, 255).astype(np.uint8) if a <= i <= z else img
    transcode(vids[cam], vids[cam] + ".tmp", b.codec(cam), b.fps, fn); os.replace(vids[cam] + ".tmp", vids[cam])
    return {"item": "IMG-2", "scope": {"stream": cam}, "frames": [a, z], "params": {"luma_factor": factor}}


def fault_overexposed(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    gain, bias = (3.2, 90) if sev == "obvious" else (1.6, 40)
    a, z = (0, n - 1) if sev == "obvious" else seg(n, b.fps, 4.0, rnd)
    def fn(i, img):
        return (img.astype(np.float32) * gain + bias).clip(0, 255).astype(np.uint8) if a <= i <= z else img
    transcode(vids[cam], vids[cam] + ".tmp", b.codec(cam), b.fps, fn); os.replace(vids[cam] + ".tmp", vids[cam])
    return {"item": "IMG-2", "scope": {"stream": cam}, "frames": [a, z], "params": {"gain": gain, "bias": bias, "kind": "overexposed"}}


def fault_blur(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    radius = 8 if sev == "obvious" else 2.5
    def fn(i, img):
        return np.asarray(Image.fromarray(img).filter(ImageFilter.GaussianBlur(radius)))
    transcode(vids[cam], vids[cam] + ".tmp", b.codec(cam), b.fps, fn); os.replace(vids[cam] + ".tmp", vids[cam])
    return {"item": "IMG-4", "scope": {"stream": cam}, "frames": [0, n - 1], "params": {"gaussian_radius_px": radius}}


def fault_occlusion(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    cover = 0.7 if sev == "obvious" else 0.25
    a, z = seg(n, b.fps, 5.0 if sev == "obvious" else 2.0, rnd)
    def fn(i, img):
        if a <= i <= z:
            h, w = img.shape[:2]; img = img.copy()
            hh, ww = int(h * cover ** 0.5), int(w * cover ** 0.5)
            y0, x0 = (h - hh) // 2, (w - ww) // 2
            img[y0:y0 + hh, x0:x0 + ww] = (38, 34, 30)
        return img
    transcode(vids[cam], vids[cam] + ".tmp", b.codec(cam), b.fps, fn); os.replace(vids[cam] + ".tmp", vids[cam])
    return {"item": "IMG-8", "scope": {"stream": cam}, "frames": [a, z], "params": {"covered_fraction": cover}}


def fault_shake(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    amp = 14 if sev == "obvious" else 4
    a, z = seg(n, b.fps, 4.0, rnd)
    r = random.Random(rnd.random())
    def fn(i, img):
        if a <= i <= z:
            dx, dy = r.randint(-amp, amp), r.randint(-amp, amp)
            return np.asarray(Image.fromarray(img).transform(Image.fromarray(img).size, Image.AFFINE, (1, 0, dx, 0, 1, dy), fillcolor=(0, 0, 0)))
        return img
    transcode(vids[cam], vids[cam] + ".tmp", b.codec(cam), b.fps, fn); os.replace(vids[cam] + ".tmp", vids[cam])
    return {"item": "IMG-6", "scope": {"stream": cam}, "frames": [a, z], "params": {"max_shift_px": amp}}


def fault_smudge(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    frac = 0.35 if sev == "obvious" else 0.15
    mask = {}
    def fn(i, img):
        h, w = img.shape[:2]
        if "m" not in mask:
            m = Image.new("L", (w, h), 0); d = ImageDraw.Draw(m)
            rad = int(w * frac); cx, cy = int(w * 0.6), int(h * 0.45)
            d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=255)
            mask["m"] = np.asarray(m.filter(ImageFilter.GaussianBlur(rad * 0.4))).astype(np.float32)[..., None] / 255.0
        blurred = np.asarray(Image.fromarray(img).filter(ImageFilter.GaussianBlur(9))).astype(np.float32)
        hazy = blurred * 0.75 + 60
        return (img.astype(np.float32) * (1 - mask["m"]) + hazy * mask["m"]).clip(0, 255).astype(np.uint8)
    transcode(vids[cam], vids[cam] + ".tmp", b.codec(cam), b.fps, fn); os.replace(vids[cam] + ".tmp", vids[cam])
    return {"item": "IMG-7", "scope": {"stream": cam}, "frames": [0, n - 1], "params": {"blob_radius_fraction_of_width": frac}}


def fault_garble(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    k = 6 if sev == "obvious" else 1
    ids = sorted(rnd.sample(range(5, max(6, n - 5)), min(k, max(1, n - 10))))
    ok = garble(vids[cam], ids, seed=ep)
    if not ok:
        return None
    return {"item": "FILE-4", "scope": {"stream": cam}, "frames": [ids[0], ids[-1]], "params": {"garbled_frames": ids},
            "note": "compressed frames overwritten with random bytes: the decoder fails on those frames and on the ones predicted from them (AV1 streams stop decoding altogether)"}


def fault_blocks(b, ctx, sev, rnd):
    """visible corruption without breaking the bitstream: misplaced macroblocks and torn rows on a few frames, then re-encoded"""
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    k = 12 if sev == "obvious" else 2
    ids = set(sorted(rnd.sample(range(5, max(6, n - 5)), min(k, max(1, n - 10)))))
    r = random.Random(rnd.random())
    def fn(i, img):
        if i not in ids:
            return img
        img = img.copy(); h, w = img.shape[:2]; bs = 16
        nb = (h // bs) * (w // bs) // (4 if sev == "obvious" else 12)
        for _ in range(nb):
            y, x = r.randrange(0, h - bs, bs), r.randrange(0, w - bs, bs)
            sy, sx = r.randrange(0, h - bs, bs), r.randrange(0, w - bs, bs)
            img[y:y + bs, x:x + bs] = img[sy:sy + bs, sx:sx + bs] if r.random() < 0.7 else np.array([r.randrange(256) for _ in range(3)], dtype=np.uint8)
        if sev == "obvious":
            y0 = r.randrange(h // 4, h * 3 // 4); img[y0:] = np.roll(img[y0:], r.randrange(20, 80), axis=1)  # a torn band
        return img
    transcode(vids[cam], vids[cam] + ".tmp", b.codec(cam), b.fps, fn); os.replace(vids[cam] + ".tmp", vids[cam])
    return {"item": "IMG-5", "scope": {"stream": cam}, "frames": [min(ids), max(ids)], "params": {"corrupted_frames": sorted(ids)}}


def fault_shift_video(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    k = int(round((0.6 if sev == "obvious" else 0.2) * b.fps))
    def sf(frames):
        return frames[k:] + [frames[-1]] * k  # the picture runs k frames AHEAD of the signals
    transcode(vids[cam], vids[cam] + ".tmp", b.codec(cam), b.fps, seq_fn=sf); os.replace(vids[cam] + ".tmp", vids[cam])
    return {"item": "AV-1", "scope": {"stream": cam}, "frames": [0, n - 1], "params": {"video_leads_by_frames": k, "video_leads_by_s": round(k / b.fps, 3)}}


def fault_camera_swap(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df)
    cams = list(b.cams)
    if len(cams) < 2:
        return None
    wrist = [c for c in cams if "wrist" in c.lower()]
    if sev == "obvious" and wrist:
        a, z = wrist[0], [c for c in cams if c != wrist[0]][0]
    else:
        ext = [c for c in cams if c not in wrist]
        a, z = (ext[0], ext[1]) if len(ext) >= 2 else (cams[0], cams[1])
    os.replace(vids[a], vids[a] + ".swap"); os.replace(vids[z], vids[a]); os.replace(vids[a] + ".swap", vids[z])
    return {"item": "MV-2", "scope": {"streams": [a, z]}, "frames": [0, n - 1], "params": {"swapped": [a, z]}}


def fault_resolution(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    h, w = b.info["features"][cam]["shape"][:2]
    size = (w // 2, h // 2) if sev == "obvious" else (w - 16, h - 16)
    transcode(vids[cam], vids[cam] + ".tmp", b.codec(cam), b.fps, size=size); os.replace(vids[cam] + ".tmp", vids[cam])
    return {"item": "FILE-7", "scope": {"stream": cam}, "frames": [0, n - 1], "params": {"declared": [w, h], "actual": list(size)}}


def fault_truncate_video(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    frac = 0.6 if sev == "obvious" else 0.97
    truncate(vids[cam], frac)
    return {"item": "FILE-2", "scope": {"stream": cam}, "params": {"kept_fraction": frac}, "note": "moov at the end of the file is lost when the cut is deep enough"}


def fault_zero_fill(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    size = os.path.getsize(vids[cam])
    off, k = (0, 4096) if sev == "obvious" else (size // 2, 512)
    zero_fill(vids[cam], off, k)
    return {"item": "FILE-2", "scope": {"stream": cam}, "params": {"zeroed_offset": off, "zeroed_bytes": k, "kind": "zero_filled"}}


def fault_empty_video(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df); cam = rnd.choice(b.cams)
    if sev == "obvious":
        os.remove(vids[cam]); kind = "missing"
    else:
        open(vids[cam], "wb").close(); kind = "empty"
    return {"item": "FILE-1", "scope": {"stream": cam}, "params": {"kind": kind}, "also": ["STRM-1"]}


def rewrite(pq_path, fn):
    df = pd.read_parquet(pq_path); fn(df); df.to_parquet(pq_path)


def fault_nan_action(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df)
    rows = sorted(rnd.sample(range(5, n - 5), 5 if sev == "obvious" else 1))
    def fn(d):
        acts = [np.asarray(a, dtype=np.float32) for a in d["action"]]
        for r in rows:
            acts[r] = np.full_like(acts[r], np.nan)
        d["action"] = acts
    rewrite(pq_path, fn)
    return {"item": "FILE-6", "scope": {"stream": "action"}, "frames": [rows[0], rows[-1]], "params": {"nan_rows": rows}}


def fault_drop_rows(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df)
    k = int(round(b.fps * 1.0)) if sev == "obvious" else 2
    a = int(n * 0.5)
    def fn(d):
        d.drop(d.index[a:a + k], inplace=True); d.reset_index(drop=True, inplace=True)
    rewrite(pq_path, fn)
    return {"item": "STRM-3", "scope": {"stream": "action"}, "frames": [a - 1, a + k], "params": {"dropped_rows": k, "gap_s": round(k / b.fps, 3)},
            "also": ["FILE-5"], "note": "rows removed from the table, the videos keep their frames: a timestamp gap and a frame-count mismatch"}


def fault_timestamps(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df)
    a = int(n * 0.5)
    def fn(d):
        ts = d["timestamp"].to_numpy().copy()
        if sev == "obvious":
            ts[a:a + 3] = ts[a - 1] - 0.5  # jumps back half a second for three rows
        else:
            ts[a] = ts[a - 1]  # one duplicated timestamp
        d["timestamp"] = ts
    rewrite(pq_path, fn)
    return {"item": "STRM-4", "scope": {"stream": "timestamp"}, "frames": [a - 1, a + (3 if sev == "obvious" else 0)],
            "params": {"kind": "backwards" if sev == "obvious" else "duplicate"}}


def position_dims(b):
    names = [str(x).lower() for x in b.action_names]
    pos = [i for i, nm in enumerate(names) if nm in ("x", "y", "z") or nm.endswith(("_x", "_y", "_z", ".x", ".y", ".z"))]
    return pos[:3]


def fault_spike(b, ctx, sev, rnd):
    """a single-step excursion in the position dims of observation.state (a recording glitch), scaled to the episode's own steps"""
    ep, pq_path, vids, df = ctx; n = len(df)
    col = "observation.state" if "observation.state" in df else "action"
    a = int(n * rnd.uniform(0.35, 0.65))
    def fn(d):
        arr = np.stack([np.asarray(x, dtype=np.float32) for x in d[col]])
        dims = list(range(min(3, arr.shape[1])))
        steps = np.abs(np.diff(arr[:, dims], axis=0)); p95 = np.percentile(steps, 95, axis=0) + 1e-6
        mult = 12.0 if sev == "obvious" else 4.0
        arr[a, dims] += mult * p95 * np.array([1, -1, 1][:len(dims)])
        d[col] = list(arr)
    rewrite(pq_path, fn)
    return {"item": "ACT-2", "scope": {"stream": col, "dims": [0, 1, 2]}, "frames": [a, a + 1], "params": {"multiple_of_p95_step": 12.0 if sev == "obvious" else 4.0},
            "note": "one row displaced and the next row back on the trajectory"}


def fault_constant_channel(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df)
    names = [str(x).lower() for x in b.action_names]
    g = next((i for i, nm in enumerate(names) if "gripper" in nm), len(names) - 1)
    a = 0 if sev == "obvious" else int(n * 0.5)
    def fn(d):
        acts = np.stack([np.asarray(x, dtype=np.float32) for x in d["action"]])
        acts[a:, g] = acts[a, g]
        d["action"] = list(acts)
    rewrite(pq_path, fn)
    return {"item": "ACT-3", "scope": {"channel": b.action_names[g] if g < len(b.action_names) else f"action[{g}]"}, "frames": [a, n - 1],
            "params": {"held_from_frame": a}}


def fault_sawtooth(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df)
    col = "observation.state" if "observation.state" in df else "action"
    a, z = seg(n, b.fps, 4.0, rnd)
    def fn(d):
        arr = np.stack([np.asarray(x, dtype=np.float32) for x in d[col]])
        j = 0
        rng = float(arr[:, j].max() - arr[:, j].min()) or 1.0
        amp = (0.2 if sev == "obvious" else 0.05) * rng
        arr[a:z + 1, j] += amp * np.where(np.arange(z - a + 1) % 2 == 0, 1.0, -1.0)
        d[col] = list(arr)
    rewrite(pq_path, fn)
    return {"item": "ACT-1", "scope": {"stream": col, "dims": [0]}, "frames": [a, z], "params": {"amplitude_fraction_of_range": 0.2 if sev == "obvious" else 0.05}}


def fault_stale_state(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df)
    if "observation.state" not in df:
        return None
    a = int(n * (0.4 if sev == "obvious" else 0.85))
    def fn(d):
        arr = np.stack([np.asarray(x, dtype=np.float32) for x in d["observation.state"]])
        arr[a:] = arr[a]
        d["observation.state"] = list(arr)
    rewrite(pq_path, fn)
    return {"item": "ACT-8", "scope": {"stream": "observation.state"}, "frames": [a, n - 1], "params": {"held_from_frame": a}}


def fault_label_swap(b, ctx, sev, rnd):
    ep, pq_path, vids, df = ctx; n = len(df)
    if len(b.tasks) < 2:
        return None
    cur = int(df["task_index"].iloc[0])
    other = rnd.choice([t["task_index"] for t in b.tasks if t["task_index"] != cur])
    def fn(d):
        d["task_index"] = other
    rewrite(pq_path, fn)
    return {"item": "LABEL-5", "scope": {"field": "task_index"}, "frames": [0, n - 1], "params": {"was": cur, "now": other, "text_now": next(t["task"] for t in b.tasks if t["task_index"] == other)}}


CURRENT_OUT: "Out | None" = None  # set by main(); the duplicate fault needs the previous output episode


def fault_duplicate(b, ctx, sev, rnd):
    """the episode becomes a copy of the previous output episode: byte-identical files (obvious) or re-encoded videos (borderline)"""
    ep, pq_path, vids, df = ctx
    out = CURRENT_OUT
    if out is None or len(out.records) < 2:
        return None
    prev = out.records[-2]["episode_index"]
    prev_pq, prev_vids = out.paths(prev)
    d = pd.read_parquet(prev_pq); d["episode_index"] = ep; d["index"] = np.arange(int(df["index"].iloc[0]), int(df["index"].iloc[0]) + len(d)); d.to_parquet(pq_path)
    out.rows[-1] = dict(out.rows[-2]); out.rows[-1]["episode_index"] = ep
    out.frames += len(d) - len(df)
    for cam in b.cams:
        if not os.path.exists(prev_vids[cam]) or os.path.getsize(prev_vids[cam]) == 0:
            shutil.copyfile(prev_vids[cam], vids[cam]) if os.path.exists(prev_vids[cam]) else None; continue
        if sev == "obvious":
            shutil.copyfile(prev_vids[cam], vids[cam])
        else:
            try:
                transcode(prev_vids[cam], vids[cam], b.codec(cam), b.fps)
            except Exception:  # noqa: BLE001  (the previous episode may carry an undecodable fault)
                shutil.copyfile(prev_vids[cam], vids[cam])
    return {"item": "SET-1", "scope": {"duplicate_of": prev}, "params": {"kind": "byte_copy" if sev == "obvious" else "re_encoded_copy"},
            "note": f"full copy of episode {prev} (which therefore is the other half of the duplicate pair)"}


FAULTS = {
    "frozen": fault_frozen, "dark": fault_dark, "overexposed": fault_overexposed, "blur": fault_blur, "occlusion": fault_occlusion, "shake": fault_shake,
    "smudge": fault_smudge, "garble": fault_garble, "blocks": fault_blocks, "shift_video": fault_shift_video, "camera_swap": fault_camera_swap, "resolution": fault_resolution,
    "truncate_video": fault_truncate_video, "zero_fill": fault_zero_fill, "empty_video": fault_empty_video, "nan_action": fault_nan_action,
    "drop_rows": fault_drop_rows, "timestamps": fault_timestamps, "spike": fault_spike, "constant_channel": fault_constant_channel,
    "sawtooth": fault_sawtooth, "stale_state": fault_stale_state, "label_swap": fault_label_swap, "duplicate": fault_duplicate,
}
PLANS = {
    # image / file / table faults on a small, fast base (DROID: 15 fps, 320x180, AV1)
    "droid": ["frozen", "dark", "overexposed", "blur", "occlusion", "shake", "smudge", "garble", "blocks", "shift_video", "camera_swap", "resolution",
              "truncate_video", "zero_fill", "empty_video", "nan_action", "timestamps", "spike", "constant_channel", "sawtooth", "stale_state", "duplicate"],
    # a table shorter than episodes.jsonl says makes the platform's LeRobot reader refuse the WHOLE dataset (design 16 §11), so
    # this fault lives in a subset of its own and does not take the other faults down with it
    "droid_tablegap": ["drop_rows"],
    # action-space faults where the action is an end-effector pose in metres, plus labels (FastUMI)
    "fastumi": ["spike", "sawtooth", "constant_channel", "stale_state", "label_swap", "camera_swap", "shift_video", "frozen"],
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--episodes", required=True, help="comma-separated clean base episodes to draw from")
    ap.add_argument("--plan", default="droid", help="plan name or comma-separated fault names")
    ap.add_argument("--severities", default="obvious,borderline")
    ap.add_argument("--controls", type=int, default=3, help="pristine copies added as controls")
    ap.add_argument("--seed", type=int, default=16)
    a = ap.parse_args()
    b = Base(a.base)
    eps = [int(x) for x in a.episodes.split(",")]
    faults = PLANS.get(a.plan) or a.plan.split(",")
    sevs = a.severities.split(",")
    rnd = random.Random(a.seed)
    out = Out(b, a.out)
    global CURRENT_OUT
    CURRENT_OUT = out
    pool = list(eps); rnd.shuffle(pool)
    def next_base():
        if not pool:
            pool.extend(eps); rnd.shuffle(pool)
        return pool.pop()
    for name in faults:
        for sev in sevs:
            src = next_base()
            ctx = out.add(src, {"fault": name, "severity": sev})
            t0 = time.time()
            rec = FAULTS[name](b, ctx, sev, rnd)
            if rec is None:
                print(f"skip {name} ({sev}): not applicable to this base", flush=True)
                out.records.pop(); out.n -= 1; out.frames -= len(ctx[3]); out.rows.pop()
                for p in [ctx[1]] + list(ctx[2].values()):
                    if os.path.exists(p):
                        os.remove(p)
                continue
            out.records[-1].update(rec)
            print(f"ep {ctx[0]:3d} <- base {src:4d}: {name:16s} {sev:10s} {rec['item']:7s} {json.dumps(rec.get('params'), ensure_ascii=False)[:90]} ({time.time() - t0:.1f}s)", flush=True)
    for _ in range(a.controls):
        src = next_base()
        ctx = out.add(src, {"fault": None, "severity": None, "item": None, "note": "pristine copy of the base episode: a control inside the injected subset"})
        print(f"ep {ctx[0]:3d} <- base {src:4d}: control", flush=True)
    out.finish(f"faults injected into clean episodes of {os.path.basename(a.base.rstrip('/'))}; plan {a.plan}; seed {a.seed}")
    print("INJECT_DONE", out.n, "episodes ->", a.out)


if __name__ == "__main__":
    main()
