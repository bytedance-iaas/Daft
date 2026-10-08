"""Decoded mcap messages, read the visualizer's way (design doc 18 §6).

The check reader's flattening (``ingest.mcap_reader._flatten_numeric``, A-class) drops protobuf
repeated numbers and nested ROS 2 messages; the visualizer reads every numeric leaf: protobuf
messages field by field (repeated scalars included), ROS 2 messages by their slots, dicts in
order, lists and arrays element by element. Paths are dotted (``pose.position.x``, ``q.3``);
``*`` in a selection path takes every element of a list or every field of a message.

Also here: what a compressed frame is (JPEG / PNG / H.264 / H.265 from its format string or its
bytes) and how big its picture is, without decoding it.
"""
from __future__ import annotations

import json
import math
import struct
from typing import Any, Iterator

import numpy as np

#: leaves that describe a message rather than measure something (as the check reader skips them)
SKIP = frozenset({"timestamp", "sequence_num", "frame_id", "header", "stamp", "seq"})


class Decoder:
    """Decodes messages of any channel encoding the reader knows (json, protobuf, cdr)."""

    def __init__(self):
        self._factories: dict = {}
        self._cache: dict = {}

    def decode(self, channel, schema, message):
        from ..ingest import mcap_reader as MR

        try:
            return MR._decode(channel, schema, message, self._factories, self._cache)
        except Exception:  # noqa: BLE001 - an undecodable topic answers None; the probe says so
            return None


def _fields_of(node) -> list[tuple[str, Any]] | None:
    """(name, value) of a decoded message, or None when it is a leaf / list."""
    desc = getattr(node, "DESCRIPTOR", None)
    if desc is not None and hasattr(desc, "fields"):
        return [(f.name, getattr(node, f.name)) for f in desc.fields]
    if isinstance(node, dict):
        return list(node.items())
    slots = getattr(type(node), "__slots__", None)
    if slots and not isinstance(node, (str, bytes)):
        names = [s.lstrip("_") for s in slots if s not in ("__weakref__", "__dict__")]
        return [(n, getattr(node, n, None)) for n in names]
    fields = getattr(node, "get_fields_and_field_types", None)
    if callable(fields):
        return [(n, getattr(node, n, None)) for n in fields()]
    if hasattr(node, "__dict__") and not isinstance(node, (list, tuple, np.ndarray)):
        d = {k: v for k, v in vars(node).items() if not k.startswith("_")}
        return list(d.items()) if d else None
    return None


def _is_sequence(node) -> bool:
    if isinstance(node, (str, bytes, bytearray, dict)):
        return False
    if isinstance(node, (list, tuple, np.ndarray)):
        return True
    return hasattr(node, "__len__") and hasattr(node, "__getitem__") and _fields_of(node) is None


def _number(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float, np.integer, np.floating)):
        f = float(v)
        return f if math.isfinite(f) else float("nan")
    return None


def leaves(node, prefix: str = "", *, skip: frozenset = SKIP, limit: int = 4096) -> Iterator[tuple[str, float]]:
    """Every numeric leaf of a decoded message: (dotted path, value), in declaration order."""
    count = 0

    def walk(n, path):
        nonlocal count
        if count >= limit:
            return
        num = _number(n)
        if num is not None:
            count += 1
            yield path, num
            return
        if isinstance(n, (str, bytes, bytearray)) or n is None:
            return
        if _is_sequence(n):
            for i, x in enumerate(list(n)):
                yield from walk(x, f"{path}.{i}" if path else str(i))
            return
        fields = _fields_of(n)
        for name, value in fields or []:
            if name in skip:
                continue
            yield from walk(value, f"{path}.{name}" if path else name)

    yield from walk(node, prefix)


