"""RRD(rerun 数据格式)数据集 → 统一 Episode 行(与 lerobot_reader 同一行契约)。

P1(2026-08-10):客户用 rerun 录制/转换的数据(每 episode 一个 .rrd 文件)直接进现有
质检漏斗。行的字段、语义解析、校验全部复用 lerobot_reader 的那一套 —— 下游(漏斗/
报告/画像)一个字都不用改,只有"字节怎么读出来"这一层是新的。

与 LeRobot 的三处结构差异,以及本模块的应对:
1. **没有 meta/info.json**:机器人型号/fps/特征名都不在文件里,只能从 entity 反推
   (维名来自 SeriesLines:names,机器人型号只能由调用方 --embodiment 指定)。
2. **视频不是 mp4 文件而是 entity 里的字节**,且有两种模式:
   - AssetVideo:整段 mp4 blob(bridge 式)→ 原样写盘,零转码;
   - VideoStream:逐帧 H.264 Annex-B 样本(so101 式)→ 重封装进 mp4 容器(不重编码)。
   两种都落成本地 mp4,下游 decode_window(av.open + 时间窗)与 LeRobot v2 完全同构。
3. **没有时间线只有 frame_index**:fps 要么从 VideoFrameReference 时间戳反推,要么由
   调用方显式给(--set ingest.rrd_fps=30);两者都没有就响亮失败,绝不默认 30 蒙混。

⚠️ rerun-sdk 是**懒导入**:基础镜像不装它,LeRobot 路径不能因为这个可选依赖受牵连。
⚠️ 视频落盘一律进 tempfile 目录(/tmp),绝不写 /mnt/tos —— FSX 挂载拒绝随机写,
   mp4 muxer 收尾要 seek 回文件头改 moov(与 export/lerobot_writer 同一族的坑)。

## 版本兼容(P4,2026-08-10)

**rerun-sdk pin 死 0.35**(requirements.txt / Dockerfile)。本模块读的是
`rerun.experimental` 下的 RrdReader/ChunkStore/LazyChunkStream —— 名字里就写着
experimental,**API 形状不受任何稳定性承诺保护**(0.35 才把 `store()` / `stream()` /
`to_chunks()` 定成现在这样)。

官方承诺的只有**容器层**:RRF2 文件格式自 0.23 起向后兼容,新版 SDK 读得开旧文件。
**承诺不覆盖语义解释** —— 组件的列名(如 `Scalars:scalars`、`VideoStream:sample`)、
archetype 的字段命名、静态/时序的表达方式都可能随版本改;本模块正是靠这些字符串
认数据的(见上面的 `_C_*` 常量),所以"文件能打开"不等于"我们还读得对"。

⇒ **升级 SDK 的唯一验收口径 = test_rrd_reader.py + test_rrd_writer.py 全绿**
(合成用例钉住组件命名与 chunk 结构,真数据用例钉住 so101/bridge 两种视频模式)。
两个文件都跑过再动 pin,别只看 `import rerun` 没报错。
"""
from __future__ import annotations

import glob
import json
import os
import re
import tempfile

import numpy as np

from .lerobot_reader import NotADatasetError, _attach_semantics, resolve_dataset_semantics

# entity 命名约定(LeRobot → rerun 转换的事实标准;认不出时报错会把这份约定原样打出去)
DEFAULT_MAPPING = {
    "action": "/action",
    "state": "/observation.state",
    "task": "/task",
    "video_prefix": "/observation.images.",
}

# rerun 组件列名(0.35 的 "Archetype:field" 命名)
_C_SCALARS = "Scalars:scalars"
_C_DIM_NAMES = "SeriesLines:names"
_C_TEXT = "TextDocument:text"
_C_ASSET_BLOB = "AssetVideo:blob"          # 模式B:整段 mp4
_C_STREAM_SAMPLE = "VideoStream:sample"    # 模式A:逐帧 H.264 Annex-B 样本
_C_FRAME_TS = "VideoFrameReference:timestamp"
_C_CODEC = "VideoStream:codec"
_PROPERTIES = "/__properties"

# VideoStream 的 codec 是 fourcc 整数。只有 H.264 能按现在的做法"换封装不重编码";
# 别的编码要么容器不收、要么得真转码,先响亮拒绝,别产出一个能打开但解不出帧的 mp4。
_CODEC_H264 = 1635148593           # 'avc1'
_CODEC_NAMES = {_CODEC_H264: "H.264", 1752589105: "H.265", 1635135537: "AV1",
                1987409465: "VP9"}

