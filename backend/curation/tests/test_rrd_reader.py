"""RRD(rerun 格式)reader 验收(P1,2026-08-10)。

合成小 .rrd 现场写、现场读:钉住 entity 自动识别、mapping 覆盖、fps 缺失时的报错
文案、模式B 的时间戳推导、视频落盘幂等、缺 entity 报错要列出实际见到的 entity。
真数据(so101 的 VideoStream 模式 + 与 LeRobot 原版的逐值对账)走 integration,
数据不在就跳过。
"""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("rerun", reason="未装 rerun-sdk(RRD 是可选格式)")

from curation.ingest.lerobot_reader import NotADatasetError  # noqa: E402
from curation.ingest.rrd_reader import (  # noqa: E402
    is_rrd_dataset,
    read_rrd_meta,
    read_rrd_rows,
)

RRD_SO101 = "/mnt/tos/datasets/rrd/so101-pick-place"
LEROBOT_SO101 = "/mnt/tos/datasets/so101-pick-place"
RRD_BRIDGE = "/mnt/tos/datasets/rrd/bridge_v2_lerobot"

DIM_NAMES = ["x", "y", "z", "roll", "pitch", "yaw", "gripper"]


def _tiny_mp4(n_frames: int, fps: float, size: int = 32) -> bytes:
    """极小 mp4(合成测试用的 AssetVideo 载荷):n 帧渐变色块。"""
    import io

    import av

    buf = io.BytesIO()
    with av.open(buf, "w", format="mp4") as out:
        stream = out.add_stream("libx264", rate=round(fps))
        stream.width = stream.height = size
        stream.pix_fmt = "yuv420p"
        for i in range(n_frames):
            img = np.full((size, size, 3), (i * 7) % 255, dtype=np.uint8)
            for pkt in stream.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                out.mux(pkt)
        for pkt in stream.encode():
            out.mux(pkt)
    return buf.getvalue()


def _synth_action(n: int, dim: int = 7) -> np.ndarray:
    t = np.linspace(0.0, 1.0, n)
    return np.stack([t * (i + 1) + i for i in range(dim)], axis=1).astype(np.float32)


def _write_rrd(path: str, n: int = 8, fps: float = 5.0, *, with_frame_ts: bool = True,
               task: str | None = "put spoon in tray",
               names: dict | None = None,
               recording_name: str | None = None,
               properties: dict | None = None) -> np.ndarray:
    """写一个模式B(AssetVideo)的小 .rrd,返回写进去的 action(供 roundtrip 对账)。

    names 可改 entity 命名(测 mapping 覆盖与"认不出"的报错);
    recording_name 可设 RecordingInfo:name(so101 转换把任务文本混在这个展示串里);
    properties 经 send_property('curation', …) 写进录制属性(内嵌元数据/溯源约定)。
    """
    import rerun as rr

    ent = {"action": "action", "state": "observation.state", "task": "task",
           "cam": "observation.images.cam0", **(names or {})}
    action = _synth_action(n)
    state = action + 0.5
    rec = rr.RecordingStream("curation-rrd-test")
    rec.save(path)
    if recording_name is not None:
        if hasattr(rec, "send_recording_name"):
            rec.send_recording_name(recording_name)
        else:
            rr.send_recording_name(recording_name, recording=rec)
    if properties:
        rr.send_property("curation", rr.AnyValues(**properties), recording=rec)
    rr.log(ent["action"], rr.SeriesLines(names=DIM_NAMES), static=True, recording=rec)
    rr.log(ent["state"], rr.SeriesLines(names=DIM_NAMES), static=True, recording=rec)
    rr.log(ent["cam"], rr.AssetVideo(contents=_tiny_mp4(n, fps)), static=True, recording=rec)
    for i in range(n):
        rr.set_time("frame_index", sequence=i, recording=rec)
        rr.log(ent["action"], rr.Scalars(action[i].tolist()), recording=rec)
        rr.log(ent["state"], rr.Scalars(state[i].tolist()), recording=rec)
        if task is not None:
            rr.log(ent["task"], rr.TextDocument(task), recording=rec)
        if with_frame_ts:
            rr.log(ent["cam"],
                   rr.VideoFrameReference(nanoseconds=int(round(i / fps * 1e9))),
                   recording=rec)
    rec.flush()
    return action


