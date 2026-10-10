"""Probing one mcap episode file for its mapping (design doc 18 §6.4 step 2).

The summary section (channels, schemas, counts, metadata, attachments) plus the first message of
every topic - for a compressed video topic the first few, until a picture size can be decoded. One
pass in time order over the head of the file settles the topics that start there (most files have
every topic in their first chunk); a topic still unsettled after it is read on its own through the
chunk indexes, which costs the one chunk it starts in. A file on TOS costs a few ranged reads, never
a download (measured on the sample set: about one chunk plus one per late topic, instead of one
chunk per topic).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import depth as D
from . import mcap_messages as M

#: messages of a video topic read at most to find its picture size (a recording that starts mid-GOP
#: has no parameter sets before its first keyframe; ten seconds at 30 fps)
VIDEO_PROBE_MESSAGES = 300

#: the shared pass over the head of the file stops this long after the first message (seconds)
HEAD_S = 1.0


@dataclass
class TopicProbe:
    topic: str
    schema: str | None
    schema_encoding: str | None
    message_encoding: str | None
    count: int | None
    first_ns: int | None = None
    kind: str = "unknown"                     # camera | depth | series | text | other | unknown
    codec: str | None = None
    format: str | None = None                 # the first picture message's own format string
    width: int | None = None
    height: int | None = None
    fields: list[dict] | None = None          # [{path, size}] of the first message
    leaves: list[str] | None = None           # the first message's numeric leaf paths (at most 512)
    image: dict | None = None                 # a raw image's encoding, size and row stride
    names: list[str] | None = None            # a name list (JointState.name)
    strings: list[str] | None = None          # paths of the first message's text fields
    text: str | None = None
    decodable: bool = True
    #: whether the check reader (an A-class file) reads numbers at each field path of the first
    #: message, and from the whole message (``_extract_source`` / ``_as_vector``); design doc 18 §6.2
    check_fields: dict[str, bool] | None = None
    check_whole: bool | None = None
    #: a camera calibration topic's first message (CameraCalibration / CameraInfo): K, the model and coefficients,
    #: the picture size - what a dataset declaration drafts a camera's intrinsics from (design doc 25 §3.3)
    calibration: dict | None = None

    def rate_hz(self, end_ns: int | None) -> float | None:
        if not self.count or self.count < 2 or self.first_ns is None or end_ns is None or end_ns <= self.first_ns:
            return None
        return round((self.count - 1) / ((end_ns - self.first_ns) / 1e9), 2)


@dataclass
class FileProbe:
    file: str
    topics: dict[str, TopicProbe]
    metadata: dict[str, dict[str, str]] = field(default_factory=dict)
    attachments: list[dict] = field(default_factory=list)
    start_ns: int | None = None
    end_ns: int | None = None
    error: str | None = None


def probe(stream, name: str) -> FileProbe:
    from mcap.reader import make_reader

    reader = make_reader(stream)
    try:
        summary = reader.get_summary()
    except Exception as exc:  # noqa: BLE001 - truncated / foreign file: say so
        return FileProbe(name, {}, error=f"读不到 summary：{type(exc).__name__}: {exc}"[:300])
    if summary is None:
        return FileProbe(name, {}, error="这个 mcap 文件没有 summary 段（录制被截断？）")
    st = summary.statistics
    counts: dict[int, int] = dict(st.channel_message_counts or {}) if st else {}
    topics: dict[str, TopicProbe] = {}
    for cid, ch in sorted((summary.channels or {}).items()):
        sc = (summary.schemas or {}).get(ch.schema_id)
        prev = topics.get(ch.topic)
        count = counts.get(cid) if st else None
        if prev is not None:
            prev.count = (prev.count or 0) + (count or 0)
            continue
        topics[ch.topic] = TopicProbe(ch.topic, getattr(sc, "name", None) or None,
                                      getattr(sc, "encoding", None) or None, ch.message_encoding or None, count)
    out = FileProbe(name, topics, start_ns=int(st.message_start_time) if st else None,
                    end_ns=int(st.message_end_time) if st else None)
    try:
        for rec in reader.iter_metadata():
            out.metadata.setdefault(rec.name, {}).update({str(k): str(v) for k, v in (rec.metadata or {}).items()})
    except Exception:  # noqa: BLE001 - metadata is optional
        pass
    for idx in getattr(summary, "attachment_indexes", None) or []:
        out.attachments.append({"name": idx.name, "media_type": idx.media_type or "", "size": int(idx.data_size)})
    dec = M.Decoder()
    state: dict[str, _State] = {t: _State() for t in topics}
    pending = set(topics)
    try:
        head_end = None
        for schema, channel, message in reader.iter_messages():
            if head_end is None:
                head_end = int(message.log_time) + int(HEAD_S * 1e9)
            elif int(message.log_time) > head_end:
                break
            if channel.topic in pending and _feed(dec, topics[channel.topic], state[channel.topic],
                                                  schema, channel, message):
                pending.discard(channel.topic)
                if not pending:
                    break
    except Exception:  # noqa: BLE001 - a bad chunk ends the shared pass; the topics go on alone
        pass
    for topic in sorted(pending):
        _alone(reader, dec, topics[topic], state[topic])
    return out


@dataclass
class _State:
    fed: int = 0                                  # messages of the topic seen so far
    sizer: M.VideoSizer | None = None


def _feed(dec: M.Decoder, tp: TopicProbe, st: _State, schema, channel, message) -> bool:
    """One message of ``tp``'s topic; True once the topic is settled."""
    st.fed += 1
    try:
        if tp.first_ns is None:
            tp.first_ns = int(message.log_time)
        decoded = dec.decode(channel, schema, message)
        if decoded is None:
            tp.decodable = channel.message_encoding not in ("protobuf", "cdr", "json")
            tp.kind = "unknown"
            return True
        raw = M.raw_kind(getattr(schema, "name", None), decoded)
        if raw == "pointcloud":                    # 3-D points: no picture, no curve (design doc 21 §2)
            tp.kind = "other"
            return True
        if raw == "raw":
            tp.image = M.raw_image_info(decoded)
            depth = D.raw_depth_codec(tp.image["encoding"])       # 16UC1 / mono16 / 32FC1: depth (design doc 21 §5.4)
            tp.kind, tp.codec = ("depth", depth) if depth else ("camera", "raw")
            tp.width, tp.height = tp.image["width"], tp.image["height"]
            return True
        frame = M.as_frame(decoded)
        if frame is not None:
            fmt, data = frame
            depth = D.frame_depth_codec(fmt, data)
            if depth:                                  # a 16-bit PNG, a ROS compressedDepth picture
                tp.kind, tp.codec, tp.format = "depth", depth, fmt or None
                tp.width, tp.height = D.depth_size(depth, data)
                return True
            tp.kind = "camera"
            tp.format = fmt or None
            tp.codec = M.frame_codec(fmt, data)
            if tp.codec in ("jpeg", "png"):
                tp.width, tp.height = M.picture_size(tp.codec, data)
                return True
            if tp.codec in ("h264", "h265"):
                if st.sizer is None:
                    st.sizer = M.VideoSizer(tp.codec)
                size = st.sizer.feed(data)
                if size:
                    tp.width, tp.height = size
                return bool(size) or st.fed >= VIDEO_PROBE_MESSAGES
            return True
        if "camerainfo" in str(tp.schema or "").lower().replace("_", "") or \
                "cameracalibration" in str(tp.schema or "").lower():
            tp.calibration = calibration_of(decoded)
        text = M.text_of(decoded)
        fields = M.field_sizes(decoded)
        tp.strings = M.string_paths(decoded) or None
        if fields:
            tp.kind = "series"
            tp.fields = fields
            tp.leaves = [path for path, _ in M.leaves(decoded, limit=512)]
            tp.names = M.names_at(decoded, "name") or None
            tp.check_fields, tp.check_whole = _check_reads(decoded, fields)
        elif text:
            tp.kind = "text"
            tp.text = text[:500]
        else:
            tp.kind = "other"
        return True
    except Exception:  # noqa: BLE001 - an unreadable topic stays unknown; the probe goes on
        tp.decodable = False
        return True


