"""Lance / LanceDB 数据集 → 统一 Episode 行(与 lerobot_reader 同一行契约)。

P1(2026-09-18):客户把 LeRobot 形态的数据存进 lancedb(帧级行的 lance 表)直接进
现有质检漏斗。行的字段、语义解析、校验全部复用 lerobot_reader 的那一套 —— 下游
(漏斗/报告/画像)一个字都不用改,只有"字节怎么读出来"这一层是新的(与 rrd_reader
同一条路,做法也刻意同构:总开关、mapping 覆盖、认不出时报错列出实见列名)。

与 LeRobot 的结构差异,以及本模块的应对:
1. **一张表,帧级行**:没有 meta/、没有 episode 文件边界。按 episode 列的值分组切回
   逐 episode;编号 = episode 列的整数值(报告里的 ep000007 就是表里 episode=7 的行,
   回源对账一步到位)。
2. **列名不带点**:lance 顶层字段禁止 `.`(实测 LanceError(Schema)),所以 LeRobot
   的 observation.state 在 lance 里只能是下划线形态 —— 默认映射按 observation_state /
   observation_images_* 认,客户命名不同用 mapping 覆盖。
3. **视频有两种存法**:
   - 相机列是 binary:逐帧 JPEG 字节 → 封装进 mjpeg-mp4 容器(**只换封装,不重编码**,
     原始字节逐帧进容器,视觉检查看到的就是原图);非 JPEG 字节响亮拒绝(转码尚未实现)。
   - 相机列是 string:指向每 episode 一个的视频文件(相对表目录或绝对路径/tos://),
     原样当指针用,零拷贝零转码。
   两种下游看到的形态都与 LeRobot v2 完全同构({path, from_ts, to_ts} 指针)。
4. **时间轴**:优先 timestamp 列(归零成 episode 内相对秒,fps 从中推);没有该列时
   靠 schema metadata 的 fps 或调用方给(--set ingest.lance_fps=30);都没有响亮失败,
   绝不默认 30 蒙混(与 rrd_fps 同一条纪律)。
5. **机器人型号**:schema metadata 的 robot_type(转换方写表时带上即全自动),
   没有则靠 --embodiment;分层与冲突留痕的规则与 rrd 一致(用户指定 > 文件内嵌)。

⚠️ pylance 是**懒导入**:基础镜像可不装它,LeRobot 路径不能因为这个可选依赖受牵连。
⚠️ 内存纪律:表里帧级视频字节可能比数值大三个量级,**绝不 to_table() 整表**;按
   episode 分批(GROUP_EPISODES 条一批)filter 下推读取,视频字节落成 mp4 即弃。
⚠️ 视频落盘进 tempfile 目录(/tmp),绝不写 /mnt/tos(FSX 拒绝随机写,mp4 muxer
   收尾要 seek 回文件头 —— 与 rrd_reader/export 同一族的坑)。
⚠️ 只支持本地/挂载路径:lance 需要真文件系统;tos:// 直读(object store 配置)本版本
   未接,挂载盘(/mnt/tos)照常能读。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from fractions import Fraction

import numpy as np

from .lerobot_reader import NotADatasetError, _attach_semantics, resolve_dataset_semantics

# 列名约定(LeRobot → lance 转换的自然转写:点换下划线;认不出时报错会把这份约定
# 原样打出去)。episode 列要求整数值 —— 它就是 episode 编号。
DEFAULT_MAPPING = {
    "episode": "episode_index",
    "timestamp": "timestamp",
    "action": "action",
    "state": "observation_state",
    "task": "task",
    "video_prefix": "observation_images_",
}

#: 每批读几条 episode(filter 下推):批越大 IO 越省、内存越涨 —— 帧级 JPEG 字节
#: 一条 episode 就是几十 MB,8 条一批把峰值压在百 MB 量级。
GROUP_EPISODES = 8

#: schema metadata 里认的键(转换方写表时 replace_schema_metadata 带上即可)
_MD_FPS_KEYS = ("fps", "frame_rate", "framerate")
_MD_ROBOT_KEYS = ("robot_type", "robot", "embodiment")

_JPEG_MAGIC = b"\xff\xd8"

# 一个数据集一个视频临时目录(进程内缓存):meta/漏斗/去重/导出会多次重读同一批
# episode,重复封装同样的 mp4 纯属浪费 —— 目录固定 + 文件名确定 ⇒ 天然幂等。
_VIDEO_DIRS: dict[str, str] = {}


class LanceDependencyError(ImportError):
    """缺 pylance。lance 是可选格式,报错要给出可照抄的安装命令。"""


def _lance_mod():
    """懒导入 pylance(装没装不影响 LeRobot 路径)。"""
    try:
        import lance
    except ImportError as e:
        raise LanceDependencyError(
            "读取 lance 数据集需要 pylance(基础镜像未预装):\n"
            "  pip install pylance==8.0.0\n"
            f"  (原始错误: {e})") from e
    return lance


#: lance 总开关(与 ingest.rrd_enabled 同一套纪律):release 默认关,开法两种 ——
#: 配置 `ingest.lance_enabled: true`(apply_config)或环境变量 CURATION_LANCE_ENABLED=1。
_ENABLED: bool | None = None
_CFG_FLAG: bool | None = None
_CFG_TABLE: str | None = None     # ingest.lance_table:库目录多表时指定表名
LANCE_DISABLED_MSG = ("这是 lance(lancedb)格式的数据集;lance 质检暂未开放"
                      "(需要时在配置里打开 ingest.lance_enabled: true)")


def set_enabled(flag: bool | None) -> None:
    """进程级开关:True/False 直接定;None 退回"按环境变量/默认关"。"""
    global _ENABLED
    _ENABLED = None if flag is None else bool(flag)


def apply_config(cfg: dict | None) -> None:
    """从流水线配置读 ingest.lance_enabled / lance_table(没写 = 不改当前状态)。"""
    global _CFG_FLAG, _CFG_TABLE
    ing = (cfg or {}).get("ingest") or {}
    if ing.get("lance_enabled") is not None:
        _CFG_FLAG = bool(ing["lance_enabled"])
    if ing.get("lance_table") is not None:
        _CFG_TABLE = str(ing["lance_table"])


def lance_enabled() -> bool:
    """优先级:set_enabled 强设 > 环境变量 CURATION_LANCE_ENABLED > 配置 > 默认关。"""
    if _ENABLED is not None:
        return _ENABLED
    env = os.environ.get("CURATION_LANCE_ENABLED", "").strip().lower()
    if env:
        return env in ("1", "true", "yes", "on")
    if _CFG_FLAG is not None:
        return _CFG_FLAG
    return False


def _is_lance_table_dir(d: str) -> bool:
    """一个目录是不是 lance 数据集本体(_versions/ 是它的身份证,实测 8.0 布局)。"""
    return os.path.isdir(os.path.join(d, "_versions"))


def _table_subdirs(dataset_dir: str) -> list[str]:
    """lancedb 库目录下的表(<表名>.lance 子目录),按名排序。"""
    try:
        names = sorted(os.listdir(dataset_dir))
    except OSError:
        return []
    return [n for n in names
            if n.endswith(".lance") and _is_lance_table_dir(os.path.join(dataset_dir, n))]


def has_lance_files(dataset_dir: str) -> bool:
    """目录是 lance 表本体,或是含 *.lance 表的 lancedb 库目录(不看开关;
    报错措辞与清单排除用)。"""
    try:
        return _is_lance_table_dir(dataset_dir) or bool(_table_subdirs(dataset_dir))
    except Exception:  # noqa: BLE001 列不到 = 没有
        return False


def is_lance_dataset(dataset_dir: str) -> bool:
    """有 lance 表**且开关打开**才认作 lance 数据集(管线的格式嗅探入口)。"""
    return lance_enabled() and has_lance_files(dataset_dir)


def _resolve_table(dataset_dir: str, table: str | None) -> str:
    """--input 目录 → 实际 lance 表路径。表本体原样;库目录单表自动选,多表要指名。"""
    if _is_lance_table_dir(dataset_dir):
        return dataset_dir
    subs = _table_subdirs(dataset_dir)
    if not subs:
        raise NotADatasetError(
            f"'{dataset_dir}' 不是 lance 数据集(既非表本体,目录下也没有 *.lance 表)")
    table = table if table is not None else _CFG_TABLE
    if table:
        cand = table if table.endswith(".lance") else table + ".lance"
        if cand in subs:
            return os.path.join(dataset_dir, cand)
        raise NotADatasetError(
            f"'{dataset_dir}' 里没有名为 {table!r} 的表;实有: "
            f"{[s[:-len('.lance')] for s in subs]}")
    if len(subs) == 1:
        return os.path.join(dataset_dir, subs[0])
    raise NotADatasetError(
        f"'{dataset_dir}' 是含 {len(subs)} 张表的 lancedb 库目录,请指定表名"
        f"(--set ingest.lance_table=<表名>);实有: "
        f"{[s[:-len('.lance')] for s in subs]}")


def _videos_dir(dataset_dir: str) -> str:
    key = os.path.abspath(dataset_dir)
    if key not in _VIDEO_DIRS:
        _VIDEO_DIRS[key] = tempfile.mkdtemp(prefix="lance_videos_")
    return _VIDEO_DIRS[key]


def cleanup_video_cache(dataset_dir: str | None = None) -> int:
    """删掉本进程为 lance 封装的临时 mp4 目录(理由与 rrd_reader 同款:/tmp 是容器
    可写层,不清会积 GB 级)。幂等,收尾清理绝不能成为新的失败源。"""
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


# ---------------------------------------------------------------------------
# 视频落盘:逐帧 JPEG 字节 → mjpeg-mp4(只换封装;spike 实测 av 读回帧数/时间戳全对)
# ---------------------------------------------------------------------------

def _video_path(videos_dir: str, episode_id: str, cam: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", cam)
    return os.path.join(videos_dir, f"{episode_id}__{safe}.mp4")


def mux_jpeg_frames(blobs: list[bytes], times_s: list[float], out_path: str,
                    fps: float) -> None:
    """JPEG 字节序列 → mjpeg-mp4 容器(**不解码不重编码**,pts 按真实相对时间填)。

    时基 90kHz:非整数 fps 也能整除到 1 个 tick 内(与 rrd_reader._remux_annexb 同款)。
    先写 .part 再 rename:半截文件被下游当成有效视频指针是最难查的一类事故。
    """
    import av
    import cv2

    bad = [i for i, b in enumerate(blobs) if not bytes(b[:2]) == _JPEG_MAGIC]
    if bad:
        raise NotADatasetError(
            f"{out_path}: 相机列的字节不是 JPEG(第 {bad[0]} 帧,前 4 字节 "
            f"{bytes(blobs[bad[0]][:4])!r});当前只支持 JPEG 帧免转码封装,"
            "其它编码(PNG/raw)需要真转码,尚未实现")
    img0 = cv2.imdecode(np.frombuffer(blobs[0], np.uint8), cv2.IMREAD_COLOR)
    if img0 is None:
        raise NotADatasetError(f"{out_path}: 首帧 JPEG 解码失败,数据疑似损坏")
    h, w = img0.shape[:2]
    time_base = Fraction(1, 90000)
    step = int(round((1 / fps) / time_base))
    tmp = out_path + ".part"
    with av.open(tmp, "w", format="mp4") as dst:
        s = dst.add_stream("mjpeg", rate=max(1, round(fps)))
        s.width, s.height = w, h
        s.pix_fmt = "yuvj420p"
        s.time_base = time_base
        for b, t in zip(blobs, times_s):
            pkt = av.Packet(bytes(b))
            pkt.stream = s
            pkt.time_base = time_base
            pkt.pts = pkt.dts = int(round(t * 90000))
            pkt.duration = step
            dst.mux(pkt)
    os.replace(tmp, out_path)


# ---------------------------------------------------------------------------
# 表扫描:episode 清单(轻)与分批行构造(重,filter 下推)
# ---------------------------------------------------------------------------

def _open(dataset_dir: str, table: str | None):
    lance = _lance_mod()
    path = _resolve_table(dataset_dir, table)
    return lance.dataset(path), path


def _schema_meta(schema) -> dict:
    md = schema.metadata or {}
    out = {}
    for k, v in md.items():
        try:
            out[k.decode() if isinstance(k, bytes) else str(k)] = (
                v.decode() if isinstance(v, bytes) else str(v))
        except Exception:  # noqa: BLE001 元数据五花八门,取不动就不取
            continue
    return out


def _md_leaf(meta: dict, keys: tuple) -> str:
    for k, v in meta.items():
        if k.strip().lower() in keys:
            return str(v).strip()
    return ""


def _require_columns(schema, mapping: dict, table_path: str) -> tuple[list[str], dict]:
    """必需列(episode/action)与相机列清单;认不出时报错列出实见列名与出路。"""
    names = list(schema.names)
    cams = {n: ("frames" if _is_binary(schema.field(n).type) else
                "path" if _is_string(schema.field(n).type) else "")
            for n in names if n.startswith(mapping["video_prefix"])}
    cams = {n: k for n, k in cams.items() if k}
    missing = []
    if mapping["episode"] not in names:
        missing.append(f"episode 列 {mapping['episode']!r}")
    if mapping["action"] not in names:
        missing.append(f"动作列 {mapping['action']!r}")
    if not cams:
        missing.append(f"相机列 {mapping['video_prefix']!r}*(binary=逐帧 JPEG / "
                       "string=视频文件路径)")
    if missing:
        raise NotADatasetError(
            f"'{table_path}': lance 表里找不到必需列({', '.join(missing)})。\n"
            f"  实际的列: {names}\n"
            f"  期望的命名约定(LeRobot→lance 的下划线转写):{mapping['episode']}、"
            f"{mapping['action']}、{mapping['state']}、{mapping['video_prefix']}<相机名>、"
            f"{mapping['task']}\n"
            "  客户命名不同时,用 mapping 参数覆盖,例如 "
            "mapping={'action': 'cmd', 'video_prefix': 'cams_'}")
    return names, cams


def _is_binary(t) -> bool:
    import pyarrow as pa
    return pa.types.is_binary(t) or pa.types.is_large_binary(t)


def _is_string(t) -> bool:
    import pyarrow as pa
    return pa.types.is_string(t) or pa.types.is_large_string(t)


def _episode_values(ds, mapping: dict, table_path: str,
                    max_episodes: int | None, start_episode: int,
                    episode_indices: set[int] | None) -> list[int]:
    """episode 列 → 选中的编号列表(升序)。只读这一列,万帧秒级。"""
    col = ds.to_table(columns=[mapping["episode"]])[mapping["episode"]]
    try:
        vals = sorted({int(v) for v in col.to_pylist()})
    except (TypeError, ValueError):
        raise NotADatasetError(
            f"'{table_path}': episode 列 {mapping['episode']!r} 不是整数"
            "(episode 编号约定为整数值;字符串编号尚未支持)")
    if episode_indices is not None:
        vals = [v for v in vals if v in episode_indices]
    vals = vals[start_episode:]
    if max_episodes is not None:
        vals = vals[:max_episodes]
    return vals


def _resolve_video_path(raw: str, table_path: str) -> str:
    """string 相机列的值 → 视频指针路径:tos://或绝对路径原样,相对路径按表目录解析。"""
    p = str(raw or "").strip()
    if not p:
        return p
    if p.startswith("tos://") or os.path.isabs(p):
        return p
    return os.path.join(os.path.dirname(table_path.rstrip("/")), p)