@pytest.fixture(autouse=True, scope="module")
def _clean_video_cache():
    """本模块跑完清掉 reader 落的临时 mp4(P4,2026-08-10)。不清的话每跑一次 pytest
    就在 /tmp 里留下一批目录,pod 上实测积到 2GB —— 容器可写层撑满整个服务陪葬。"""
    yield
    from curation.ingest.rrd_reader import cleanup_video_cache
    cleanup_video_cache()


@pytest.fixture()
def rrd_dir(tmp_path):
    """两条 episode 的合成 RRD 数据集(模式B,5fps)。"""
    d = tmp_path / "synth"
    d.mkdir()
    acts = {i: _write_rrd(str(d / f"episode_{i}.rrd"), n=8, fps=5.0) for i in (0, 1)}
    return str(d), acts


def test_sniff_and_episode_order(rrd_dir, tmp_path):
    d, _ = rrd_dir
    assert is_rrd_dataset(d)
    assert not is_rrd_dataset(str(tmp_path / "nope"))
    # 序号取自文件名而非排序位置:episode_10 排在 episode_2 之后
    _write_rrd(os.path.join(d, "episode_10.rrd"), n=8, fps=5.0)
    ids = [r["episode_id"] for r in read_rrd_meta(d)]
    assert ids == ["ep000000", "ep000001", "ep000010"]


def test_auto_mapping_roundtrip(rrd_dir):
    """默认命名自动识别;action 数值逐值 roundtrip 一致。"""
    d, acts = rrd_dir
    rows = read_rrd_rows(d)
    assert [r["episode_id"] for r in rows] == ["ep000000", "ep000001"]
    r = rows[0]
    assert np.allclose(r["action"], acts[0], atol=1e-6)
    assert np.allclose(r["proprio_state"], acts[0] + 0.5, atol=1e-6)
    assert r["action"].dtype == np.float32 and r["action"].ndim == 2
    assert r["instruction"] == "put spoon in tray"
    assert set(r["video"]) == {"observation.images.cam0"}
    assert os.path.exists(r["video"]["observation.images.cam0"]["path"])
    # 维名不能丢:并进 semantics_extras(不给下游多加一列)
    import json
    assert json.loads(r["semantics_extras"])["action_names"] == DIM_NAMES


def test_frame_timestamps_drive_fps(rrd_dir):
    """模式B:fps 与时间戳从 VideoFrameReference 推,不用调用方给。"""
    d, _ = rrd_dir
    r = read_rrd_rows(d, max_episodes=1)[0]
    assert r["fps"] == pytest.approx(5.0)
    assert np.allclose(r["timestamps"], np.arange(8) / 5.0, atol=1e-6)
    v = r["video"]["observation.images.cam0"]
    assert (v["from_ts"], v["to_ts"]) == (0.0, 8 / 5.0)


def test_missing_time_info_errors(tmp_path):
    """无时间戳、无 fps 属性、调用方也不给 → 响亮失败并给出可照抄的命令。"""
    d = tmp_path / "notime"
    d.mkdir()
    _write_rrd(str(d / "episode_0.rrd"), n=8, fps=5.0, with_frame_ts=False)
    with pytest.raises(NotADatasetError) as e:
        read_rrd_rows(str(d))
    assert "ingest.rrd_fps=30" in str(e.value)
    # 显式给 fps 就能读,时间戳 = frame_index/fps
    r = read_rrd_rows(str(d), fps=10.0)[0]
    assert r["fps"] == 10.0
    assert np.allclose(r["timestamps"], np.arange(8) / 10.0)