def field_sizes(node, *, max_fields: int = 64) -> list[dict]:
    """The numeric fields of a message by their top paths: ``[{path, size}]`` (``pose.position``
    holds 3, ``q`` holds 7) - what the mapping table offers to pick from."""
    sizes: dict[str, int] = {}
    order: list[str] = []
    for path, _ in leaves(node):
        parts = path.split(".")
        # group by the path up to the first list index or the second level
        key_parts = []
        for p in parts:
            if p.isdigit():
                break
            key_parts.append(p)
        key = ".".join(key_parts[:2]) if len(key_parts) > 2 else ".".join(key_parts)
        key = key or path
        if key not in sizes:
            order.append(key)
            sizes[key] = 0
        sizes[key] += 1
    return [{"path": k, "size": sizes[k]} for k in order[:max_fields]]


def _get(node, name: str):
    if isinstance(node, dict):
        return node.get(name)
    if _is_sequence(node) and name.isdigit():
        seq = list(node)
        i = int(name)
        return seq[i] if i < len(seq) else None
    return getattr(node, name, None)


def select(node, path: str | None) -> list[float]:
    """The numbers at ``path`` (all numeric leaves under it; ``*`` fans out). None / "" = the
    whole message read by shape (``position``, ``data``, a bare list), as the check reader does,
    else every numeric leaf."""
    if not path:
        for attr in ("position", "data"):
            v = _get(node, attr)
            if v is not None and _is_sequence(v):
                vals = [x for _, x in leaves(v)]
                if vals:
                    return vals
        if _is_sequence(node):
            return [x for _, x in leaves(node)]
        return [x for _, x in leaves(node)]
    nodes = [node]
    for part in path.split("."):
        nxt = []
        for n in nodes:
            if n is None:
                continue
            if part == "*":
                if _is_sequence(n):
                    nxt += list(n)
                else:
                    nxt += [v for k, v in (_fields_of(n) or []) if k not in SKIP]
            else:
                nxt.append(_get(n, part))
        nodes = nxt
    out: list[float] = []
    for n in nodes:
        if n is None:
            continue
        num = _number(n)
        if num is not None:
            out.append(num)
        else:
            out += [x for _, x in leaves(n, skip=frozenset())]
    return out


def string_paths(node, prefix: str = "", depth: int = 0) -> list[str]:
    """Paths of the text fields of a message (one level of nesting)."""
    out = []
    for name, value in _fields_of(node) or []:
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, str):
            out.append(path)
        elif depth < 1 and value is not None and not _is_sequence(value) and _number(value) is None:
            out += string_paths(value, path, depth + 1)
    return out


def select_labeled(node, path: str | None) -> tuple[list[float], list[str]]:
    """``select`` with a name per number: the leaf paths under ``path`` (``pose.position.x``)."""
    if path and "*" not in path:
        n = node
        for part in path.split("."):
            n = _get(n, part) if n is not None else None
        if n is None:
            return [], []
        num = _number(n)
        if num is not None:
            return [num], [path]
        pairs = list(leaves(n, path, skip=frozenset()))
        return [v for _, v in pairs], [p for p, _ in pairs]
    vals = select(node, path)
    base = path or "dim"
    return vals, ([base] if len(vals) == 1 and path else [f"{base}_{i}" if not path else f"{base}.{i}" for i in range(len(vals))])


def names_at(node, path: str | None) -> list[str]:
    """Strings at ``path`` (JointState ``name``)."""
    if not path:
        return []
    v = node
    for part in path.split("."):
        v = _get(v, part) if v is not None else None
    if v is None or isinstance(v, (str, bytes)):
        return [v] if isinstance(v, str) else []
    try:
        return [str(x) for x in list(v)]
    except TypeError:
        return []


def text_of(node, field: str | None = None) -> str:
    """The text of a message: ``field``, else ``data`` / ``text`` / a bare string / JSON string."""
    v = node
    if field:
        for part in field.split("."):
            v = _get(v, part) if v is not None else None
    if isinstance(v, (bytes, bytearray)):
        try:
            v = v.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    if isinstance(v, str):
        return v.strip()
    if field:
        return ""
    for attr in ("data", "text", "instruction", "task"):
        t = _get(node, attr)
        if isinstance(t, str):
            return t.strip()
    return ""