def _group_payloads(ds, table_path: str, mapping: dict, cams: dict,
                    ep_values: list[int], fps_arg: float | None,
                    meta: dict, videos_dir: str) -> list[dict]:
    """按批 filter 下推读取选中 episode → payload 列表(视频字节落成 mp4 即弃)。"""
    names = list(ds.schema.names)
    has_ts = mapping["timestamp"] in names
    has_state = mapping["state"] in names
    has_task = mapping["task"] in names
    cols = [mapping["episode"], mapping["action"]]
    cols += [mapping["timestamp"]] if has_ts else []
    cols += [mapping["state"]] if has_state else []
    cols += [mapping["task"]] if has_task else []
    cols += list(cams)

    payloads: list[dict] = []
    for i in range(0, len(ep_values), GROUP_EPISODES):
        batch_vals = ep_values[i:i + GROUP_EPISODES]
        flt = f"{mapping['episode']} IN ({', '.join(str(v) for v in batch_vals)})"
        tbl = ds.to_table(columns=cols, filter=flt)
        ep_col = np.asarray([int(v) for v in tbl[mapping["episode"]].to_pylist()])
        ts_col = (np.asarray(tbl[mapping["timestamp"]].to_pylist(), dtype=np.float64)
                  if has_ts else None)
        for v in batch_vals:
            idx = np.nonzero(ep_col == v)[0]
            if not len(idx):
                continue
            # 行序:有 timestamp 按它排(帧级行不保证物理有序),没有就按存储序
            if ts_col is not None:
                idx = idx[np.argsort(ts_col[idx], kind="stable")]
            payloads.append(_payload_from_slice(
                tbl, idx, v, mapping, cams, has_ts, has_state, has_task,
                fps_arg, meta, videos_dir, table_path))
    return payloads


