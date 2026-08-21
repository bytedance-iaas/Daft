"""LeRobot 数据集 → 统一 DataFrame(文档中的 M1)。

P1 spike1 雏形:LeRobot **v3.0** 最小 reader(2026-07-02 实测 hub 官方数据集已全是 v3)。
v3 布局:多条 episode 合并进同一 data parquet / mp4;边界在 meta/episodes/ parquet 里
(dataset_from_index/to_index = 全局帧范围;video 用 from/to_timestamp 切)。
v2.x(每 episode 一文件,DROID/Bridge 等社区转换)= P2 补的兼容分支。

fan-out:一个数据集目录 → N 行(每 episode 一行);video 只存路径+时间边界(指针,不读字节);
小数据(action/state/timestamps)直接存值。

两条读取通路(2026-07-10 懒扫描落地):
- **急切**:read_lerobot_rows → Python dict 列表(测试/幸存者按需重读/小数据集);
- **懒扫描**:daft_source.LeRobotDataSource → daft 原生 DataSource,引擎执行时按
  task(v3=data 文件,v2=episode 组)流式拉取,内存与数据集大小无关。
两路共用本文件的行构造器(_rows_v3_group/_row_v2/_attach_semantics),逻辑不会漂移。
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from . import dsfs   # 本地路径与 tos:// 同一套读法(2026-08-21 读端会说 tos://)

# 语义多数票采样条数:控制模式是数据集级约定,前 N 条足够;懒/急两路同一常数保证一致
SEMANTICS_VOTE_EPISODES = 100


class NotADatasetError(Exception):
    """输入目录不是有效的 LeRobot 数据集(缺 meta/info.json)。带可操作建议。"""


class OutputExistsError(Exception):
    """`--output` 指到了一份 2026-08-14 之前布局的交付(passed.json 直接在里面)。

    新布局下每次跑批各进一个时间戳子目录、永不覆盖,所以"输出目录已有结果"本身
    不再是错;唯一还要拦的是这种新旧混合(见 pipeline/run.py 里的理由)。
    """


def _verify_layout(dataset_dir: str, version: str) -> None:
    """codebase_version 声明与 meta/ 实际布局对账;不符时指名道姓。

    背景(2026-08-09):doctor 在 so101-v2 上拿 v3 的文件名去找 v2.1 的任务表,
    产出误导性的红色 FAIL。我们按声明分派本不会犯那个错,但**声明本身写错时**,
    旧报错只说"找不到 v3 episodes 元数据"——客户会以为我们读不了他的数据,
    而真相是他的 info.json 标错了。响亮失败还要说清怪谁。
    """
    has_v3 = dsfs.isdir(dsfs.join(dataset_dir, "meta", "episodes"))
    has_v2 = dsfs.exists(dsfs.join(dataset_dir, "meta", "episodes.jsonl"))
    if version.startswith("v3") and not has_v3 and has_v2:
        raise NotADatasetError(
            f"info.json 声明 codebase_version={version},但 meta/ 是 v2.x 布局"
            f"(有 episodes.jsonl、无 episodes/ 目录)。疑似 codebase_version 标错:"
            f"数据大概率是 v2.x,把 info.json 的 codebase_version 改为实际版本即可读取。")
    if version.startswith("v2") and not has_v2 and has_v3:
        raise NotADatasetError(
            f"info.json 声明 codebase_version={version},但 meta/ 是 v3.x 布局"
            f"(有 episodes/ 目录、无 episodes.jsonl)。疑似 codebase_version 标错:"
            f"数据大概率是 v3.x,把 info.json 的 codebase_version 改为实际版本即可读取。")


def _load_info(dataset_dir: str) -> dict:
    if dsfs.is_remote(dataset_dir):
        # 远端:先把数据集根下的对象清单一次列进内存,之后 exists/glob 全是字典查找
        # (v2 社区集逐条 exists 若每次出网就是几十万次调用)。幂等,谁先到谁列。
        dsfs.prefetch(dataset_dir)
    info_path = dsfs.join(dataset_dir, "meta", "info.json")
    if not dsfs.exists(info_path):
        # 友好报错:是不是指到了"装多个数据集的父目录"?列出其中的有效数据集
        subs = []
        try:
            for name in dsfs.listdir(dataset_dir):
                if dsfs.exists(dsfs.join(dataset_dir, name, "meta", "info.json")):
                    subs.append(name)
        except (NotADirectoryError, FileNotFoundError, OSError):
            raise NotADatasetError(f"路径不存在或不是目录: {dataset_dir}")
        if subs:
            hint = "\n".join(f"    --input {dsfs.join(dataset_dir, s)}" for s in subs)
            raise NotADatasetError(
                f"'{dataset_dir}' 不是单个数据集,而是包含 {len(subs)} 个数据集的目录。\n"
                f"请指定其中一个(--input 指向具体数据集):\n{hint}")
        raise NotADatasetError(
            f"'{dataset_dir}' 不是有效的 LeRobot 数据集(缺 meta/info.json)。\n"
            "  应指向单个数据集目录,其结构为: <数据集>/meta/info.json + data/ + videos/")
    return dsfs.read_json(info_path)


_EE_NAMES = {"x", "y", "z", "roll", "pitch", "yaw"}


def _infer_proprio_space(info: dict) -> str:
    """从 observation.state 特征名推断本体状态空间(ee/joint)——
    stuck/saturation 等"指令 vs 实际"对照检查要求两者同空间才可比
    (droid: EE 速度指令 × 关节位置读数,跨空间直比=垃圾结论)。"""
    names = info["features"].get("observation.state", {}).get("names") or []
    if isinstance(names, dict):
        names = next(iter(names.values()), [])
    flat = {str(n).lower() for n in names}
    return "ee" if flat & _EE_NAMES else "joint"


def _infer_action_space(info: dict) -> str:
    """从 action 特征名推断数据的动作空间:'ee'(笛卡尔指令,如 DROID/Bridge)或 'joint'。

    ⚠️ 分派安全规则的依据:EE 数据配关节 profile 时,维度巧合(DROID 7=Franka dof 7)
    会让关节极限静默地卡在笛卡尔坐标上——必须由本推断挡住。
    """
    names = info["features"].get("action", {}).get("names") or []
    if isinstance(names, dict):
        names = next(iter(names.values()), [])
    flat = {str(n).lower() for n in names}
    return "ee" if flat & _EE_NAMES else "joint"


def _load_episodes_meta(dataset_dir: str) -> pd.DataFrame:
    """meta/episodes/chunk-*/file-*.parquet → 每 episode 一行的边界表。"""
    paths = dsfs.glob(dsfs.join(dataset_dir, "meta", "episodes", "chunk-*", "file-*.parquet"))
    if not paths:
        raise FileNotFoundError(f"找不到 v3 episodes 元数据: {dataset_dir}/meta/episodes/")
    return pd.concat([dsfs.read_parquet(p) for p in paths], ignore_index=True)


# ---------------------------------------------------------------------------
# v2 行构造(懒/急共用)
# ---------------------------------------------------------------------------

def _v2_episode_paths(dataset_dir: str, info: dict, ep: dict) -> tuple[str, dict]:
    """一条 v2 episode → (data parquet 路径, video 指针 dict)。纯路径推导,不碰磁盘。"""
    idx = int(ep["episode_index"])
    chunk = idx // int(info["chunks_size"])
    data_path = dsfs.join(
        dataset_dir, info["data_path"].format(episode_chunk=chunk, episode_index=idx))
    fps = float(info["fps"])
    length_s = int(ep["length"]) / fps
    video_keys = [k for k, v in info["features"].items() if v["dtype"] == "video"]
    videos = {
        vk: {
            "path": dsfs.join(dataset_dir, info["video_path"].format(
                episode_chunk=chunk, video_key=vk, episode_index=idx)),
            "from_ts": 0.0,          # v2 独立 mp4:边界即整个文件
            "to_ts": length_s,
        }
        for vk in video_keys
    }
    return data_path, videos


def _v2_missing(data_path: str, videos: dict) -> bool:
    return (not dsfs.exists(data_path)
            or any(not dsfs.exists(v["path"]) for v in videos.values()))


def _row_v2(dataset_dir: str, info: dict, ep: dict) -> dict:
    """一条 v2 episode → 统一行(读它的 data parquet)。缺文件由调用方先行判断。"""
    idx = int(ep["episode_index"])
    data_path, videos = _v2_episode_paths(dataset_dir, info, ep)
    state_key = "observation.state" if "observation.state" in info["features"] else None
    df = dsfs.read_parquet(data_path)
    if len(df) != int(ep["length"]):
        raise ValueError(
            f"episode {idx}: parquet 帧数 {len(df)} != episodes.jsonl length {ep['length']}")
    tasks = ep.get("tasks") or []
    return {
        "episode_id": f"ep{idx:06d}",
        "embodiment_id": str(info.get("robot_type") or "unknown"),
        "action_space": _infer_action_space(info),
        "proprio_space": _infer_proprio_space(info),
        "instruction": str(tasks[0]) if tasks else "",
        "action": np.stack(df["action"].to_numpy()).astype(np.float32),
        "proprio_state": (
            np.stack(df[state_key].to_numpy()).astype(np.float32) if state_key else None),
        "video": videos,
        "timestamps": df["timestamp"].to_numpy().astype(np.float64),
        "fps": float(info["fps"]),
    }


_JSONL_CACHE: dict = {}


def _v2_episode_list(dataset_dir: str, max_episodes: int | None = None,
                     start_episode: int = 0,
                     episode_indices: set[int] | None = None) -> list[dict]:
    """meta/episodes.jsonl → episode 元数据 dict 列表(轻量,不读数值)。

    解析结果按 (路径, mtime) 缓存:droid 清单 9.2 万行,去重阶段分批重读幸存者
    时曾每批重复解析(50 批×2秒,2026-07-15 实测浪费)。"""
    jp = dsfs.join(dataset_dir, "meta", "episodes.jsonl")
    key = (jp, dsfs.mtime_key(jp))
    if key not in _JSONL_CACHE:
        _JSONL_CACHE.clear()                       # 只留最近一个数据集,防内存积累
        with dsfs.open_text(jp) as f:
            _JSONL_CACHE[key] = [json.loads(line) for line in f if line.strip()]
    eps = list(_JSONL_CACHE[key])
    if episode_indices is not None:
        eps = [e for e in eps if int(e["episode_index"]) in episode_indices]
    eps = eps[start_episode:]
    if max_episodes is not None:
        eps = eps[:max_episodes]
    return eps


def _report_skipped(skipped: list[int]) -> None:
    if skipped:
        import sys
        print(f"[curation] ⚠️ 跳过 {len(skipped)} 条缺失文件的 episode(前几个: "
              f"{skipped[:8]}{'…' if len(skipped) > 8 else ''})", file=sys.stderr)


def _episode_rows_v2(dataset_dir: str, info: dict, max_episodes: int | None = None,
                     start_episode: int = 0, skip_missing: bool = False,
                     episode_indices: set[int] | None = None) -> list[dict]:
    """v2.x:每 episode 独立 parquet/mp4,边界天然;meta/episodes.jsonl 一行一条。"""
    eps = _v2_episode_list(dataset_dir, max_episodes, start_episode, episode_indices)
    rows: list[dict] = []
    skipped: list[int] = []
    for ep in eps:
        data_path, videos = _v2_episode_paths(dataset_dir, info, ep)
        if skip_missing and _v2_missing(data_path, videos):
            skipped.append(int(ep["episode_index"]))   # 缺口常态:跳过,末尾如实汇报
            continue
        rows.append(_row_v2(dataset_dir, info, ep))
    _report_skipped(skipped)
    return rows


# ---------------------------------------------------------------------------
# v3 行构造(懒/急共用)
# ---------------------------------------------------------------------------

def _v3_ep_meta(dataset_dir: str, max_episodes: int | None = None,
                start_episode: int = 0,
                episode_indices: set[int] | None = None) -> pd.DataFrame:
    ep_meta = _load_episodes_meta(dataset_dir)
    if episode_indices is not None:
        ep_meta = ep_meta[ep_meta["episode_index"].astype(int).isin(episode_indices)]
    ep_meta = ep_meta.iloc[start_episode:]
    if max_episodes is not None:
        ep_meta = ep_meta.iloc[:max_episodes]
    return ep_meta


def _v3_video_pointers(dataset_dir: str, info: dict, ep) -> dict:
    videos = {}
    for vk in [k for k, v in info["features"].items() if v["dtype"] == "video"]:
        videos[vk] = {
            "path": dsfs.join(dataset_dir, info["video_path"].format(
                video_key=vk,
                chunk_index=int(ep[f"videos/{vk}/chunk_index"]),
                file_index=int(ep[f"videos/{vk}/file_index"]),
            )),
            "from_ts": float(ep[f"videos/{vk}/from_timestamp"]),
            "to_ts": float(ep[f"videos/{vk}/to_timestamp"]),
        }
    return videos


def _rows_v3_group(dataset_dir: str, info: dict, grp: pd.DataFrame) -> list[dict]:
    """同一 (chunk,file) 的一组 episode → 行列表(读一个 data parquet)。"""
    chunk = int(grp["data/chunk_index"].iloc[0])
    file = int(grp["data/file_index"].iloc[0])
    data_path = dsfs.join(
        dataset_dir, info["data_path"].format(chunk_index=chunk, file_index=file))
    df = dsfs.read_parquet(data_path)
    state_key = "observation.state" if "observation.state" in info["features"] else None

    rows: list[dict] = []
    for _, ep in grp.iterrows():
        lo, hi = int(ep["dataset_from_index"]), int(ep["dataset_to_index"])
        frames = df.iloc[lo - int(df["index"].iloc[0]): hi - int(df["index"].iloc[0])]
        assert len(frames) == int(ep["length"]), (
            f"episode {ep['episode_index']} 切片长度 {len(frames)} != meta length {ep['length']}")

        tasks = ep.get("tasks")
        instruction = str(tasks[0]) if tasks is not None and len(tasks) else ""

        rows.append({
            "episode_id": f"ep{int(ep['episode_index']):06d}",
            "embodiment_id": str(info.get("robot_type") or "unknown"),
            "action_space": _infer_action_space(info),
            "proprio_space": _infer_proprio_space(info),
            "instruction": instruction,
            "action": np.stack(frames["action"].to_numpy()).astype(np.float32),
            "proprio_state": (
                np.stack(frames[state_key].to_numpy()).astype(np.float32)
                if state_key else None),
            "video": _v3_video_pointers(dataset_dir, info, ep),
            "timestamps": frames["timestamp"].to_numpy().astype(np.float64),
            "fps": float(info["fps"]),
        })
    return rows


def _v3_file_groups(ep_meta: pd.DataFrame):
    """按 (chunk,file) 分组(与急切路同序:groupby 键升序)→ [(键, 组df), ...]。"""
    return list(ep_meta.groupby(["data/chunk_index", "data/file_index"]))


def _episode_rows_v3(dataset_dir: str, info: dict, max_episodes: int | None = None,
                     start_episode: int = 0,
                     episode_indices: set[int] | None = None) -> list[dict]:
    """v3:按 meta/episodes 的边界把合并的 data parquet 切回逐 episode。"""
    ep_meta = _v3_ep_meta(dataset_dir, max_episodes, start_episode, episode_indices)
    rows: list[dict] = []
    # 同一 (chunk,file) 的 episode 共享一个 data parquet:按文件分组读,免重复 IO
    for _, grp in _v3_file_groups(ep_meta):
        rows.extend(_rows_v3_group(dataset_dir, info, grp))
    return rows


# ---------------------------------------------------------------------------
# 语义解析(懒/急共用)
# ---------------------------------------------------------------------------

def resolve_dataset_semantics(info: dict, sample_rows: list[dict]):
    """数据集语义:profile 命中→权威声明;否则数值指纹推断+采样多数票。

    sample_rows: 用于指纹/多数票的样本行(前 SEMANTICS_VOTE_EPISODES 条足够——
    控制模式是数据集级约定;懒/急两路用同一采样规则保证结果一致)。
    """
    from .dataset_semantics import infer_control_mode_majority, resolve_semantics
    sem = resolve_semantics(info, sample_rows[0]["action"] if sample_rows else None)
    if sem.source == "inferred":
        sem.control_mode = infer_control_mode_majority(
            sample_rows[:SEMANTICS_VOTE_EPISODES])
    return sem


def _attach_semantics(rows: list[dict], sem, embodiment_id: str | None = None) -> None:
    for r in rows:
        if embodiment_id is not None:
            r["embodiment_id"] = embodiment_id
        r["control_mode"] = sem.control_mode
        r["action_space"] = sem.action_space
        r["proprio_space"] = sem.proprio_space
        r["gripper_dims"] = sem.gripper_dims
        r["angle_dims"] = sem.angle_dims
        r["euler_triplet"] = sem.euler_triplet
        r["stuck_strategy"] = sem.stuck_strategy
        r["unit"] = sem.unit
        r["semantics_source"] = sem.source
        r["semantics_extras"] = json.dumps(sem.extras or {}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 急切读取(公开 API)
# ---------------------------------------------------------------------------

def read_lerobot_rows(
    dataset_dir: str,
    max_episodes: int | None = None,
    embodiment_id: str | None = None,
    validate: bool = True,
    start_episode: int = 0,
    skip_missing: bool = False,
    episode_indices: set[int] | None = None,
) -> list[dict]:
    """纯 Python 读取(不 import daft,便于测试/复用);返回每 episode 一个 dict。

    embodiment_id: 人工覆盖(数据集 robot_type 缺失/为 unknown 时接入方指定,DESIGN.md M2);
    validate: 结构校验(必需字段/维度/时间戳),不合格拒收(validate.py);
    episode_indices: 只读这些 episode_index(幸存者按需重读,懒扫描管线的第二遍用)。
    """
    from .validate import validate_info, validate_rows

    info = _load_info(dataset_dir)
    if validate:
        validate_info(info, dataset_dir)
    version = info["codebase_version"]
    _verify_layout(dataset_dir, version)
    if version.startswith("v3"):
        rows = _episode_rows_v3(dataset_dir, info, max_episodes, start_episode,
                                episode_indices)
    elif version.startswith("v2"):
        rows = _episode_rows_v2(dataset_dir, info, max_episodes, start_episode,
                                skip_missing, episode_indices)
    else:
        raise ValueError(f"未知 LeRobot 版本: {version}")

    # 数据集语义:profile 命中→权威声明;否则数值指纹推断(见 dataset_semantics)。
    # control_mode 是数据集约定(非单条属性),profile 未命中时用采样多数票兜底。
    sem = resolve_dataset_semantics(info, rows)
    _attach_semantics(rows, sem, embodiment_id)
    if validate:
        validate_rows(rows)
    return rows


# ---------------------------------------------------------------------------
# 轻量元数据(懒扫描管线用:caption 兜底/报告上下文,不读任何数值 parquet)
# ---------------------------------------------------------------------------

def read_lerobot_meta(
    dataset_dir: str,
    max_episodes: int | None = None,
    embodiment_id: str | None = None,
    skip_missing: bool = False,
    episode_indices: set[int] | None = None,
) -> list[dict]:
    """每 episode 一个轻量 dict:episode_id/instruction/video指针/fps/length/语义。

    只读 meta 文件(KB 级),不碰 data parquet → 万条数据集秒级返回。
    与懒扫描 df 的行集合一致(同样的 max_episodes/skip_missing 语义)。
    """
    info = _load_info(dataset_dir)
    version = info["codebase_version"]
    _verify_layout(dataset_dir, version)
    metas: list[dict] = []
    skipped: list[int] = []
    if version.startswith("v3"):
        ep_meta = _v3_ep_meta(dataset_dir, max_episodes, episode_indices=episode_indices)
        for _, ep in ep_meta.iterrows():
            tasks = ep.get("tasks")
            metas.append({
                "episode_id": f"ep{int(ep['episode_index']):06d}",
                "episode_index": int(ep["episode_index"]),
                "embodiment_id": str(info.get("robot_type") or "unknown"),
                "instruction": str(tasks[0]) if tasks is not None and len(tasks) else "",
                "video": _v3_video_pointers(dataset_dir, info, ep),
                "fps": float(info["fps"]),
                "length": int(ep["length"]),
            })
    elif version.startswith("v2"):
        for ep in _v2_episode_list(dataset_dir, max_episodes, episode_indices=episode_indices):
            data_path, videos = _v2_episode_paths(dataset_dir, info, ep)
            if skip_missing and _v2_missing(data_path, videos):
                skipped.append(int(ep["episode_index"]))
                continue
            tasks = ep.get("tasks") or []
            metas.append({
                "episode_id": f"ep{int(ep['episode_index']):06d}",
                "episode_index": int(ep["episode_index"]),
                "embodiment_id": str(info.get("robot_type") or "unknown"),
                "instruction": str(tasks[0]) if tasks else "",
                "video": videos,
                "fps": float(info["fps"]),
                "length": int(ep["length"]),
            })
        _report_skipped(skipped)
    else:
        raise ValueError(f"未知 LeRobot 版本: {version}")

    # 语义解析:profile 命中零数据读;inferred 才读采样 episode 的 action
    sample: list[dict] = []
    from .dataset_semantics import resolve_semantics
    if resolve_semantics(info, None).source != "profile":
        n_sample = (min(SEMANTICS_VOTE_EPISODES, max_episodes)
                    if max_episodes else SEMANTICS_VOTE_EPISODES)
        sample = read_lerobot_rows(dataset_dir, max_episodes=n_sample,
                                   validate=False, skip_missing=skip_missing)
    sem = resolve_dataset_semantics(info, sample)
    _attach_semantics(metas, sem, embodiment_id)
    return metas


def _infer_control_mode(action) -> str:
    """增量/绝对指纹:绝对目标值量级远大于帧间变化;增量与帧间变化同量级。"""
    import numpy as np

    a = np.asarray(action, dtype=np.float64)
    if a.ndim != 2 or a.shape[0] < 3:
        return "unknown"
    scale = np.abs(a).mean()
    step = np.abs(np.diff(a, axis=0)).mean()
    if scale < 1e-9:
        return "unknown"
    return "absolute" if scale > 10.0 * (step + 1e-9) else "delta"


def rows_to_daft(rows: list[dict]):
    """episode 行列表 → daft DataFrame(测试注入病灶后重建 DataFrame 也走这里)。"""
    import daft
    from daft import DataType, col

    # tuple 型语义字段(gripper_dims/angle_dims)不进 daft(会破坏 schema);
    # 它们是数据集级常量,由 registry/dispatch 另行提供。标量语义(control_mode/
    # stuck_strategy/action_space...)保留为列供 UDF 使用。
    _skip = {"gripper_dims", "angle_dims"}
    df = daft.from_pydict({k: [r[k] for r in rows] for k in rows[0] if k not in _skip})
    df = df.with_column("action", col("action").cast(DataType.tensor(DataType.float32())))
    if rows[0]["proprio_state"] is not None:
        df = df.with_column(
            "proprio_state", col("proprio_state").cast(DataType.tensor(DataType.float32())))
    if rows[0].get("timestamps") is not None:
        df = df.with_column(
            "timestamps", col("timestamps").cast(DataType.tensor(DataType.float64())))
    return df


def read_lerobot(
    dataset_dir: str,
    max_episodes: int | None = None,
    embodiment_id: str | None = None,
    validate: bool = True,
):
    """LeRobot 数据集目录 → daft DataFrame(每 episode 一行,action=变长 tensor)。"""
    return rows_to_daft(read_lerobot_rows(dataset_dir, max_episodes, embodiment_id, validate))


def iter_lerobot_batches(dataset_dir: str, batch_size: int = 500,
                         max_episodes: int | None = None,
                         embodiment_id: str | None = None,
                         skip_missing: bool = False):
    """分块流式读取(P5.2):每次只装载 batch_size 条进内存 → 内存与数据集大小无关。

    编排级流式;daft 原生懒扫描见 daft_source.LeRobotDataSource(2026-07-10 落地)。
    """
    start = 0
    while True:
        if max_episodes is not None:
            n = min(batch_size, max_episodes - start)
            if n <= 0:
                return
        else:
            n = batch_size
        rows = read_lerobot_rows(dataset_dir, max_episodes=n,
                                 embodiment_id=embodiment_id, start_episode=start,
                                 skip_missing=skip_missing)
        if not rows:
            return
        yield start, rows
        if len(rows) < n:
            return
        start += len(rows)
