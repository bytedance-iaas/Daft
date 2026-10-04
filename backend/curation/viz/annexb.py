"""H.264 / H.265 Annex-B access units, as much as the browser's own decoding needs (design doc 19 §3).

An mcap ``CompressedVideo`` message is one access unit: NAL units behind start codes. To let a browser
decode the stream itself (WebCodecs, no ``description`` = Annex-B) the Daemon only has to know, per
message, whether it starts a decodable run (a keyframe: H.264 IDR, H.265 IRAP), which parameter sets
(H.264 SPS / PPS, H.265 VPS / SPS / PPS) the stream has - the browser puts them in front of the keyframe
it starts at, since a recording may send them once - whether it has B slices (decode order is not
display order then, which neither the remux nor this path handles), and the codec string
``VideoDecoder.configure`` wants, read from the SPS (``avc1.PPCCLL``,
``hvc1.<profile>.<compat>.<tier><level>.<constraints>``). Nothing is decoded.
"""
from __future__ import annotations

from typing import Iterator

#: H.264 NAL unit types (ITU-T H.264 Table 7-1)
H264_IDR, H264_SPS, H264_PPS = 5, 7, 8
#: H.265 NAL unit types (ITU-T H.265 Table 7-1): IRAP is 16-23 (BLA, IDR, CRA, reserved IRAP)
H265_VPS, H265_SPS, H265_PPS = 32, 33, 34


def nal_units(data: bytes) -> Iterator[tuple[int, int]]:
    """``(start, end)`` of every NAL unit's bytes (after its start code) in an Annex-B buffer."""
    n = len(data)
    i = data.find(b"\x00\x00\x01")
    while 0 <= i < n:
        start = i + 3
        nxt = data.find(b"\x00\x00\x01", start)
        if nxt < 0:
            yield start, n
            return
        end = nxt - 1 if nxt > start and data[nxt - 1] == 0 else nxt       # a 4-byte start code's zero
        yield start, end
        i = nxt


def nal_type(codec: str, data: bytes, start: int) -> int:
    if start >= len(data):
        return -1
    return (data[start] >> 1) & 0x3F if codec == "h265" else data[start] & 0x1F


def is_keyframe(codec: str, data: bytes) -> bool:
    for s, _ in nal_units(data):
        t = nal_type(codec, data, s)
        if (codec == "h265" and 16 <= t <= 23) or (codec != "h265" and t == H264_IDR):
            return True
    return False


def parameter_sets(codec: str, data: bytes) -> dict[int, bytes]:
    """``{nal type: the unit with a 4-byte start code}`` of the parameter sets in an access unit."""
    wanted = (H265_VPS, H265_SPS, H265_PPS) if codec == "h265" else (H264_SPS, H264_PPS)
    out = {}
    for s, e in nal_units(data):
        t = nal_type(codec, data, s)
        if t in wanted:
            out[t] = b"\x00\x00\x00\x01" + data[s:e]
    return out


def has_all(codec: str, sets: dict[int, bytes]) -> bool:
    return all(t in sets for t in ((H265_VPS, H265_SPS, H265_PPS) if codec == "h265" else (H264_SPS, H264_PPS)))


def joined(codec: str, sets: dict[int, bytes]) -> bytes:
    """The parameter sets in decoding order (VPS, SPS, PPS)."""
    order = (H265_VPS, H265_SPS, H265_PPS) if codec == "h265" else (H264_SPS, H264_PPS)
    return b"".join(sets[t] for t in order if t in sets)


def _rbsp(unit: bytes) -> bytes:
    """A NAL unit's payload without emulation-prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    zeros = 0
    for b in unit:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def codec_string(codec: str, sets: dict[int, bytes]) -> str | None:
    """RFC 6381 / ISO 14496-15 codec string from the SPS, or None without one."""
    sps = sets.get(H265_SPS if codec == "h265" else H264_SPS)
    if not sps:
        return None
    unit = sps[4:] if sps.startswith(b"\x00\x00\x00\x01") else sps
    if codec != "h265":
        if len(unit) < 4:
            return None
        return f"avc1.{unit[1]:02X}{unit[2]:02X}{unit[3]:02X}"
    rbsp = _rbsp(unit[2:])                    # after the two-byte NAL header
    if len(rbsp) < 13:
        return None
    # sps_video_parameter_set_id (4) | sps_max_sub_layers_minus1 (3) | temporal_id_nesting (1), then
    # profile_tier_level: profile_space (2) | tier (1) | profile_idc (5), 32 compatibility flags,
    # 48 constraint bits, level_idc (8)
    ptl = rbsp[1:]
    space, tier, profile = ptl[0] >> 6, (ptl[0] >> 5) & 1, ptl[0] & 0x1F
    compat = int.from_bytes(ptl[1:5], "big")
    rev = int(f"{compat:032b}"[::-1], 2)       # the flags in reverse bit order, as hex
    constraints = list(ptl[5:11])
    level = ptl[11]
    while constraints and constraints[-1] == 0:
        constraints.pop()
    out = f"hvc1.{'' if space == 0 else 'ABC'[space - 1]}{profile}.{rev:X}.{'H' if tier else 'L'}{level}"
    return out + "".join(f".{c:X}" for c in constraints)


class _Bits:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def bit(self) -> int:
        byte = self.data[self.pos >> 3]                     # IndexError past the end: the caller's problem
        b = (byte >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return b

    def bits(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v

    def ue(self) -> int:
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("bad exp-Golomb code")
        return (1 << zeros) - 1 + self.bits(zeros)


def pps_extra_bits(unit: bytes) -> tuple[int, int] | None:
    """(pps id, num_extra_slice_header_bits) of an H.265 PPS unit (with its start code); None when
    unreadable. The slice header has that many reserved bits before slice_type."""
    unit = unit[4:] if unit.startswith(b"\x00\x00\x00\x01") else unit
    try:
        r = _Bits(_rbsp(unit[2:]))
        pps_id = r.ue()
        r.ue()                                             # sps id
        r.bits(2)                                          # dependent_slice_segments_enabled, output_flag_present
        return pps_id, r.bits(3)
    except (IndexError, ValueError):
        return None


def has_b_slice(codec: str, data: bytes, extra_bits: dict[int, int] | None = None) -> bool:
    """Whether an access unit's first slice of a picture is a B slice. H.265 needs the PPSs'
    ``num_extra_slice_header_bits`` (``{pps id: bits}``, from :func:`pps_extra_bits`)."""
    for s, e in nal_units(data):
        t = nal_type(codec, data, s)
        try:
            if codec == "h265":
                if t > 31:                                 # not a VCL unit
                    continue
                r = _Bits(_rbsp(data[s + 2:min(e, s + 34)]))
                if not r.bit():                            # not the first slice segment of the picture
                    continue
                if 16 <= t <= 23:
                    r.bit()                                # no_output_of_prior_pics
                pps = r.ue()
                r.bits((extra_bits or {}).get(pps, 0))
                return r.ue() == 0                         # slice_type 0 = B
            if t in (1, 5):                                # H.264 coded slice (non-IDR / IDR)
                r = _Bits(_rbsp(data[s + 1:min(e, s + 32)]))
                r.ue()                                     # first_mb_in_slice
                return r.ue() % 5 == 1                     # slice_type 1 / 6 = B
        except (IndexError, ValueError):
            return False
    return False
