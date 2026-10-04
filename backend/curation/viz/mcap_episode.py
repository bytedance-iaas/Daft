"""One mcap episode read for the visualizer in a single pass over its mapped topics (design doc 18 §4).

What the pass leaves behind, in ``out_dir`` (the Daemon's disk cache):

* ``<camera>.frames`` + the index in ``episode.json`` - a JPEG / PNG camera's frames, their own
  bytes back to back (the frame pack the player reads by Range and draws on a canvas);
* ``<camera>.mp4`` - an H.264 / H.265 camera remuxed into fragmented mp4 (:mod:`.remux`);
* ``<camera>.annexb`` + the index in ``episode.json`` (``client_decode``, design doc 19 §3) - the same
  camera's access units as they were written, kept for the browser to decode itself: the index starts
  at the first keyframe and carries the stream's parameter sets (``config``) and codec string. The
  remux then waits until a ``<video>`` asks for it (:func:`remux_samples`);
* ``series.npz`` - every curve topic's message times and numbers;
* ``episode.json`` - the clock (zero, the frame reference's times, the checks' anchor and rate), the
  cameras' indexes, the task text, the segments, the warnings.

Times are nanoseconds on the mapping's clock (``timeline.source``); the episode's zero is the first
message of any mapped camera or curve topic.

Memory stays small however long the episode: camera bytes go to disk as they are read (a frame pack,
or an Annex-B file the remux then streams), curve numbers go into flat typed arrays. A file that ends
early (truncated recording, no footer) keeps what was read before the break, with a warning.
"""
from __future__ import annotations

import base64
import io
import json
import os
import pathlib
from array import array
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import annexb as AB
from . import annotations as A
from . import mcap_messages as M
from .mcap_mapping import check_mapping


#: the first samples of a video camera kept in memory, to read its picture size from
VIDEO_HEAD = 30


@dataclass
class SeriesData:
    topic: str
    times_ns: array = field(default_factory=lambda: array("q"))
    values: array = field(default_factory=lambda: array("d"))     # the rows back to back
    lengths: array = field(default_factory=lambda: array("I"))
    labels: list[str] | None = None

    def add(self, t_ns: int, row: list[float]) -> None:
        self.times_ns.append(t_ns)
        self.values.extend(row)
        self.lengths.append(len(row))

    def matrix(self) -> tuple[np.ndarray, np.ndarray]:
        """(times in ns, values ``[n, width]`` padded with NaN), sorted by time."""
        t = np.frombuffer(self.times_ns, dtype=np.int64).copy()
        lengths = np.frombuffer(self.lengths, dtype=np.uint32)
        flat = np.frombuffer(self.values, dtype=np.float64)
        width = int(lengths.max()) if len(lengths) else 0
        if len(lengths) and int(lengths.min()) == width:
            mat = flat.reshape(len(lengths), width).copy()
        else:
            mat = np.full((len(lengths), width), np.nan, dtype=np.float64)
            pos = 0
            for i, n in enumerate(lengths.tolist()):
                mat[i, :n] = flat[pos:pos + n]
                pos += n
        if len(t) > 1 and np.any(np.diff(t) < 0):
            order = np.argsort(t, kind="stable")
            t, mat = t[order], mat[order]
        return t, mat


def _camera_key(topic: str) -> str:
    import re

    return re.sub(r"[^0-9A-Za-z_-]+", "_", topic.lstrip("/")).strip("_")[:96] or "camera"


def camera_keys(mapping: dict) -> dict[str, str]:
    """``{topic: URL-safe key}`` of the mapping's cameras (``/robot0/sensor/camera0/compressed`` ->
    ``robot0_sensor_camera0_compressed``, as the check reader names the videos)."""
    out, used = {}, set()
    for c in mapping.get("cameras") or []:
        base = _camera_key(c["topic"])
        key, n = base, 2
        while key in used:
            key, n = f"{base}_{n}", n + 1
        used.add(key)
        out[c["topic"]] = key
    return out


def _row(decoded, entry: dict) -> tuple[list[float], list[str]]:
    """The numbers of one message for one series entry, and their labels."""
    fields = entry.get("fields") or [None]
    transforms = entry.get("transforms") or {}
    values, labels = [], []
    for f in fields:
        nums, names = M.select_labeled(decoded, f)
        t = transforms.get(f) if f else None
        out = M.apply_transform(nums, t)
        values += out
        labels += names if len(out) == len(nums) else M.transform_labels(f or "dim", len(nums), t)
    return values, labels


