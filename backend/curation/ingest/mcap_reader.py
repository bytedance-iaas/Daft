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
2. **消息编码三种都认**(按 channel.message_encoding 分派):
   - `cdr`(ROS2 bag 常态):经 mcap-ros2-support 解码,字段**按形状认**不按类型名认
     —— 有 .position 的当 JointState,有 .data 数值列表的当数组,有 .format+.data
     的当压缩图像;客户自定义 msg 只要字段形状对就能读。
   - `protobuf`(foxglove 录制常态,2026-09-21 真数据实证:UMI das_gripper 全 protobuf):
     经 mcap-protobuf-support 解码,同样按形状/字段路径取数。
   - `json`:直接 json.loads,取 "data" 键或裸值。
2b. **action 可能不在一个 topic 里**(UMI 真数据教训:位姿在 /robotN/vio/eef_pose 的
   pose 嵌套结构里,夹爪开度在 /robotN/sensor/magnetic_encoder 的 value 里,双设备
   还要拼维度)。mapping 的 action/state 因此收两种形态:
   - 字符串(老形态):一个 topic,消息体整体按形状取数;
   - **来源列表**(组合形态):[{"topic": ..., "fields": "pose"}, ...] —— 逐来源按
     字段路径抽数值叶子,第 0 号来源定时间轴,其余按最近时间对齐后按序横拼。
   哪路信号当 action 是**人的指认**,机器只负责:默认约定 → 内置 UMI 约定自动识别
   (topic 形如 /robotN/vio/eef_pose 即命中,零配置)→ 都认不出响亮报错列候选。
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


def _protobuf_decoder_factory():
    """protobuf 解码走 mcap-protobuf-support(foxglove 录制常态);同款懒策略。"""
    try:
        from mcap_protobuf.decoder import DecoderFactory
        return DecoderFactory()
    except ImportError:
        return None


#: mcap 总开关:**默认开**(2026-09-21 用户定);个别实例要关,
#: 配置 `ingest.mcap_enabled: false`(apply_config)或环境变量 CURATION_MCAP_ENABLED=0。
_ENABLED: bool | None = None
_CFG_FLAG: bool | None = None
MCAP_DISABLED_MSG = ("这是 mcap 格式的数据集;本实例已关闭 mcap 质检"
                     "(开法:配置 ingest.mcap_enabled: true 或环境变量 CURATION_MCAP_ENABLED=1)")


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
    """优先级:set_enabled 强设 > 环境变量 CURATION_MCAP_ENABLED > 配置 > **默认开**(2026-09-21 用户定:多格式是产品要支持的能力)。"""
    if _ENABLED is not None:
        return _ENABLED
    env = os.environ.get("CURATION_MCAP_ENABLED", "").strip().lower()
    if env:
        return env in ("1", "true", "yes", "on")
    if _CFG_FLAG is not None:
        return _CFG_FLAG
    return True


def has_mcap_files(dataset_dir: str) -> bool:
    """目录下有没有 *.mcap(不看开关;报错措辞与清单排除用)。"""
    try:
        return bool(glob.glob(os.path.join(dataset_dir, "*.mcap")))
    except Exception:  # noqa: BLE001 列不到 = 没有
        return False


def is_mcap_dataset(dataset_dir: str) -> bool:
    """目录下有 *.mcap **且开关打开**才认作 mcap 数据集(管线的格式嗅探入口)。

    有 meta/info.json 的目录**不算**(2026-09-21 审查实锤:LeRobot 数据集旁边留一个
    采集原始 .mcap 是常态,默认开之后曾把整个合法 LeRobot 数据集静默改道成"只质检
    那个杂散文件"——错数据、假成功。判据收紧在嗅探器本身,run/rejudge/审片/清单
    所有调用点一起生效)。"""
    if os.path.isfile(os.path.join(dataset_dir, "meta", "info.json")):
        return False
    return mcap_enabled() and has_mcap_files(dataset_dir)