def test_mapping_override_and_unknown_entity_error(tmp_path):
    """客户自定义 entity 命名:默认认不出要列出实见 entity;mapping 覆盖后能读。"""
    d = tmp_path / "custom"
    d.mkdir()
    action = _write_rrd(str(d / "episode_0.rrd"), n=8, fps=5.0,
                        names={"action": "cmd", "state": "prop",
                               "task": "instr", "cam": "cams/rgb"})
    with pytest.raises(NotADatasetError) as e:
        read_rrd_rows(str(d))
    msg = str(e.value)
    assert "/cmd" in msg and "/cams/rgb" in msg      # 实际见到的 entity 要点名
    assert "/action" in msg and "mapping" in msg     # 期望的约定 + 出路

    rows = read_rrd_rows(str(d), mapping={"action": "/cmd", "state": "/prop",
                                          "task": "/instr", "video_prefix": "/cams/"})
    assert np.allclose(rows[0]["action"], action, atol=1e-6)
    assert set(rows[0]["video"]) == {"cams/rgb"}


def test_video_materialization_idempotent(rrd_dir):
    """同一 episode 重复读:复用已落盘的 mp4,不重写(同路径同 mtime)。"""
    d, _ = rrd_dir
    p1 = read_rrd_rows(d, max_episodes=1)[0]["video"]["observation.images.cam0"]["path"]
    st1 = os.stat(p1)
    p2 = read_rrd_meta(d, max_episodes=1)[0]["video"]["observation.images.cam0"]["path"]
    st2 = os.stat(p2)
    assert p1 == p2
    assert (st1.st_mtime_ns, st1.st_size) == (st2.st_mtime_ns, st2.st_size)
    assert not os.path.exists(p1 + ".part")          # 落盘是原子的,不留半截文件


def test_meta_matches_rows(rrd_dir):
    """轻量元数据与完整行的公共字段必须一致(下游按 episode_id 对表)。"""
    d, _ = rrd_dir
    metas = read_rrd_meta(d)
    rows = read_rrd_rows(d)
    for m, r in zip(metas, rows):
        assert m["episode_id"] == r["episode_id"]
        assert m["instruction"] == r["instruction"]
        assert m["fps"] == r["fps"]
        assert m["length"] == len(r["action"])
        assert m["video"] == r["video"]


def test_dataset_info_container_signals(rrd_dir):
    """容器体检信号:自带帧时间戳 + /task 的 rrd,dataset_info 要如实报"文件自带"。"""
    from curation.ingest.rrd_reader import rrd_dataset_info

    d, _ = rrd_dir
    info = rrd_dataset_info(d)
    assert info["time_source"] == "video_timestamps"
    assert info["has_task_text"] is True


def test_container_signals_all_manual(tmp_path):
    """无时间信息、无 /task、任务文本混在录制名里 —— so101 转换的真实形态。
    dataset_info 必须报出:时间是人工补的(config)、无任务文本、录制名线索。"""
    from curation.ingest.rrd_reader import rrd_dataset_info

    d = tmp_path / "bare"
    d.mkdir()
    _write_rrd(str(d / "episode_0.rrd"), with_frame_ts=False, task=None,
               recording_name="Episode 0 · ~28.8 MiB · Grab the red cube · 593 frames")
    info = rrd_dataset_info(str(d), fps=5.0)
    assert info["time_source"] == "config"
    assert info["has_task_text"] is False
    assert "Grab the red cube" in info["recording_name"]


# ---------------------------------------------------------------------------
# 内嵌元数据 + 溯源回填(2026-08-10):属性带就用,缺了回源补,都无才人工/缺失
# ---------------------------------------------------------------------------

_SO101_NAMES = ["shoulder_pan.pos", "shoulder_lift.pos", "elbow_flex.pos",
                "wrist_flex.pos", "wrist_roll.pos", "gripper.pos"]