# ---------------------------------------------------------------- transforms (display only)

def quat_to_rpy(x: float, y: float, z: float, w: float) -> tuple[float, float, float]:
    sinr = 2 * (w * x + y * z)
    cosr = 1 - 2 * (x * x + y * y)
    roll = math.atan2(sinr, cosr)
    sinp = 2 * (w * y - z * x)
    pitch = math.copysign(math.pi / 2, sinp) if abs(sinp) >= 1 else math.asin(sinp)
    siny = 2 * (w * z + x * y)
    cosy = 1 - 2 * (y * y + z * z)
    return roll, pitch, math.atan2(siny, cosy)


def apply_transform(values: list[float], name: str | None) -> list[float]:
    if not name:
        return values
    if name in ("quat_xyzw_to_rpy", "quat_wxyz_to_rpy") and len(values) == 4:
        x, y, z, w = values if name == "quat_xyzw_to_rpy" else (values[1], values[2], values[3], values[0])
        return list(quat_to_rpy(x, y, z, w))
    if name == "deg_to_rad":
        return [math.radians(v) for v in values]
    if name == "rad_to_deg":
        return [math.degrees(v) for v in values]
    return values


def transform_labels(base: str, size: int, name: str | None) -> list[str]:
    if name in ("quat_xyzw_to_rpy", "quat_wxyz_to_rpy") and size == 4:
        return [f"{base}.roll", f"{base}.pitch", f"{base}.yaw"]
    if size == 1:
        return [base]
    return [f"{base}.{i}" for i in range(size)]


# ---------------------------------------------------------------- frames

def _attr(node, name: str):
    return node.get(name) if isinstance(node, dict) else getattr(node, name, None)


def raw_kind(schema_name: str | None, decoded) -> str | None:
    """``pointcloud`` / ``raw`` for messages that carry bytes without being a compressed picture, else
    None (:func:`as_frame` decides). A point cloud (foxglove ``PointCloud``, ROS ``PointCloud2``) is not a
    camera; a raw image (foxglove ``RawImage``, ROS ``sensor_msgs/Image``: ``encoding`` + ``step``
    instead of ``format``) is a picture the browser cannot show as it is (design doc 21 §2)."""
    short = (schema_name or "").replace("/msg/", "/").rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    if decoded is None:
        return None
    if "PointCloud" in short or _attr(decoded, "point_stride") is not None or (
            _attr(decoded, "point_step") is not None and _attr(decoded, "row_step") is not None):
        return "pointcloud"
    if isinstance(_attr(decoded, "encoding"), str) and _attr(decoded, "step") is not None and (
            _attr(decoded, "data") is not None) and _attr(decoded, "format") in (None, ""):
        return "raw"
    return None


def raw_image_bytes(decoded) -> bytes:
    """The pixel bytes of a raw image message."""
    return bytes(_attr(decoded, "data") or b"")


def raw_image_info(decoded) -> dict:
    """``{encoding, width, height, step, bigendian}`` of a raw image message."""
    def num(name):
        v = _attr(decoded, name)
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {"encoding": str(_attr(decoded, "encoding") or ""), "width": num("width"), "height": num("height"),
            "step": num("step"), "bigendian": bool(_attr(decoded, "is_bigendian") or False)}


def as_frame(decoded) -> tuple[str, bytes] | None:
    """(format, bytes) of a compressed image / video message (``.format`` + ``.data``)."""
    from ..ingest import mcap_reader as MR

    return MR._as_frame(decoded)