def list_episode_indices(dataset_dir: str) -> list[int]:
    """数据集实有的 episode 编号(只列目录,零解码零视频 —— --episodes 对账用;
    2026-09-21 审查实锤:此前对账走 read_meta 全量解码,大库上先干几小时白活)。"""
    return [i for i, _ in _episode_files(dataset_dir)]


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
    named = [(int(m.group(1)), p) for m, p in matched if m]
    strays = [os.path.basename(p) for m, p in matched if not m]
    if named:
        # 命名文件按名编号;混进来的杂散文件(calib.mcap 等)忽略并点名 ——
        # 2026-09-21 审查实锤:此前一个杂散文件会把全库打回位置编号,还把
        # 杂散文件当 episode 扫(必报"找不到 action topic"拖垮整批)
        if strays:
            import sys
            print(f"[curation] ⚠️ 忽略 {len(strays)} 个不合 episode_N.mcap 约定的"
                  f"文件(不当 episode 读): {strays[:5]}"
                  f"{'…' if len(strays) > 5 else ''}", file=sys.stderr)
        items = named
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

_DECODER_DEPS = {
    "cdr": ("mcap-ros2-support==0.5.7", _cdr_decoder_factory),
    "protobuf": ("mcap-protobuf-support==0.5.4", _protobuf_decoder_factory),
}


def _decode(channel, schema, message, factories: dict, cache: dict):
    enc = (channel.message_encoding or "").lower()
    if enc == "json":
        try:
            return json.loads(message.data)
        except (TypeError, ValueError):
            return None
    if enc in _DECODER_DEPS:
        pkg, make = _DECODER_DEPS[enc]
        if enc not in factories:
            factories[enc] = make()
        if factories[enc] is None:
            raise McapDependencyError(
                f"该 mcap 含 {enc} 编码的消息,解码需要对应支持包:\n"
                f"  pip install {pkg}")
        key = (enc, schema.id if schema else 0)
        if key not in cache:
            dec = factories[enc].decoder_for(enc, schema)
            if dec is None:
                # 2026-09-21 审查实锤:mcap-ros2-support 对 ros2idl 编码的 schema
                # 返回 None(只支持 ros2msg),缓存后调用是裸 TypeError —— 要说人话
                raise NotADatasetError(
                    f"topic {channel.topic!r} 的 {enc} 消息无法解码:schema "
                    f"{getattr(schema, 'name', None)!r}(schema encoding="
                    f"{getattr(schema, 'encoding', None)!r})不被解码器支持"
                    "(如 ros2idl 只有 IDL 定义,mcap-ros2-support 只认 ros2msg);"
                    "请用 ros2msg schema 重录,或用 mapping 换用其它可解码的 topic")
            cache[key] = dec
        return cache[key](message.data)
    return None       # 其它编码:记进 seen,由必需项检查去喊


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


#: 展平时跳过的字段名:时间/序号/坐标系是描述信息,不是运动量,混进 action 是灾难
_SKIP_LEAF_NAMES = {"timestamp", "sequence_num", "frame_id", "header"}


def _flatten_numeric(node) -> list[float]:
    """任意解码结果 → 数值叶子按声明序展平(protobuf 按 DESCRIPTOR 字段序,dict 按
    插入序)。字符串/字节/header 类字段跳过 —— 只收"像运动量"的数。"""
    if isinstance(node, bool):
        return []
    if isinstance(node, (int, float)):
        return [float(node)]
    if isinstance(node, (str, bytes)):
        return []
    if isinstance(node, (list, tuple)):
        out: list[float] = []
        for x in node:
            out.extend(_flatten_numeric(x))
        return out
    if hasattr(node, "DESCRIPTOR"):            # protobuf Message:按 proto 声明的字段序
        out = []
        for fd in node.DESCRIPTOR.fields:
            if fd.name in _SKIP_LEAF_NAMES:
                continue
            out.extend(_flatten_numeric(getattr(node, fd.name)))
        return out
    if isinstance(node, dict):
        out = []
        for k, v in node.items():
            if str(k) in _SKIP_LEAF_NAMES:
                continue
            out.extend(_flatten_numeric(v))
        return out
    return []