_EPISODE_RE = re.compile(r"episode_(\d+)\.rrd$")

# 一个数据集一个视频临时目录(进程内缓存):meta/漏斗/去重/导出会多次重读同一批
# episode,重复解出同样的 mp4 纯属浪费 —— 目录固定 + 文件名确定 ⇒ 天然幂等。
_VIDEO_DIRS: dict[str, str] = {}


class RrdDependencyError(ImportError):
    """缺 rerun-sdk。RRD 是可选格式,报错要给出可照抄的安装命令。"""


def _rrd_reader_cls():
    """懒导入 rerun 的 RrdReader(装没装 rerun 不影响 LeRobot 路径)。"""
    try:
        from rerun.experimental import RrdReader
    except ImportError as e:
        raise RrdDependencyError(
            "读取 .rrd 需要 rerun-sdk(基础镜像未预装):\n"
            "  pip install rerun-sdk==0.35\n"
            f"  (原始错误: {e})") from e
    return RrdReader


def is_rrd_dataset(dataset_dir: str) -> bool:
    """目录下有 *.rrd 即认作 RRD 数据集(管线的格式嗅探入口)。"""
    try:
        from . import dsfs
        return bool(dsfs.glob(dsfs.join(dataset_dir, "*.rrd")))
    except OSError:
        return False


def _videos_dir(dataset_dir: str) -> str:
    key = os.path.abspath(dataset_dir)
    if key not in _VIDEO_DIRS:
        _VIDEO_DIRS[key] = tempfile.mkdtemp(prefix="rrd_videos_")
    return _VIDEO_DIRS[key]


def cleanup_video_cache(dataset_dir: str | None = None) -> int:
    """删掉本进程为 RRD 解出的临时 mp4 目录,返回清掉的目录数(整个 run 收尾时调)。

    为什么必须显式清:一条 so101 episode 解出两路 mp4 就是几十 MB,几百条跑完
    /tmp 里躺着几个 GB —— pod 的 /tmp 是容器可写层,涨满了整个服务一起挂。
    tempfile 只保证"进程外看不见",不保证进程退出时删。

    幂等:目录早被删了 / 这个数据集根本没读过 / 传 None 清全部,三种情况都不报错
    (收尾清理绝不能成为新的失败源)。
    """
    keys = [os.path.abspath(dataset_dir)] if dataset_dir else list(_VIDEO_DIRS)
    n = 0
    for k in keys:
        d = _VIDEO_DIRS.pop(k, None)
        if not d:
            continue
        import shutil
        shutil.rmtree(d, ignore_errors=True)     # 目录已不在也当成功
        n += 1
    if dataset_dir is None:
        _PROV_CACHE.clear()      # 全量收尾时溯源缓存一并清(测试隔离/长进程防陈旧)
    return n


def _episode_files(dataset_dir: str, max_episodes: int | None = None,
                   start_episode: int = 0,
                   episode_indices: set[int] | None = None) -> list[tuple[int, str]]:
    """目录 → [(episode 序号, .rrd 路径)],按序号升序。

    序号取自 episode_(\\d+).rrd;文件名不合约定时退回"排序后的位置序号"——
    序号只是 episode 的稳定标识(报告/--episodes 用它),不参与任何语义。
    """
    paths = sorted(glob.glob(os.path.join(dataset_dir, "*.rrd")))
    if not paths:
        raise NotADatasetError(
            f"'{dataset_dir}' 里没有 .rrd 文件(RRD 数据集应为一目录 N 个 episode_N.rrd)")
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
# 单个 .rrd → 原始列(一次遍历取全,视频字节走 arrow 缓冲区)
# ---------------------------------------------------------------------------

def _blobs(column):
    """arrow 的视频字节列 list<list<uint8>> → 每行一段 bytes。

    ⚠️ 绝不能用 chunk.to_pydict():它把上千万个 uint8 逐个变成 Python int,
    单条 so101 episode 实测 6.9s;走 arrow 缓冲区同样的数据 0.2s(35 倍)。
    外层 list 是"一格里的组件数组"(恒为 1 个 blob),flatten 掉才是真正的字节数组。
    """
    flat = column.flatten()
    return [flat[i].values.to_numpy(zero_copy_only=False).tobytes()
            for i in range(len(flat))]


