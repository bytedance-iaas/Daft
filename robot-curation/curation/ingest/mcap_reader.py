"""MCAP(ROS2 bag 容器)数据集 → 统一 Episode 行(与 lerobot_reader 同一行契约)。

P1(2026-09-18):客户用 mcap 录制的演示数据(每 episode 一个 .mcap 文件)直接进
现有质检漏斗。行的字段、语义解析、校验全部复用 lerobot_reader 的那一套 —— 下游
(漏斗/报告/画像)一个字都不用改,只有"字节怎么读出来"这一层是新的(与 rrd_reader
同一条路:总开关、mapping 覆盖、认不出时报错列出实见 topic)。

与 LeRobot 的结构差异,以及本模块的应对:
1. **消息按 topic 组织,各 topic 各自的时间戳与频率**:
   - 时间轴以 **action topic 的 log_time 为准**(t0 = 第一条 action),换算成
     episode 内相对秒;fps 从 action 消息间隔推。mcap 消息必带 log_time,
     所以不需要 rrd_fps 那样的配置兜底。
   - state topic 与 action 同长时逐帧对应;频率不同(常见:关节读数 100Hz、
     指令 30Hz)按**最近时间对齐**重采样到 action 的时间轴,并在 stderr 报一行。
   - 相机帧带着自己的真实时间进 mp4(pts = 消息时间 - t0),多速率天然对齐,
     视频-动作同步检查在真实时间轴上做。
2. **消息编码两种都认**(按 channel.message_encoding 分派):
   - `cdr`(ROS2 bag 常态):经 mcap-ros2-support 解码,字段**按形状认**不按类型名认
     —— 有 .position 的当 JointState,有 .data 数值列表的当数组,有 .format+.data
     的当压缩图像;客户自定义 msg 只要字段形状对就能读。
   - `json`:直接 json.loads,取 "data" 键或裸值。
3. **视频不是 mp4 文件而是逐帧压缩消息**,两种编码:
   - JPEG(CompressedImage,format=jpeg/jpg)→ 封装进 mjpeg-mp4(**不重编码**);
   - H.264 Annex-B(foxglove CompressedVideo,format=h264)→ 重封装进 mp4(不重编码,
     与 rrd VideoStream 同款做法)。
   其它编码(raw Image / h265 / av1)响亮拒绝,别产出一个能打开但解不出帧的 mp4。
4. **机器人型号/任务文本**:mcap metadata 记录(record)里认 robot_type / task 等
   键(录制方 add_metadata 带上即全自动);没有则靠 --embodiment,分层与冲突留痕
   的规则与 rrd 一致(用户指定 > 文件内嵌)。

⚠️ mcap / mcap-ros2-support 是**懒导入**:基础镜像可不装,LeRobot 路径不受牵连。
⚠️ 视频落盘进 tempfile 目录(/tmp),绝不写 /mnt/tos(FSX 拒绝随机写,mp4 muxer
   收尾要 seek 回文件头 —— 与 rrd_reader/export 同一族的坑)。
⚠️ 只支持本地/挂载路径(与 rrd 同限):mcap 顺序读一个本地文件;tos:// 直读未接。
"""
from __future__ import annotations

import glob
import io
import json
import os
import re
import tempfile
from fractions import Fraction

import numpy as np

from .lerobot_reader import NotADatasetError, _attach_semantics, resolve_dataset_semantics

# topic 命名约定(与 rrd 的 entity 约定同一套;认不出时报错会把这份约定原样打出去)
DEFAULT_MAPPING = {
    "action": "/action",
    "state": "/observation.state",
    "task": "/task",
    "video_prefix": "/observation.images.",
}

_EPISODE_RE = re.compile(r"episode_(\d+)\.mcap$")
_NS = 1e9
_JPEG_MAGIC = b"\xff\xd8"

#: metadata 记录里认的键(按 leaf 名,组名不限)
_ROBOT_KEYS = ("robot_type", "robot", "embodiment")
_TASK_KEYS = ("task", "instruction", "language_instruction")

