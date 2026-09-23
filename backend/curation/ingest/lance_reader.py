"""Lance(lerobot-lance-convert 三表布局)数据集 → 统一 Episode 行。

P2(2026-09-21,用户定):只支持官方 `lerobot-lance-convert`(≥0.3.0)的布局 ——
生态唯一正主(出处 lancedb/lerobot-lancedb README);0.3.0 前旧插件的单表布局
官方已废弃,本模块不支持(第一版曾支持,按"代码做 lean"决定移除)。

布局(转换器产出,README 原文):
    <root>/
      meta/             # 逐字节的 LeRobot v3.0 元数据(info.json/episodes/tasks)
      frames.lance      # 一行一帧:全部数值特征,列名=特征名点转下划线,按 index 排序
      videos.lance      # 一行一个源 mp4:字节在 blob v2 列 + 字节索引列
      meta.lance        # meta/ 文件的 (path, bytes) 镜像(远端根的元数据搬运)
    info.json 带 "storage_format": "lance" 标记,读端凭它认后端。

实现路线 = **LeRobot v3 语义 + lance 字节后端**:episode 边界/任务表/语义解析全部
复用 lerobot_reader 的 v3 现成逻辑(meta/ 就是标准 v3,零新代码),只换两处字节访问:
- data parquet 的读取 → frames.lance 按全局 index 范围 filter 查询;
- videos/ 目录的 mp4 文件 → videos.lance 的 blob **整段落盘**(逐字节原样,零转码),
  多条 episode 共享同一个合并 mp4(v3 布局的本性)→ 按 (video_key,chunk,file) 缓存,
  指针的 from/to_timestamp 照旧来自 episodes 元数据。
meta/ 目录缺失(只拷了三张表的根)时,从 meta.lance 镜像把 meta/ 物化到临时目录再走
同一条路 —— 镜像的存在意义就是这个。

列名对账:优先 frames 表 schema metadata 里转换器写的 `source-column-name-map`
(lance 列名 → 源特征名),缺失才按"点转下划线"约定倒推。

⚠️ pylance 懒导入(不装不影响 LeRobot/RRD/mcap 路径);只支持本地/挂载路径。
⚠️ blob 落盘进 tempfile 目录,绝不写 /mnt/tos(FSX 拒绝随机写,老坑)。
⚠️ 约定还年轻(0.3.0 已断代一次):靠 storage_format 标记 + 三表探测认版本,
   认不出响亮失败,绝不静默读错 —— 与 rrd 钉 SDK 版本同一条纪律。
"""
from __future__ import annotations

import json
import os
import re
import tempfile

import numpy as np

from .lerobot_reader import (
    NotADatasetError,
    _attach_semantics,
    _load_info,
    _load_tasks_map,
    _v3_ep_meta,
    _v3_instruction,
    resolve_dataset_semantics,
)


class LanceDependencyError(ImportError):
    """缺 pylance。lance 是可选格式,报错要给出可照抄的安装命令。"""


def _lance_mod():
    try:
        import lance
    except ImportError as e:
        raise LanceDependencyError(
            "读取 lance 数据集需要 pylance(基础镜像未预装):\n"
            "  pip install pylance==8.0.0\n"
            f"  (原始错误: {e})") from e
    return lance


#: lance 总开关:**默认开**(2026-09-21 用户定);个别实例要关,
#: 配置 `ingest.lance_enabled: false`(apply_config)或环境变量 CURATION_LANCE_ENABLED=0。
_ENABLED: bool | None = None
_CFG_FLAG: bool | None = None
LANCE_DISABLED_MSG = ("这是 lance(lerobot-lance-convert)格式的数据集;本实例已关闭"
                      " lance 质检(开法:配置 ingest.lance_enabled: true 或环境变量 CURATION_LANCE_ENABLED=1)")


def set_enabled(flag: bool | None) -> None:
    """进程级开关:True/False 直接定;None 退回"按环境变量/默认关"。"""
    global _ENABLED
    _ENABLED = None if flag is None else bool(flag)


def apply_config(cfg: dict | None) -> None:
    """从流水线配置读 ingest.lance_enabled(没写 = 不改当前状态)。"""
    global _CFG_FLAG
    v = ((cfg or {}).get("ingest") or {}).get("lance_enabled")
    if v is not None:
        _CFG_FLAG = bool(v)