def _payload_from_slice(tbl, idx, ep_value: int, mapping: dict, cams: dict,
                        has_ts: bool, has_state: bool, has_task: bool,
                        fps_arg: float | None, meta: dict, videos_dir: str,
                        table_path: str) -> dict:
    episode_id = f"ep{ep_value:06d}"
    n = len(idx)
    take = idx.tolist()

    action = np.asarray(tbl[mapping["action"]].take(take).to_pylist(),
                        dtype=np.float32)
    state = (np.asarray(tbl[mapping["state"]].take(take).to_pylist(),
                        dtype=np.float32) if has_state else None)

    # 时间轴:timestamp 列(归零) → metadata fps → 配置 fps → 响亮失败
    if has_ts:
        raw_ts = np.asarray(tbl[mapping["timestamp"]].take(take).to_pylist(),
                            dtype=np.float64)
        timestamps = raw_ts - (raw_ts[0] if n else 0.0)
        span = float(timestamps[-1]) if n > 1 else 0.0
        fps = round((n - 1) / span, 6) if span > 0 else 0.0
        time_source = "timestamps"
        if fps <= 0:
            raise NotADatasetError(
                f"{episode_id}: timestamp 列无法推出帧率(首尾同值或单帧),数据疑似损坏")
    else:
        md_fps = _md_leaf(meta, _MD_FPS_KEYS)
        fps = float(md_fps) if md_fps else (float(fps_arg) if fps_arg else 0.0)
        if fps <= 0:
            raise NotADatasetError(
                f"'{table_path}': 表里没有 {mapping['timestamp']!r} 列,schema metadata "
                "也没有 fps,无法把帧序换算成秒。\n"
                "  请显式指定采集帧率,例如:\n"
                "    curation run --input <数据集> --output <目录> --set ingest.lance_fps=30")
        timestamps = np.arange(n, dtype=np.float64) / fps
        time_source = "metadata" if md_fps else "config"

    instruction = ""
    if has_task:
        for t in tbl[mapping["task"]].take(take).to_pylist():
            if t and str(t).strip():
                instruction = str(t).strip()
                break

    videos: dict = {}
    duration = float(timestamps[-1]) + 1.0 / fps if n else 0.0
    for cam, kind in cams.items():
        if kind == "path":
            raw = tbl[cam].take(take[:1]).to_pylist()
            p = _resolve_video_path(raw[0] if raw else "", table_path)
            if p:
                videos[cam] = {"path": p, "from_ts": 0.0, "to_ts": duration}
            continue
        out_path = _video_path(videos_dir, episode_id, cam)
        if not os.path.exists(out_path):        # 幂等:同批 episode 会被读好几遍
            blobs = tbl[cam].take(take).to_pylist()
            blobs = [b for b in blobs if b]
            if not blobs:
                continue                        # 全空的占位相机列:这一路没画面
            mux_jpeg_frames(blobs, timestamps[:len(blobs)].tolist(), out_path, fps)
        videos[cam] = {"path": out_path, "from_ts": 0.0, "to_ts": duration}

    return {
        "episode_id": episode_id,
        "episode_index": ep_value,
        "instruction": instruction,
        "time_source": time_source,
        "action": action,
        "proprio_state": state,
        "video": videos,
        "timestamps": timestamps,
        "fps": float(fps),
        "length": n,
    }