def _index_values(rb, chunk) -> list[int]:
    """chunk 的时间线列(so101/bridge 都叫 frame_index,但不写死名字)。"""
    names = list(rb.schema.names)
    for tl in (chunk.timeline_names or []):
        if tl in names:
            return rb.column(names.index(tl)).to_pylist()
    if "frame_index" in names:
        return rb.column(names.index("frame_index")).to_pylist()
    return list(range(rb.num_rows))


def _scan_rrd(rrd_path: str, mapping: dict) -> dict:
    """一次遍历一个 .rrd → 各 entity 的原始数据(视频字节已取成 bytes)。"""
    store = _rrd_reader_cls()(rrd_path).store()
    seen: set[str] = set()
    out: dict = {"action": {}, "state": {}, "action_names": [], "state_names": [],
                 "task": [], "video": {}, "video_ts": {}, "video_mode": {},
                 "codec": {}, "properties": {}}
    prefix = mapping["video_prefix"]
    for chunk in store.stream().to_chunks():
        ep = chunk.entity_path
        seen.add(ep)
        rb = chunk.to_record_batch()
        names = list(rb.schema.names)

        def col(name):
            return rb.column(names.index(name))

        if ep == mapping["action"] or ep == mapping["state"]:
            slot = "action" if ep == mapping["action"] else "state"
            if _C_DIM_NAMES in names and rb.num_rows:
                out[f"{slot}_names"] = [str(x) for x in col(_C_DIM_NAMES)[0].as_py()]
            if _C_SCALARS in names and rb.num_rows:
                for fi, v in zip(_index_values(rb, chunk), col(_C_SCALARS).to_pylist()):
                    out[slot][int(fi)] = v
        elif ep == mapping["task"]:
            if _C_TEXT in names and rb.num_rows:
                out["task"].extend(col(_C_TEXT).to_pylist())
        elif ep.startswith(prefix):
            cam = ep.lstrip("/")
            if _C_ASSET_BLOB in names and rb.num_rows:
                out["video_mode"][cam] = "asset"
                out["video"][cam] = {0: _blobs(col(_C_ASSET_BLOB))[0]}
            elif _C_STREAM_SAMPLE in names and rb.num_rows:
                out["video_mode"][cam] = "stream"
                bucket = out["video"].setdefault(cam, {})
                for fi, b in zip(_index_values(rb, chunk),
                                 _blobs(col(_C_STREAM_SAMPLE))):
                    bucket[int(fi)] = b
            elif _C_FRAME_TS in names and rb.num_rows:
                bucket = out["video_ts"].setdefault(cam, {})
                for fi, v in zip(_index_values(rb, chunk), col(_C_FRAME_TS).to_pylist()):
                    bucket[int(fi)] = v
            elif _C_CODEC in names and rb.num_rows:
                v = col(_C_CODEC)[0].as_py()
                out["codec"][cam] = int(v[0] if isinstance(v, (list, tuple)) else v)
        elif ep == _PROPERTIES or ep.startswith(_PROPERTIES + "/"):
            # 根属性列名自带组前缀("RecordingInfo:name");send_property('组名', …)
            # 落在 /__properties/<组名> 子路径且列名是裸键 —— 统一压平成 "组名:键"。
            sub = ep[len(_PROPERTIES):].lstrip("/")
            for n in names:
                if n.startswith("rerun.controls") or not rb.num_rows:
                    continue
                key = f"{sub}:{n}" if sub else n
                try:
                    out["properties"][key] = col(n)[0].as_py()
                except Exception:  # noqa: BLE001  属性五花八门,取不动就不取
                    continue
    out["seen_entities"] = sorted(seen)
    return out


# ---------------------------------------------------------------------------
# 视频落盘(两种模式 → 本地 mp4;下游看到的形态与 LeRobot v2 完全一样)
# ---------------------------------------------------------------------------

def _video_path(videos_dir: str, episode_id: str, cam: str) -> str:
    # 相机名来自 entity 路径,可能带 '/' 和 '.'(如 cams/rgb):一律压成安全文件名,
    # 否则会当成子目录去写而目录并不存在
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", cam)
    return os.path.join(videos_dir, f"{episode_id}__{safe}.mp4")


def _atomic_write(data: bytes, out_path: str) -> None:
    """先写 .part 再 rename:半截文件被下游当成有效视频指针是最难查的一类事故。"""
    tmp = out_path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, out_path)


