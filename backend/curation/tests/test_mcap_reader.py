"""mcap reader + 交付导出验收(P1,2026-09-18)。

合成小 .mcap 现场写(cdr 编码,mcap-ros2-support 的 writer 序列化 dict)、现场读:
钉住 topic 自动识别、mapping 覆盖、时间轴取自 action log_time、state 多速率最近
对齐、JPEG 帧带真实时间封装 mp4、metadata 记录里的 robot_type/task、开关默认关、
认不出 topic 报错要列出实见 topic、mcap_curated 导出(字节拷贝 + index.json)。
json 编码通路单独一条用例钉住(纯 mcap 无 ros2 依赖的客户写法)。
"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

pytest.importorskip("mcap", reason="未装 mcap(mcap 是可选格式)")
pytest.importorskip("mcap_ros2", reason="未装 mcap-ros2-support(cdr 解码用)")

from curation.ingest.lerobot_reader import NotADatasetError  # noqa: E402
from curation.ingest.mcap_reader import (  # noqa: E402
    is_mcap_dataset,
    read_mcap_meta,
    read_mcap_rows,
)

FPS = 5.0
N = 8
T0 = 1_700_000_000_000_000_000        # 任意的绝对纳秒起点:读端要归零


def _jpeg(i: int, size: int = 32) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".jpg", np.full((size, size, 3), (i * 30) % 255, np.uint8))
    assert ok
    return buf.tobytes()


def _synth_action(n: int = N, dim: int = 3) -> np.ndarray:
    t = np.linspace(0.0, 1.0, n)
    return np.stack([t * (d + 1) for d in range(dim)], axis=1).astype(np.float32)


def _write_mcap(path: str, *, topics: dict | None = None, task: str | None = "pick cube",
                state_rate_mult: int = 1, metadata: dict | None = None,
                cam_lead_frames: int = 0) -> np.ndarray:
    """写一个 cdr 编码的小 .mcap,返回写进去的 action(供 roundtrip 对账)。

    topics 可改 topic 命名(测 mapping 覆盖与"认不出"的报错);
    state_rate_mult: state 的频率倍数(>1 时读端要最近对齐);
    cam_lead_frames: 相机比第一条 action 早开录几帧(读端要丢弃 t<0 的帧)。
    """
    from mcap_ros2.writer import Writer as Ros2Writer

    tp = {"action": "/action", "state": "/observation.state", "task": "/task",
          "cam": "/observation.images.cam0", **(topics or {})}
    action = _synth_action()
    dt_ns = int(1e9 / FPS)
    with open(path, "wb") as f:
        w = Ros2Writer(f)
        if metadata:
            w._writer.add_metadata("curation", {k: str(v) for k, v in metadata.items()})
        s_arr = w.register_msgdef("curation_msgs/msg/FloatArray", "float64[] data")
        s_txt = w.register_msgdef("std_msgs/msg/String", "string data")
        s_img = w.register_msgdef("curation_msgs/msg/CompressedImage",
                                  "string format\nuint8[] data")
        if task is not None:
            w.write_message(tp["task"], s_txt, {"data": task}, log_time=T0)
        for i in range(-cam_lead_frames, 0):
            w.write_message(tp["cam"], s_img, {"format": "jpeg", "data": _jpeg(0)},
                            log_time=T0 + i * dt_ns)
        for i in range(N):
            t = T0 + i * dt_ns
            w.write_message(tp["action"], s_arr, {"data": action[i].tolist()},
                            log_time=t)
            for k in range(state_rate_mult):
                w.write_message(tp["state"], s_arr,
                                {"data": (action[i] + 0.5).tolist()},
                                log_time=t + k * dt_ns // state_rate_mult)
            w.write_message(tp["cam"], s_img, {"format": "jpeg", "data": _jpeg(i)},
                            log_time=t)
        w.finish()
    return action


@pytest.fixture(autouse=True, scope="module")
def _clean_video_cache():
    """本模块跑完清掉 reader 落的临时 mp4(与 test_rrd_reader 同一道纪律)。"""
    yield
    from curation.ingest.mcap_reader import cleanup_video_cache
    cleanup_video_cache()


@pytest.fixture()
def mcap_dir(tmp_path):
    """两条 episode 的合成 mcap 数据集(cdr,5fps,含 metadata 的 robot_type)。"""
    d = tmp_path / "synth"
    d.mkdir()
    acts = {i: _write_mcap(str(d / f"episode_{i}.mcap"),
                           metadata={"robot_type": "so101_follower"})
            for i in (0, 1)}
    return str(d), acts


def test_sniff_and_switch(mcap_dir, tmp_path):
    d, _ = mcap_dir
    assert is_mcap_dataset(d)
    assert not is_mcap_dataset(str(tmp_path / "nope"))
    from curation.ingest import mcap_reader
    mcap_reader.set_enabled(False)
    try:
        assert not is_mcap_dataset(d)        # 开关关着 = 当它不存在
    finally:
        mcap_reader.set_enabled(True)


def test_auto_mapping_roundtrip(mcap_dir):
    """默认 topic 自动识别;时间轴取 action log_time 并归零;fps 从间隔推;
    metadata 的 robot_type 内嵌生效;视频可解码且帧数对。"""
    d, acts = mcap_dir
    rows = read_mcap_rows(d)
    assert [r["episode_id"] for r in rows] == ["ep000000", "ep000001"]
    r = rows[0]
    assert np.allclose(r["action"], acts[0], atol=1e-6)
    assert np.allclose(r["proprio_state"], acts[0] + 0.5, atol=1e-6)
    assert r["instruction"] == "pick cube"
    assert r["embodiment_id"] == "so101_follower"
    assert r["fps"] == pytest.approx(FPS)
    assert np.allclose(r["timestamps"], np.arange(N) / FPS, atol=1e-6)
    v = r["video"]["observation.images.cam0"]
    assert (v["from_ts"], v["to_ts"]) == (0.0, pytest.approx(N / FPS))
    import av
    with av.open(v["path"]) as c:
        st = c.streams.video[0]
        got = [(float(f.pts * st.time_base)) for f in c.decode(st)]
    assert len(got) == N
    assert got[1] == pytest.approx(1 / FPS, abs=1e-3)   # pts 是真实相对时间
    assert json.loads(r["semantics_extras"])["source_format"] == "mcap"


def test_state_rate_mismatch_nearest_align(tmp_path, capsys):
    """state 频率是 action 的 2 倍:按最近时间对齐到 action 时间轴,长度对得上。"""
    d = tmp_path / "fast_state"
    d.mkdir()
    action = _write_mcap(str(d / "episode_0.mcap"), state_rate_mult=2)
    rows = read_mcap_rows(str(d))
    r = rows[0]
    assert r["proprio_state"].shape == r["action"].shape
    assert np.allclose(r["proprio_state"], action + 0.5, atol=1e-6)  # 最近的就是同刻那条
    assert "最近时间对齐" in capsys.readouterr().err


def test_camera_lead_frames_preserved(tmp_path):
    """P2 回归(2026-09-21 审查):相机早于第一条 action 开录的帧**保留**(h264 的
    SPS/PPS/关键帧常在最前,裁头会毁掉解码依赖);action 窗口用 from/to_ts 表达,
    窗内解码得到的仍是 action 时段的 N 帧。"""
    import av

    from curation.adapters.decode import decode_window

    d = tmp_path / "lead"
    d.mkdir()
    _write_mcap(str(d / "episode_0.mcap"), cam_lead_frames=3)
    r = read_mcap_rows(str(d))[0]
    v = r["video"]["observation.images.cam0"]
    assert v["from_ts"] == pytest.approx(3 / FPS)        # 窗口起点 = 提前量
    with av.open(v["path"]) as c:
        assert sum(1 for _ in c.decode(c.streams.video[0])) == N + 3   # 一帧不丢
    frames, _ = decode_window(v["path"], v["from_ts"], v["to_ts"])
    assert len(frames) == N                              # 窗内 = action 时段的帧


def test_codec_of_compound_format_strings():
    """P2 回归(2026-09-21 审查):ROS2 CompressedImage 的标准描述是组合串
    ("bgr8; jpeg compressed bgr8"),按子串认 codec,不做全串等值。"""
    from curation.ingest.mcap_reader import _codec_of

    jpg = b"\xff\xd8rest"
    annexb = b"\x00\x00\x00\x01rest"
    assert _codec_of("bgr8; jpeg compressed bgr8", b"") == "jpeg"
    assert _codec_of("rgb8; jpeg compressed ", b"") == "jpeg"
    assert _codec_of("JPG", b"") == "jpeg"
    assert _codec_of("h264", b"") == "h264"
    assert _codec_of("video/avc", b"") == "h264"
    assert _codec_of("", jpg) == "jpeg"                  # 描述缺失按 magic 兜底
    assert _codec_of("", annexb) == "h264"
    assert _codec_of("png", b"") == "png"                # 不认识的原样上报


def test_ros2_compound_format_reads(tmp_path):
    """整链:format="bgr8; jpeg compressed bgr8" 的相机流照常读取、封装、可解码。"""
    import av

    from mcap_ros2.writer import Writer as Ros2Writer

    d = tmp_path / "ros2fmt"
    d.mkdir()
    dt = int(1e9 / FPS)
    with open(d / "episode_0.mcap", "wb") as f:
        w = Ros2Writer(f)
        s_arr = w.register_msgdef("curation_msgs/msg/FloatArray", "float64[] data")
        s_img = w.register_msgdef("curation_msgs/msg/CompressedImage",
                                  "string format\nuint8[] data")
        for i in range(N):
            t = T0 + i * dt
            w.write_message("/action", s_arr, {"data": [float(i)]}, log_time=t)
            w.write_message("/observation.images.cam0", s_img,
                            {"format": "bgr8; jpeg compressed bgr8",
                             "data": _jpeg(i)}, log_time=t)
        w.finish()
    r = read_mcap_rows(str(d))[0]
    with av.open(r["video"]["observation.images.cam0"]["path"]) as c:
        assert sum(1 for _ in c.decode(c.streams.video[0])) == N


def test_export_stable_ids_for_noncanonical_names(tmp_path):
    """P2 回归(2026-09-21 审查):源文件名不合 episode_N 约定时,交付按编号重命名
    (episode_<N>.mcap)—— 否则筛选后重读位置变了,编号与 index.json 对不上。"""
    from curation.export.mcap_writer import export_mcap_curated

    d = tmp_path / "odd"
    d.mkdir()
    for name in ("a.mcap", "b.mcap", "c.mcap"):          # 位置编号:a=0 b=1 c=2
        _write_mcap(str(d / name))
    delivery = str(tmp_path / "delivery")
    os.makedirs(delivery)
    stats = export_mcap_curated(delivery, str(d), ["ep000002"],
                                generated_at="2026-09-21 00:00:00")
    out_dir = stats["out_dir"]
    assert sorted(os.listdir(out_dir)) == ["episode_2.mcap", "index.json"]
    with open(os.path.join(out_dir, "index.json"), encoding="utf-8") as f:
        index = json.load(f)
    assert index["episodes"][0]["file"] == "episode_2.mcap"
    assert index["episodes"][0]["source_file"] == "c.mcap"
    # 交付集重读:编号与清单一致(这是修的本体)
    rows = read_mcap_rows(out_dir)
    assert [r["episode_id"] for r in rows] == ["ep000002"]


def test_review_page_uses_mcap_mapping(tmp_path, monkeypatch):
    """P2 回归(2026-09-21 审查):run 用 mcap_mapping 能读的数据,review-page 带
    同一份 --config 也要能读;不带配置则如实报输入错误(退出码 2)。"""
    import yaml

    from curation.cli import main

    d = tmp_path / "custom"
    d.mkdir()
    _write_mcap(str(d / "episode_0.mcap"),
                topics={"action": "/cmd", "state": "/prop",
                        "task": "/instr", "cam": "/camera/rgb"})
    cfg = tmp_path / "site.yaml"
    cfg.write_text(yaml.safe_dump({"ingest": {"mcap_mapping": {
        "action": "/cmd", "state": "/prop", "task": "/instr",
        "video_prefix": "/camera/"}}}), encoding="utf-8")
    # 片段编码打桩(与 test_review_page 同款:不真编码视频)
    import curation.export.evidence as ev
    monkeypatch.setattr(ev, "write_audit_clips",
                        lambda flagged_ids, videos, out_dir, **kw: (
                            os.makedirs(os.path.join(out_dir, "details",
                                                     "audit_clips"),
                                        exist_ok=True) or 0))
    out = tmp_path / "site"
    assert main(["review-page", "--input", str(d), "--output", str(out),
                 "--config", str(cfg)]) == 0
    assert (out / "index.html").exists()
    assert main(["review-page", "--input", str(d),
                 "--output", str(tmp_path / "site2")]) == 2   # 不带配置认不出


def test_mapping_override_and_unknown_topic_error(tmp_path):
    """客户自定义 topic:默认认不出要列出实见 topic;mapping 覆盖后能读
    (JointState 形状的 .position 同样按形状认——这里用 data 字段版覆盖)。"""
    d = tmp_path / "custom"
    d.mkdir()
    action = _write_mcap(str(d / "episode_0.mcap"),
                         topics={"action": "/joint_states", "state": "/prop",
                                 "task": "/instr", "cam": "/cameras/rgb"})
    with pytest.raises(NotADatasetError) as e:
        read_mcap_rows(str(d))
    msg = str(e.value)
    assert "/joint_states" in msg and "/cameras/rgb" in msg   # 实际见到的 topic 要点名
    assert "/action" in msg and "mapping" in msg              # 期望的约定 + 出路
    rows = read_mcap_rows(str(d), mapping={"action": "/joint_states",
                                           "state": "/prop", "task": "/instr",
                                           "video_prefix": "/cameras/"})
    assert np.allclose(rows[0]["action"], action, atol=1e-6)
    assert set(rows[0]["video"]) == {"cameras_rgb"}   # cam 键路径安全化


def test_json_encoding_roundtrip(tmp_path):
    """json 编码通路:纯 mcap writer(无 ros2 依赖)写的数据同样读得回。"""
    from mcap.writer import Writer as McapWriter

    d = tmp_path / "jsonenc"
    d.mkdir()
    dt_ns = int(1e9 / FPS)
    with open(d / "episode_0.mcap", "wb") as f:
        w = McapWriter(f)
        w.start()
        sid = w.register_schema("Frame", "jsonschema", b"{}")
        ch_a = w.register_channel("/action", "json", sid)
        ch_t = w.register_channel("/task", "json", sid)
        ch_c = w.register_channel("/observation.images.cam0", "json", sid)
        w.add_message(ch_t, log_time=T0, publish_time=T0,
                      data=json.dumps({"data": "fold towel"}).encode())
        for i in range(N):
            t = T0 + i * dt_ns
            w.add_message(ch_a, log_time=t, publish_time=t,
                          data=json.dumps({"data": [float(i), i * 2.0]}).encode())
            # json 里的帧字节按 latin-1 走不了 —— 现实里 json 客户存的是引用/裸数组,
            # 这里用最小可行:base64 不在约定里,直接给 format+list[int] 的 data
            w.add_message(ch_c, log_time=t, publish_time=t,
                          data=json.dumps({"format": "jpeg",
                                           "data": list(_jpeg(i))}).encode())
        w.finish()
    rows = read_mcap_rows(str(d))
    r = rows[0]
    assert r["instruction"] == "fold towel"
    assert r["action"].shape == (N, 2)
    assert np.allclose(r["action"][:, 1], np.arange(N) * 2.0)
    assert os.path.exists(r["video"]["observation.images.cam0"]["path"])


def _write_umi_mcap(path: str, n: int = N, robots=("robot0", "robot1"),
                    enc_rate_mult: int = 2) -> np.ndarray:
    """写一个 UMI(das_gripper)形态的 protobuf mcap:每设备 eef_pose(30Hz)+
    magnetic_encoder(更高频)+ 相机(JPEG)。返回期望的拼接 action [N, 8×设备数]。

    编码器没有现成 foxglove 消息类,用 PoseInFrame 顶(fields="pose.position.x"
    取单值)—— 内置识别用不了它(topic 名对但字段路径是 value),所以内置识别的
    用例只放 pose;组合 mapping 的用例显式给 fields,两条路都钉住。
    """
    from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage
    from foxglove_schemas_protobuf.PoseInFrame_pb2 import PoseInFrame
    from mcap_protobuf.writer import Writer as PbWriter

    dt = int(1e9 / FPS)
    cols = []
    with open(path, "wb") as f:
        w = PbWriter(f)
        for r_i, robot in enumerate(robots):
            block = np.zeros((n, 8), dtype=np.float64)
            for i in range(n):
                t = T0 + i * dt
                m = PoseInFrame()
                m.frame_id = "world"
                m.pose.position.x = r_i * 10 + i * 0.1
                m.pose.position.y = 0.2
                m.pose.position.z = 0.3
                m.pose.orientation.x = 0.0
                m.pose.orientation.y = 0.0
                m.pose.orientation.z = 0.0
                m.pose.orientation.w = 1.0
                w.write_message(f"/{robot}/vio/eef_pose", m, log_time=t,
                                publish_time=t)
                block[i, :7] = [r_i * 10 + i * 0.1, 0.2, 0.3, 0, 0, 0, 1]
                img = CompressedImage()
                img.format = "jpeg"
                img.data = _jpeg(i)
                w.write_message(f"/{robot}/sensor/camera0/compressed", img,
                                log_time=t, publish_time=t)
                for k in range(enc_rate_mult):     # 编码器更高频,测最近对齐
                    g = PoseInFrame()
                    g.pose.position.x = 0.05 + i * 0.001
                    w.write_message(f"/{robot}/sensor/magnetic_encoder", g,
                                    log_time=t + k * dt // enc_rate_mult,
                                    publish_time=t)
                block[i, 7] = 0.05 + i * 0.001
            cols.append(block)
        w.finish()
    return np.hstack(cols).astype(np.float32)


def test_protobuf_composite_mapping(tmp_path):
    """protobuf 解码 + 嵌套字段路径 + 多来源横拼 + 高频来源最近对齐,一条龙。"""
    pytest.importorskip("mcap_protobuf", reason="未装 mcap-protobuf-support")
    pytest.importorskip("foxglove_schemas_protobuf", reason="未装 foxglove 消息类")
    d = tmp_path / "pb"
    d.mkdir()
    expect = _write_umi_mcap(str(d / "episode_0.mcap"), robots=("robot0",))
    mapping = {"action": [
        {"topic": "/robot0/vio/eef_pose", "fields": "pose"},
        {"topic": "/robot0/sensor/magnetic_encoder", "fields": "pose.position.x"},
    ], "video_topics": ["/robot0/sensor/camera0/compressed"], "state": None}
    rows = read_mcap_rows(str(d), mapping=mapping)
    r = rows[0]
    assert r["action"].shape == (N, 8)                    # 7(pose) + 1(encoder)
    assert np.allclose(r["action"], expect, atol=1e-5)    # 展平序 = proto 声明序
    assert r["proprio_state"] is None
    assert r["fps"] == pytest.approx(FPS)
    assert os.path.exists(r["video"]["robot0_sensor_camera0_compressed"]["path"])


def test_builtin_umi_detection(tmp_path, capsys):
    """默认约定不命中但 topic 形如 /robotN/vio/eef_pose → 零配置自动识别,
    双设备按名序横拼(本合成文件无编码器 topic → 7 维/设备;编码器字段的
    真实形态验收走 Archive 3 真数据,不进单测)。"""
    pytest.importorskip("mcap_protobuf", reason="未装 mcap-protobuf-support")
    pytest.importorskip("foxglove_schemas_protobuf", reason="未装 foxglove 消息类")
    from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage
    from foxglove_schemas_protobuf.PoseInFrame_pb2 import PoseInFrame
    from mcap_protobuf.writer import Writer as PbWriter

    d = tmp_path / "umi"
    d.mkdir()
    dt = int(1e9 / FPS)
    with open(d / "episode_0.mcap", "wb") as f:
        w = PbWriter(f)
        for robot in ("robot0", "robot1"):
            for i in range(N):
                t = T0 + i * dt
                m = PoseInFrame()
                m.pose.position.x = i * 0.1
                m.pose.orientation.w = 1.0
                w.write_message(f"/{robot}/vio/eef_pose", m, log_time=t,
                                publish_time=t)
                img = CompressedImage()
                img.format = "jpeg"
                img.data = _jpeg(i)
                w.write_message(f"/{robot}/sensor/camera0/compressed", img,
                                log_time=t, publish_time=t)
        w.finish()
    rows = read_mcap_rows(str(d))                          # 不给 mapping
    assert "UMI" in capsys.readouterr().out                # 识别命中要出声
    r = rows[0]
    assert r["action"].shape == (N, 14)                    # 7 维 × 2 设备
    assert r["action"][3, 0] == pytest.approx(0.3, abs=1e-6)   # robot0 的 x
    assert r["action"][3, 7] == pytest.approx(0.3, abs=1e-6)   # robot1 的 x
    assert set(r["video"]) == {"robot0_sensor_camera0_compressed",
                               "robot1_sensor_camera0_compressed"}


def test_episode_selection_and_meta(mcap_dir):
    d, acts = mcap_dir
    rows = read_mcap_rows(d, episode_indices={1})
    assert [r["episode_id"] for r in rows] == ["ep000001"]
    metas = read_mcap_meta(d, max_episodes=1)
    assert metas[0]["episode_id"] == "ep000000"
    assert metas[0]["length"] == N


def test_video_materialization_idempotent(mcap_dir):
    d, _ = mcap_dir
    p1 = read_mcap_rows(d, max_episodes=1)[0]["video"]["observation.images.cam0"]["path"]
    st1 = os.stat(p1)
    p2 = read_mcap_meta(d, max_episodes=1)[0]["video"]["observation.images.cam0"]["path"]
    st2 = os.stat(p2)
    assert p1 == p2
    assert (st1.st_mtime_ns, st1.st_size) == (st2.st_mtime_ns, st2.st_size)
    assert not os.path.exists(p1 + ".part")


def test_export_mcap_curated(mcap_dir, tmp_path):
    """交付导出:通过的逐字节一致,剔除的不拷,改标只落 index.json。"""
    from curation.export.mcap_writer import export_mcap_curated

    d, _ = mcap_dir
    delivery = str(tmp_path / "delivery")
    os.makedirs(delivery)
    stats = export_mcap_curated(
        delivery, d, ["ep000001"], relabels={"ep000001": "stack the cups"},
        episodes={"ep000001": {"verdict": "通过", "instruction": "stack the cups",
                               "instruction_source": "人工裁决改标"}},
        generated_at="2026-09-18 00:00:00")
    assert stats["episodes"] == 1 and stats["relabeled"] == 1 and stats["removed"] == 1
    out_dir = stats["out_dir"]
    assert sorted(os.listdir(out_dir)) == ["episode_1.mcap", "index.json"]
    with open(os.path.join(d, "episode_1.mcap"), "rb") as f:
        src = f.read()
    with open(os.path.join(out_dir, "episode_1.mcap"), "rb") as f:
        assert f.read() == src                      # 逐字节一致
    with open(os.path.join(out_dir, "index.json"), encoding="utf-8") as f:
        index = json.load(f)
    assert index["episodes"][0]["instruction"] == "stack the cups"
    assert index["episodes"][0]["relabeled"] is True


def test_tied_log_times_no_raw_crash(tmp_path):
    """P1 回归(2026-09-21 审查):录制端毫秒级打点会产生并列 log_time —— 此前
    sorted() 落到 ndarray 比较直接 ValueError 裸崩;现在相机并列帧 pts 强制递增
    照常封装,action 并列则由校验层给人话(时间戳非严格递增),绝不裸 traceback。"""
    from mcap_ros2.writer import Writer as Ros2Writer

    from curation.ingest.validate import IngestValidationError

    # 相机两帧同一 log_time:封装不许崩
    d = tmp_path / "tiedcam"
    d.mkdir()
    dt = int(1e9 / FPS)
    with open(d / "episode_0.mcap", "wb") as f:
        w = Ros2Writer(f)
        s_arr = w.register_msgdef("curation_msgs/msg/FloatArray", "float64[] data")
        s_img = w.register_msgdef("curation_msgs/msg/CompressedImage",
                                  "string format\nuint8[] data")
        for i in range(N):
            t = T0 + i * dt
            w.write_message("/action", s_arr, {"data": [float(i)]}, log_time=t)
            w.write_message("/observation.images.cam0", s_img,
                            {"format": "jpeg", "data": _jpeg(i)}, log_time=t)
            if i == 3:      # 并列:同一时刻再来一帧
                w.write_message("/observation.images.cam0", s_img,
                                {"format": "jpeg", "data": _jpeg(99)}, log_time=t)
        w.finish()
    r = read_mcap_rows(str(d))[0]
    assert os.path.exists(r["video"]["observation.images.cam0"]["path"])

    # action 两条同一 log_time:要的是校验层的人话,不是 numpy ValueError
    d2 = tmp_path / "tiedact"
    d2.mkdir()
    with open(d2 / "episode_0.mcap", "wb") as f:
        w = Ros2Writer(f)
        s_arr = w.register_msgdef("curation_msgs/msg/FloatArray", "float64[] data")
        s_img = w.register_msgdef("curation_msgs/msg/CompressedImage",
                                  "string format\nuint8[] data")
        for i in range(N):
            t = T0 + i * dt
            w.write_message("/action", s_arr, {"data": [float(i)]}, log_time=t)
            w.write_message("/observation.images.cam0", s_img,
                            {"format": "jpeg", "data": _jpeg(i)}, log_time=t)
        w.write_message("/action", s_arr, {"data": [99.0]},
                        log_time=T0 + 3 * dt)               # 并列的 action
        w.finish()
    with pytest.raises(IngestValidationError) as e:
        read_mcap_rows(str(d2))
    assert "非严格递增" in str(e.value)


def test_stray_mcap_does_not_hijack_lerobot_dir(tmp_path):
    """P2 回归(2026-09-21 审查):LeRobot 数据集旁边留一个采集原始 .mcap 是常态,
    嗅探不许把整个数据集改道成"只质检那个杂散文件"。"""
    d = tmp_path / "lerobot_with_sidecar"
    (d / "meta").mkdir(parents=True)
    (d / "meta" / "info.json").write_text("{}", encoding="utf-8")
    _write_mcap(str(d / "raw_recording.mcap"))
    assert not is_mcap_dataset(str(d))          # 有 meta/info.json ⇒ 不算 mcap 数据集


def test_stray_file_ignored_with_warning(tmp_path, capsys):
    """P2 回归(2026-09-21 审查):episode_N 命名的数据集里混进 calib.mcap ——
    忽略并点名,编号仍按文件名,不退回位置编号、不拿杂散文件当 episode 扫。"""
    d = tmp_path / "with_stray"
    d.mkdir()
    acts = {i: _write_mcap(str(d / f"episode_{i}.mcap")) for i in (0, 2, 10)}
    (d / "calib.mcap").write_bytes(b"\x89MCAP0\r\n" + b"\x00" * 32)   # 杂散文件
    rows = read_mcap_rows(str(d))
    assert "忽略 1 个" in capsys.readouterr().err
    assert [r["episode_id"] for r in rows] == ["ep000000", "ep000002", "ep000010"]
    assert np.allclose(rows[2]["action"], acts[10], atol=1e-6)   # 编号=文件名,非位置


def test_unsupported_schema_encoding_loud(tmp_path):
    """P2 回归(2026-09-21 审查):ros2idl 编码的 schema 解码器给 None —— 要报
    可行动的人话,不是缓存后 'NoneType is not callable'。"""
    from mcap.writer import Writer as McapWriter

    d = tmp_path / "idl"
    d.mkdir()
    with open(d / "episode_0.mcap", "wb") as f:
        w = McapWriter(f)
        w.start()
        sid = w.register_schema("some_msgs/msg/Foo", "ros2idl", b"module some {}")
        ch = w.register_channel("/action", "cdr", sid)
        w.add_message(ch, log_time=T0, publish_time=T0, data=b"\x00\x01\x00\x00")
        w.finish()
    with pytest.raises(NotADatasetError) as e:
        read_mcap_rows(str(d))
    assert "ros2idl" in str(e.value) and "ros2msg" in str(e.value)


def test_partial_state_sources_warn(tmp_path, capsys):
    """P2 回归(2026-09-21 审查):组合 state 有一路取不出数 → 置空但**点名**,
    不再静默丢掉整个 proprio_state。"""
    d = tmp_path / "halfstate"
    d.mkdir()
    _write_mcap(str(d / "episode_0.mcap"))      # 只有 /observation.state 一路
    mapping = {"action": "/action", "task": "/task",
               "video_prefix": "/observation.images.",
               "state": [{"topic": "/observation.state", "fields": None},
                         {"topic": "/gripper_state", "fields": "value"}]}   # 第二路不存在
    rows = read_mcap_rows(str(d), mapping=mapping)
    assert rows[0]["proprio_state"] is None
    err = capsys.readouterr().err
    assert "/gripper_state" in err and "proprio_state 置空" in err