def lance_enabled() -> bool:
    """优先级:set_enabled 强设 > 环境变量 CURATION_LANCE_ENABLED > 配置 > **默认开**(2026-09-21 用户定:多格式是产品要支持的能力)。"""
    if _ENABLED is not None:
        return _ENABLED
    env = os.environ.get("CURATION_LANCE_ENABLED", "").strip().lower()
    if env:
        return env in ("1", "true", "yes", "on")
    if _CFG_FLAG is not None:
        return _CFG_FLAG
    return True


def _is_table_dir(d: str) -> bool:
    return os.path.isdir(os.path.join(d, "_versions"))


def has_lance_files(dataset_dir: str) -> bool:
    """目录是不是 V1 三表布局(frames.lance + videos.lance 两张表是它的身份证;
    不看开关 —— 报错措辞与清单排除用)。"""
    try:
        return (_is_table_dir(os.path.join(dataset_dir, "frames.lance"))
                and _is_table_dir(os.path.join(dataset_dir, "videos.lance")))
    except Exception:  # noqa: BLE001 列不到 = 没有
        return False


def is_lance_dataset(dataset_dir: str) -> bool:
    """V1 三表齐 **且开关打开**才认作 lance 数据集(管线的格式嗅探入口)。"""
    return lance_enabled() and has_lance_files(dataset_dir)


# ---------------------------------------------------------------------------
# meta/ 的取得:目录在就直接用;缺了从 meta.lance 镜像物化(进程内缓存)
# ---------------------------------------------------------------------------

_META_ROOTS: dict[str, str] = {}      # 数据集根 → 可读 meta/ 的根(本体或物化临时根)
_VIDEO_DIRS: dict[str, str] = {}      # 数据集根 → blob 落盘的临时目录
#: **只有本模块 mkdtemp 出来的目录**才进这个集合;清理只认它,绝不用"路径长得像
#: 临时目录"去猜 —— 2026-09-21 审查实锤:源数据集本身就可能在 /tmp 下(测试/
#: scratchpad 常态),按前缀猜会把客户源数据整个 rmtree 掉。
_MATERIALIZED: set[str] = set()


def _meta_root(dataset_dir: str) -> str:
    """返回"meta/ 目录可读"的数据集根。本体有 meta/ 就是它自己;只有三张表的根
    从 meta.lance 把 (path, bytes) 写到临时根下(逐字节镜像,天然幂等)。"""
    key = os.path.abspath(dataset_dir)
    if key in _META_ROOTS:
        return _META_ROOTS[key]
    if os.path.isfile(os.path.join(dataset_dir, "meta", "info.json")):
        _META_ROOTS[key] = dataset_dir
        return dataset_dir
    meta_table = os.path.join(dataset_dir, "meta.lance")
    if not _is_table_dir(meta_table):
        raise NotADatasetError(
            f"'{dataset_dir}': 缺 meta/ 目录且没有 meta.lance 镜像可物化 —— "
            "不是完整的 lerobot-lance-convert 产出")
    lance = _lance_mod()
    root = tempfile.mkdtemp(prefix="lance_meta_")
    _MATERIALIZED.add(root)
    t = lance.dataset(meta_table).to_table()
    for path, data in zip(t["path"].to_pylist(), t["data"].to_pylist()):
        # 越界防护(2026-09-21 审查实锤):lstrip("/") 挡不住 "../",镜像行是客户
        # 数据,必须当不可信输入 —— 解析后不在临时根之下的路径响亮拒绝,绝不落盘
        p = os.path.normpath(os.path.join(root, str(path).lstrip("/")))
        if not p.startswith(root + os.sep):
            raise NotADatasetError(
                f"meta.lance 镜像里有越界路径 {str(path)!r}(解析到临时目录之外),"
                "拒绝物化 —— 数据疑似被篡改或转换器有 bug")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    print(f"[curation] lance: meta/ 目录缺失,已从 meta.lance 镜像物化 "
          f"{t.num_rows} 个元数据文件", flush=True)
    _META_ROOTS[key] = root
    return root


def _videos_dir(dataset_dir: str) -> str:
    key = os.path.abspath(dataset_dir)
    if key not in _VIDEO_DIRS:
        d = tempfile.mkdtemp(prefix="lance_videos_")
        _MATERIALIZED.add(d)
        _VIDEO_DIRS[key] = d
    return _VIDEO_DIRS[key]