def frame_codec(fmt: str, data: bytes) -> str:
    """jpeg / png / h264 / h265 / av1 / raw / unknown, from the format string and the bytes."""
    f = (fmt or "").lower()
    if "jpeg" in f or "jpg" in f:
        return "jpeg"
    if "png" in f:
        return "png"
    if "265" in f or "hevc" in f:
        return "h265"
    if "264" in f or "avc" in f:
        return "h264"
    if "av1" in f:
        return "av1"
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    head = _first_nal(data)
    if head is not None:
        # an HEVC NAL header is two bytes, the second 0x01 for layer 0 / temporal id 1; the usual
        # first units are VPS / SPS / PPS / AUD / SEI / IDR / CRA / TRAIL (0x40 0x42 0x44 0x46 0x4e
        # 0x26 0x28 0x2a 0x02); an H.264 header is one byte (SPS 0x67, PPS 0x68, SEI 0x06 ...)
        first, second = head
        if first in (0x40, 0x42, 0x44, 0x46, 0x4E, 0x26, 0x28, 0x2A, 0x02) and second == 0x01:
            return "h265"
        return "h264"
    return "unknown"


def _first_nal(data: bytes) -> tuple[int, int] | None:
    for start in (b"\x00\x00\x00\x01", b"\x00\x00\x01"):
        if data.startswith(start) and len(data) > len(start) + 1:
            return data[len(start)], data[len(start) + 1]
    return None


def picture_size(codec: str, data: bytes) -> tuple[int | None, int | None]:
    """(width, height) from a JPEG SOF or a PNG IHDR; video sizes come from decoding."""
    if codec == "jpeg":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + length
        return None, None
    if codec == "png" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    return None, None


class VideoSizer:
    """(width, height) of an Annex-B stream fed one sample at a time. The decoder learns the size from
    the first parameter sets it can use, which may be well into the stream: a recording that starts
    mid-GOP (GenRobot) has no SPS before its first keyframe, about a second in."""

    def __init__(self, codec: str):
        import av

        self.ctx = av.CodecContext.create("hevc" if codec == "h265" else "h264", "r")
        self.fed = 0

    def feed(self, blob: bytes) -> tuple[int, int] | None:
        self.fed += 1
        try:
            for pkt in self.ctx.parse(blob):
                for frame in self.ctx.decode(pkt):
                    return frame.width, frame.height
        except Exception:  # noqa: BLE001 - samples before the first keyframe do not decode
            pass
        if self.ctx.width and self.ctx.height:
            return self.ctx.width, self.ctx.height
        return None


def video_size(codec: str, samples: list[bytes]) -> tuple[int | None, int | None]:
    """(width, height) of an Annex-B stream from its first samples (None, None when they hold no
    usable parameter sets)."""
    try:
        sizer = VideoSizer(codec)
    except Exception:  # noqa: BLE001 - no decoder for it
        return None, None
    for blob in samples:
        size = sizer.feed(blob)
        if size:
            return size
    return None, None


def message_time(message, channel, decoded, source: str, field: str | None) -> int:
    """The time (ns) that places a message on the clock (C7 ``timeline.source``)."""
    if source == "publish_time":
        return int(message.publish_time)
    if source == "message_timestamp" and field:
        v = decoded
        for part in field.split("."):
            v = _get(v, part) if v is not None else None
        ns = _to_ns(v)
        if ns is not None:
            return ns
    return int(message.log_time)


def _to_ns(v) -> int | None:
    if v is None:
        return None
    num = _number(v)
    if num is not None:
        return int(num * 1e9) if num < 1e12 else int(num)           # seconds, or already ns
    sec = _get(v, "sec") if _get(v, "sec") is not None else _get(v, "seconds")
    nsec = _get(v, "nanosec") if _get(v, "nanosec") is not None else (
        _get(v, "nsec") if _get(v, "nsec") is not None else _get(v, "nanos"))
    if sec is not None:
        return int(sec) * 1_000_000_000 + int(nsec or 0)
    return None


def load_json_bytes(data: bytes):
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