def scan(stream, mapping: dict, out_dir: os.PathLike | str, *, client_decode: bool = False) -> dict:
    """Read the episode; write the products; return the ``episode.json`` document. ``client_decode``
    keeps the H.264 / H.265 cameras' Annex-B with a sample index and leaves their remux for later."""
    from mcap.reader import make_reader

    from .remux import remux_annexb_file

    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    reader = make_reader(stream)
    try:
        reader.get_summary()
    except Exception:  # noqa: BLE001 - no valid footer (a truncated recording): read it from the top
        from mcap.reader import NonSeekingReader

        stream.seek(0)
        reader = NonSeekingReader(stream)
    timeline = mapping.get("timeline") or {}
    tsource = timeline.get("source") or "log_time"
    tfield = timeline.get("timestamp_field")
    keys = camera_keys(mapping)
    series_entries = {s["topic"]: s for s in mapping.get("series") or []}
    task_spec = mapping.get("task") if isinstance(mapping.get("task"), dict) else None
    seg_spec = mapping.get("segments") if isinstance(mapping.get("segments"), dict) else None
    topics = set(keys) | set(series_entries)
    if task_spec and task_spec.get("topic"):
        topics.add(task_spec["topic"])
    if seg_spec and seg_spec.get("topic"):
        topics.add(seg_spec["topic"])
    dec = M.Decoder()
    cams: dict[str, dict] = {t: {"key": k, "codec": None, "t": [], "offset": [], "size": [], "head": [],
                                 "width": None, "height": None,
                                 # client decode: per sample keyframe flags, the parameter sets seen so far,
                                 # the first keyframe, the sets in force there, B slices, H.265 PPS bits
                                 "kf": [], "sets": {}, "first_key": None, "config": None, "b": False,
                                 "pps_bits": {}, "key_head": []} for t, k in keys.items()}
    files: dict[str, Any] = {}                   # topic -> the open .frames.part / .annexb.part
    series = {t: SeriesData(t) for t in series_entries}
    task_text = ""
    segments: list[dict] = []
    warnings: list[dict] = []
    seen: set[str] = set()
    broke: str | None = None
    # file order: a video's samples must reach the decoder in the order they were written, a file
    # without a summary streams up to its break instead of being read whole to be sorted, and on TOS
    # the chunks come in one forward sweep; frames and curves are put in time order afterwards
    messages = reader.iter_messages(topics=sorted(topics), log_time_order=False)
    try:
        while True:
            try:
                schema, channel, message = next(messages)
            except StopIteration:
                break
            except Exception as exc:  # noqa: BLE001 - a truncated or corrupt file: keep what was read
                broke = (f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)[:200]
                break
            topic = channel.topic
            seen.add(topic)
            decoded = dec.decode(channel, schema, message)
            if decoded is None:
                continue
            t_ns = M.message_time(message, channel, decoded, tsource, tfield)
            if topic in cams:
                frame = M.as_frame(decoded)
                if frame is None:
                    continue
                fmt, data = frame
                cam = cams[topic]
                codec = cam["codec"] or M.frame_codec(fmt, data)
                cam["codec"] = codec
                if codec in ("jpeg", "png", "h264", "h265"):
                    fh = files.get(topic)
                    if fh is None:
                        video = codec in ("h264", "h265")
                        fh = files[topic] = open(out / f"{cam['key']}.{'annexb' if video else 'frames'}.part", "wb")
                        if not video:
                            cam["width"], cam["height"] = M.picture_size(codec, data)
                    cam["offset"].append(fh.tell())
                    cam["size"].append(len(data))
                    fh.write(data)
                    if codec in ("h264", "h265") and len(cam["head"]) < VIDEO_HEAD:
                        cam["head"].append(data)
                    if codec in ("h264", "h265") and client_decode:
                        _note_sample(cam, codec, data)
                cam["t"].append(t_ns)
            if topic in series:
                values, labels = _row(decoded, series_entries[topic])
                sd = series[topic]
                if sd.labels is None:
                    entry = series_entries[topic]
                    names = M.names_at(decoded, entry.get("names_field")) if entry.get("names_field") else []
                    given = entry.get("labels")
                    if given and len(given) == len(values):
                        sd.labels = list(given)
                    elif names and len(names) == len(values):
                        sd.labels = names
                    else:
                        sd.labels = labels if len(labels) == len(values) else [f"dim_{i}" for i in range(len(values))]
                sd.add(t_ns, values)
            if task_spec and topic == task_spec.get("topic") and not task_text:
                task_text = M.text_of(decoded, task_spec.get("field"))
            if seg_spec and topic == seg_spec.get("topic"):
                start = M.select(decoded, seg_spec["start_field"])
                end = M.select(decoded, seg_spec["end_field"])
                label = M.text_of(decoded, seg_spec["label_field"])
                if start and end:
                    segments.append({"start": start[0], "end": end[0], "label": label})
    finally:
        for fh in files.values():
            fh.close()
    if task_spec and task_spec.get("metadata_key") and not task_text:
        try:
            for rec in reader.iter_metadata():
                v = (rec.metadata or {}).get(task_spec["metadata_key"])
                if v:
                    task_text = str(v).strip()
                    break
        except Exception:  # noqa: BLE001
            pass
    attachment_doc = None
    if seg_spec and seg_spec.get("attachment"):
        try:
            for att in reader.iter_attachments():
                if att.name == seg_spec["attachment"]:
                    attachment_doc = M.load_json_bytes(att.data)
                    break
        except Exception:  # noqa: BLE001
            attachment_doc = None
    for t in sorted(topics - seen):
        if broke is None:
            warnings.append({"code": "topic_missing", "message": f"这条 episode 里没有 topic {t} 的消息"})
    curves = {topic: sd.matrix() for topic, sd in series.items() if len(sd.times_ns)}
    firsts = [min(c["t"]) for c in cams.values() if c["t"]] + [int(t[0]) for t, _ in curves.values()]
    lasts = [max(c["t"]) for c in cams.values() if c["t"]] + [int(t[-1]) for t, _ in curves.values()]
    if not firsts:
        if broke is not None:
            raise ValueError(f"文件读不出映射里的消息（{broke}）")
        raise ValueError("映射里的相机与曲线 topic 在这条 episode 里都没有消息")
    zero = min(firsts)
    end = max(lasts)
    if broke is not None:
        warnings.append({"code": "truncated",
                         "message": f"文件没读完就出错（{broke}），只显示前 {round((end - zero) / 1e9, 1)} 秒"})

    def rel(ns: int) -> float:
        return round((ns - zero) / 1e9, 6)

    cam_docs = {}
    for topic, cam in cams.items():
        key = cam["key"]
        doc = {"topic": topic, "key": key, "codec": cam["codec"], "width": cam["width"], "height": cam["height"],
               "count": len(cam["t"]), "offset_s": rel(cam["t"][0]) if cam["t"] else 0.0}
        if cam["codec"] in ("jpeg", "png") and cam["t"]:
            os.replace(out / f"{key}.frames.part", out / f"{key}.frames")
            order = sorted(range(len(cam["t"])), key=cam["t"].__getitem__)      # the index in time order
            doc.update(t=[rel(cam["t"][i]) for i in order], offset=[cam["offset"][i] for i in order],
                       size=[cam["size"][i] for i in order], bytes=int(sum(cam["size"])),
                       offset_s=rel(cam["t"][order[0]]))
        elif cam["codec"] in ("h264", "h265") and cam["t"] and client_decode and _decodable(cam):
            # the browser decodes it (design doc 19 §3): keep the Annex-B, index it from the first
            # keyframe, remux only when a <video> asks (remux_samples)
            k0 = cam["first_key"]
            os.replace(out / f"{key}.annexb.part", out / f"{key}.annexb")
            width, height = M.video_size(cam["codec"], [cam["config"] + cam["key_head"][0]] + cam["key_head"][1:])
            sets = AB.parameter_sets(cam["codec"], cam["config"])
            doc.update(width=width, height=height, mp4=False, samples=True, b_frames=False, skipped=k0,
                       offset_s=rel(cam["t"][k0]), t=[rel(x) for x in cam["t"][k0:]], offset=cam["offset"][k0:],
                       size=cam["size"][k0:], kf=cam["kf"][k0:], bytes=int(cam["offset"][-1] + cam["size"][-1]),
                       codec_string=AB.codec_string(cam["codec"], sets),
                       config=base64.b64encode(cam["config"]).decode("ascii"))
            if k0:
                lead = (cam["t"][k0] - cam["t"][0]) / 1e9
                warnings.append({"code": "leading_frames",
                                 "message": f"相机 {topic} 开头 {k0} 帧在第一个关键帧之前，解不出来，"
                                            f"从 {lead:.2f} 秒处开始播放"})
        elif cam["codec"] in ("h264", "h265") and cam["t"]:
            first = cam["t"][0]
            times = [(x - first) / 1e9 for x in cam["t"]]
            annexb = out / f"{key}.annexb.part"
            try:
                made = remux_annexb_file(str(annexb), times, cam["codec"], str(out / f"{key}.mp4"))
                if made.width is None:
                    made.width, made.height = M.video_size(cam["codec"], cam["head"])
                doc.update(width=made.width, height=made.height, mp4=True, b_frames=made.b_frames,
                           skipped=made.skipped, offset_s=round(rel(first) + made.lead_s, 6))
                if made.skipped:
                    warnings.append({"code": "leading_frames",
                                     "message": f"相机 {topic} 开头 {made.skipped} 帧在第一个关键帧之前，解不出来，"
                                                f"从 {made.lead_s:.2f} 秒处开始播放"})
                if made.b_frames:
                    warnings.append({"code": "b_frames",
                                     "message": f"相机 {topic} 的码流有 B 帧，转封装后的播放顺序可能不对"})
            except Exception as exc:  # noqa: BLE001 - the camera is shown as unplayable, with why
                doc["error"] = f"转封装失败：{exc}"[:200]
            finally:
                annexb.unlink(missing_ok=True)
        elif cam["t"]:
            doc["error"] = f"编码 {cam['codec']} 本期不支持"
        cam_docs[key] = doc
    arrays = {}
    series_docs = {}
    for topic, (t, mat) in curves.items():
        sd = series[topic]
        safe = _camera_key(topic)
        arrays[f"{safe}__t"] = (t - zero).astype(np.float64) / 1e9
        arrays[f"{safe}__v"] = mat
        span = (int(t[-1]) - int(t[0])) / 1e9
        series_docs[topic] = {"key": safe, "labels": list(sd.labels or []), "count": int(len(t)), "width": int(mat.shape[1]),
                              "rate_hz": round((len(t) - 1) / span, 3) if len(t) > 1 and span > 0 else None}
    np.savez(out / "series.npz", **arrays)
    # the frame reference and the checks' clock (design doc 18 §4.4)
    ref = timeline.get("frame_reference")
    actions = [s["topic"] for s in mapping.get("series") or [] if s.get("role") == "action"]
    states = [s["topic"] for s in mapping.get("series") or [] if s.get("role") == "state"]
    if not ref:
        ref = next((t for t in actions + list(cams) + states if t in seen), None)
    ref_times = (sorted(cams[ref]["t"]) if ref in cams else curves[ref][0].tolist() if ref in curves else [])
    chk = check_mapping(mapping)
    action = chk.get("action")
    anchor_topic = action if isinstance(action, str) else (action[0]["topic"] if action else None)
    anchor_times = curves[anchor_topic][0].tolist() if anchor_topic in curves else []
    if len(anchor_times) > 1:
        span = (anchor_times[-1] - anchor_times[0]) / 1e9
        check_clock = {"offset_s": rel(anchor_times[0]), "fps": round((len(anchor_times) - 1) / span, 6) if span > 0 else 30.0}
    else:
        firsts_cam = [c["t"][0] for c in cams.values() if c["t"]]
        check_clock = {"offset_s": rel(min(firsts_cam)) if firsts_cam else 0.0, "fps": 30.0}
    if attachment_doc is not None:
        ann = A.argus_annotations(attachment_doc, source=f"附件 {seg_spec['attachment']}")
        segs = ann.tracks[0]["segments"] if ann.tracks else []
    else:
        segs = []
        for s in segments:
            a, b = _seg_time(s["start"], zero), _seg_time(s["end"], zero)
            if a is not None and b is not None:
                segs.append({"start_s": a, "end_s": max(a, b), "label": s["label"], "quality": None,
                             "contribution": None, "arm": None, "flags": []})
        segs.sort(key=lambda x: x["start_s"])
    doc = {"zero_ns": zero, "duration_s": rel(end) + (1.0 / check_clock["fps"] if check_clock["fps"] else 0.0),
           "frame_reference": ref, "frame_times": [rel(x) for x in ref_times], "check_clock": check_clock,
           "cameras": cam_docs, "series": series_docs, "task": task_text, "segments": segs,
           "segments_source": (f"附件 {seg_spec['attachment']}" if seg_spec and seg_spec.get("attachment")
                               else f"topic {seg_spec['topic']}" if seg_spec else None),
           "warnings": warnings}
    (out / "episode.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return doc


def _note_sample(cam: dict, codec: str, data: bytes) -> None:
    """Client decode: what one written sample tells - its keyframe flag, the parameter sets it brings,
    whether it is a B slice; the first keyframe and the sets in force there."""
    sets = AB.parameter_sets(codec, data)
    if sets:
        cam["sets"].update(sets)
        if codec == "h265" and AB.H265_PPS in sets:
            bits = AB.pps_extra_bits(sets[AB.H265_PPS])
            if bits is not None:
                cam["pps_bits"][bits[0]] = bits[1]
    key = AB.is_keyframe(codec, data)
    cam["kf"].append(key)
    if key and cam["first_key"] is None and AB.has_all(codec, cam["sets"]):
        cam["first_key"] = len(cam["kf"]) - 1
        cam["config"] = AB.joined(codec, cam["sets"])
    if cam["first_key"] is not None and len(cam["key_head"]) < VIDEO_HEAD:
        cam["key_head"].append(data)
    if not cam["b"] and AB.has_b_slice(codec, data, cam["pps_bits"]):
        cam["b"] = True


def _decodable(cam: dict) -> bool:
    """A camera the browser can decode from its sample pack: a keyframe with its parameter sets, no B
    slices (decode order would not be display order). The others are remuxed now, as before."""
    return cam["first_key"] is not None and bool(cam["config"]) and not cam["b"]


class _Tail(io.RawIOBase):
    """``config`` followed by ``path`` from byte ``start``: the sample pack from its first keyframe with
    the parameter sets in front, as one stream (the remux's input)."""

    def __init__(self, config: bytes, path: os.PathLike | str, start: int):
        super().__init__()
        self.head, self.fh, self.start = config, open(path, "rb"), int(start)
        self.size = len(config) + os.path.getsize(path) - self.start
        self.pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = max(0, base + offset)
        return self.pos

    def tell(self) -> int:
        return self.pos

    def readinto(self, buf) -> int:
        if self.pos >= self.size:
            return 0
        n = min(len(buf), self.size - self.pos)
        if self.pos < len(self.head):
            chunk = self.head[self.pos:self.pos + n]
        else:
            self.fh.seek(self.start + self.pos - len(self.head))
            chunk = self.fh.read(n)
        buf[:len(chunk)] = chunk
        self.pos += len(chunk)
        return len(chunk)

    def close(self) -> None:
        self.fh.close()
        super().close()


def remux_samples(out_dir: os.PathLike | str, cam_doc: dict) -> dict:
    """The fMP4 of a camera kept for client decode, made when a ``<video>`` first asks for it: the sample
    pack from its first keyframe, the parameter sets in front, stamped with the index's times. Returns
    what changes in the camera's ``episode.json`` entry (``mp4``, ``b_frames``)."""
    from .remux import remux_annexb_file

    out = pathlib.Path(out_dir)
    key = cam_doc["key"]
    config = base64.b64decode(cam_doc["config"])
    t0 = cam_doc["t"][0]
    src = _Tail(config, out / f"{key}.annexb", cam_doc["offset"][0])
    try:
        made = remux_annexb_file(src, [x - t0 for x in cam_doc["t"]], cam_doc["codec"], str(out / f"{key}.mp4"))
    finally:
        src.close()
    return {"mp4": True, "b_frames": made.b_frames}


def _seg_time(v: float, zero_ns: int) -> float | None:
    """A segment bound on the episode clock: nanoseconds or epoch seconds are absolute, anything
    smaller is seconds from the episode's start."""
    if v is None or not np.isfinite(v):
        return None
    if abs(v) > 1e12:
        return round((v - zero_ns) / 1e9, 6)
    if abs(v) > 1e8:
        return round((v * 1e9 - zero_ns) / 1e9, 6)
    return round(float(v), 6)