def _remux_annexb(samples: list[bytes], out_path: str, fps: float) -> None:
    """H.264 Annex-B 样本序列 → mp4 容器(**只换封装,不重编码**)。

    做法:把样本首尾相接当成裸 H.264 基本流交给 PyAV 解析出 packet,再原样 mux 进
    mp4,pts/dts 按 frame_index/fps 自己填 —— 裸流没有容器时间,demux 出来的
    pts/dts 全是 None,不填的话 muxer 会拿 25fps 的默认时基编故事。
    """
    import io
    from fractions import Fraction

    import av

    time_base = Fraction(1, 90000)      # 90kHz:非整数 fps 也能整除到 1 个 tick 内
    step = int(round(float(1 / fps) / time_base))
    tmp = out_path + ".part"
    with av.open(io.BytesIO(b"".join(samples)), format="h264") as src:
        in_stream = src.streams.video[0]
        # format 必须显式给:临时文件名是 .part,PyAV 从后缀猜不出容器
        with av.open(tmp, "w", format="mp4") as dst:
            out_stream = dst.add_stream_from_template(in_stream)
            out_stream.time_base = time_base
            n = 0
            for packet in src.demux(in_stream):
                if packet.size == 0:        # demux 末尾的 flush 包,不是一帧
                    continue
                packet.stream = out_stream
                packet.time_base = time_base
                packet.pts = packet.dts = n * step
                packet.duration = step
                n += 1
                dst.mux(packet)
    os.replace(tmp, out_path)


def _materialize_videos(scan: dict, episode_id: str, videos_dir: str,
                        fps: float, length: int) -> dict:
    """各相机的视频字节 → 本地 mp4,返回 LeRobot 同构的指针 dict。

    已存在的文件直接复用(幂等):同一批 episode 在 meta/漏斗/去重/导出里要过好几遍。
    """
    videos: dict = {}
    for cam in sorted(scan["video"]):
        path = _video_path(videos_dir, episode_id, cam)
        if not os.path.exists(path):
            frames = scan["video"][cam]
            if scan["video_mode"].get(cam) == "asset":
                _atomic_write(frames[min(frames)], path)
            else:
                codec = scan["codec"].get(cam, _CODEC_H264)
                if codec != _CODEC_H264:
                    raise NotADatasetError(
                        f"{cam}: VideoStream 编码是 "
                        f"{_CODEC_NAMES.get(codec, codec)},当前只支持 H.264 换封装"
                        "(其它编码需要真转码,尚未实现)")
                _remux_annexb([frames[k] for k in sorted(frames)], path, fps)
        videos[cam] = {"path": path, "from_ts": 0.0, "to_ts": length / fps}
    return videos


# ---------------------------------------------------------------------------
# fps / 时间戳
# ---------------------------------------------------------------------------

_FPS_KEYS = ("fps", "frame_rate", "framerate", "frames_per_second")

# VideoFrameReference 的时间戳单位是纳秒(rerun VideoTimestamp;bridge 实测
# 0/200000000/400000000… = 5fps,与 BridgeData V2 的 5Hz 采集吻合)
_NS = 1e9


def _fps_from_properties(properties: dict) -> float | None:
    for k, v in properties.items():
        leaf = k.split(":")[-1].lower()
        if any(t in leaf for t in _FPS_KEYS):
            try:
                f = float(v[0] if isinstance(v, (list, tuple)) and v else v)
            except (TypeError, ValueError):
                continue
            if f > 0:
                return f
    return None


# ---------------------------------------------------------------------------
# 内嵌元数据 + 溯源(2026-08-10 用户定):属性里带就用属性的;缺了但有溯源信息,
# 就回原始数据集只读 meta 补全;都没有才落到人工指定/诚实缺失。
# 原则:**只补空缺,不覆盖任何已有来源**(不覆盖文件内更强的信号,也不覆盖用户
# 显式指定;用户指定与文件派生冲突时照用用户的,但报告点名)。
# ---------------------------------------------------------------------------

#: 属性键按 leaf 名(冒号后)精确匹配,组名不限 —— 转换方用什么组名都认。
_ROBOT_KEYS = ("robot_type", "robot", "embodiment")
_TASK_KEYS = ("task", "instruction", "language_instruction")
_SOURCE_KEYS = ("source_dataset", "source_path", "origin_dataset")
_SOURCE_IDX_KEYS = ("source_episode_index", "source_episode")