#: 压缩帧 format 串 → 处理方式
_JPEG_FORMATS = ("jpeg", "jpg")
_H264_FORMATS = ("h264",)

# 一个数据集一个视频临时目录(进程内缓存;与 rrd/lance 同款幂等策略)
_VIDEO_DIRS: dict[str, str] = {}


class McapDependencyError(ImportError):
    """缺 mcap。mcap 是可选格式,报错要给出可照抄的安装命令。"""


def _mcap_reader_mod():
    try:
        from mcap.reader import make_reader
    except ImportError as e:
        raise McapDependencyError(
            "读取 .mcap 需要 mcap(基础镜像未预装):\n"
            "  pip install mcap==1.4.0 mcap-ros2-support==0.5.7\n"
            f"  (原始错误: {e})") from e
    return make_reader


def _cdr_decoder_factory():
    """cdr 解码走 mcap-ros2-support;没装且数据里确有 cdr 通道时才报错。"""
    try:
        from mcap_ros2.decoder import DecoderFactory
        return DecoderFactory()
    except ImportError:
        return None


#: mcap 总开关(与 ingest.rrd_enabled 同一套纪律):release 默认关,开法两种 ——
#: 配置 `ingest.mcap_enabled: true`(apply_config)或环境变量 CURATION_MCAP_ENABLED=1。
_ENABLED: bool | None = None
_CFG_FLAG: bool | None = None
MCAP_DISABLED_MSG = ("这是 mcap 格式的数据集;mcap 质检暂未开放"
                     "(需要时在配置里打开 ingest.mcap_enabled: true)")


def set_enabled(flag: bool | None) -> None:
    """进程级开关:True/False 直接定;None 退回"按环境变量/默认关"。"""
    global _ENABLED
    _ENABLED = None if flag is None else bool(flag)


def apply_config(cfg: dict | None) -> None:
    """从流水线配置读 ingest.mcap_enabled(没写 = 不改当前状态)。"""
    global _CFG_FLAG
    v = ((cfg or {}).get("ingest") or {}).get("mcap_enabled")
    if v is not None:
        _CFG_FLAG = bool(v)


def mcap_enabled() -> bool:
    """优先级:set_enabled 强设 > 环境变量 CURATION_MCAP_ENABLED > 配置 > 默认关。"""
    if _ENABLED is not None:
        return _ENABLED
    env = os.environ.get("CURATION_MCAP_ENABLED", "").strip().lower()
    if env:
        return env in ("1", "true", "yes", "on")
    if _CFG_FLAG is not None:
        return _CFG_FLAG
    return False


def has_mcap_files(dataset_dir: str) -> bool:
    """目录下有没有 *.mcap(不看开关;报错措辞与清单排除用)。"""
    try:
        return bool(glob.glob(os.path.join(dataset_dir, "*.mcap")))
    except Exception:  # noqa: BLE001 列不到 = 没有
        return False


def is_mcap_dataset(dataset_dir: str) -> bool:
    """目录下有 *.mcap **且开关打开**才认作 mcap 数据集(管线的格式嗅探入口)。"""
    return mcap_enabled() and has_mcap_files(dataset_dir)


def _videos_dir(dataset_dir: str) -> str:
    key = os.path.abspath(dataset_dir)
    if key not in _VIDEO_DIRS:
        _VIDEO_DIRS[key] = tempfile.mkdtemp(prefix="mcap_videos_")
    return _VIDEO_DIRS[key]


def cleanup_video_cache(dataset_dir: str | None = None) -> int:
    """删掉本进程为 mcap 封装的临时 mp4 目录(理由与 rrd_reader 同款)。幂等。"""
    keys = [os.path.abspath(dataset_dir)] if dataset_dir else list(_VIDEO_DIRS)
    n = 0
    for k in keys:
        d = _VIDEO_DIRS.pop(k, None)
        if not d:
            continue
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        n += 1
    return n