#: a camera_info distortion model -> the declaration's model (design doc 25 §3.2)
_MODELS = {"plumb_bob": "opencv_brown", "rational_polynomial": "opencv_brown", "equidistant": "opencv_fisheye",
           "kannala_brandt": "opencv_fisheye", "fisheye": "opencv_fisheye", "": "pinhole", "pinhole": "pinhole"}


def calibration_of(decoded) -> dict | None:
    """The intrinsics a CameraCalibration (foxglove: ``K``, ``D``) or sensor_msgs CameraInfo (``k`` / ``K``, ``d`` /
    ``D``) message gives, in the declaration's terms; None when it does not say them."""
    def get(*names):
        for n in names:
            v = decoded.get(n) if isinstance(decoded, dict) else getattr(decoded, n, None)
            if v is not None:
                return v
        return None

    try:
        K = [float(x) for x in list(get("K", "k") or [])]
        if len(K) != 9 or K[0] <= 0 or K[4] <= 0:
            return None
        model = _MODELS.get(str(get("distortion_model") or "").lower())
        if model is None:
            return None
        D = [float(x) for x in list(get("D", "d") or [])]
        if model == "pinhole" or not any(D):
            model, D = ("pinhole", []) if not any(D) else (model, D)
        if model == "opencv_brown":
            D = (D + [0.0] * 5)[:5] if len(D) <= 5 else D[:8]
        elif model == "opencv_fisheye":
            D = (D + [0.0] * 4)[:4]
        out = {"K": [K[0:3], K[3:6], K[6:9]], "model": model, "coefficients": D}
        w, h = int(get("width") or 0), int(get("height") or 0)
        if w > 0 and h > 0:
            out["image_size_wh"] = [w, h]
        return out
    except (TypeError, ValueError):
        return None


def _check_reads(decoded, fields: list[dict]) -> tuple[dict[str, bool], bool]:
    from ..ingest import mcap_reader as MR

    paths = set()
    for f in fields:
        parts = f["path"].split(".")
        paths.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
    reads = {}
    for p in sorted(paths):
        try:
            reads[p] = MR._extract_source(decoded, p) is not None
        except Exception:  # noqa: BLE001 - what the reader cannot take is a gap
            reads[p] = False
    try:
        whole = MR._as_vector(decoded) is not None
    except Exception:  # noqa: BLE001
        whole = False
    return reads, whole


def _alone(reader, dec: M.Decoder, tp: TopicProbe, st: _State) -> None:
    """A topic the shared pass did not settle: its own messages through the chunk indexes."""
    skip = st.fed                         # the shared pass already fed these
    try:
        for schema, channel, message in reader.iter_messages(topics=[tp.topic]):
            if skip:
                skip -= 1
                continue
            if _feed(dec, tp, st, schema, channel, message):
                return
    except Exception:  # noqa: BLE001 - an unreadable topic stays unknown; the probe goes on
        tp.decodable = False
