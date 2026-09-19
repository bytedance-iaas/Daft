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


def test_camera_lead_frames_dropped(tmp_path):
    """相机早于第一条 action 开录:t<0 的帧丢弃,mp4 里只有 N 帧。"""
    import av

    d = tmp_path / "lead"
    d.mkdir()
    _write_mcap(str(d / "episode_0.mcap"), cam_lead_frames=3)
    r = read_mcap_rows(str(d))[0]
    with av.open(r["video"]["observation.images.cam0"]["path"]) as c:
        n = sum(1 for _ in c.decode(c.streams.video[0]))
    assert n == N


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
    assert set(rows[0]["video"]) == {"cameras/rgb"}


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