#: 溯源缓存:同一数据集的所有 .rrd 几乎总指向同一个源,info.json 只读一次;
#: 逐条任务文本按 (源目录, episode 序号) 缓存。测试间用 clear() 隔离。
_PROV_CACHE: dict = {}


def _prop_leaf(properties: dict, names: tuple) -> object | None:
    for k, v in properties.items():
        if k.split(":")[-1].lower() in names:
            return v[0] if isinstance(v, (list, tuple)) and v else v
    return None


def _resolve_source_dir(raw: str) -> str | None:
    """溯源路径 → 本地可读目录;解析不了返回 None(不猜)。

    支持两种写法:本地/挂载路径原样;tos://<bucket>/<key> 按当前部署的挂载
    约定(curation 桶挂在 /mnt/tos)试探 —— 试探失败就是不可达,不硬猜。
    """
    p = str(raw or "").strip()
    if not p:
        return None
    if os.path.isdir(p):
        return p
    if p.startswith("tos://"):
        parts = p[len("tos://"):].split("/", 1)
        if len(parts) == 2:
            cand = os.path.join("/mnt/tos", parts[1])
            if os.path.isdir(cand):
                return cand
    return None


def _provenance_info(properties: dict) -> dict:
    """属性里的溯源信息 → 源数据集的 robot_type/fps(只读 meta/info.json,KB 级)。

    返回 {"source", "resolved", "reachable", "episode_index", "robot_type", "fps"};
    没写溯源就是 source="";写了但访问不到 reachable=False —— 两种情况都要在
    报告里分开说,所以这里不合并。任何读取失败都按不可达处理,绝不让溯源把
    rrd 本体的读取搞崩。
    """
    src = _prop_leaf(properties, _SOURCE_KEYS)
    out = {"source": str(src or "").strip(), "resolved": None, "reachable": False,
           "episode_index": None, "robot_type": "", "fps": None}
    if not out["source"]:
        return out
    idx = _prop_leaf(properties, _SOURCE_IDX_KEYS)
    try:
        out["episode_index"] = None if idx is None else int(idx)
    except (TypeError, ValueError):
        out["episode_index"] = None
    resolved = _resolve_source_dir(out["source"])
    if not resolved:
        return out
    out["resolved"] = resolved
    if resolved not in _PROV_CACHE:
        try:
            with open(os.path.join(resolved, "meta", "info.json"),
                      encoding="utf-8") as f:
                info = json.load(f)
            _PROV_CACHE[resolved] = {
                "robot_type": str(info.get("robot_type") or ""),
                "fps": float(info["fps"]) if info.get("fps") else None}
        except Exception:  # noqa: BLE001  源在但 meta 读不动 → 按不可达
            return out
    out.update(_PROV_CACHE[resolved])
    out["reachable"] = True
    return out


def _provenance_instruction(resolved: str, idx: int) -> str:
    """源数据集第 idx 条的任务文本(轻量元数据路径,不碰数值/视频)。"""
    key = (resolved, idx)
    if key not in _PROV_CACHE:
        try:
            from .lerobot_reader import read_lerobot_meta
            # skip_missing=False:这里只要任务文本,数据/视频文件在不在无所谓 ——
            # True 会把"文件不在"的条目整条跳过,归档后只剩 meta 的源就取不回了。
            rows = read_lerobot_meta(resolved, episode_indices={idx},
                                     skip_missing=False)
            _PROV_CACHE[key] = str(rows[0].get("instruction") or "") if rows else ""
        except Exception:  # noqa: BLE001
            _PROV_CACHE[key] = ""
    return _PROV_CACHE[key]


