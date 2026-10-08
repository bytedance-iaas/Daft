"""What a LeRobot ``meta/info.json`` says about features and cameras (design doc 18 §3.2, §4.2).

Feature ``names`` come in several shapes in the wild: a list (``["shoulder_pan.pos", ...]``), a
dict holding one list (``{"motors": [...]}``), a list wrapped in another list (G1), or nothing at
all (RH20T). :func:`flat_names` turns all of them into one list of strings, or None.

Camera codecs are in the feature's ``info`` (v2.1, v3) or ``video_info`` (v2.0) block. A browser
plays AV1, H.264, VP8 / VP9 and - depending on the platform - HEVC; anything else (MPEG-4 Part 2
from FastUMI, MJPEG, ...) is marked ``needs_transcode`` and served by the Daemon as H.264 (D60).
``codec_string`` gives the RFC 6381 ``codecs`` parameter the player checks its decoders with.
"""
from __future__ import annotations

from typing import Any

#: codecs every current desktop browser decodes (HEVC is checked by the player: platform-dependent)
BROWSER_CODECS = frozenset({"h264", "avc1", "av1", "vp8", "vp9", "hevc", "h265"})
_ALIASES = {"avc": "h264", "avc1": "h264", "x264": "h264", "libx264": "h264", "h265": "hevc",
            "hvc1": "hevc", "hev1": "hevc", "x265": "hevc", "libdav1d": "av1", "libaom-av1": "av1",
            "libsvtav1": "av1", "av01": "av1", "mp4v": "mpeg4", "xvid": "mpeg4", "divx": "mpeg4"}
#: numeric dtypes drawn as curves
NUMERIC_DTYPES = frozenset({"float16", "float32", "float64", "int8", "int16", "int32", "int64",
                            "uint8", "uint16", "uint32", "uint64", "bool"})
#: bookkeeping columns: never curves, never annotations
BOOKKEEPING = frozenset({"timestamp", "frame_index", "episode_index", "index", "task_index"})


def norm_codec(codec: Any) -> str | None:
    if not isinstance(codec, str) or not codec.strip():
        return None
    c = codec.strip().lower()
    return _ALIASES.get(c, c)


def needs_transcode(codec: str | None) -> bool:
    """True when no browser plays it; an unknown codec (None) is tried as it is."""
    return codec is not None and norm_codec(codec) not in BROWSER_CODECS


def codec_string(codec: str | None, pix_fmt: str | None = None) -> str | None:
    """The RFC 6381 ``codecs`` value the player asks ``MediaCapabilities`` about. Profiles and
    levels are generic (main / high, level 4): enough to tell which decoders exist."""
    c = norm_codec(codec)
    ten_bit = bool(pix_fmt and "10" in pix_fmt)
    if c == "h264":
        return "avc1.640028"
    if c == "av1":
        return "av01.0.08M.10" if ten_bit else "av01.0.08M.08"
    if c == "hevc":
        return "hvc1.2.4.L120.90" if ten_bit else "hvc1.1.6.L120.90"
    if c == "vp9":
        return "vp09.02.40.10" if ten_bit else "vp09.00.40.08"
    if c == "vp8":
        return "vp8"
    if c == "mpeg4":
        return "mp4v.20.9"
    return None


def flat_names(names: Any, size: int | None = None) -> list[str] | None:
    """One list of dimension names, or None when the feature has none usable."""
    out: list[str] | None = None
    if isinstance(names, dict):
        lists = [v for v in names.values() if isinstance(v, list)]
        if len(lists) == 1:
            out = [str(x) for x in lists[0]]
        elif lists:                                  # several axes named: the longest one
            out = [str(x) for x in max(lists, key=len)]
    elif isinstance(names, list):
        if len(names) == 1 and isinstance(names[0], list):
            out = [str(x) for x in names[0]]
        elif all(isinstance(x, (str, int, float)) for x in names):
            out = [str(x) for x in names]
    if out is not None and size is not None and len(out) != size:
        return None if len(out) < size else out[:size]
    return out or None


def shape_of(feature: dict) -> list[int] | None:
    shape = feature.get("shape")
    if isinstance(shape, (list, tuple)) and all(isinstance(x, int) and not isinstance(x, bool)
                                                 for x in shape):
        return [int(x) for x in shape]
    return None


