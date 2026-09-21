"""LeRobot 导出(交付层的 schema 映射)。幸存 episode 重组为**合法 LeRobot 数据集**,
验收=官方 lerobot loader 无警告加载 + 自家 reader 读得回(关键回环)。

源是 v2 还是 v3,导出代价差一个数量级,因此两个导出器分开写:

**v3**(多条 episode 合并进同一 parquet/mp4)= `export_lerobot_v3`:
- data:从源 data parquet 按边界切行拼接(所有列天然齐全),只改写编号列
  (episode_index 重排/index 全局重排/task_index 按新任务表);
- meta/episodes:切源 episodes 元数据行(stats/* 列免费继承),覆写边界/编号列;
- videos:各相机把幸存 episode 的时间窗重编码拼接成新合并 mp4(av,libx264),记录新边界;
- info.json 改计数;stats.json 原样拷贝(全局近似统计,加载够用)。

**v2**(每条 episode 独立 parquet + 独立 mp4,如 DROID/Bridge)= `export_lerobot_v2`:
文件边界天然就是 episode 边界 → **选中文件拷过去 + 重编号 + 重建 meta 即可,零视频重编码**。
mp4 直接 `shutil.copyfile`(不解码不转码,几百 GB 也只是顺序拷贝);parquet 逐个读入只改
编号列。同样一批幸存者,v2 导出比 v3 便宜一个数量级——这不是偷懒,是源布局本来就允许。
"""
from __future__ import annotations

import json
import os
import shutil

import numpy as np
import pandas as pd

from . import publish
from .safe_write import write_json, write_text


def _reencode_concat(src_windows: list[dict], out_path: str, fps: float) -> list[tuple]:
    """把多段 [from_ts,to_ts) 窗口按序重编码进一个 mp4;返回每段新 (from_ts, to_ts)。

    ⚠️ 必须**先写本地临时文件再整文件拷贝**:PyAV 复用编码器时要 seek 回文件头
    改写 moov,而 TOS 的 FSX 挂载拒绝随机写 → EINVAL(2026-08-07 so101 v3 导出
    实锤;与 evidence._encode_mp4、pyarrow 直写 parquet 同一族)。交付目录只见顺序写。
    """
    import shutil
    import tempfile

    import av

    from ..adapters.decode import decode_window

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as _tf:
        tmp_path = _tf.name
    bounds = []
    n_written = 0
    with av.open(tmp_path, "w") as out:
        stream = None
        for _wi, w in enumerate(src_windows):
            if _wi % 10 == 0 or _wi == len(src_windows) - 1:
                print(f"[curation]   视频切割 {os.path.basename(out_path)}: "
                      f"{_wi + 1}/{len(src_windows)} 段", flush=True)
            frames, _ = decode_window(w["path"], w["from_ts"], w["to_ts"])
            start = n_written / fps
            for img in frames:
                if stream is None:
                    h, wd = img.shape[:2]
                    stream = out.add_stream("libx264", rate=round(fps))
                    stream.width, stream.height = wd, h
                    stream.pix_fmt = "yuv420p"
                frame = av.VideoFrame.from_ndarray(img, format="rgb24")
                for pkt in stream.encode(frame):
                    out.mux(pkt)
                n_written += 1
            bounds.append((start, n_written / fps))
        if stream is not None:
            for pkt in stream.encode():
                out.mux(pkt)
    try:
        shutil.copyfile(tmp_path, out_path)     # 顺序写落交付,FSX 安全
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return bounds


def _size_mb(info: dict, key: str, default: float, override=None) -> float:
    """分文件阈值(MB):显式覆盖 > 源 info.json 的 *_files_size_in_mb > 缺省。"""
    for v in (override, info.get(key)):
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return default