# ---------------------------------------------------------------------------
# info 合成 + 公开 API(签名与 lerobot_reader/rrd_reader 同构,管线可直接换用)
# ---------------------------------------------------------------------------

def _synth_info(payloads: list[dict], meta: dict, cams: dict,
                embodiment_id: str | None, table_path: str) -> dict:
    """合成 LeRobot info.json 形状的元数据,喂给共用的语义解析层(与 rrd 同款)。

    维名:schema metadata 里可选的 action_names / state_names(JSON 列表);带了
    profile/预检就能按名认布局,没带走数值指纹。"""
    p = payloads[0] if payloads else {}

    def _names(key: str) -> list:
        try:
            v = json.loads(meta.get(key, "") or "[]")
            return v if isinstance(v, list) else []
        except (TypeError, ValueError):
            return []

    features: dict = {"action": {"dtype": "float32", "names": _names("action_names")}}
    if p.get("proprio_state") is not None:
        features["observation.state"] = {"dtype": "float32",
                                         "names": _names("state_names")}
    for cam in cams:
        features[cam] = {"dtype": "video"}
    file_rt = _md_leaf(meta, _MD_ROBOT_KEYS)
    if embodiment_id:
        rt, rt_src = embodiment_id, "flag"
    elif file_rt:
        rt, rt_src = file_rt, "embedded"
    else:
        rt, rt_src = "unknown", ""
    return {
        "codebase_version": "lance",
        "fps": float(p.get("fps") or 0.0),
        "robot_type": rt,
        "features": features,
        "chunks_size": 1,
        "data_path": "",
        "robot_type_source": rt_src,
        "robot_type_file": file_rt,
        "time_source": p.get("time_source") or "",
        "has_task_text": bool(p.get("instruction")),
        "table_path": table_path,
    }