def _extract_source(decoded, fields: str | None) -> np.ndarray | None:
    """一条消息 → 该来源的一帧向量。fields 是点分字段路径(如 "pose" /
    "pose.position.x"),走到路径终点后展平数值叶子;fields 为空退回按形状认。"""
    if fields:
        node = decoded
        for seg in str(fields).split("."):
            nxt = getattr(node, seg, None)
            if nxt is None and isinstance(node, dict):
                nxt = node.get(seg)
            if nxt is None:
                return None
            node = nxt
        vals = _flatten_numeric(node)
        return np.asarray(vals, dtype=np.float64) if vals else None
    return _as_vector(decoded)


def _norm_sources(spec) -> list[dict] | None:
    """mapping 的 action/state 规格 → 来源列表。str = 单来源整消息;list = 组合。"""
    if spec is None:
        return None
    if isinstance(spec, str):
        return [{"topic": spec, "fields": None}]
    return [{"topic": str(s["topic"]), "fields": s.get("fields")} for s in spec]


# ---------------------------------------------------------------------------
# 内置 UMI(das_gripper)约定:2026-09-21 从客户真数据(Archive 3)提炼。
# 默认 topic 认不出、但见到 /robotN/vio/eef_pose 形状的 topic 即命中:
#   action = 各设备 [eef_pose 的 pose(7 维:xyz+四元数) ⊕ magnetic_encoder(夹爪开度)]
#   按序横拼;相机 = /robotN/sensor/camera*/compressed。没有独立指令流 → state 为空。
# ---------------------------------------------------------------------------

_UMI_POSE_RE = re.compile(r"^/(robot\d+)/vio/eef_pose$")
_UMI_CAM_RE = re.compile(r"^/robot\d+/sensor/camera\d+/compressed$")
_UMI_ANNOUNCED: set = set()      # 识别提示按数据集去重(一次 run 读同一集好几遍)


def _umi_mapping(topics: list[str]) -> dict | None:
    robots = sorted({m.group(1) for t in topics if (m := _UMI_POSE_RE.match(t))})
    if not robots:
        return None
    sources, names = [], []
    for r in robots:
        sources.append({"topic": f"/{r}/vio/eef_pose", "fields": "pose"})
        names += [f"{r}_{n}" for n in ("x", "y", "z", "qx", "qy", "qz", "qw")]
        enc = f"/{r}/sensor/magnetic_encoder"
        if enc in topics:
            sources.append({"topic": enc, "fields": "value"})
            names.append(f"{r}_gripper")
    return {"action": sources, "state": None, "task": DEFAULT_MAPPING["task"],
            "video_prefix": DEFAULT_MAPPING["video_prefix"],
            "video_topics": sorted(t for t in topics if _UMI_CAM_RE.match(t)),
            "action_names": names, "profile": "umi_das"}


def _effective_mapping(mcap_path: str, mapping: dict | None) -> dict:
    """定生效 mapping:显式覆盖 > 默认约定命中 > 内置 UMI 识别 > 默认(报错时列候选)。

    只读文件尾部 summary 的 channel 清单(KB 级),不扫消息。用户显式给了 mapping
    就绝不自动识别 —— 指认权在人。"""
    if mapping:
        return dict(DEFAULT_MAPPING, **mapping)
    mp = dict(DEFAULT_MAPPING)
    try:
        make_reader = _mcap_reader_mod()
        with open(mcap_path, "rb") as f:
            summary = make_reader(f).get_summary()
        topics = sorted({ch.topic for ch in (summary.channels or {}).values()}
                        ) if summary else []
    except Exception:  # noqa: BLE001 summary 缺失/读不动:退回默认,错误留给正扫
        return mp
    if mp["action"] in topics:
        return mp
    umi = _umi_mapping(topics)
    if umi:
        # 同一数据集会被读好几遍(meta/漏斗/去重/导出),识别提示只报一次,别刷屏
        key = os.path.dirname(os.path.abspath(mcap_path))
        if key not in _UMI_ANNOUNCED:
            _UMI_ANNOUNCED.add(key)
            print(f"[curation] mcap: 识别为 UMI(das_gripper)约定 —— action = "
                  f"{len(umi['action'])} 路来源横拼({len(umi['action_names'])} 维),"
                  f"相机 {len(umi['video_topics'])} 路", flush=True)
        return umi
    return mp