def cleanup_video_cache(dataset_dir: str | None = None) -> int:
    """删掉本进程为 lance 落盘的临时 mp4 与物化的 meta(理由与 rrd_reader 同款:
    /tmp 是容器可写层,不清会积 GB)。幂等,收尾清理绝不能成为新的失败源。

    ⚠️ 只删 _MATERIALIZED 里登记过的目录(即本模块自己 mkdtemp 的),不做任何
    "看路径像临时目录"的推断 —— 源数据集完全可能就住在 /tmp 下。"""
    import shutil

    keys = ([os.path.abspath(dataset_dir)] if dataset_dir
            else list(set(_VIDEO_DIRS) | set(_META_ROOTS)))
    n = 0
    for k in keys:
        for cand in (_VIDEO_DIRS.pop(k, None), _META_ROOTS.pop(k, None)):
            if cand and cand in _MATERIALIZED:
                shutil.rmtree(cand, ignore_errors=True)
                _MATERIALIZED.discard(cand)
                n += 1
    if dataset_dir is None:
        _META_ROOTS.clear()
    return n


# ---------------------------------------------------------------------------
# 两处字节后端:frames 表按 index 范围查数值;videos 表按 (key,chunk,file) 落 blob
# ---------------------------------------------------------------------------

def _open_frames(dataset_dir: str):
    lance = _lance_mod()
    return lance.dataset(os.path.join(dataset_dir, "frames.lance"))


def _colmap(frames_schema, info: dict) -> dict:
    """源特征名 → frames 表列名。优先转换器写的 source-column-name-map
    (lance 列 → 源特征),缺失按"点转下划线"约定倒推;要的列不在响亮失败。"""
    md = frames_schema.metadata or {}
    raw = md.get(b"source-column-name-map") or md.get("source-column-name-map")
    mapping: dict[str, str] = {}
    if raw:
        try:
            for lance_col, src in json.loads(
                    raw.decode() if isinstance(raw, bytes) else raw).items():
                mapping[str(src)] = str(lance_col)
        except (TypeError, ValueError):
            mapping = {}
    cols = set(frames_schema.names)

    def col_of(feature: str) -> str:
        c = mapping.get(feature) or feature.replace(".", "_")
        if c not in cols:
            raise NotADatasetError(
                f"frames.lance 里找不到特征 {feature!r} 对应的列(试过 {c!r});"
                f"实际的列: {sorted(cols)}")
        return c

    return {"col_of": col_of}


def _episode_frames(frames_ds, col_of, info: dict, ep) -> dict:
    """一条 episode 的数值列(按全局 index 范围 filter 下推,与 v3 的 data parquet
    切片同一语义;转换器保证 frames 按 index 排序)。"""
    lo, hi = int(ep["dataset_from_index"]), int(ep["dataset_to_index"])
    idx_col = col_of("index")
    want = {"action": col_of("action"), "timestamp": col_of("timestamp")}
    if "observation.state" in info["features"]:
        want["state"] = col_of("observation.state")
    if "task_index" in info["features"]:
        want["task_index"] = col_of("task_index")
    t = frames_ds.to_table(columns=sorted({idx_col, *want.values()}),
                           filter=f"{idx_col} >= {lo} AND {idx_col} < {hi}")
    if t.num_rows != int(ep["length"]):
        raise NotADatasetError(
            f"episode {int(ep['episode_index'])}: frames 表切片 {t.num_rows} 行 "
            f"!= meta length {int(ep['length'])}(index {lo}..{hi});三表不一致,"
            "疑似残缺拷贝或转换中断")
    order = np.argsort(np.asarray(t[idx_col].to_pylist()))
    out = {}
    for name, c in want.items():
        vals = t[c].to_pylist()
        out[name] = [vals[i] for i in order]
    return out


_SAFE = re.compile(r"[^0-9A-Za-z_-]")


class _VideoStore:
    """videos.lance 的 blob → 本地 mp4(逐字节原样)。多 episode 共享同一个合并
    mp4 → 按 (video_key, chunk, file) 缓存;行号索引只建一次。"""

    def __init__(self, dataset_dir: str):
        lance = _lance_mod()
        self._ds = lance.dataset(os.path.join(dataset_dir, "videos.lance"))
        t = self._ds.to_table(columns=["video_key", "chunk_index", "file_index"])
        self._row_of = {(str(k), int(c), int(f)): i for i, (k, c, f) in enumerate(
            zip(t["video_key"].to_pylist(), t["chunk_index"].to_pylist(),
                t["file_index"].to_pylist()))}
        self._dir = _videos_dir(dataset_dir)

    def path_of(self, video_key: str, chunk: int, file: int) -> str:
        key = (str(video_key), int(chunk), int(file))
        if key not in self._row_of:
            raise NotADatasetError(
                f"videos.lance 里找不到 (video_key={video_key!r}, chunk={chunk}, "
                f"file={file}) 的行;三表不一致,疑似残缺拷贝")
        out = os.path.join(self._dir,
                           f"{_SAFE.sub('_', str(video_key))}__c{chunk}f{file}.mp4")
        if not os.path.exists(out):        # 幂等:同一 mp4 被多条 episode 引用
            blob = self._ds.take_blobs("video_bytes",
                                       indices=[self._row_of[key]])[0]
            tmp = out + ".part"            # 先写 .part 再 rename:不留半截指针
            with open(tmp, "wb") as f:
                f.write(blob.read())
            os.replace(tmp, out)
        return out