def _effective_embodiment(meta: dict, embodiment_id: str | None) -> str | None:
    """行级 embodiment:用户显式指定 > schema metadata 内嵌 > None(照旧 unknown)。"""
    return embodiment_id or (_md_leaf(meta, _MD_ROBOT_KEYS) or None)


def _record_source_format(rows: list[dict], table_path: str) -> None:
    for row in rows:
        try:
            extras = json.loads(row.get("semantics_extras") or "{}")
        except (TypeError, ValueError):
            extras = {}
        extras["source_format"] = "lance"
        extras["lance_table"] = os.path.basename(table_path.rstrip("/"))
        row["semantics_extras"] = json.dumps(extras, ensure_ascii=False)


def _read_payloads(dataset_dir: str, max_episodes: int | None, start_episode: int,
                   episode_indices: set[int] | None, fps: float | None,
                   mapping: dict | None, table: str | None
                   ) -> tuple[list[dict], dict, dict, str]:
    mp = dict(DEFAULT_MAPPING, **(mapping or {}))
    ds, table_path = _open(dataset_dir, table)
    meta = _schema_meta(ds.schema)
    _, cams = _require_columns(ds.schema, mp, table_path)
    ep_values = _episode_values(ds, mp, table_path, max_episodes, start_episode,
                                episode_indices)
    payloads = _group_payloads(ds, table_path, mp, cams, ep_values, fps, meta,
                               _videos_dir(dataset_dir))
    return payloads, meta, cams, table_path