class _RollingVideoWriter:
    """一路相机的"滚动"输出:轨迹按序编码进当前 mp4,文件到阈值就在**轨迹边界**封口,
    封口的文件交给 publish(边出边传),再开下一个 file-NNN(2026-08-21 方案 1)。

    从前是整个数据集一路相机一个大 mp4,编到最后一条才能封口 —— 封口前既传不了也删不了,
    直连交付的体积就被 pod 本地盘卡死。滚动写之后 pod 上同时只有一个没封口的文件。
    本地临时文件先写再顺序拷到目标(PyAV 改写 moov 要随机写,FSX 挂载不接受)。
    """

    def __init__(self, out_dir: str, video_path_tpl: str, video_key: str, fps: float,
                 max_bytes: float, chunks_size: int):
        self.out_dir, self.tpl, self.vk, self.fps = out_dir, video_path_tpl, video_key, fps
        self.max_bytes, self.chunks_size = max_bytes, max(1, int(chunks_size))
        self.n_file = 0
        self.files: list[str] = []
        self._tmp = None
        self._out = None
        self._stream = None
        self._n_written = 0
        self._bytes = 0

    def _open(self) -> None:
        import tempfile

        import av
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            self._tmp = tf.name
        self._out = av.open(self._tmp, "w")
        self._stream, self._n_written, self._bytes = None, 0, 0

    def _seal(self) -> None:
        """封口:刷编码器、关文件、拷到 chunk/file 位置、交给发布器。"""
        from . import publish
        if self._stream is not None:
            for pkt in self._stream.encode():
                self._out.mux(pkt)
        self._out.close()
        chunk, fidx = divmod(self.n_file, self.chunks_size)
        dst = os.path.join(self.out_dir, self.tpl.format(
            video_key=self.vk, chunk_index=chunk, file_index=fidx))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copyfile(self._tmp, dst)       # 顺序写落交付,FSX 安全
        finally:
            try:
                os.unlink(self._tmp)
            except OSError:
                pass
        self.files.append(dst)
        self.n_file += 1
        self._out = self._stream = self._tmp = None
        publish.file_done(dst)

    def add(self, frames) -> tuple[int, int, float, float]:
        """一条轨迹的帧 → 写进当前文件;返回 (chunk_index, file_index, from_ts, to_ts),
        时间戳相对**所在文件**(LeRobot v3 语义)。"""
        import av
        if self._out is None:
            self._open()
        chunk, fidx = divmod(self.n_file, self.chunks_size)
        start = self._n_written / self.fps
        for img in frames:
            if self._stream is None:
                h, wd = img.shape[:2]
                self._stream = self._out.add_stream("libx264", rate=round(self.fps))
                self._stream.width, self._stream.height = wd, h
                self._stream.pix_fmt = "yuv420p"
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            for pkt in self._stream.encode(frame):
                # 文件大小有 I/O 缓冲看不准,按吐出的 packet 字节累计。x264 有约 40 帧前瞻,
                # 计数比真实落后不到一秒的视频量,对几百 MB 的阈值可以忽略
                self._bytes += int(pkt.size or 0)
                self._out.mux(pkt)
            self._n_written += 1
        end = self._n_written / self.fps
        if self._bytes >= self.max_bytes:
            self._seal()                      # 只在轨迹边界封口:一条轨迹不跨文件
        return chunk, fidx, start, end

    def close(self) -> None:
        if self._out is not None:
            self._seal()


class _RollingParquetWriter:
    """data 文件的"滚动"输出:逐轨迹追加行组,文件到阈值就在轨迹边界封口、交给发布器。
    用 pyarrow ParquetWriter 逐行组写,内存里只有当前这条轨迹,不攒整个文件。"""

    def __init__(self, out_dir: str, data_path_tpl: str, max_bytes: float, chunks_size: int):
        self.out_dir, self.tpl = out_dir, data_path_tpl
        self.max_bytes, self.chunks_size = max_bytes, max(1, int(chunks_size))
        self.n_file = 0
        self.files: list[str] = []
        self._tmp = None
        self._writer = None
        self._schema = None

    def add(self, df) -> tuple[int, int]:
        import tempfile

        import pyarrow as pa
        import pyarrow.parquet as pq
        table = pa.Table.from_pandas(df, preserve_index=False)
        if self._writer is None:
            with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tf:
                self._tmp = tf.name
            self._schema = table.schema
            self._writer = pq.ParquetWriter(self._tmp, self._schema)
        elif not table.schema.equals(self._schema):
            table = table.cast(self._schema)      # 同源数据集列一致,只是类型推断偶有出入
        chunk, fidx = divmod(self.n_file, self.chunks_size)
        self._writer.write_table(table)
        try:
            if os.path.getsize(self._tmp) >= self.max_bytes:
                self._seal()
        except OSError:
            pass
        return chunk, fidx

    def _seal(self) -> None:
        from . import publish
        self._writer.close()
        chunk, fidx = divmod(self.n_file, self.chunks_size)
        dst = os.path.join(self.out_dir, self.tpl.format(chunk_index=chunk, file_index=fidx))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copyfile(self._tmp, dst)
        finally:
            try:
                os.unlink(self._tmp)
            except OSError:
                pass
        self.files.append(dst)
        self.n_file += 1
        self._writer = self._tmp = self._schema = None
        publish.file_done(dst)

    def close(self) -> None:
        if self._writer is not None:
            self._seal()