def width_of(feature: dict) -> int:
    """How many numbers a numeric feature holds per frame (a scalar counts 1)."""
    shape = shape_of(feature) or [1]
    n = 1
    for x in shape:
        n *= max(1, x)
    return n


def features_of(info: dict) -> list[dict]:
    """C2 preflight ``dataset.features``: every feature as declared, names flattened."""
    out = []
    for key, feat in (info.get("features") or {}).items():
        if not isinstance(feat, dict):
            continue
        shape = shape_of(feat)
        dtype = str(feat.get("dtype") or "")
        size = width_of(feat) if dtype in NUMERIC_DTYPES else None
        out.append({"key": str(key), "dtype": dtype, "shape": shape,
                    "names": flat_names(feat.get("names"), size)})
    return out


def video_info(feature: dict) -> dict:
    block = feature.get("info") if isinstance(feature.get("info"), dict) else None
    if block is None and isinstance(feature.get("video_info"), dict):
        block = feature["video_info"]
    return block or {}


def short_camera(feature_key: str) -> str:
    for prefix in ("observation.images.", "observation.image."):
        if feature_key.startswith(prefix):
            return feature_key[len(prefix):]
    return feature_key


def camera_info_of(info: dict) -> list[dict]:
    """C2 preflight ``dataset.camera_info``: one per video feature, in info.json order."""
    fps = info.get("fps")
    out = []
    for key, feat in (info.get("features") or {}).items():
        if not isinstance(feat, dict) or feat.get("dtype") != "video":
            continue
        vi = video_info(feat)
        shape = shape_of(feat) or []
        codec = norm_codec(vi.get("video.codec"))
        width = _int(vi.get("video.width"))
        height = _int(vi.get("video.height"))
        if (width is None or height is None) and len(shape) == 3:
            # [h, w, c] in most datasets, [c, h, w] in a few (G1): the channel axis is the small one
            dims = [x for x in shape if x > 4] if min(shape) <= 4 else shape[:2]
            if len(dims) >= 2:
                height, width = dims[0], dims[1]
        vfps = vi.get("video.fps")
        out.append({"name": short_camera(str(key)), "key": str(key), "codec": codec,
                    "pix_fmt": vi.get("video.pix_fmt") if isinstance(vi.get("video.pix_fmt"), str) else None,
                    "width": width, "height": height,
                    "fps": float(vfps) if _number(vfps) else (float(fps) if _number(fps) else None),
                    "needs_transcode": needs_transcode(codec)})
    return out


def depth_features(info: dict) -> list[str]:
    """uint16 frame columns and ``video.is_depth_map`` videos (shown in phase two)."""
    out = []
    for key, feat in (info.get("features") or {}).items():
        if not isinstance(feat, dict):
            continue
        shape = shape_of(feat) or []
        if feat.get("dtype") in ("uint16", "float32") and len(shape) == 2 and min(shape) > 16 and (
                "depth" in key.lower()):
            out.append(str(key))
        elif feat.get("dtype") == "video" and video_info(feat).get("video.is_depth_map") is True:
            out.append(str(key))
    return out


def image_features(info: dict) -> list[str]:
    """``dtype: image`` frame sequences (not shown in this phase, design doc 18 §2)."""
    return [str(k) for k, f in (info.get("features") or {}).items()
            if isinstance(f, dict) and f.get("dtype") == "image"]


def raw_detail(entry: Any, prefix: str = "") -> dict[str, Any]:
    """A metadata entry key by key as the dataset writes it (design doc 21 §3, D71): nested objects with
    their keys joined by dots (``info.video.codec``), lists as JSON text, scalars as they are."""
    import json

    out: dict[str, Any] = {}
    if not isinstance(entry, dict):
        return out
    for k, v in entry.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(raw_detail(v, key + "."))
        elif isinstance(v, (list, tuple)):
            out[key] = json.dumps(list(v), ensure_ascii=False)
        elif v is None or isinstance(v, (str, bool, int, float)):
            out[key] = v
        else:
            out[key] = str(v)
    return out


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v) -> int | None:
    return int(v) if _number(v) else None