def _mini_lerobot_source(tmp_path, robot_type="so_follower", fps=5.0,
                         tasks=("t0", "t1", "wipe the table", "t3")) -> str:
    """只有 meta 的最小 v2.1 源(溯源只读 meta,数据/视频文件在不在无所谓)。

    默认按 so101_follower profile 的 match 条件造(robot_type + 关节名):
    profile 命中 → 语义解析零数据读;否则 read_lerobot_meta 会去采样 parquet
    (meta-only 源没有)。这也是真实约束:归档后只剩 meta 的源,profile 命中
    才能取回任务文本,未知机型退化为取不回(已是诚实降级路径)。
    """
    import json

    src = tmp_path / "src"
    (src / "meta").mkdir(parents=True)
    (src / "meta" / "info.json").write_text(json.dumps({
        "codebase_version": "v2.1", "fps": fps, "robot_type": robot_type,
        "chunks_size": 1000,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/"
                      "episode_{episode_index:06d}.mp4",
        "features": {"action": {"dtype": "float32", "names": _SO101_NAMES}}}))
    (src / "meta" / "tasks.jsonl").write_text("")
    (src / "meta" / "episodes.jsonl").write_text("\n".join(
        json.dumps({"episode_index": i, "tasks": [t], "length": 8})
        for i, t in enumerate(tasks)))
    return str(src)


@pytest.fixture(autouse=True)
def _clean_prov_cache():
    """溯源缓存按测试隔离(同一 tmp 路径不同内容会串档)。"""
    from curation.ingest.rrd_reader import _PROV_CACHE
    _PROV_CACHE.clear()
    yield
    _PROV_CACHE.clear()


def test_embedded_properties_backfill(tmp_path):
    """属性内嵌 robot_type/fps/task:三样全自动,行级与信号双确认。"""
    from curation.ingest.rrd_reader import rrd_dataset_info

    d = tmp_path / "emb"
    d.mkdir()
    _write_rrd(str(d / "episode_0.rrd"), with_frame_ts=False, task=None,
               properties={"robot_type": "so101", "fps": 5.0, "task": "wipe table"})
    rows = read_rrd_rows(str(d))                 # 不给 fps:属性里的顶上
    assert rows[0]["embodiment_id"] == "so101"
    assert rows[0]["instruction"] == "wipe table"
    assert rows[0]["fps"] == 5.0
    info = rrd_dataset_info(str(d))
    assert info["robot_type_source"] == "embedded"
    assert info["task_source"] == "embedded"
    assert info["time_source"] == "properties"


def test_provenance_backfill(tmp_path):
    """只带溯源两行(source_dataset + source_episode_index):
    robot_type / fps / 逐条任务文本全部从源 meta 读回。"""
    from curation.ingest.rrd_reader import rrd_dataset_info

    src = _mini_lerobot_source(tmp_path)
    d = tmp_path / "rrd"
    d.mkdir()
    _write_rrd(str(d / "episode_0.rrd"), with_frame_ts=False, task=None,
               properties={"source_dataset": src, "source_episode_index": 2})
    rows = read_rrd_rows(str(d))                 # 什么都不给:全靠溯源
    assert rows[0]["embodiment_id"] == "so_follower"
    assert rows[0]["fps"] == 5.0
    assert rows[0]["instruction"] == "wipe the table"   # 源第 2 条,不是第 0 条
    info = rrd_dataset_info(str(d))
    assert info["robot_type_source"] == "provenance"
    assert info["time_source"] == "provenance"
    assert info["task_source"] == "provenance"
    assert info["provenance"]["reachable"] is True


def test_provenance_never_overrides(tmp_path):
    """只补空缺:用户 fps/型号显式给了就用用户的;文件派生值单独记(供报告点名)。"""
    from curation.ingest.rrd_reader import rrd_dataset_info

    src = _mini_lerobot_source(tmp_path, robot_type="widowx", fps=30.0)
    d = tmp_path / "rrd"
    d.mkdir()
    _write_rrd(str(d / "episode_0.rrd"), with_frame_ts=False, task=None,
               properties={"source_dataset": src, "source_episode_index": 0})
    info = rrd_dataset_info(str(d), fps=5.0, embodiment_id="so101")
    assert info["robot_type"] == "so101"          # 用户显式指定赢
    assert info["robot_type_source"] == "flag"
    assert info["robot_type_file"] == "widowx"    # 冲突证据要带出去
    assert info["time_source"] == "config"        # 用户 fps 赢过溯源 fps
    assert info["fps"] == 5.0