def _refuse_nonempty(out_dir: str) -> None:
    """输出目录非空一律拒绝:parquet/mp4 写入是"补文件"语义,和上次的残留混在一起
    会产出一个看着完整、实际编号错位的假数据集——宁可报错让人换目录。"""
    if os.path.exists(out_dir) and os.listdir(out_dir):
        raise FileExistsError(f"输出目录非空: {out_dir}(写入追加语义危险,拒绝)")


def _task_table(tasks_per_episode: list[list[str]]) -> tuple[list[str], dict]:
    """各 episode 的任务文本(覆写已生效)→ (保序去重的任务表, 文本→新 task_index)。

    保序而非排序:任务表顺序即 task_index,按出现顺序排能让新旧编号尽量接近,
    人眼对账 meta/tasks 时不至于整体错位。"""
    task_strings: list[str] = []
    for tasks in tasks_per_episode:
        for t in tasks:
            if t not in task_strings:
                task_strings.append(t)
    return task_strings, {t: i for i, t in enumerate(task_strings)}


def _to_parquet_fsx_safe(df, dst: str, **kw) -> None:
    """pandas.to_parquet 的 FSX 安全版:pyarrow 直写 TOS 挂载会 EPERM
    (随机写被拒,2026-08-06 v2 导出端到端实锤,与 faststart/aria2 同族)。
    本地临时文件写好再整文件顺序拷贝,交付目录只见顺序写。
    kw 原样透传 to_parquet(index 语义由调用方定,勿在此处强加)。"""
    import shutil
    import tempfile
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        df.to_parquet(tmp_path, **kw)
        shutil.copyfile(tmp_path, dst)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _write_camera_health(out_dir: str, camera_health: dict | None,
                         keep_episode_indices: list[int]) -> None:
    """相机流健康度旁挂 `meta/curation_camera_health.json`(2026-08-07 逐相机改造)。

    为什么旁挂而不进 info.json:info.json 是 LeRobot 的**标准 schema**,官方 loader
    与全世界的下游脚本都按它解析,往里塞自定义字段是在别人的公共契约上刻字——
    宁可多一个文件,也不动标准。旁挂文件不认识的工具直接忽略,认识的按名字读。

    内容 = 逐 episode 逐相机的标注与读数 + 数据集级建议。**不删任何视频**:
    被标注的相机照样在数据集里,这里只是告诉客户"这一路读数可疑/需标定"。
    交付集重编号了,故每条同时给 源 episode_id 与 新 episode_index,便于对账。
    """
    if not camera_health:
        return
    eps = camera_health.get("episodes") or {}
    rows = []
    for new_idx, src_idx in enumerate(keep_episode_indices):
        src_id = f"ep{int(src_idx):06d}"
        det = eps.get(src_id)
        if not det:
            continue
        rows.append({"episode_index": new_idx, "source_episode_id": src_id,
                     "verdict": det.get("verdict"),
                     "flagged_cameras": det.get("flagged_cameras") or [],
                     "consensus_lag_s": det.get("consensus_lag_s"),
                     "per_camera": det.get("per_camera") or {}})
    payload = {
        "说明": ("视频-动作同步的逐相机读数与标注。lag_s > 0 = 画面晚于动作"
                 "(相机链路延迟,常见且可标定);< 0 = 画面早于动作(无良性解释,"
                 "疑似数据装配错误)。trusted=false 表示该路读数测不准,不构成结论。"
                 "**本文件只标注,不代表任何相机被废弃——视频一路未删**。"),
        "dataset": camera_health.get("dataset") or {},
        "episodes": rows,
    }
    path = os.path.join(out_dir, "meta", "curation_camera_health.json")
    write_json(path, payload, default=str)