def read_lance_rows(
    dataset_dir: str,
    max_episodes: int | None = None,
    embodiment_id: str | None = None,
    validate: bool = True,
    start_episode: int = 0,
    skip_missing: bool = False,
    episode_indices: set[int] | None = None,
    fps: float | None = None,
    mapping: dict | None = None,
    table: str | None = None,
) -> list[dict]:
    """lance 数据集 → 每 episode 一个 dict(字段与 read_lerobot_rows 完全一致)。

    fps: 表里没有 timestamp 列、metadata 也没有 fps 时由调用方给(管线走配置
    ingest.lance_fps);
    mapping: 列名覆盖(见 DEFAULT_MAPPING),客户命名不合约定时用;
    table: lancedb 库目录多表时指定表名(管线走配置 ingest.lance_table);
    skip_missing: 为签名兼容保留 —— 表里的行就是清单本身,没有"缺文件"。
    """
    from .validate import validate_rows

    payloads, meta, cams, table_path = _read_payloads(
        dataset_dir, max_episodes, start_episode, episode_indices, fps, mapping, table)
    eff_emb = _effective_embodiment(meta, embodiment_id)
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
    sem = resolve_dataset_semantics(
        _synth_info(payloads, meta, cams, embodiment_id, table_path), rows)
    _attach_semantics(rows, sem, eff_emb)
    _record_source_format(rows, table_path)
    if validate:
        validate_rows(rows)
    return rows