def _episode_files(dataset_dir: str, max_episodes: int | None = None,
                   start_episode: int = 0,
                   episode_indices: set[int] | None = None) -> list[tuple[int, str]]:
    """目录 → [(episode 序号, .mcap 路径)],按序号升序(与 rrd 同一套规则:
    序号取自 episode_(\\d+).mcap,不合约定退回排序后的位置序号)。"""
    paths = sorted(glob.glob(os.path.join(dataset_dir, "*.mcap")))
    if not paths:
        raise NotADatasetError(
            f"'{dataset_dir}' 里没有 .mcap 文件(mcap 数据集应为一目录 N 个 episode_N.mcap)")
    matched = [(_EPISODE_RE.search(os.path.basename(p)), p) for p in paths]
    if all(m is not None for m, _ in matched):
        items = [(int(m.group(1)), p) for m, p in matched]
    else:
        items = [(i, p) for i, (_, p) in enumerate(matched)]
    items.sort(key=lambda t: t[0])
    if episode_indices is not None:
        items = [it for it in items if it[0] in episode_indices]
    items = items[start_episode:]
    if max_episodes is not None:
        items = items[:max_episodes]
    return items


# ---------------------------------------------------------------------------
# 单个 .mcap → 原始通道数据(一次遍历取全;字段按形状认,不按类型名认)
# ---------------------------------------------------------------------------

def _decode(channel, schema, message, cdr_factory, cache: dict):
    enc = (channel.message_encoding or "").lower()
    if enc == "json":
        try:
            return json.loads(message.data)
        except (TypeError, ValueError):
            return None
    if enc == "cdr":
        if cdr_factory is None:
            raise McapDependencyError(
                "该 mcap 含 cdr(ROS2)编码的消息,解码需要 mcap-ros2-support:\n"
                "  pip install mcap-ros2-support==0.5.7")
        key = (schema.id if schema else 0)
        if key not in cache:
            cache[key] = cdr_factory.decoder_for("cdr", schema)
        return cache[key](message.data)
    return None       # 其它编码(protobuf 等):记进 seen,由必需项检查去喊


def _as_vector(decoded) -> np.ndarray | None:
    """解码结果 → 一帧数值向量。认三种形状:.position(JointState)、
    .data / ["data"] 数值列表、裸列表。认不出返回 None。"""
    if decoded is None:
        return None
    for attr in ("position", "data"):
        v = getattr(decoded, attr, None)
        if v is None and isinstance(decoded, dict):
            v = decoded.get(attr)
        if v is not None and not isinstance(v, (str, bytes)):
            try:
                a = np.asarray(v, dtype=np.float64)
            except (TypeError, ValueError):
                continue
            if a.ndim == 1 and a.size:
                return a
    if isinstance(decoded, (list, tuple)):
        try:
            a = np.asarray(decoded, dtype=np.float64)
            return a if a.ndim == 1 and a.size else None
        except (TypeError, ValueError):
            return None
    return None


def _as_text(decoded) -> str:
    if decoded is None:
        return ""
    if isinstance(decoded, str):
        return decoded
    v = getattr(decoded, "data", None)
    if v is None and isinstance(decoded, dict):
        v = decoded.get("data")
    return str(v).strip() if isinstance(v, str) else ""


def _as_frame(decoded) -> tuple[str, bytes] | None:
    """解码结果 → (format 串, 帧字节)。认 .format + .data 的形状(CompressedImage /
    foxglove CompressedVideo 都是它)。"""
    if decoded is None:
        return None
    fmt = getattr(decoded, "format", None)
    data = getattr(decoded, "data", None)
    if isinstance(decoded, dict):
        fmt = fmt if fmt is not None else decoded.get("format")
        data = data if data is not None else decoded.get("data")
    if data is None:
        return None
    try:
        b = bytes(data)
    except (TypeError, ValueError):
        return None
    return (str(fmt or "").lower(), b) if b else None


