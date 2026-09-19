"""Lance 交付导出:幸存 episode 重组为**可直接使用的 lance 表**。

与 RRD 导出同一条哲学 —— **按原格式交付**,不强行转成 LeRobot:客户的数据栈本来就
吃 lance,转格式既贵又会丢掉我们没读懂的列(自定义传感器列、embedding 列……)。

主体动作只有三个,全部在**列层面**做,行内容零改写:
- **按 episode 过滤**:只保留通过的 episode 的行(filter 下推,被剔除的行不进交付);
- **改标落列**:采纳的新任务文本写进 task 列(该 episode 全部行统一换文本);
- **追加溯源列**:curation_verdict(通过/rejudged-pass)与 curation_instruction_source
  (original/自产caption/human-adjudicated)—— 客户 diff 源表即知每行的来历。
其余列(客户自定义的一切)原样透传,信息零损耗。episode 编号保留源值,
交付表里的 episode=7 就是源表的第 7 条,回源对账一步到位。

⚠️ FSX 纪律(与 rrd_writer 同款):交付目录可能在 TOS 的 FSX 挂载上,拒绝随机写。
   lance 写数据集的写序不在我们控制之下 → **先写本地临时目录,再顺序整拷**过去。
"""
from __future__ import annotations

import os
import shutil
import tempfile

from .lerobot_writer import _refuse_nonempty

#: 交付表目录名与清单文件名(与 rrd 交付的 index.json 同位:一眼看清交了什么)
INDEX_NAME = "index.json"
TABLE_NAME = "episodes.lance"

#: 追加的溯源列(客户源表里已有同名列时响亮失败,绝不静默覆盖)
COL_VERDICT = "curation_verdict"
COL_INSTR_SOURCE = "curation_instruction_source"


def export_lance_curated(delivery_dir: str, input_dir: str, keep_ids: list[str],
                         relabels: dict[str, str] | None = None,
                         out_name: str = "lance_curated",
                         episodes: dict[str, dict] | None = None,
                         generated_at: str = "",
                         mapping: dict | None = None,
                         table: str | None = None) -> dict:
    """幸存 episode → `<delivery_dir>/<out_name>/episodes.lance` + index.json。

    keep_ids:  交付条目的 episode_id(`ep000007` 形态 → 源表 episode 列值 7);
    relabels:  {episode_id: 新任务文本} —— 写进 task 列,其余行原样;
    episodes:  {episode_id: {"verdict","instruction","instruction_source"}} 可选补充,
               清单里的判决与标注溯源由调用方给(导出器不重新判断任何东西);
    generated_at: 生成时间串由调用方传入(库代码不调 datetime.now,测试要钉住输出)。

    返回导出统计(与 export_rrd_curated 同形状:调用方按 out_dir 记交付物)。
    """
    import pyarrow as pa

    from ..ingest.lance_reader import DEFAULT_MAPPING, _lance_mod, _resolve_table

    lance = _lance_mod()
    relabels = dict(relabels or {})
    episodes = episodes or {}
    mp = dict(DEFAULT_MAPPING, **(mapping or {}))
    out_dir = os.path.join(delivery_dir, out_name)
    _refuse_nonempty(out_dir)

    src_path = _resolve_table(input_dir, table)
    ds = lance.dataset(src_path)
    ep_col = mp["episode"]
    task_col = mp["task"]
    for c in (COL_VERDICT, COL_INSTR_SOURCE):
        if c in ds.schema.names:
            raise ValueError(
                f"源表已有 {c!r} 列(疑似对交付再跑质检?);溯源列不覆盖,拒绝导出")

    keep_vals = [int(e.replace("ep", "")) for e in keep_ids]
    info = {int(e.replace("ep", "")): (episodes.get(e) or {}) for e in keep_ids}
    relabel_of = {int(e.replace("ep", "")): str(t).strip()
                  for e, t in relabels.items() if str(t).strip()}
    print(f"[curation] lance 导出:{len(keep_vals)} 条 episode(改标 "
          f"{len(relabel_of)} 条落 task 列,其余行原样;追加 2 列溯源)", flush=True)

    tmp_root = tempfile.mkdtemp(prefix="lance_curated_")
    tmp_table = os.path.join(tmp_root, TABLE_NAME)
    n_rows = 0
    try:
        # 分批过滤读(与 reader 同款内存纪律:帧级视频字节不整表进内存),逐批写
        writer_ds = None
        for i in range(0, len(keep_vals), 8):
            batch = keep_vals[i:i + 8]
            flt = f"{ep_col} IN ({', '.join(str(v) for v in batch)})"
            tbl = ds.to_table(filter=flt)
            if tbl.num_rows == 0:
                missing = [f"ep{v:06d}" for v in batch]
                raise KeyError(f"这些 episode 在源表里找不到对应行: {missing[:8]}")
            eps = [int(v) for v in tbl[ep_col].to_pylist()]
            if task_col in tbl.schema.names and relabel_of:
                tasks = [relabel_of.get(e, t)
                         for e, t in zip(eps, tbl[task_col].to_pylist())]
                tbl = tbl.set_column(tbl.schema.get_field_index(task_col), task_col,
                                     pa.array(tasks, tbl.schema.field(task_col).type))
            tbl = tbl.append_column(
                COL_VERDICT,
                pa.array([str(info.get(e, {}).get("verdict") or "通过") for e in eps]))
            tbl = tbl.append_column(
                COL_INSTR_SOURCE,
                pa.array([("human-adjudicated" if e in relabel_of else
                           str(info.get(e, {}).get("instruction_source") or "original"))
                          for e in eps]))
            if writer_ds is None:
                writer_ds = lance.write_dataset(tbl, tmp_table, mode="create")
            else:
                writer_ds = lance.write_dataset(tbl, tmp_table, mode="append")
            n_rows += tbl.num_rows
        os.makedirs(out_dir, exist_ok=True)
        shutil.copytree(tmp_table, os.path.join(out_dir, TABLE_NAME))  # 顺序整拷,FSX 安全
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    records = [{
        "episode_id": e,
        "episode": int(e.replace("ep", "")),
        "verdict": (episodes.get(e) or {}).get("verdict") or "通过",
        "instruction": (relabels.get(e) or "").strip()
                       or ((episodes.get(e) or {}).get("instruction") or ""),
        "instruction_source": (episodes.get(e) or {}).get("instruction_source") or "",
        "relabeled": bool((relabels.get(e) or "").strip()),
    } for e in keep_ids]
    n_src_eps = len(set(int(v) for v in
                        ds.to_table(columns=[ep_col])[ep_col].to_pylist()))
    index = {
        "source_format": "lance",
        "source_table": src_path,
        "table": TABLE_NAME,
        "n_kept": len(keep_ids),
        "n_removed": n_src_eps - len(keep_ids),
        "n_rows": n_rows,
        "generated_at": generated_at,
        "说明": ("交付是一张 lance 表(帧级行,schema 与源表一致,另加 "
                 f"{COL_VERDICT}/{COL_INSTR_SOURCE} 两列溯源);episode 编号保留源值"
                 "便于回源对账,被剔除的 episode 的行不在表中;改标条目的 task 列"
                 "已换成新文本,其余列与源表逐值一致。"),
        "episodes": records,
    }
    from .safe_write import write_json
    write_json(os.path.join(out_dir, INDEX_NAME), index)

    return {"episodes": len(keep_ids), "relabeled": len(relabel_of),
            "removed": index["n_removed"], "out_dir": out_dir}