def _video_pointers(store: _VideoStore, info: dict, ep) -> dict:
    """一条 episode 的视频指针(与 lerobot_reader._v3_video_pointers 同构,
    路径从"videos/ 目录的文件"换成"blob 落盘的本地 mp4")。"""
    videos = {}
    for vk in [k for k, v in info["features"].items() if v["dtype"] == "video"]:
        videos[vk] = {
            "path": store.path_of(vk, int(ep[f"videos/{vk}/chunk_index"]),
                                  int(ep[f"videos/{vk}/file_index"])),
            "from_ts": float(ep[f"videos/{vk}/from_timestamp"]),
            "to_ts": float(ep[f"videos/{vk}/to_timestamp"]),
        }
    return videos


# ---------------------------------------------------------------------------
# 行构造 + 公开 API(签名与 lerobot_reader 同构,管线可直接换用)
# ---------------------------------------------------------------------------

def _build_rows(dataset_dir: str, max_episodes: int | None, start_episode: int,
                episode_indices: set[int] | None) -> tuple[list[dict], dict]:
    meta_root = _meta_root(dataset_dir)
    info = _load_info(meta_root)
    if str(info.get("storage_format") or "") != "lance":
        raise NotADatasetError(
            f"'{dataset_dir}': 三表齐但 info.json 没有 storage_format=\"lance\" 标记 "
            f"(实际: {info.get('storage_format')!r})—— 不是 lerobot-lance-convert "
            "≥0.3.0 的产出,疑似旧插件布局(官方已废弃,请用 lerobot-lance-convert 重转)")
    ep_meta = _v3_ep_meta(meta_root, max_episodes, start_episode, episode_indices)
    if not len(ep_meta):
        raise NotADatasetError(
            f"'{dataset_dir}': episodes 元数据里一条 episode 都没有"
            "(空数据集或 --episodes 全部超界)")
    frames_ds = _open_frames(dataset_dir)
    col_of = _colmap(frames_ds.schema, info)["col_of"]
    store = _VideoStore(dataset_dir)
    tasks_map: dict | None = None

    rows: list[dict] = []
    for _, ep in ep_meta.iterrows():
        fr = _episode_frames(frames_ds, col_of, info, ep)
        instruction = _v3_instruction(ep)
        if not instruction and fr.get("task_index"):
            if tasks_map is None:
                tasks_map = _load_tasks_map(meta_root)
            instruction = _v3_instruction(ep, tasks_map, fr["task_index"][0])
        rows.append({
            "episode_id": f"ep{int(ep['episode_index']):06d}",
            "embodiment_id": str(info.get("robot_type") or "unknown"),
            "action_space": "joint",
            "proprio_space": "joint",
            "instruction": instruction,
            "action": np.asarray(fr["action"], dtype=np.float32),
            "proprio_state": (np.asarray(fr["state"], dtype=np.float32)
                              if "state" in fr else None),
            "video": _video_pointers(store, info, ep),
            "timestamps": np.asarray(fr["timestamp"], dtype=np.float64),
            "fps": float(info["fps"]),
        })
    return rows, info


def _record_source_format(rows: list[dict]) -> None:
    for row in rows:
        try:
            extras = json.loads(row.get("semantics_extras") or "{}")
        except (TypeError, ValueError):
            extras = {}
        extras["source_format"] = "lance"
        row["semantics_extras"] = json.dumps(extras, ensure_ascii=False)


def read_lance_rows(
    dataset_dir: str,
    max_episodes: int | None = None,
    embodiment_id: str | None = None,
    validate: bool = True,
    start_episode: int = 0,
    skip_missing: bool = False,
    episode_indices: set[int] | None = None,
) -> list[dict]:
    """lance(三表)数据集 → 每 episode 一个 dict(字段与 read_lerobot_rows 一致)。

    skip_missing: 为签名兼容保留 —— 三表不一致走的是响亮报错,不是静默跳过。
    """
    from .validate import validate_info, validate_rows

    rows, info = _build_rows(dataset_dir, max_episodes, start_episode,
                             episode_indices)
    if validate:
        validate_info(info, dataset_dir)
    sem = resolve_dataset_semantics(
        info, rows, os.path.basename(str(dataset_dir).rstrip("/")), embodiment_id)
    _attach_semantics(rows, sem, embodiment_id)
    _record_source_format(rows)
    if validate:
        validate_rows(rows)
    return rows


