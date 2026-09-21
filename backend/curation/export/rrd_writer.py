"""RRD(rerun 格式)交付导出:幸存 episode 重组为**可直接使用的 .rrd 数据集**。

与 LeRobot 导出同一条哲学 —— "拿走即用,原数据可弃"。但 RRD 的源布局比 LeRobot v2
还要干净:**一条 episode 就是一个自包含的 .rrd 文件**(数值 + 视频字节全在里面,
没有 meta/、没有跨文件索引),所以导出的主体动作只有两个:

- **通过且未改标** → `shutil.copyfile` 原样字节拷贝(不解码、不重写、不重编号);
- **改标(采纳的新任务文本)** → chunk 级重写:读出全部 chunk,把 `/task` 那一族
  换成一条**静态 TextDocument**,其余 chunk 原样,重新 write_rrd 出去。

**为什么保留原 episode 编号**(不像 LeRobot 导出那样从 0 重排):rrd 没有任何跨文件
索引会因为编号有洞而失效,而保号意味着交付里的 `episode_7.rrd` 就是源里的第 7 条 ——
客户拿报告对账、回源复查都是一步到位。重编号在这里只会毁掉可溯源性,换不来任何东西。

⚠️ FSX 纪律(已咬五次的老坑):交付目录在 TOS 的 FSX 挂载上,**拒绝随机写**。
   本模块落交付只有两种写法:①`shutil.copyfile` 顺序整拷;②先写本地临时文件再
   copyfile。绝不让 rerun 的 write_rrd 直接对着交付路径写(它是库内部的写序,
   不在我们控制之下),也绝不用追加/seek 回填。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

from .lerobot_writer import _refuse_nonempty

#: 交付集里那份人类可读的清单文件名(与 lerobot 交付的 meta/ 同位:一眼看清交了什么)
INDEX_NAME = "index.json"


def _source_files(input_dir: str) -> dict[str, str]:
    """源目录 → {episode_id: .rrd 路径}。编号解析复用 reader 的同一套规则,
    保证"报告里的 ep000007"和"交付里的 episode_7.rrd"指的是同一条。"""
    from ..ingest.rrd_reader import _episode_files

    return {f"ep{idx:06d}": path for idx, path in _episode_files(input_dir)}


def _rewrite_task(src_path: str, dst_path: str, new_text: str,
                  task_entity: str = "/task") -> None:
    """chunk 级改标:原 .rrd 的 `/task` 换成静态 TextDocument(新文本),其余原样。

    为什么走 chunk 级而不是"读出数据重新 rr.log 一遍":重录会把视频字节、组件描述符、
    静态/时序属性全部经过一次 SDK 的重新编码 —— 那等于把整条 episode 重做一遍,任何
    我们没读懂的组件(客户自定义 archetype、blueprint、录制属性)都会在途中悄悄丢掉。
    chunk 级只动 `/task` 这一族,其余 chunk 是**原对象**直接转交,信息零损耗。

    原文件 `/task` 为空(so101 那种 rows=0 的占位 chunk)也照样能写进新任务:
    我们是"过滤掉旧的 + 追加一条新的",不依赖旧 chunk 存在。

    ⚠️ 内存:to_chunks() 会把该 episode 的全部 chunk(含视频字节)拉进内存。单条
    episode 量级(几十 MB)可接受,而且只有**被改标的**条目才走这条路 —— 通过且未改标
    的条目全程只是字节拷贝。
    """
    import rerun as rr
    from rerun.experimental import Chunk, LazyChunkStream, RrdReader

    reader = RrdReader(src_path)
    recordings = list(reader.recordings())
    if not recordings:
        raise ValueError(f"{src_path}: 文件里没有 recording,无法改标重写")
    rec = recordings[0]
    # write_rrd 的 application_id/recording_id 两参数必填且没有默认:沿用原 recording 的,
    # 否则交付出去的文件在客户的 rerun 里会变成另一段录制(身份漂移)
    kept = [c for c in reader.store().stream().to_chunks()
            if c.entity_path != task_entity]
    new_chunk = Chunk.from_columns(task_entity, indexes=[],
                                   columns=rr.TextDocument.columns(text=[new_text]))
    with tempfile.NamedTemporaryFile(suffix=".rrd", delete=False) as tf:
        tmp_path = tf.name
    try:
        LazyChunkStream.from_iter([*kept, new_chunk]).write_rrd(
            tmp_path, application_id=rec.application_id,
            recording_id=rec.recording_id)
        shutil.copyfile(tmp_path, dst_path)      # 顺序整拷落交付,FSX 安全
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def export_rrd_curated(delivery_dir: str, input_dir: str, keep_ids: list[str],
                       relabels: dict[str, str] | None = None,
                       out_name: str = "rrd_curated",
                       episodes: dict[str, dict] | None = None,
                       generated_at: str = "") -> dict:
    """幸存 episode → `<delivery_dir>/<out_name>/` 下的 .rrd 交付集 + index.json。

    keep_ids:  交付条目的 episode_id(`ep000007` 形态),顺序即清单顺序;
    relabels:  {episode_id: 新任务文本} —— 改标条目走 chunk 级重写,其余原样拷贝;
    episodes:  {episode_id: {"verdict","instruction","instruction_source"}} 可选补充,
               清单里的判决与标注溯源由调用方给(导出器不重新判断任何东西);
    generated_at: 生成时间串**由调用方传入** —— 库代码里不调 datetime.now,
               否则测试没法钉住输出(与 lerobot 导出把时间留给报告层同一取舍)。

    返回导出统计(与 export_lerobot_v2 同形状:调用方按 out_dir 记交付物)。
    """
    relabels = dict(relabels or {})
    episodes = episodes or {}
    out_dir = os.path.join(delivery_dir, out_name)
    _refuse_nonempty(out_dir)

    src_of = _source_files(input_dir)
    unknown = [e for e in keep_ids if e not in src_of]
    if unknown:
        raise KeyError(f"这些 episode 在源目录里找不到对应 .rrd: {unknown[:8]}")

    os.makedirs(out_dir, exist_ok=True)
    n_relabeled = 0
    records = []
    print(f"[curation] RRD 导出:{len(keep_ids)} 条(改标 {len(relabels)} 条走 chunk 级"
          f"重写,其余原样字节拷贝)", flush=True)
    for i, eid in enumerate(keep_ids):
        src = src_of[eid]
        fname = os.path.basename(src)            # 保留源文件名 = 保留原 episode 编号
        dst = os.path.join(out_dir, fname)
        new_text = str(relabels.get(eid) or "").strip()
        if new_text:
            _rewrite_task(src, dst, new_text)
            n_relabeled += 1
        else:
            shutil.copyfile(src, dst)            # 顺序整拷:挂载盘上比任何重写都安全
        meta = episodes.get(eid) or {}
        records.append({
            "episode_id": eid,
            "file": fname,
            "verdict": meta.get("verdict") or "通过",
            "instruction": new_text or (meta.get("instruction") or ""),
            "instruction_source": meta.get("instruction_source") or "",
            "relabeled": bool(new_text),
        })
        if (i + 1) % 50 == 0 or i == len(keep_ids) - 1:
            print(f"[curation] RRD 导出 {i + 1}/{len(keep_ids)} 条", flush=True)

    index = {
        "source_format": "rrd",
        "n_kept": len(keep_ids),
        "n_removed": len(src_of) - len(keep_ids),
        "generated_at": generated_at,
        "说明": ("每条 episode 一个自包含 .rrd(数值+视频都在里面),**文件名保留源编号**"
                 "便于回源对账;通过且未改标的条目与源文件逐字节一致,改标条目只重写了"
                 "/task(其余 chunk 原样)。被剔除的条目不在本目录中。"),
        "episodes": records,
    }
    from .safe_write import write_json
    write_json(os.path.join(out_dir, INDEX_NAME), index)

    return {"episodes": len(keep_ids), "relabeled": n_relabeled,
            "removed": index["n_removed"], "out_dir": out_dir}
