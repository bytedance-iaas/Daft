"""Small mcap datasets for the visualizer tests (design doc 18 §6).

``make_umi`` - GenRobot-like: foxglove protobuf, a JPEG camera, ``/robot0/vio/eef_pose``, a magnetic
encoder (``value``), a 200 Hz IMU, a camera calibration (repeated ``K``), task text in metadata; small
chunks, so a file cut short still has whole chunks before the cut.
``make_abc`` - ABC-130k-like: H.264 and H.265 ``foxglove.CompressedVideo`` cameras, a custom
``RobotState`` (``repeated double q``) on ``/left-arm-state`` and ``/left-arm-action``, an
``/instruction`` topic, a segments topic.
``make_default`` - the check reader's own convention: JSON ``/action`` / ``/observation.state``,
``/observation.images.front`` and ``/task``.
"""
from __future__ import annotations

import json
import os
from fractions import Fraction

import numpy as np

FPS = 10
T0 = 1_700_000_000_000_000_000
STEP = int(1e9 / FPS)


def _proto(name: str, fields: list[tuple[str, int, bool]]):
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    fdp = descriptor_pb2.FileDescriptorProto(name=f"{name.lower()}_test.proto", package="vt", syntax="proto3")
    m = fdp.message_type.add(name=name)
    for i, (fname, ftype, repeated) in enumerate(fields, 1):
        m.field.add(name=fname, number=i, type=ftype,
                    label=(descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED if repeated
                           else descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL))
    pool = descriptor_pool.DescriptorPool()
    pool.Add(fdp)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName(f"vt.{name}"))


def _jpeg(i: int) -> bytes:
    import cv2

    img = np.full((48, 64, 3), 30, np.uint8)
    img[:, (i * 4) % 64] = 255
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()


def _annexb(codec: str, n: int) -> list[bytes]:
    import av

    enc = av.CodecContext.create("libx264" if codec == "h264" else "libx265", "w")
    enc.width, enc.height, enc.pix_fmt = 64, 48, "yuv420p"
    enc.time_base = Fraction(1, FPS)
    enc.options = {"bf": "0", "g": "10"} if codec == "h264" else {"x265-params": "log-level=none:bframes=0:keyint=10"}
    out = []
    for i in range(n):
        f = av.VideoFrame.from_ndarray(np.full((48, 64, 3), (i * 9) % 255, np.uint8), format="rgb24").reformat(format="yuv420p")
        f.pts = i
        out += [bytes(p) for p in enc.encode(f)]
    out += [bytes(p) for p in enc.encode(None)]
    return out