def export_lerobot_v3(dataset_dir: str, keep_episode_indices: list[int], out_dir: str,
                      task_overrides: dict | None = None,
                      camera_health: dict | None = None,
                      video_file_mb: float | None = None,
                      data_file_mb: float | None = None) -> dict:
    """源 v3 数据集 + 幸存 episode 序号 → out_dir 下的合法 v3 数据集。返回导出统计。

    task_overrides: {源 episode_index: 新任务文本}——两类来源(2026-08-06 出数据闭环):
    ①无标注条目的自产 caption 补标;②人工裁决采纳的改标(rejudge 重导出)。
    覆写同时作用于任务表、逐帧 task_index 与 meta/episodes 的 tasks 列。

    2026-08-21 方案 1:data / 视频都按 LeRobot v3 自己的分文件规则滚动写(阈值 =
    video_file_mb / data_file_mb,缺省取源 info.json 的 *_files_size_in_mb),文件封口即交给
    export.publish(直连交付边出边传);轨迹不跨文件,判废的轨迹只影响编号。
    """
    from ..ingest import dsfs
    from ..ingest.lerobot_reader import _load_episodes_meta, _load_info
    from ..adapters.decode import decode_window

    _refuse_nonempty(out_dir)
    info = _load_info(dataset_dir)
    assert info["codebase_version"].startswith("v3"), "本导出器只收 v3 源(v2 源走 export_lerobot_v2)"
    fps = float(info["fps"])
    video_keys = [k for k, v in info["features"].items() if v["dtype"] == "video"]
    chunks_size = int(info.get("chunks_size") or 1000)
    data_max = _size_mb(info, "data_files_size_in_mb", 100.0, data_file_mb) * 1024 * 1024
    video_max = _size_mb(info, "video_files_size_in_mb", 200.0, video_file_mb) * 1024 * 1024

    ep_meta = _load_episodes_meta(dataset_dir).set_index("episode_index", drop=False)
    keep = list(keep_episode_indices)
    sel = ep_meta.loc[keep].reset_index(drop=True)

    task_overrides = task_overrides or {}

    # 源 episodes 表缺 tasks 列(v3 规范里可选;libero 2026-09-02):回退到
    # data.task_index → 源 meta/tasks.parquet,与读取侧同一套(lerobot_reader)。
    _src_tasks_missing = "tasks" not in sel.columns or any(
        not (isinstance(t, (list, tuple, np.ndarray)) and len(t)) for t in sel["tasks"])
    _tmap: dict = {}
    _tidx: dict = {}
    if _src_tasks_missing:
        from ..ingest.lerobot_reader import _load_tasks_map, _v3_task_index_of
        _tmap = _load_tasks_map(dataset_dir)
        _tidx = _v3_task_index_of(dataset_dir, info, sel) if _tmap else {}

    def _tasks_of(ep):
        ov = task_overrides.get(int(ep["episode_index"]))
        if ov:
            return [ov]
        t = ep.get("tasks") if hasattr(ep, "get") else None
        if isinstance(t, (list, tuple, np.ndarray)) and len(t):
            return list(t)
        return [str(_tmap.get(_tidx.get(int(ep["episode_index"])), ""))]

    # 新任务表(保序去重;覆写生效后的文本)
    task_strings, task_index_of = _task_table([_tasks_of(ep) for _, ep in sel.iterrows()])

    # ---------- data:逐轨迹切源 parquet → 滚动写 ----------
    dw = _RollingParquetWriter(out_dir, info["data_path"], data_max, chunks_size)
    data_loc: list[tuple[int, int]] = []
    new_from, cursor, lengths = [], 0, []
    _cache: dict = {}
    for new_idx, (_, ep) in enumerate(sel.iterrows()):
        src = dsfs.join(dataset_dir, info["data_path"].format(
            chunk_index=int(ep["data/chunk_index"]), file_index=int(ep["data/file_index"])))
        if src not in _cache:                  # 同一源文件连着几条:读一次
            _cache.clear()
            _cache[src] = dsfs.read_parquet(src)
        df = _cache[src]
        base = int(df["index"].iloc[0])
        lo, hi = int(ep["dataset_from_index"]), int(ep["dataset_to_index"])
        piece = df.iloc[lo - base: hi - base].copy()
        piece["episode_index"] = new_idx
        piece["task_index"] = task_index_of[_tasks_of(ep)[0]]
        piece["index"] = np.arange(cursor, cursor + len(piece), dtype=piece["index"].dtype)
        data_loc.append(dw.add(piece))
        new_from.append(cursor)
        lengths.append(len(piece))
        cursor += len(piece)
    dw.close()

    # ---------- videos:逐轨迹切窗重编码 → 滚动写(最耗时环节,打进度防误判卡死) ----------
    print(f"[curation] 开始导出交付数据集:{len(sel)} 条 × {len(video_keys)} 路相机的视频"
          f"切割重编码(耗时较长;只要质检结果可用 --report-only 跳过此步)", flush=True)
    video_loc: dict[str, list[tuple]] = {}
    for vk in video_keys:
        vw = _RollingVideoWriter(out_dir, info["video_path"], vk, fps, video_max, chunks_size)
        locs = []
        n = len(sel)
        for i, (_, ep) in enumerate(sel.iterrows()):
            if i % 10 == 0 or i == n - 1:
                print(f"[curation]   视频切割 {vk}: {i + 1}/{n} 段", flush=True)
            src = dsfs.join(dataset_dir, info["video_path"].format(
                video_key=vk, chunk_index=int(ep[f"videos/{vk}/chunk_index"]),
                file_index=int(ep[f"videos/{vk}/file_index"])))
            frames, _ = decode_window(src, float(ep[f"videos/{vk}/from_timestamp"]),
                                      float(ep[f"videos/{vk}/to_timestamp"]))
            locs.append(vw.add(frames))
        vw.close()
        video_loc[vk] = locs

    # ---------- meta/episodes:切源行继承 stats,覆写边界/编号/文件位置 ----------
    new_meta = sel.copy()
    if task_overrides or _src_tasks_missing:
        # 源缺 tasks 列时导出的交付补上这一列:下游(含我们自己)再读就不用回退了
        new_meta["tasks"] = [_tasks_of(ep) for _, ep in sel.iterrows()]
    new_meta["episode_index"] = np.arange(len(sel))
    new_meta["data/chunk_index"] = [c for c, _f in data_loc]
    new_meta["data/file_index"] = [f for _c, f in data_loc]
    new_meta["dataset_from_index"] = new_from
    new_meta["dataset_to_index"] = [f + int(l) for f, l in zip(new_from, lengths)]
    new_meta["meta/episodes/chunk_index"] = 0
    new_meta["meta/episodes/file_index"] = 0
    for vk in video_keys:
        new_meta[f"videos/{vk}/chunk_index"] = [c for c, _f, _a, _b in video_loc[vk]]
        new_meta[f"videos/{vk}/file_index"] = [f for _c, f, _a, _b in video_loc[vk]]
        new_meta[f"videos/{vk}/from_timestamp"] = [a for _c, _f, a, _b in video_loc[vk]]
        new_meta[f"videos/{vk}/to_timestamp"] = [b for _c, _f, _a, b in video_loc[vk]]
    ep_path = os.path.join(out_dir, "meta", "episodes", "chunk-000", "file-000.parquet")
    os.makedirs(os.path.dirname(ep_path), exist_ok=True)
    _to_parquet_fsx_safe(new_meta, ep_path, index=False)

    # ---------- meta/tasks + info + stats ----------
    tasks_df = pd.DataFrame({"task_index": range(len(task_strings))},
                            index=pd.Index(task_strings))
    _to_parquet_fsx_safe(tasks_df,          # 保留 task 文本索引(v3 任务表语义)
                         os.path.join(out_dir, "meta", "tasks.parquet"))

    new_info = dict(info)
    new_info["total_episodes"] = len(sel)
    new_info["total_frames"] = int(cursor)
    new_info["total_tasks"] = len(task_strings)
    new_info["splits"] = {"train": f"0:{len(sel)}"}
    if "total_videos" in new_info:
        new_info["total_videos"] = len(video_keys)
    write_json(os.path.join(out_dir, "meta", "info.json"), new_info, indent=2)
    _src_stats = dsfs.join(dataset_dir, "meta", "stats.json")
    if dsfs.exists(_src_stats):            # 统计缺失不影响加载(与 v2 导出同一口径)
        dsfs.copy_to_local(_src_stats, os.path.join(out_dir, "meta", "stats.json"))
    _write_camera_health(out_dir, camera_health, keep)
    publish.dir_done(out_dir)        # 余下的小文件也交发布器;meta/info.json 是标志,它只记不传

    return {"episodes": len(sel), "frames": int(cursor), "tasks": len(task_strings),
            "video_keys": video_keys, "out_dir": out_dir,
            "data_files": dw.n_file, "video_files": {vk: len(set((c, f) for c, f, _a, _b in video_loc[vk])) for vk in video_keys}}