def _resolve_time(scan: dict, frame_ids: list[int], fps_arg: float | None,
                  dataset_dir: str, provenance_fps: float | None = None
                  ) -> tuple[float, np.ndarray, str]:
    """(fps, timestamps, 来源)。优先级:视频帧时间戳 → 录制属性 → 调用方给的 fps。

    三样都没有就报错 —— RRD 里"没有时间"和"30fps"是两回事,猜一个默认值会让
    时序检查/同步检查在错误的时间轴上给出看起来很正常的结论。
    来源("video_timestamps"/"properties"/"config")随行透传:哪些容器元信息是
    文件自带、哪些是人工补的,要能写进报告留档,不能只活在这一次函数调用里。
    """
    for cam in sorted(scan["video_ts"]):
        ts_map = scan["video_ts"][cam]
        if len(ts_map) < 2:
            continue
        keys = sorted(ts_map)
        vals = []
        for k in keys:
            v = ts_map[k]
            vals.append(float(v[0] if isinstance(v, (list, tuple)) else v) / _NS)
        span = vals[-1] - vals[0]
        if span <= 0:
            continue
        fps = round((len(vals) - 1) / span, 6)
        if len(vals) == len(frame_ids):
            return fps, np.asarray(vals, dtype=np.float64), "video_timestamps"
        return fps, np.asarray(frame_ids, dtype=np.float64) / fps, "video_timestamps"

    # 落链:属性 fps → 用户配置 → 溯源到的源数据集 fps。溯源排在配置之后 ——
    # 它只救"什么都没给"的场景,永远不覆盖用户显式指定的值。
    prop_fps = _fps_from_properties(scan["properties"])
    fps = prop_fps or fps_arg or (provenance_fps if provenance_fps
                                  and provenance_fps > 0 else None)
    if not fps or fps <= 0:
        raise NotADatasetError(
            f"'{dataset_dir}': RRD 内没有任何时间信息(既无 VideoFrameReference 时间戳,"
            "录制属性里也没有 fps),无法把 frame_index 换算成秒。\n"
            "  请显式指定采集帧率,例如:\n"
            "    curation run --input <数据集> --output <目录> --set ingest.rrd_fps=30")
    source = ("properties" if prop_fps else
              "config" if fps_arg else "provenance")
    return float(fps), np.asarray(frame_ids, dtype=np.float64) / float(fps), source


# ---------------------------------------------------------------------------
# 一条 episode → 统一行
# ---------------------------------------------------------------------------

def _require_entities(scan: dict, mapping: dict, dataset_dir: str) -> None:
    missing = []
    if not scan["action"]:
        missing.append(f"动作 {mapping['action']!r}")
    if not scan["video"]:
        missing.append(f"视频 {mapping['video_prefix']!r}*")
    if not missing:
        return
    raise NotADatasetError(
        f"'{dataset_dir}': RRD 里找不到必需 entity({', '.join(missing)})。\n"
        f"  实际见到的 entity: {scan['seen_entities']}\n"
        f"  期望的命名约定(LeRobot→rerun 转换的事实标准):{mapping['action']}、"
        f"{mapping['state']}、{mapping['video_prefix']}<相机名>、{mapping['task']}\n"
        "  客户命名不同时,用 mapping 参数覆盖,例如 "
        "mapping={'action': '/cmd', 'video_prefix': '/cam/'}")


def _stack(frames: dict, frame_ids: list[int]) -> np.ndarray:
    return np.asarray([frames[i] for i in frame_ids], dtype=np.float32)


def _payload(rrd_path: str, idx: int, mapping: dict, fps_arg: float | None,
             videos_dir: str, dataset_dir: str) -> dict:
    """一个 .rrd → 该 episode 的全部内容(数值 + 落盘后的视频指针)。"""
    scan = _scan_rrd(rrd_path, mapping)
    _require_entities(scan, mapping, dataset_dir)
    episode_id = f"ep{idx:06d}"
    frame_ids = sorted(scan["action"])
    prov = _provenance_info(scan["properties"])
    fps, timestamps, time_source = _resolve_time(scan, frame_ids, fps_arg,
                                                 dataset_dir,
                                                 provenance_fps=prov.get("fps"))
    length = len(frame_ids)

    state_ids = sorted(scan["state"])
    # 任务描述:静态或逐帧都可能,取第一条非空即可(逐帧文本在实测数据里恒等);
    # 没有 /task 就留空 —— 下游对无标注数据自有补标线,这里不该编造。
    instruction, instruction_source = "", ""
    for t in scan["task"]:
        s = (t[0] if isinstance(t, (list, tuple)) and t else t)
        if s and str(s).strip():
            instruction, instruction_source = str(s).strip(), "task_channel"
            break
    if not instruction:
        # 降级链:/task 通道 → 属性内嵌 → 溯源回源逐条取。只补空缺。
        s = _prop_leaf(scan["properties"], _TASK_KEYS)
        if s and str(s).strip():
            instruction, instruction_source = str(s).strip(), "embedded"
        elif prov["reachable"] and prov["episode_index"] is not None:
            s = _provenance_instruction(prov["resolved"], prov["episode_index"])
            if s:
                instruction, instruction_source = s, "provenance"
    # 录制名(RecordingInfo:name):so101 的转换把任务文本混在这个展示串里
    # ("Episode 0 · ~28.8 MiB · Grab the red cube · 593 frames")。没有稳定约定,
    # 不敢当 instruction 用,但要带出去 —— 容器体检得能指出"文本疑似在这里"。
    recording_name = ""
    for k, v in scan["properties"].items():
        if k.split(":")[-1].lower() == "name":
            s = v[0] if isinstance(v, (list, tuple)) and v else v
            recording_name = str(s or "").strip()
            break
    return {
        "episode_id": episode_id,
        "episode_index": idx,
        "instruction": instruction,
        "instruction_source": instruction_source,
        "time_source": time_source,
        "recording_name": recording_name,
        "robot_type_embedded": str(_prop_leaf(scan["properties"], _ROBOT_KEYS)
                                   or "").strip(),
        "provenance": prov,
        "action": _stack(scan["action"], frame_ids),
        "proprio_state": _stack(scan["state"], state_ids) if state_ids else None,
        "video": _materialize_videos(scan, episode_id, videos_dir, fps, length),
        "timestamps": timestamps,
        "fps": float(fps),
        "length": length,
        "action_names": scan["action_names"],
        "state_names": scan["state_names"],
    }


