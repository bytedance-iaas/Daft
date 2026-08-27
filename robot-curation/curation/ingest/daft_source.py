"""LeRobot 数据集的 daft 原生懒扫描(M1 懒扫描,2026-07-10)。

与急切路(read_lerobot_rows → rows_to_daft)的区别:
- 构造 DataFrame 时**零数据读取**(只读 meta 文件,KB 级);
- 数值 parquet 由 daft 引擎在执行时按 task 流式拉取:v3 一个 data 文件一个 task
  (多条 episode 共享文件,读一次切多条),v2 每 TASK_EPISODES 条一个 task;
- 内存 = 在飞的几个 task,与数据集大小无关(直到下游显式 collect);
- NativeRunner 串行拉 task,RayRunner(P6)下同一套代码天然并行。

行构造逻辑全部复用 lerobot_reader 的共享 helper(_rows_v3_group/_row_v2/
_attach_semantics)——懒/急两路读同一段代码,质检结果不会漂移(金对齐测试锚定)。

⚠️ limit 下推:引擎会把 df.limit(n) 经 pushdowns.limit 传进来,但**不会**替我们
截断 task(spike 实测 50 个 task 全跑)→ get_tasks 里自己按行数配额停发。
"""
from __future__ import annotations

import os
from typing import AsyncIterator

from daft.io.source import DataSource, DataSourceTask

from .lerobot_reader import (
    SEMANTICS_VOTE_EPISODES,
    _attach_semantics,
    _load_info,
    _report_skipped,
    _row_v2,
    _rows_v3_group,
    _v2_episode_list,
    _v2_episode_paths,
    _v2_missing,
    _v3_ep_meta,
    _v3_file_groups,
    resolve_dataset_semantics,
)

TASK_EPISODES_V2 = 64          # v2 每 task 的 episode 数(每条独立文件,组一批摊薄开销)

# rows_to_daft 同款排除:tuple 语义字段不进 schema(数据集级常量,registry 另行提供)
_SKIP_FIELDS = {"gripper_dims", "angle_dims"}


def _rows_to_recordbatch(rows: list[dict], validate: bool):
    """行列表 → RecordBatch(列序固定;变长 numpy 数组原生推断为 Tensor,spike 已验)。"""
    from daft.recordbatch import RecordBatch

    if validate:
        from .validate import validate_rows
        validate_rows(rows)
    keys = [k for k in rows[0] if k not in _SKIP_FIELDS]
    return RecordBatch.from_pydict({k: [r[k] for r in rows] for k in keys})