def make_umi(root: str, episodes: int = 2, n: int = 20, chunk_size: int = 8192) -> str:
    from foxglove_schemas_protobuf.CameraCalibration_pb2 import CameraCalibration
    from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage
    from foxglove_schemas_protobuf.PoseInFrame_pb2 import PoseInFrame
    from google.protobuf import descriptor_pb2 as D
    from mcap_protobuf.writer import Writer

    Encoder = _proto("MagneticEncoderMeasurement", [("value", D.FieldDescriptorProto.TYPE_DOUBLE, False)])
    Imu = _proto("IMUMeasurement", [("ax", D.FieldDescriptorProto.TYPE_DOUBLE, False),
                                    ("ay", D.FieldDescriptorProto.TYPE_DOUBLE, False)])
    os.makedirs(root, exist_ok=True)
    for ep in range(episodes):
        with open(os.path.join(root, f"episode_{ep}.mcap"), "wb") as fh:
            w = Writer(fh, chunk_size=chunk_size)
            w._writer.add_metadata("episode", {"task_name": f"tidy up {ep}", "robot": "das_gripper"})
            cal = CameraCalibration()
            cal.width, cal.height = 64, 48
            cal.K.extend([1, 0, 32, 0, 1, 24, 0, 0, 1])
            w.write_message("/robot0/sensor/camera0/camera_info", cal, log_time=T0, publish_time=T0)
            for i in range(n):
                t = T0 + i * STEP
                img = CompressedImage()
                img.format = "jpeg"
                img.data = _jpeg(i + ep)
                w.write_message("/robot0/sensor/camera0/compressed", img, log_time=t + STEP // 2, publish_time=t)
                p = PoseInFrame()
                p.frame_id = "world"
                p.pose.position.x, p.pose.position.y, p.pose.position.z = 0.1 * i, 0.2, 0.3 + ep
                p.pose.orientation.w = 1.0
                w.write_message("/robot0/vio/eef_pose", p, log_time=t, publish_time=t)
                e = Encoder()
                e.value = 0.05 + 0.001 * i
                w.write_message("/robot0/sensor/magnetic_encoder", e, log_time=t, publish_time=t)
                for k in range(20):
                    m = Imu()
                    m.ax, m.ay = float(k), float(i)
                    w.write_message("/robot0/sensor/imu", m, log_time=t + k * STEP // 20, publish_time=t)
            w.finish()
    return root


def make_abc(root: str, episodes: int = 1, n: int = 20, wrist_skip: int = 0) -> str:
    """ABC-130k-like: an H.265 top camera, an H.264 wrist camera, a custom protobuf arm at twice the camera
    rate, the instruction and a segment. ``wrist_skip`` starts the wrist stream that many frames into its
    first GOP (GenRobot-like: P-frames before the first keyframe)."""
    from foxglove_schemas_protobuf.CompressedVideo_pb2 import CompressedVideo
    from google.protobuf import descriptor_pb2 as D
    from mcap_protobuf.writer import Writer

    RobotState = _proto("RobotState", [("q", D.FieldDescriptorProto.TYPE_DOUBLE, True),
                                       ("gripper", D.FieldDescriptorProto.TYPE_DOUBLE, False)])
    Text = _proto("Instruction", [("text", D.FieldDescriptorProto.TYPE_STRING, False)])
    Segment = _proto("Segment", [("start", D.FieldDescriptorProto.TYPE_DOUBLE, False),
                                 ("end", D.FieldDescriptorProto.TYPE_DOUBLE, False),
                                 ("label", D.FieldDescriptorProto.TYPE_STRING, False)])
    os.makedirs(root, exist_ok=True)
    h264, h265 = _annexb("h264", n + wrist_skip)[wrist_skip:], _annexb("h265", n)
    for ep in range(episodes):
        with open(os.path.join(root, f"episode_{ep}.mcap"), "wb") as fh:
            w = Writer(fh)
            w._writer.add_metadata("episode-metadata", {"task_name": "arrange flowers"})
            ins = Text()
            ins.text = "arrange the flowers in the vase"
            w.write_message("/instruction", ins, log_time=T0, publish_time=T0)
            for i in range(n):
                t = T0 + i * STEP
                for topic, data in (("/camera/top", h265[i]), ("/camera/wrist", h264[i])):
                    v = CompressedVideo()
                    v.format = "h265" if topic == "/camera/top" else "h264"
                    v.data = data
                    w.write_message(topic, v, log_time=t, publish_time=t)
                for k in range(2):                       # the arm at 2x the camera rate
                    ts = t + k * STEP // 2
                    s = RobotState()
                    s.q.extend([np.sin(0.1 * i + j) for j in range(6)])
                    s.gripper = 0.5
                    w.write_message("/left-arm-state", s, log_time=ts, publish_time=ts)
                    a = RobotState()
                    a.q.extend([np.sin(0.1 * i + j + 0.05) for j in range(6)])
                    a.gripper = 0.4
                    w.write_message("/left-arm-action", a, log_time=ts, publish_time=ts)
            seg = Segment()
            seg.start, seg.end, seg.label = 0.0, 1.0, "reach"
            w.write_message("/subtask", seg, log_time=T0 + 5 * STEP, publish_time=T0)
            w.finish()
    return root


def make_default(root: str, episodes: int = 1, n: int = 15) -> str:
    from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage
    from mcap.writer import Writer as RawWriter
    from mcap_protobuf.writer import Writer

    os.makedirs(root, exist_ok=True)
    for ep in range(episodes):
        with open(os.path.join(root, f"episode_{ep}.mcap"), "wb") as fh:
            w = Writer(fh)
            raw: RawWriter = w._writer
            schema = raw.register_schema(name="vec", encoding="jsonschema", data=b"{}")
            ch = {t: raw.register_channel(t, "json", schema) for t in ("/action", "/observation.state", "/task")}
            raw.add_message(ch["/task"], log_time=T0, data=json.dumps({"data": "pick the cube"}).encode(), publish_time=T0)
            for i in range(n):
                t = T0 + i * STEP
                raw.add_message(ch["/action"], log_time=t, data=json.dumps({"data": [0.1 * i, 0.2, 0.3]}).encode(), publish_time=t)
                raw.add_message(ch["/observation.state"], log_time=t, data=json.dumps({"data": [0.1 * i - 0.01, 0.2, 0.3]}).encode(), publish_time=t)
                img = CompressedImage()
                img.format = "jpeg"
                img.data = _jpeg(i)
                w.write_message("/observation.images.front", img, log_time=t, publish_time=t)
            w.finish()
    return root


def depth16(i: int, h: int = 48, w: int = 64) -> np.ndarray:
    """A synthetic depth picture in millimetres: a slope that moves with the frame, a hole (0) top left."""
    y, x = np.mgrid[0:h, 0:w]
    d = (500 + 10 * x + 5 * y + 7 * i).astype(np.uint16)
    d[:6, :8] = 0
    return d


def png16(a: np.ndarray) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(a.astype(np.uint16)).save(buf, format="PNG")
    return buf.getvalue()


def make_rgbd(root: str, episodes: int = 1, n: int = 12) -> str:
    """Depth the ways recordings write it (design doc 21 §5.4): a JPEG camera ``/front-camera`` with its
    depth as a 16-bit PNG ``CompressedImage`` (RoboMIND's ``/front-depth``), a ``16UC1`` and a ``32FC1``
    ``RawImage`` (metres), a ROS ``compressedDepth`` ``CompressedImage`` (12-byte header + PNG); and what
    is no picture to show: an ``rgb8`` ``RawImage`` and a point cloud. An arm (``/arm-action``) anchors
    the checks' clock."""
    import struct

    from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage
    from foxglove_schemas_protobuf.PointCloud_pb2 import PointCloud
    from foxglove_schemas_protobuf.RawImage_pb2 import RawImage
    from google.protobuf import descriptor_pb2 as D
    from mcap_protobuf.writer import Writer

    Arm = _proto("ArmState", [("q", D.FieldDescriptorProto.TYPE_DOUBLE, True)])
    os.makedirs(root, exist_ok=True)
    for ep in range(episodes):
        with open(os.path.join(root, f"episode_{ep}.mcap"), "wb") as fh:
            w = Writer(fh)
            for i in range(n):
                t = T0 + i * STEP
                d = depth16(i + ep)
                cam = CompressedImage()
                cam.format, cam.data = "jpeg", _jpeg(i + ep)
                w.write_message("/front-camera", cam, log_time=t, publish_time=t)
                dep = CompressedImage()
                dep.format, dep.data = "png", png16(d)
                w.write_message("/front-depth", dep, log_time=t, publish_time=t)
                raw = RawImage()
                raw.width, raw.height, raw.encoding, raw.step = 64, 48, "16UC1", 128
                raw.data = d.astype("<u2").tobytes()
                w.write_message("/raw-depth", raw, log_time=t, publish_time=t)
                flt = RawImage()
                flt.width, flt.height, flt.encoding, flt.step = 64, 48, "32FC1", 256
                flt.data = (d.astype(np.float32) / 1000.0).astype("<f4").tobytes()
                w.write_message("/float-depth", flt, log_time=t, publish_time=t)
                cd = CompressedImage()
                cd.format = "16UC1; compressedDepth png"
                cd.data = struct.pack("<iff", 0, 0.0, 0.0) + png16(d)
                w.write_message("/wrist/depth/compressedDepth", cd, log_time=t, publish_time=t)
                rgb = RawImage()
                rgb.width, rgb.height, rgb.encoding, rgb.step = 64, 48, "rgb8", 192
                rgb.data = np.full((48, 64, 3), 9 * i % 255, np.uint8).tobytes()
                w.write_message("/raw-color", rgb, log_time=t, publish_time=t)
                pc = PointCloud()
                pc.frame_id, pc.point_stride = "world", 12
                pc.data = np.arange(30, dtype="<f4").tobytes()
                w.write_message("/cloud", pc, log_time=t, publish_time=t)
                a = Arm()
                a.q.extend([0.1 * i + j for j in range(3)])
                w.write_message("/arm-action", a, log_time=t, publish_time=t)
            w.finish()
    return root