def _synth_info(payloads: list[dict], embodiment_id: str | None) -> dict:
    """合成一份 LeRobot info.json 形状的元数据,喂给共用的语义解析层。

    RRD 没有 info.json,但语义解析只吃 robot_type / 特征名 / 版本三样;凑齐这三样,
    dataset_profiles 里的 so101/bridge 等 profile 就能照常命中(前提是调用方用
    --embodiment 报出机器人型号,RRD 文件里没有这个信息)。
    """
    p = payloads[0] if payloads else {}
    features: dict = {"action": {"dtype": "float32", "names": p.get("action_names") or []}}
    if p.get("proprio_state") is not None:
        features["observation.state"] = {"dtype": "float32",
                                         "names": p.get("state_names") or []}
    for cam in (p.get("video") or {}):
        features[cam] = {"dtype": "video"}
    prov = p.get("provenance") or {}
    # 文件派生的型号:属性内嵌优先,其次溯源取回 —— 与用户显式指定分开记,
    # 两者冲突时报告要点名(照用用户的,但"不一致"这件事本身值得知道)。
    file_rt = str(p.get("robot_type_embedded") or "") or str(prov.get("robot_type")
                                                             or "")
    if embodiment_id:
        rt, rt_src = embodiment_id, "flag"
    elif p.get("robot_type_embedded"):
        rt, rt_src = p["robot_type_embedded"], "embedded"
    elif prov.get("robot_type"):
        rt, rt_src = prov["robot_type"], "provenance"
    else:
        rt, rt_src = "unknown", ""
    return {
        "codebase_version": "rrd",
        "fps": float(p.get("fps") or 0.0),
        "robot_type": rt,
        "features": features,
        "chunks_size": 1,
        "data_path": "",
        # 容器体检信号(2026-08-10):文件自带了什么、缺了什么、补救来自哪,
        # 报告要留档。语义解析层只认上面几个 info.json 标准键,多余键不影响命中。
        "robot_type_source": rt_src,
        "robot_type_file": file_rt,
        "time_source": p.get("time_source") or "",
        "has_task_text": bool(p.get("instruction")),
        "task_source": p.get("instruction_source") or "",
        "recording_name": p.get("recording_name") or "",
        "provenance": {"source": prov.get("source") or "",
                       "reachable": bool(prov.get("reachable"))},
    }


def _effective_embodiment(payloads: list[dict],
                          embodiment_id: str | None) -> str | None:
    """行级 embodiment:用户显式指定 > 属性内嵌 > 溯源取回 > None(照旧 unknown)。"""
    if embodiment_id:
        return embodiment_id
    p = payloads[0] if payloads else {}
    return (str(p.get("robot_type_embedded") or "").strip()
            or str((p.get("provenance") or {}).get("robot_type") or "").strip()
            or None)


def _record_dim_names(rows: list[dict], payloads: list[dict]) -> None:
    """维名并进 semantics_extras(信息别丢,又不给下游多一列 schema)。"""
    for row, p in zip(rows, payloads):
        try:
            extras = json.loads(row.get("semantics_extras") or "{}")
        except (TypeError, ValueError):
            extras = {}
        if p.get("action_names"):
            extras["action_names"] = p["action_names"]
        if p.get("state_names"):
            extras["state_names"] = p["state_names"]
        extras["source_format"] = "rrd"
        row["semantics_extras"] = json.dumps(extras, ensure_ascii=False)