def _write_jsonl(path: str, records: list[dict]) -> None:
    """一行一条 JSON 写 meta/*.jsonl。ensure_ascii=False:任务文本常是中文,
    转义成 \\uXXXX 客户打开 meta 一片乱码,没法人工核对。"""
    write_text(path, "".join(json.dumps(r, ensure_ascii=False) + "\n"
                            for r in records))


def _copy_v2_stats(dataset_dir: str, out_dir: str, keep: list[int]) -> None:
    """统计文件跟着走:v2.0 是全局 meta/stats.json(与子集无关,原样拷);
    v2.1 是逐条 meta/episodes_stats.jsonl(按选中条目过滤并重编号,否则新旧编号错位)。
    两个都没有也不报错——统计缺失不影响加载,交付照常。"""
    from ..ingest import dsfs
    os.makedirs(os.path.join(out_dir, "meta"), exist_ok=True)
    src_stats = dsfs.join(dataset_dir, "meta", "stats.json")
    if dsfs.exists(src_stats):
        dsfs.copy_to_local(src_stats, os.path.join(out_dir, "meta", "stats.json"))
    src_ep_stats = dsfs.join(dataset_dir, "meta", "episodes_stats.jsonl")
    if dsfs.exists(src_ep_stats):
        by_index = {}
        with dsfs.open_text(src_ep_stats) as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    by_index[int(rec["episode_index"])] = rec
        rows = []
        for new_idx, src_idx in enumerate(keep):
            rec = by_index.get(src_idx)
            if rec is None:
                continue                     # 源就缺这条统计:不伪造
            rec = dict(rec)
            rec["episode_index"] = new_idx
            rows.append(rec)
        _write_jsonl(os.path.join(out_dir, "meta", "episodes_stats.jsonl"), rows)


