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


def make_abc(root: str, episodes: int = 1, n: int = 20) -> str:
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
    h264, h265 = _annexb("h264", n), _annexb("h265", n)
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