def _scan_mcap(mcap_path: str, mapping: dict) -> dict:
    """一次遍历一个 .mcap → 各来源的原始序列(按 log_time 纳秒键)。

    action/state 支持多来源(组合 mapping):逐来源各攒一条 (t, vec) 序列,
    拼接在 _payload 里做 —— 扫描层只管"把数取出来",不做时间对齐。"""
    make_reader = _mcap_reader_mod()
    factories: dict = {}
    decoder_cache: dict = {}
    prefix = mapping["video_prefix"]
    video_topics = set(mapping.get("video_topics") or [])
    act_srcs = _norm_sources(mapping["action"]) or []
    st_srcs = _norm_sources(mapping.get("state")) or []
    wanted: dict = {}                  # topic → [(slot, 来源号, 字段路径)]
    for i, s in enumerate(act_srcs):
        wanted.setdefault(s["topic"], []).append(("action", i, s["fields"]))
    for i, s in enumerate(st_srcs):
        wanted.setdefault(s["topic"], []).append(("state", i, s["fields"]))
    out: dict = {"action": [[] for _ in act_srcs], "state": [[] for _ in st_srcs],
                 "task": [], "video": {}, "properties": {}, "seen_topics": set()}
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
            if topic in wanted:
                decoded = _decode(channel, schema, message, factories, decoder_cache)
                for slot, i, fields in wanted[topic]:
                    vec = _extract_source(decoded, fields)
                    if vec is not None:
                        out[slot][i].append((message.log_time, vec))
            elif topic == mapping["task"]:
                decoded = _decode(channel, schema, message, factories, decoder_cache)
                t = _as_text(decoded)
                if t:
                    out["task"].append(t)
            elif topic in video_topics or topic.startswith(prefix):
                decoded = _decode(channel, schema, message, factories, decoder_cache)
                fr = _as_frame(decoded)
                if fr is not None:
                    # cam 键要**路径安全**(2026-09-21 审查实锤:下游片段写入器拿
                    # 它拼文件名,'/' 会拼出不存在的子目录且失败被静默吞掉)
                    cam = topic.lstrip("/").replace("/", "_")
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