def export_lerobot_v2(dataset_dir: str, keep_episode_indices: list[int], out_dir: str,
                      task_overrides: dict | None = None,
                      camera_health: dict | None = None,
                      video_file_mb: float | None = None,     # v2 每条一个文件,无分文件阈值;
                      data_file_mb: float | None = None) -> dict:  # 与 v3 同签名便于统一调用
    """源 v2 数据集 + 幸存 episode 序号 → out_dir 下的合法 v2 数据集。返回导出统计。

    v2 每条 episode 一个 parquet + 每路相机一个 mp4,**文件边界即 episode 边界**:
    选中的 parquet 逐个读入只改编号列后另存,mp4 直接字节拷贝(不解码不转码)。
    因此这里没有 v3 导出器里那段最贵的视频切割重编码。

    task_overrides: {源 episode_index: 新任务文本}——语义与 v3 版一致(该条任务整体
    替换成这一条文本),同时作用于 meta/episodes.jsonl 的 tasks、meta/tasks.jsonl 任务表
    与 parquet 的 task_index 三处,三者必须一致否则下游按 task_index 取文本会错。
    """
    from ..ingest import dsfs
    from ..ingest.lerobot_reader import _load_info, _v2_episode_list, _v2_episode_paths

    _refuse_nonempty(out_dir)
    info = _load_info(dataset_dir)
    assert info["codebase_version"].startswith("v2"), "本导出器只收 v2 源(v3 源走 export_lerobot_v3)"
    chunks_size = int(info["chunks_size"])
    video_keys = [k for k, v in info["features"].items() if v["dtype"] == "video"]

    keep = [int(i) for i in keep_episode_indices]
    src_eps = {int(e["episode_index"]): e for e in _v2_episode_list(dataset_dir)}
    unknown = [i for i in keep if i not in src_eps]
    if unknown:
        raise KeyError(f"这些 episode_index 不在源 meta/episodes.jsonl 里: {unknown[:8]}")

    task_overrides = {int(k): v for k, v in (task_overrides or {}).items()}

    def _tasks_of(ep) -> list[str]:
        ov = task_overrides.get(int(ep["episode_index"]))
        return [ov] if ov else (list(ep.get("tasks") or []) or [""])

    sel = [src_eps[i] for i in keep]
    task_strings, task_index_of = _task_table([_tasks_of(ep) for ep in sel])

    print(f"[curation] 导出交付数据集:{len(sel)} 条 episode × {len(video_keys)} 路相机 "
          f"(每条独立文件 → 视频直接拷贝,无需重编码)", flush=True)

    # ---------- 逐条:parquet 改编号另存 + mp4 拷贝 ----------
    n_frames, n_videos = 0, 0
    missing_videos: list[str] = []
    new_eps: list[dict] = []
    for new_idx, ep in enumerate(sel):
        src_idx = int(ep["episode_index"])
        src_data, src_videos = _v2_episode_paths(dataset_dir, info, ep)
        out_chunk = new_idx // chunks_size

        df = dsfs.read_parquet(src_data)
        tasks = _tasks_of(ep)
        df["episode_index"] = new_idx
        if "task_index" in df.columns:
            df["task_index"] = task_index_of[tasks[0]]
        if "index" in df.columns:
            # index 是**全局**帧序号(跨 episode 连续),抽走一部分 episode 后必须整体重排,
            # 否则新数据集的帧号有洞,按 index 定位的下游会取到错帧
            df["index"] = np.arange(n_frames, n_frames + len(df), dtype=df["index"].dtype)
        out_data = os.path.join(out_dir, info["data_path"].format(
            episode_chunk=out_chunk, episode_index=new_idx))
        os.makedirs(os.path.dirname(out_data), exist_ok=True)
        _to_parquet_fsx_safe(df, out_data, index=False)
        publish.file_done(out_data)              # 每条文件写完即交发布器(直连边出边传)
        n_frames += len(df)

        for vk in video_keys:
            src_mp4 = src_videos[vk]["path"]
            if not dsfs.exists(src_mp4):
                # 缺某路视频是 v2 社区转换集的常态(如 DROID 部分条目缺腕部相机):
                # 有多少拷多少,末尾如实汇报,不因一路缺失丢掉整条 episode
                missing_videos.append(f"ep{src_idx:06d}/{vk}")
                continue
            out_mp4 = os.path.join(out_dir, info["video_path"].format(
                episode_chunk=out_chunk, video_key=vk, episode_index=new_idx))
            os.makedirs(os.path.dirname(out_mp4), exist_ok=True)
            dsfs.copy_to_local(src_mp4, out_mp4)   # 顺序整文件拷贝:挂载盘上比随机写安全
            publish.file_done(out_mp4)
            n_videos += 1

        # 源行的其它列(如社区转换集自带的额外字段)原样继承,只覆写编号/任务/长度
        meta_row = dict(ep)
        meta_row["episode_index"] = new_idx
        meta_row["tasks"] = tasks
        meta_row["length"] = len(df)
        new_eps.append(meta_row)

        # 条数多时逐条打印会淹没日志:每 50 条或每写满一个 chunk 报一次进度
        if (new_idx + 1) % 50 == 0 or (new_idx + 1) % chunks_size == 0 \
                or new_idx == len(sel) - 1:
            print(f"[curation] 导出交付数据集 {new_idx + 1}/{len(sel)} 条"
                  f"(chunk-{out_chunk:03d},累计 {n_frames} 帧 / {n_videos} 个视频)",
                  flush=True)

    if missing_videos:
        print(f"[curation] ⚠️ 源缺 {len(missing_videos)} 个视频文件,已跳过"
              f"(前几个: {missing_videos[:5]})", flush=True)

    # ---------- meta 三件套 + 统计 ----------
    _write_jsonl(os.path.join(out_dir, "meta", "episodes.jsonl"), new_eps)
    _write_jsonl(os.path.join(out_dir, "meta", "tasks.jsonl"),
                 [{"task_index": i, "task": t} for i, t in enumerate(task_strings)])

    new_info = dict(info)
    new_info["total_episodes"] = len(sel)
    new_info["total_frames"] = int(n_frames)
    new_info["total_tasks"] = len(task_strings)
    new_info["total_videos"] = n_videos          # 实拷贝数(缺路时如实少,不按理论值虚报)
    new_info["total_chunks"] = (len(sel) + chunks_size - 1) // chunks_size
    new_info["splits"] = {"train": f"0:{len(sel)}"}
    # codebase_version 原样保留:导出没有升级格式,声称 v2.1 却按 v2.0 写 meta 会骗到 loader
    write_json(os.path.join(out_dir, "meta", "info.json"), new_info, indent=2)
    _copy_v2_stats(dataset_dir, out_dir, keep)
    _write_camera_health(out_dir, camera_health, keep)

    publish.dir_done(out_dir)        # 同 v3:余下小文件交发布器,标志只记不传
    return {"episodes": len(sel), "frames": int(n_frames), "tasks": len(task_strings),
            "videos": n_videos, "video_keys": video_keys, "out_dir": out_dir}