def _read_payloads(dataset_dir: str, max_episodes: int | None, start_episode: int,
                   episode_indices: set[int] | None, fps: float | None,
                   mapping: dict | None) -> list[dict]:
    mp = dict(DEFAULT_MAPPING, **(mapping or {}))
    videos_dir = _videos_dir(dataset_dir)
    return [_payload(path, idx, mp, fps, videos_dir, dataset_dir)
            for idx, path in _episode_files(dataset_dir, max_episodes,
                                            start_episode, episode_indices)]


# ---------------------------------------------------------------------------
# 公开 API(签名与 lerobot_reader 同构,管线可直接换用)
# ---------------------------------------------------------------------------

def read_rrd_rows(
    dataset_dir: str,
    max_episodes: int | None = None,
    embodiment_id: str | None = None,
    validate: bool = True,
    start_episode: int = 0,
    skip_missing: bool = False,
    episode_indices: set[int] | None = None,
    fps: float | None = None,
    mapping: dict | None = None,
) -> list[dict]:
    """RRD 数据集 → 每 episode 一个 dict(字段与 read_lerobot_rows 完全一致)。

    fps: RRD 内无时间信息时由调用方给(管线走配置 ingest.rrd_fps);
    mapping: entity 命名覆盖(见 DEFAULT_MAPPING),客户命名不合约定时用;
    skip_missing: 为签名兼容保留 —— RRD 的 episode 清单就是目录里的文件,没有"缺口"。
    """
    from .validate import validate_rows

    payloads = _read_payloads(dataset_dir, max_episodes, start_episode,
                              episode_indices, fps, mapping)
    # 行级型号用"有效值"(用户 > 内嵌 > 溯源);_synth_info 拿原始 flag 自行分层,
    # 才能既算出有效值又记住来源(冲突检测靠这个区分)。
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
    _record_dim_names(rows, payloads)
    if validate:
        validate_rows(rows)
    return rows


def read_rrd_meta(
    dataset_dir: str,
    max_episodes: int | None = None,
    embodiment_id: str | None = None,
    skip_missing: bool = False,
    episode_indices: set[int] | None = None,
    fps: float | None = None,
    mapping: dict | None = None,
) -> list[dict]:
    """每 episode 一个轻量 dict(字段与 read_lerobot_meta 一致)。

    ⚠️ 与 LeRobot 不同:这里没有"只读元数据"的捷径 —— 视频字节和数值在同一个 .rrd 里,
    要拿到视频指针就得把文件解一遍。解出来的 mp4 会留在临时目录复用(幂等),
    所以后续几遍(漏斗/去重/导出)不会重复转码。
    """
    payloads = _read_payloads(dataset_dir, max_episodes, 0, episode_indices, fps, mapping)
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
    _record_dim_names(metas, payloads)
    return metas


def rrd_dataset_info(dataset_dir: str, fps: float | None = None,
                     mapping: dict | None = None,
                     embodiment_id: str | None = None) -> dict:
    """数据集级元数据(info.json 形状),给管线的身份行/语义解析用。只解第一条。"""
    payloads = _read_payloads(dataset_dir, 1, 0, None, fps, mapping)
    return _synth_info(payloads, embodiment_id)


def read_rrd_lazy(dataset_dir: str, max_episodes: int | None = None,
                  embodiment_id: str | None = None, validate: bool = True,
                  skip_missing: bool = True,
                  episode_indices: set[int] | None = None,
                  fps: float | None = None, mapping: dict | None = None):
    """RRD 数据集 → daft DataFrame(与 read_lerobot_lazy 同 schema)。

    这里是急切读后再建 DataFrame,不像 LeRobot 那样做 DataSource 懒扫描:RRD 的大头
    是视频字节,而视频**必须**先解出成 mp4 才能给下游当指针用 —— 懒不掉。留在内存里
    的只有 action/state(每条几十 KB)。
    """
    from .lerobot_reader import rows_to_daft

    return rows_to_daft(read_rrd_rows(dataset_dir, max_episodes, embodiment_id,
                                      validate, episode_indices=episode_indices,
                                      fps=fps, mapping=mapping))