class LeRobotDataSource(DataSource):
    """LeRobot 数据集目录 → daft DataSource(懒扫描;每 episode 一行)。

    用法:df = LeRobotDataSource(dataset_dir).read()
    """

    def __init__(self, dataset_dir: str, max_episodes: int | None = None,
                 embodiment_id: str | None = None, validate: bool = True,
                 skip_missing: bool = True,
                 episode_indices: set[int] | None = None):
        from .validate import validate_info

        self._dir = dataset_dir
        self._max = max_episodes
        self._eps = episode_indices          # 只跑指定 episode(调试/复现单条问题)
        self._embodiment = embodiment_id
        self._validate = validate
        self._skip_missing = skip_missing
        self._info = _load_info(dataset_dir)
        if validate:
            validate_info(self._info, dataset_dir)
        v = self._info["codebase_version"]
        if not (v.startswith("v3") or v.startswith("v2")):
            raise ValueError(f"未知 LeRobot 版本: {v}")
        self._v3 = v.startswith("v3")
        self._sem = self._resolve_semantics()
        self._schema = self._build_schema()

    # ---- 语义:profile 命中零数据读;inferred 采样(与急切路同一规则) ----
    def _resolve_semantics(self):
        from .dataset_semantics import resolve_semantics
        _name = os.path.basename(str(self._dir).rstrip("/"))
        if resolve_semantics(self._info, None, _name).source == "profile":
            return resolve_dataset_semantics(self._info, [], _name)
        from .lerobot_reader import read_lerobot_rows
        n = (min(SEMANTICS_VOTE_EPISODES, self._max) if self._max
             else SEMANTICS_VOTE_EPISODES)
        sample = read_lerobot_rows(self._dir, max_episodes=n, validate=False,
                                   skip_missing=self._skip_missing)
        return resolve_dataset_semantics(self._info, sample, _name)

    # ---- schema:显式构造(与 rows_to_daft 产物一致,金对齐测试锚定) ----
    def _build_schema(self):
        from daft import DataType
        from daft.schema import Schema

        video_keys = [k for k, v in self._info["features"].items()
                      if v["dtype"] == "video"]
        cam_struct = DataType.struct({"path": DataType.string(),
                                      "from_ts": DataType.float64(),
                                      "to_ts": DataType.float64()})
        has_state = "observation.state" in self._info["features"]
        return Schema.from_pydict({
            "episode_id": DataType.string(),
            "embodiment_id": DataType.string(),
            "action_space": DataType.string(),
            "proprio_space": DataType.string(),
            "instruction": DataType.string(),
            "action": DataType.tensor(DataType.float32()),
            "proprio_state": (DataType.tensor(DataType.float32())
                              if has_state else DataType.null()),
            "video": DataType.struct({vk: cam_struct for vk in video_keys}),
            "timestamps": DataType.tensor(DataType.float64()),
            "fps": DataType.float64(),
            "control_mode": DataType.string(),
            "euler_triplet": DataType.bool(),
            "stuck_strategy": DataType.string(),
            "unit": DataType.string(),
            "semantics_source": DataType.string(),
            "semantics_extras": DataType.string(),
        })

    # ---- DataSource 协议 ----
    @property
    def name(self) -> str:
        return f"LeRobotDataSource({os.path.basename(self._dir.rstrip('/'))})"

    @property
    def schema(self):
        return self._schema

    async def get_tasks(self, pushdowns) -> AsyncIterator["_LeRobotScanTask"]:
        # 行数配额 = min(max_episodes, 引擎下推的 limit)——引擎不替我们截断 task
        quota = self._max
        if getattr(pushdowns, "limit", None) is not None:
            quota = min(quota, pushdowns.limit) if quota is not None else pushdowns.limit

        emitted = 0
        if self._v3:
            ep_meta = _v3_ep_meta(self._dir, self._max, episode_indices=self._eps)
            for _, grp in _v3_file_groups(ep_meta):
                if quota is not None and emitted >= quota:
                    return
                yield _LeRobotScanTask(self, ("v3", grp))
                emitted += len(grp)
        else:
            eps = _v2_episode_list(self._dir, self._max, episode_indices=self._eps)
            # 缺失过滤前置到规划期(2026-07-15):部分下载的数据集(bridge 下载 1k/
            # 清单 53k)此前会给缺失文件也发任务——引擎白执行上千个空任务、每个刷一行
            # "跳过 64 条"(用户实测刷屏 1280 行)。规划期一次过滤+汇报总账;任务只发
            # 给存在的文件;任务内的逐批警告只剩"规划后才消失的文件"(罕见,值得喊)。
            if self._skip_missing:
                present = []
                n_missing = 0
                for ep in eps:
                    dp, vids = _v2_episode_paths(self._dir, self._info, ep)
                    if _v2_missing(dp, vids):
                        n_missing += 1
                    else:
                        present.append(ep)
                if n_missing:
                    import sys
                    print(f"[curation] 数据集清单 {len(eps)} 条,本地存在 "
                          f"{len(present)} 条,跳过 {n_missing} 条缺失/未下载"
                          "(规划期一次性汇报)", file=sys.stderr)
                eps = present
            for i in range(0, len(eps), TASK_EPISODES_V2):
                if quota is not None and emitted >= quota:
                    return
                batch = eps[i:i + TASK_EPISODES_V2]
                yield _LeRobotScanTask(self, ("v2", batch))
                emitted += len(batch)

    # ---- task 的行构造(共享 helper;task 只带轻量元数据,读数据在执行期) ----
    def _build_rows(self, payload) -> list[dict]:
        kind, data = payload
        if kind == "v3":
            rows = _rows_v3_group(self._dir, self._info, data)
        else:
            rows, skipped = [], []
            for ep in data:
                data_path, videos = _v2_episode_paths(self._dir, self._info, ep)
                if self._skip_missing and _v2_missing(data_path, videos):
                    skipped.append(int(ep["episode_index"]))
                    continue
                rows.append(_row_v2(self._dir, self._info, ep))
            _report_skipped(skipped)
        _attach_semantics(rows, self._sem, self._embodiment)
        return rows


class _LeRobotScanTask(DataSourceTask):
    """一个扫描单元:v3=一个 data 文件的 episode 组,v2=一批 episode。"""

    def __init__(self, source: LeRobotDataSource, payload):
        self._source = source
        self._payload = payload

    @property
    def schema(self):
        return self._source.schema

    async def read(self):
        rows = self._source._build_rows(self._payload)
        if rows:
            yield _rows_to_recordbatch(rows, self._source._validate)


def read_lerobot_lazy(dataset_dir: str, max_episodes: int | None = None,
                      embodiment_id: str | None = None, validate: bool = True,
                      skip_missing: bool = True,
                      episode_indices: set[int] | None = None):
    """LeRobot 数据集 → 懒扫描 daft DataFrame(与 read_lerobot 同 schema)。

    episode_indices:只跑指定 episode(CLI --episodes;调试/复现单条时免跑前 N 条)。
    """
    return LeRobotDataSource(dataset_dir, max_episodes, embodiment_id,
                             validate, skip_missing, episode_indices).read()