def read_lance_meta(
    dataset_dir: str,
    max_episodes: int | None = None,
    embodiment_id: str | None = None,
    skip_missing: bool = False,
    episode_indices: set[int] | None = None,
) -> list[dict]:
    """每 episode 一个轻量 dict(字段与 read_lerobot_meta 一致)。

    视频指针要 blob 落盘才有 → 与 rrd/mcap 同款:没有"纯 meta"的捷径,落盘的 mp4
    进程内复用(幂等)。语义样本取前几条的数值(KB 级 filter 查询)。"""
    from .lerobot_reader import SEMANTICS_VOTE_EPISODES

    rows, info = _build_rows(dataset_dir, max_episodes, 0, episode_indices)
    metas = [{
        "episode_id": r["episode_id"],
        "episode_index": int(r["episode_id"][2:]),
        "embodiment_id": r["embodiment_id"],
        "instruction": r["instruction"],
        "video": r["video"],
        "fps": r["fps"],
        "length": len(r["timestamps"]),
    } for r in rows]
    sample = rows[:SEMANTICS_VOTE_EPISODES]
    sem = resolve_dataset_semantics(
        info, sample, os.path.basename(str(dataset_dir).rstrip("/")), embodiment_id)
    _attach_semantics(metas, sem, embodiment_id)
    _record_source_format(metas)
    return metas


def lance_dataset_info(dataset_dir: str, embodiment_id: str | None = None) -> dict:
    """数据集级元数据 = 真正的 LeRobot info.json(V1 的红利:语义层零合成)。"""
    return _load_info(_meta_root(dataset_dir))


def list_episode_indices(dataset_dir: str) -> list[int]:
    """数据集实有的 episode 编号(只读 episodes 元数据,零 blob —— --episodes 对账用)。"""
    ep_meta = _v3_ep_meta(_meta_root(dataset_dir), None)
    return sorted(int(e) for e in ep_meta["episode_index"])


def persist_videos(rows: list[dict], dest_dir: str) -> int:
    """把行里指向临时缓存的 mp4 拷进交付目录并**就地改写指针**,返回拷贝的文件数。

    对 lance/mcap 通用(mcap 侧 2026-09-21 审查补上)。episodes_parquet 的视频指针指向
    /tmp 的 blob 落盘缓存 —— 收尾 cleanup_video_cache 一跑,交付里的视频路径全部
    失效。原格式交付做出来之前,视频必须随交付持久化。同一合并 mp4 被多行共享 →
    按源路径去重只拷一次;拷贝是顺序整拷(FSX 安全)。指针仍是绝对路径,与
    episodes_parquet 既有约定一致(LeRobot 输入的行指向源数据集也是绝对路径)。"""
    import shutil

    os.makedirs(dest_dir, exist_ok=True)
    moved: dict[str, str] = {}
    for r in rows:
        for v in (r.get("video") or {}).values():
            src = str(v.get("path") or "")
            if src in moved:
                v["path"] = moved[src]
                continue
            if not os.path.isfile(src):
                continue                      # 指针已不在(或本就指向别处):不碰
            dst = os.path.join(dest_dir, os.path.basename(src))
            if not os.path.exists(dst):
                shutil.copyfile(src, dst)
            moved[src] = dst
            v["path"] = dst
    return len(moved)


def read_lance_lazy(dataset_dir: str, max_episodes: int | None = None,
                    embodiment_id: str | None = None, validate: bool = True,
                    skip_missing: bool = True,
                    episode_indices: set[int] | None = None):
    """lance 数据集 → daft DataFrame(与 read_lerobot_lazy 同 schema)。

    急切读后建 DataFrame(与 rrd/mcap 同一取舍):视频必须先从 blob 落盘才能当指针,
    懒不掉;数值经 filter 下推逐 episode 取,内存 = 数值列本身(每条几十 KB)。
    """
    from .lerobot_reader import rows_to_daft

    return rows_to_daft(read_lance_rows(dataset_dir, max_episodes, embodiment_id,
                                        validate, episode_indices=episode_indices))