def test_provenance_unreachable(tmp_path):
    """溯源路径写了但不可达:reachable=False,各项按实际缺失走,不崩不猜。"""
    from curation.ingest.rrd_reader import rrd_dataset_info

    d = tmp_path / "rrd"
    d.mkdir()
    _write_rrd(str(d / "episode_0.rrd"), with_frame_ts=False, task=None,
               properties={"source_dataset": str(tmp_path / "not-there"),
                           "source_episode_index": 0})
    info = rrd_dataset_info(str(d), fps=5.0)
    assert info["provenance"]["source"].endswith("not-there")
    assert info["provenance"]["reachable"] is False
    assert info["robot_type"] == "unknown"
    assert info["time_source"] == "config"


def test_decodable_video(rrd_dir):
    """落盘的 mp4 必须能被下游的解码器按时间窗读出帧(不是"有个文件"就算)。"""
    from curation.adapters.decode import decode_window

    d, _ = rrd_dir
    v = read_rrd_rows(d, max_episodes=1)[0]["video"]["observation.images.cam0"]
    frames, ts = decode_window(v["path"], v["from_ts"], v["to_ts"])
    assert len(frames) == 8
    assert frames[0].shape == (32, 32, 3)


# ---------------------------------------------------------------------------
# integration:真数据(模式A = VideoStream,SDK 难合成)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.isdir(RRD_SO101), reason="无 so101 RRD 数据")
def test_video_stream_mode_remux():
    """so101 式 VideoStream:逐帧 H.264 样本重封装成 mp4,全帧可解、时间轴正确。"""
    from curation.adapters.decode import decode_window

    r = read_rrd_rows(RRD_SO101, episode_indices={0}, fps=30.0)[0]
    assert r["fps"] == 30.0
    assert len(r["action"]) == len(r["timestamps"]) == 593
    assert set(r["video"]) == {"observation.images.front", "observation.images.wrist"}
    v = r["video"]["observation.images.front"]
    frames, ts = decode_window(v["path"], v["from_ts"], v["to_ts"])
    assert len(frames) == 593
    assert ts[1] == pytest.approx(1 / 30.0, abs=1e-3)      # 重封装没把时基编错


@pytest.mark.skipif(not (os.path.isdir(RRD_SO101)
                         and os.path.exists(os.path.join(LEROBOT_SO101, "meta"))),
                    reason="无 so101 RRD/LeRobot 对照数据")
def test_golden_action_matches_lerobot():
    """黄金对账:同一条 episode,RRD 与 LeRobot 原版的 action 必须逐值一致。"""
    from curation.ingest.lerobot_reader import read_lerobot_rows

    rrd = read_rrd_rows(RRD_SO101, episode_indices={0}, fps=30.0)[0]
    lej = read_lerobot_rows(LEROBOT_SO101, episode_indices={0}, validate=False)[0]
    assert rrd["action"].shape == lej["action"].shape
    assert np.allclose(rrd["action"], lej["action"], atol=1e-5)
    assert np.allclose(rrd["proprio_state"], lej["proprio_state"], atol=1e-5)


@pytest.mark.skipif(not os.path.isdir(RRD_BRIDGE), reason="无 bridge RRD 数据")
def test_bridge_fps_from_data():
    """bridge 式(AssetVideo + 逐帧时间戳):fps 从数据里来,调用方不用给。"""
    r = read_rrd_rows(RRD_BRIDGE, episode_indices={0})[0]
    assert r["fps"] == pytest.approx(5.0)
    assert len(r["video"]) == 4
    assert r["instruction"]