def mux_jpeg_frames(blobs: list[bytes], times_s: list[float], out_path: str,
                    fps: float) -> None:
    """JPEG 字节序列 → mjpeg-mp4 容器(**不解码不重编码**,pts 按真实相对时间填)。

    时基 90kHz:非整数 fps 也能整除到 1 个 tick 内。先写 .part 再 rename:
    半截文件被下游当成有效视频指针是最难查的一类事故。
    """
    from fractions import Fraction as _Fr

    import av
    import cv2

    bad = [i for i, b in enumerate(blobs) if not bytes(b[:2]) == _JPEG_MAGIC]
    if bad:
        raise NotADatasetError(
            f"{out_path}: 相机帧字节不是 JPEG(第 {bad[0]} 帧,前 4 字节 "
            f"{bytes(blobs[bad[0]][:4])!r});当前只支持 JPEG 帧免转码封装,"
            "其它编码(PNG/raw)需要真转码,尚未实现")
    img0 = cv2.imdecode(np.frombuffer(blobs[0], np.uint8), cv2.IMREAD_COLOR)
    if img0 is None:
        raise NotADatasetError(f"{out_path}: 首帧 JPEG 解码失败,数据疑似损坏")
    h, w = img0.shape[:2]
    time_base = _Fr(1, 90000)
    step = int(round((1 / fps) / time_base))
    tmp = out_path + ".part"
    with av.open(tmp, "w", format="mp4") as dst:
        s = dst.add_stream("mjpeg", rate=max(1, round(fps)))
        s.width, s.height = w, h
        s.pix_fmt = "yuvj420p"
        s.time_base = time_base
        last_pts = -1
        for b, t in zip(blobs, times_s):
            pkt = av.Packet(bytes(b))
            pkt.stream = s
            pkt.time_base = time_base
            # 并列时间戳(录制端毫秒级打点常见)→ pts 相等会被 muxer 拒收:强制严格递增
            last_pts = max(int(round(t * 90000)), last_pts + 1)
            pkt.pts = pkt.dts = last_pts
            pkt.duration = step
            dst.mux(pkt)
    os.replace(tmp, out_path)


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
            last_pts = -1
            for n, packet in enumerate(packets):
                packet.stream = out_stream
                packet.time_base = time_base
                last_pts = max(int(round(times_s[n] * 90000)) if use_times
                               else n * step, last_pts + 1)   # 并列时间戳强制严格递增
                packet.pts = packet.dts = last_pts
                packet.duration = step
                dst.mux(packet)
    os.replace(tmp, out_path)


def _codec_of(fmt: str, b: bytes) -> str:
    """format 描述串 → codec。按**子串**认而不是全串等值(2026-09-21 审查实锤:
    ROS2 CompressedImage 的标准描述是 "bgr8; jpeg compressed bgr8" 这类组合串,
    全串等值会把标准数据当"不支持编码"拒收);描述缺失按字节 magic 兜底。"""
    f = str(fmt or "").lower()
    if "jpeg" in f or "jpg" in f:
        return "jpeg"
    if "h264" in f or "avc" in f:
        return "h264"
    if not f:
        if b[:2] == _JPEG_MAGIC:
            return "jpeg"
        if b[:4] == b"\x00\x00\x00\x01" or b[:3] == b"\x00\x00\x01":
            return "h264"
    return f or "unknown"


def _materialize_videos(scan: dict, episode_id: str, videos_dir: str,
                        t0_ns: int, fps: float, duration: float,
                        dataset_dir: str) -> dict:
    """各相机的压缩帧 → 本地 mp4,返回 LeRobot 同构的指针 dict。已存在直接复用(幂等)。

    时间轴零点 = min(该相机首帧, t0):**不丢 action 开始前的帧**(2026-09-21 审查
    实锤:h264 流的 SPS/PPS/关键帧常在最前,裁头会让剩余预测帧解不出来);action
    的窗口用指针的 from_ts/to_ts 表达,下游 decode_window 本来就按窗取。"""
    videos: dict = {}
    for cam in sorted(scan["video"]):
        items = sorted(scan["video"][cam], key=lambda t: t[0])
        if not items:
            continue
        base_ns = min(items[0][0], t0_ns)
        from_ts = (t0_ns - base_ns) / _NS
        path = _video_path(videos_dir, episode_id, cam)
        if not os.path.exists(path):
            times = [(t - base_ns) / _NS for t, _, _ in items]
            codecs = {_codec_of(fmt, b) for _, fmt, b in items}
            if codecs == {"jpeg"}:
                mux_jpeg_frames([b for _, _, b in items], times, path, fps)
            elif codecs == {"h264"}:
                _mux_annexb([b for _, _, b in items], times, path, fps)
            else:
                raw = sorted({str(fmt or "") for _, fmt, _ in items})
                raise NotADatasetError(
                    f"'{dataset_dir}' {cam}: 压缩帧编码认成 {sorted(codecs)}"
                    f"(format 描述: {raw}),当前只支持 JPEG(封装)与 H.264 "
                    "Annex-B(重封装);raw Image/其它编码需要真转码,尚未实现")
        videos[cam] = {"path": path, "from_ts": from_ts,
                       "to_ts": from_ts + duration}
    return videos