def _scan_mcap(mcap_path: str, mapping: dict) -> dict:
    """一次遍历一个 .mcap → 各 topic 的原始数据(按 log_time 纳秒键)。"""
    make_reader = _mcap_reader_mod()
    cdr_factory = _cdr_decoder_factory()
    decoder_cache: dict = {}
    prefix = mapping["video_prefix"]
    out: dict = {"action": [], "state": [], "task": [], "video": {},
                 "properties": {}, "seen_topics": set()}
    with open(mcap_path, "rb") as f:
        reader = make_reader(f)
        try:
            for meta_rec in reader.iter_metadata():
                for k, v in (meta_rec.metadata or {}).items():
                    out["properties"][f"{meta_rec.name}:{k}"] = v
        except Exception:  # noqa: BLE001 metadata 记录可选,坏了不拖垮消息读取
            pass
        for schema, channel, message in reader.iter_messages():
            topic = channel.topic
            out["seen_topics"].add(topic)
            if topic == mapping["action"] or topic == mapping["state"]:
                decoded = _decode(channel, schema, message, cdr_factory, decoder_cache)
                vec = _as_vector(decoded)
                if vec is not None:
                    slot = "action" if topic == mapping["action"] else "state"
                    out[slot].append((message.log_time, vec))
            elif topic == mapping["task"]:
                decoded = _decode(channel, schema, message, cdr_factory, decoder_cache)
                t = _as_text(decoded)
                if t:
                    out["task"].append(t)
            elif topic.startswith(prefix):
                decoded = _decode(channel, schema, message, cdr_factory, decoder_cache)
                fr = _as_frame(decoded)
                if fr is not None:
                    cam = topic.lstrip("/")
                    out["video"].setdefault(cam, []).append(
                        (message.log_time, fr[0], fr[1]))
    out["seen_topics"] = sorted(out["seen_topics"])
    return out


# ---------------------------------------------------------------------------
# 视频落盘(JPEG → mjpeg-mp4 封装;H.264 → 重封装。都不重编码,pts 用真实相对时间)
# ---------------------------------------------------------------------------