def read_lance_meta(
    dataset_dir: str,
    max_episodes: int | None = None,
    embodiment_id: str | None = None,
    skip_missing: bool = False,
    episode_indices: set[int] | None = None,
    fps: float | None = None,
    mapping: dict | None = None,
    table: str | None = None,
) -> list[dict]:
    """每 episode 一个轻量 dict(字段与 read_lerobot_meta 一致)。

    ⚠️ 与 LeRobot 不同:视频字节和数值在同一张表里,要拿到视频指针就得把选中的行
    读一遍。封装出的 mp4 留在临时目录复用(幂等),后续几遍不会重复封装。
    """
    payloads, meta, cams, table_path = _read_payloads(
        dataset_dir, max_episodes, 0, episode_indices, fps, mapping, table)
    eff_emb = _effective_embodiment(meta, embodiment_id)
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
    sem = resolve_dataset_semantics(
        _synth_info(payloads, meta, cams, embodiment_id, table_path), sample)
    _attach_semantics(metas, sem, eff_emb)
    _record_source_format(metas, table_path)
    return metas


def lance_dataset_info(dataset_dir: str, fps: float | None = None,
                       mapping: dict | None = None,
                       embodiment_id: str | None = None,
                       table: str | None = None) -> dict:
    """数据集级元数据(info.json 形状),给管线的身份行/语义解析用。只读第一条。"""
    payloads, meta, cams, table_path = _read_payloads(
        dataset_dir, 1, 0, None, fps, mapping, table)
    return _synth_info(payloads, meta, cams, embodiment_id, table_path)


def read_lance_lazy(dataset_dir: str, max_episodes: int | None = None,
                    embodiment_id: str | None = None, validate: bool = True,
                    skip_missing: bool = True,
                    episode_indices: set[int] | None = None,
                    fps: float | None = None, mapping: dict | None = None,
                    table: str | None = None):
    """lance 数据集 → daft DataFrame(与 read_lerobot_lazy 同 schema)。

    急切读后建 DataFrame,不做 DataSource 懒扫描(与 rrd 同一取舍):帧级视频字节
    **必须**先封装成 mp4 才能给下游当指针用 —— 懒不掉;分批 filter 下推已把内存
    压在一批 episode 的量级,留在内存里的只有 action/state(每条几十 KB)。
    """
    from .lerobot_reader import rows_to_daft

    return rows_to_daft(read_lance_rows(dataset_dir, max_episodes, embodiment_id,
                                        validate, episode_indices=episode_indices,
                                        fps=fps, mapping=mapping, table=table))