# ---------------------------------------------------------------------------
# 一条 episode → 统一行
# ---------------------------------------------------------------------------

def _require_topics(scan: dict, mapping: dict, dataset_dir: str) -> None:
    act_srcs = _norm_sources(mapping["action"]) or []
    seen = set(scan["seen_topics"])
    missing = []
    for i, s in enumerate(act_srcs):
        if scan["action"][i]:
            continue
        # 分清两种病(2026-09-21 自查):topic 没出现 vs topic 在但一条数值都没取出
        # —— 后者是 fields 路径/消息形状不对,说"找不到 topic"会把人支错方向
        if s["topic"] in seen:
            missing.append(f"动作来源 {s['topic']!r}(topic 存在但按 fields="
                           f"{s['fields']!r} 没取出任何数值,字段路径/消息形状不对)")
        else:
            missing.append(f"动作来源 {s['topic']!r}")
    if not act_srcs:
        missing.append(f"动作 {DEFAULT_MAPPING['action']!r}")
    if not scan["video"]:
        missing.append(f"视频 {mapping['video_prefix']!r}*")
    if not missing:
        return
    raise NotADatasetError(
        f"'{dataset_dir}': mcap 里找不到必需 topic({', '.join(missing)})。\n"
        f"  实际见到的 topic: {scan['seen_topics']}\n"
        f"  期望的命名约定:{mapping['action']}、{mapping.get('state')}、"
        f"{mapping['video_prefix']}<相机名>、{mapping['task']}\n"
        "  客户 topic 命名不同时,用 mapping 参数覆盖;action 散在多路时用来源列表,"
        "例如 mapping={'action': [{'topic': '/robot0/vio/eef_pose', 'fields': 'pose'},"
        " {'topic': '/robot0/gripper', 'fields': 'value'}]}")


def _prop_leaf(properties: dict, names: tuple) -> str:
    for k, v in properties.items():
        if k.split(":")[-1].lower() in names:
            return str(v).strip()
    return ""


def _resample_nearest(src: list[tuple[int, np.ndarray]],
                      target_ns: np.ndarray) -> np.ndarray:
    """(时间, 向量) 序列 → 按最近时间对齐到目标时间轴的 [T, dim] 数组。"""
    src = sorted(src, key=lambda t: t[0])
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


def _assemble_sources(series_list: list, t_ns: np.ndarray, what: str,
                      mcap_path: str, episode_id: str) -> np.ndarray:
    """多来源序列 → 锚定时间轴上的 [T, ΣD] 横拼。来源自己的时间轴与锚不同
    (条数或时刻不一致)按最近时间对齐,并在 stderr 报一行。"""
    import sys

    cols = []
    for i, series in enumerate(series_list):
        series = sorted(series, key=lambda t: t[0])
        dims = {v.size for _, v in series}
        if len(dims) != 1:
            raise NotADatasetError(
                f"'{mcap_path}': {what} 来源{i} 的消息维度不一致({sorted(dims)}),"
                "无法组成 [T, dim]")
        times = np.asarray([t for t, _ in series], dtype=np.int64)
        if len(series) == len(t_ns) and np.array_equal(times, t_ns):
            cols.append(np.asarray([v for _, v in series], dtype=np.float32))
        else:
            print(f"[curation] {episode_id}: {what} 来源{i} {len(series)} 条 ≠ 时间轴 "
                  f"{len(t_ns)} 条(频率/时钟不同),已按最近时间对齐", file=sys.stderr)
            cols.append(_resample_nearest(series, t_ns))
    return np.hstack(cols) if len(cols) > 1 else cols[0]