def _video_path(videos_dir: str, episode_id: str, cam: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", cam)
    return os.path.join(videos_dir, f"{episode_id}__{safe}.mp4")


def _mux_annexb(samples: list[bytes], times_s: list[float], out_path: str,
                fps: float) -> None:
    """H.264 Annex-B 样本序列 → mp4 容器(只换封装;做法与 rrd_reader._remux_annexb
    同款,pts 优先用真实相对时间 —— demux 出的包数与样本数一致时逐包对号,
    不一致(SPS/PPS 单独成包等)退回均匀 1/fps)。"""
    import av

    time_base = Fraction(1, 90000)
    step = int(round((1 / fps) / time_base))
    tmp = out_path + ".part"
    with av.open(io.BytesIO(b"".join(samples)), format="h264") as src:
        in_stream = src.streams.video[0]
        with av.open(tmp, "w", format="mp4") as dst:
            out_stream = dst.add_stream_from_template(in_stream)
            out_stream.time_base = time_base
            packets = [p for p in src.demux(in_stream) if p.size > 0]
            use_times = len(packets) == len(times_s)
            for n, packet in enumerate(packets):
                packet.stream = out_stream
                packet.time_base = time_base
                packet.pts = packet.dts = (int(round(times_s[n] * 90000))
                                           if use_times else n * step)
                packet.duration = step
                dst.mux(packet)
    os.replace(tmp, out_path)


def _materialize_videos(scan: dict, episode_id: str, videos_dir: str,
                        t0_ns: int, fps: float, duration: float,
                        dataset_dir: str) -> dict:
    """各相机的压缩帧 → 本地 mp4,返回 LeRobot 同构的指针 dict。已存在直接复用(幂等)。

    帧时间 = 消息 log_time 相对 t0;早于 t0 的帧(相机先于第一条 action 启动)丢弃
    —— mp4 的 pts 不能为负,且质检时间窗本来就从 0 开始。"""
    from .lance_reader import mux_jpeg_frames

    videos: dict = {}
    for cam in sorted(scan["video"]):
        path = _video_path(videos_dir, episode_id, cam)
        if not os.path.exists(path):
            items = sorted(scan["video"][cam])
            items = [(t, fmt, b) for t, fmt, b in items if t >= t0_ns]
            if not items:
                continue
            times = [(t - t0_ns) / _NS for t, _, _ in items]
            fmts = {fmt or ("jpeg" if b[:2] == _JPEG_MAGIC else "") for _, fmt, b in items}
            if fmts <= set(_JPEG_FORMATS):
                mux_jpeg_frames([b for _, _, b in items], times, path, fps)
            elif fmts <= set(_H264_FORMATS):
                _mux_annexb([b for _, _, b in items], times, path, fps)
            else:
                raise NotADatasetError(
                    f"'{dataset_dir}' {cam}: 压缩帧编码是 {sorted(fmts)},当前只支持 "
                    "JPEG(封装)与 H.264 Annex-B(重封装);raw Image/其它编码需要"
                    "真转码,尚未实现")
        videos[cam] = {"path": path, "from_ts": 0.0, "to_ts": duration}
    return videos


# ---------------------------------------------------------------------------
# 一条 episode → 统一行
# ---------------------------------------------------------------------------

def _require_topics(scan: dict, mapping: dict, dataset_dir: str) -> None:
    missing = []
    if not scan["action"]:
        missing.append(f"动作 {mapping['action']!r}")
    if not scan["video"]:
        missing.append(f"视频 {mapping['video_prefix']!r}*")
    if not missing:
        return
    raise NotADatasetError(
        f"'{dataset_dir}': mcap 里找不到必需 topic({', '.join(missing)})。\n"
        f"  实际见到的 topic: {scan['seen_topics']}\n"
        f"  期望的命名约定:{mapping['action']}、{mapping['state']}、"
        f"{mapping['video_prefix']}<相机名>、{mapping['task']}\n"
        "  客户 topic 命名不同时,用 mapping 参数覆盖,例如 "
        "mapping={'action': '/joint_states', 'video_prefix': '/cameras/'}")


def _prop_leaf(properties: dict, names: tuple) -> str:
    for k, v in properties.items():
        if k.split(":")[-1].lower() in names:
            return str(v).strip()
    return ""


def _resample_nearest(src: list[tuple[int, np.ndarray]],
                      target_ns: np.ndarray) -> np.ndarray:
    """(时间, 向量) 序列 → 按最近时间对齐到目标时间轴的 [T, dim] 数组。"""
    src = sorted(src)
    ts = np.asarray([t for t, _ in src], dtype=np.float64)
    vecs = [v for _, v in src]
    pos = np.searchsorted(ts, target_ns.astype(np.float64))
    out = []
    for i, p in enumerate(pos):
        lo = max(0, min(int(p) - 1, len(ts) - 1))
        hi = min(int(p), len(ts) - 1)
        t = float(target_ns[i])
        pick = hi if abs(ts[hi] - t) < abs(ts[lo] - t) else lo
        out.append(vecs[pick])
    return np.asarray(out, dtype=np.float32)


def _payload(mcap_path: str, idx: int, mapping: dict, videos_dir: str,
             dataset_dir: str) -> dict:
    """一个 .mcap → 该 episode 的全部内容(数值 + 落盘后的视频指针)。"""
    scan = _scan_mcap(mcap_path, mapping)
    _require_topics(scan, mapping, dataset_dir)
    episode_id = f"ep{idx:06d}"

    acts = sorted(scan["action"])
    t0_ns = acts[0][0]
    t_ns = np.asarray([t for t, _ in acts], dtype=np.int64)
    timestamps = (t_ns - t0_ns).astype(np.float64) / _NS
    n = len(acts)
    dims = {v.size for _, v in acts}
    if len(dims) != 1:
        raise NotADatasetError(
            f"'{mcap_path}': action 消息维度不一致({sorted(dims)}),无法组成 [T, dim]")
    action = np.asarray([v for _, v in acts], dtype=np.float32)
    span = float(timestamps[-1]) if n > 1 else 0.0
    if span <= 0:
        raise NotADatasetError(
            f"'{mcap_path}': action 消息的 log_time 推不出时长(单条消息或时间戳全同),"
            "无法建立时间轴")
    fps = round((n - 1) / span, 6)

    state = None
    if scan["state"]:
        if len(scan["state"]) == n:
            state = np.asarray([v for _, v in sorted(scan["state"])], dtype=np.float32)
        else:
            import sys
            print(f"[curation] {episode_id}: state {len(scan['state'])} 条 ≠ action "
                  f"{n} 条(频率不同),已按最近时间对齐到 action 时间轴",
                  file=sys.stderr)
            state = _resample_nearest(scan["state"], t_ns)

    instruction = ""
    for t in scan["task"]:
        if t and str(t).strip():
            instruction = str(t).strip()
            break
    instruction_source = "task_channel" if instruction else ""
    if not instruction:
        s = _prop_leaf(scan["properties"], _TASK_KEYS)
        if s:
            instruction, instruction_source = s, "embedded"

    duration = float(timestamps[-1]) + 1.0 / fps
    return {
        "episode_id": episode_id,
        "episode_index": idx,
        "instruction": instruction,
        "instruction_source": instruction_source,
        "time_source": "log_time",
        "robot_type_embedded": _prop_leaf(scan["properties"], _ROBOT_KEYS),
        "action": action,
        "proprio_state": state,
        "video": _materialize_videos(scan, episode_id, videos_dir, t0_ns, fps,
                                     duration, dataset_dir),
        "timestamps": timestamps,
        "fps": float(fps),
        "length": n,
    }


def _synth_info(payloads: list[dict], embodiment_id: str | None) -> dict:
    """合成 LeRobot info.json 形状的元数据,喂给共用的语义解析层(与 rrd 同款)。"""
    p = payloads[0] if payloads else {}
    features: dict = {"action": {"dtype": "float32", "names": []}}
    if p.get("proprio_state") is not None:
        features["observation.state"] = {"dtype": "float32", "names": []}
    for cam in (p.get("video") or {}):
        features[cam] = {"dtype": "video"}
    file_rt = str(p.get("robot_type_embedded") or "")
    if embodiment_id:
        rt, rt_src = embodiment_id, "flag"
    elif file_rt:
        rt, rt_src = file_rt, "embedded"
    else:
        rt, rt_src = "unknown", ""
    return {
        "codebase_version": "mcap",
        "fps": float(p.get("fps") or 0.0),
        "robot_type": rt,
        "features": features,
        "chunks_size": 1,
        "data_path": "",
        "robot_type_source": rt_src,
        "robot_type_file": file_rt,
        "time_source": p.get("time_source") or "",
        "has_task_text": bool(p.get("instruction")),
        "task_source": p.get("instruction_source") or "",
    }


def _effective_embodiment(payloads: list[dict],
                          embodiment_id: str | None) -> str | None:
    """行级 embodiment:用户显式指定 > metadata 记录内嵌 > None(照旧 unknown)。"""
    if embodiment_id:
        return embodiment_id
    p = payloads[0] if payloads else {}
    return str(p.get("robot_type_embedded") or "").strip() or None


def _record_source_format(rows: list[dict]) -> None:
    for row in rows:
        try:
            extras = json.loads(row.get("semantics_extras") or "{}")
        except (TypeError, ValueError):
            extras = {}
        extras["source_format"] = "mcap"
        row["semantics_extras"] = json.dumps(extras, ensure_ascii=False)


def _read_payloads(dataset_dir: str, max_episodes: int | None, start_episode: int,
                   episode_indices: set[int] | None,
                   mapping: dict | None) -> list[dict]:
    mp = dict(DEFAULT_MAPPING, **(mapping or {}))
    videos_dir = _videos_dir(dataset_dir)
    return [_payload(path, idx, mp, videos_dir, dataset_dir)
            for idx, path in _episode_files(dataset_dir, max_episodes,
                                            start_episode, episode_indices)]


# ---------------------------------------------------------------------------
# 公开 API(签名与 lerobot_reader/rrd_reader 同构,管线可直接换用)
# ---------------------------------------------------------------------------

def read_mcap_rows(
    dataset_dir: str,
    max_episodes: int | None = None,
    embodiment_id: str | None = None,
    validate: bool = True,
    start_episode: int = 0,
    skip_missing: bool = False,
    episode_indices: set[int] | None = None,
    mapping: dict | None = None,
) -> list[dict]:
    """mcap 数据集 → 每 episode 一个 dict(字段与 read_lerobot_rows 完全一致)。

    mapping: topic 命名覆盖(见 DEFAULT_MAPPING),客户命名不合约定时用;
    skip_missing: 为签名兼容保留 —— episode 清单就是目录里的文件,没有"缺口"。
    """
    from .validate import validate_rows

    payloads = _read_payloads(dataset_dir, max_episodes, start_episode,
                              episode_indices, mapping)
    eff_emb = _effective_embodiment(payloads, embodiment_id)
    rows = [{
        "episode_id": p["episode_id"],
        "embodiment_id": str(eff_emb or "unknown"),
        "action_space": "joint",
        "proprio_space": "joint",
        "instruction": p["instruction"],
        "action": p["action"],
        "proprio_state": p["proprio_state"],
        "video": p["video"],
        "timestamps": p["timestamps"],
        "fps": p["fps"],
    } for p in payloads]
    sem = resolve_dataset_semantics(_synth_info(payloads, embodiment_id), rows)
    _attach_semantics(rows, sem, eff_emb)
    _record_source_format(rows)
    if validate:
        validate_rows(rows)
    return rows


def read_mcap_meta(
    dataset_dir: str,
    max_episodes: int | None = None,
    embodiment_id: str | None = None,
    skip_missing: bool = False,
    episode_indices: set[int] | None = None,
    mapping: dict | None = None,
) -> list[dict]:
    """每 episode 一个轻量 dict(字段与 read_lerobot_meta 一致)。

    ⚠️ 与 LeRobot 不同:视频字节和数值在同一个 .mcap 里,要拿到视频指针就得把文件
    读一遍。封装出的 mp4 留在临时目录复用(幂等),后续几遍不会重复封装。
    """
    payloads = _read_payloads(dataset_dir, max_episodes, 0, episode_indices, mapping)
    eff_emb = _effective_embodiment(payloads, embodiment_id)
    metas = [{
        "episode_id": p["episode_id"],
        "episode_index": p["episode_index"],
        "embodiment_id": str(eff_emb or "unknown"),
        "instruction": p["instruction"],
        "video": p["video"],
        "fps": p["fps"],
        "length": p["length"],
    } for p in payloads]
    sample = [{"action": p["action"]} for p in payloads]
    sem = resolve_dataset_semantics(_synth_info(payloads, embodiment_id), sample)
    _attach_semantics(metas, sem, eff_emb)
    _record_source_format(metas)
    return metas


def mcap_dataset_info(dataset_dir: str, mapping: dict | None = None,
                      embodiment_id: str | None = None) -> dict:
    """数据集级元数据(info.json 形状),给管线的身份行/语义解析用。只解第一条。"""
    payloads = _read_payloads(dataset_dir, 1, 0, None, mapping)
    return _synth_info(payloads, embodiment_id)


def read_mcap_lazy(dataset_dir: str, max_episodes: int | None = None,
                   embodiment_id: str | None = None, validate: bool = True,
                   skip_missing: bool = True,
                   episode_indices: set[int] | None = None,
                   mapping: dict | None = None):
    """mcap 数据集 → daft DataFrame(与 read_lerobot_lazy 同 schema)。

    急切读后建 DataFrame,不做 DataSource 懒扫描(与 rrd 同一取舍):大头是视频字节,
    **必须**先封装成 mp4 才能给下游当指针用 —— 懒不掉;留在内存的只有 action/state。
    """
    from .lerobot_reader import rows_to_daft

    return rows_to_daft(read_mcap_rows(dataset_dir, max_episodes, embodiment_id,
                                       validate, episode_indices=episode_indices,
                                       mapping=mapping))