def _payload(mcap_path: str, idx: int, mapping: dict, videos_dir: str,
             dataset_dir: str) -> dict:
    """一个 .mcap → 该 episode 的全部内容(数值 + 落盘后的视频指针)。"""
    scan = _scan_mcap(mcap_path, mapping)
    _require_topics(scan, mapping, dataset_dir)
    episode_id = f"ep{idx:06d}"

    # 时间轴锚 = action 第 0 号来源(组合 mapping 的约定;单来源即它自己)
    anchor = sorted(scan["action"][0], key=lambda t: t[0])
    t0_ns = anchor[0][0]
    t_ns = np.asarray([t for t, _ in anchor], dtype=np.int64)
    timestamps = (t_ns - t0_ns).astype(np.float64) / _NS
    n = len(anchor)
    action = _assemble_sources(scan["action"], t_ns, "action", mcap_path, episode_id)
    span = float(timestamps[-1]) if n > 1 else 0.0
    if span <= 0:
        raise NotADatasetError(
            f"'{mcap_path}': action 消息的 log_time 推不出时长(单条消息或时间戳全同),"
            "无法建立时间轴")
    fps = round((n - 1) / span, 6)

    state = None
    if scan["state"]:
        _empty = [i for i, s in enumerate(scan["state"]) if not s]
        if _empty:
            import sys
            _sts = _norm_sources(mapping.get("state")) or []
            _names = [_sts[i]["topic"] if i < len(_sts) else f"来源{i}" for i in _empty]
            print(f"[curation] ⚠️ {episode_id}: state 来源 {_names} 没取出任何数据"
                  "(topic 缺失或 fields 路径不对),本条 proprio_state 置空,"
                  "依赖本体状态的检查将各自弃权", file=sys.stderr)
        else:
            state = _assemble_sources(scan["state"], t_ns, "state", mcap_path,
                                      episode_id)

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
        "action_names": list(mapping.get("action_names") or []),
        "mapping_profile": str(mapping.get("profile") or ""),
    }


def _synth_info(payloads: list[dict], embodiment_id: str | None) -> dict:
    """合成 LeRobot info.json 形状的元数据,喂给共用的语义解析层(与 rrd 同款)。"""
    p = payloads[0] if payloads else {}
    features: dict = {"action": {"dtype": "float32",
                                 "names": p.get("action_names") or []}}
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


def _mapping_signature(mp: dict) -> tuple:
    """生效 mapping 的可比指纹(action 来源清单 + 相机清单)。"""
    srcs = _norm_sources(mp.get("action")) or []
    return (tuple((s["topic"], s["fields"]) for s in srcs),
            tuple(mp.get("video_topics") or ()), mp.get("video_prefix"))


def _read_payloads(dataset_dir: str, max_episodes: int | None, start_episode: int,
                   episode_indices: set[int] | None,
                   mapping: dict | None) -> list[dict]:
    videos_dir = _videos_dir(dataset_dir)
    # 生效 mapping 逐文件定(只读 summary,KB 级);识别结果跨文件不一致时点名 ——
    # 否则用户只会在下游撞见"action 维度跨 episode 不一致",查不到根因
    # (2026-09-21 自查:某文件缺 magnetic_encoder topic 即触发)。
    out = []
    first_sig = first_path = None
    for idx, path in _episode_files(dataset_dir, max_episodes,
                                    start_episode, episode_indices):
        mp = _effective_mapping(path, mapping)
        sig = _mapping_signature(mp)
        if first_sig is None:
            first_sig, first_path = sig, path
        elif sig != first_sig:
            import sys
            print(f"[curation] ⚠️ {os.path.basename(path)} 识别出的 topic 结构与 "
                  f"{os.path.basename(first_path)} 不一致(如缺某路来源/相机)——"
                  "同一数据集应同构,后续大概率报维度不一致;请核查该文件",
                  file=sys.stderr)
        out.append(_payload(path, idx, mp, videos_dir, dataset_dir))
    return out


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

    rows = read_mcap_rows(dataset_dir, max_episodes, embodiment_id,
                          validate, episode_indices=episode_indices,
                          mapping=mapping)
    if not rows:
        raise NotADatasetError(
            f"'{dataset_dir}': 按当前筛选没有读到任何 episode(--episodes 全部超界?)")
    return rows_to_daft(rows)
