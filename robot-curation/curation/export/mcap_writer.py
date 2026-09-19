"""MCAP 交付导出:幸存 episode 重组为**可直接使用的 .mcap 数据集**。

与 RRD 导出同一条哲学 —— **按原格式交付**:一条 episode 一个自包含 .mcap,通过的
原样字节拷贝(不解码、不重写、不重编号),剔除的不拷,文件名保留源编号便于回源对账。

**改标不重写文件本体**(与 rrd 的 chunk 级重写是有意的不同):mcap 没有"只换一个
topic、其余原样转交"的无损通道 —— 重写意味着全部消息经 reader/writer 走一遍,
schema/channel/attachment/metadata 任何我们没读懂的记录都可能在途中悄悄丢掉,
而且 cdr 消息的重新序列化还要依赖完整的 msgdef。宁可保持源文件逐字节原样,
把新任务文本落在 index.json 的清单里(episode 级,机器可读)—— 数据不动,
标注的真相有处可查。转换方若要把改标写回消息流,拿着 index.json 一步就能做。
"""
from __future__ import annotations

import os
import shutil

from .lerobot_writer import _refuse_nonempty

#: 交付集里那份人类可读的清单文件名(与 rrd/lance 交付同位)
INDEX_NAME = "index.json"


def _source_files(input_dir: str) -> dict[str, str]:
    """源目录 → {episode_id: .mcap 路径}。编号解析复用 reader 的同一套规则,
    保证"报告里的 ep000007"和"交付里的 episode_7.mcap"指的是同一条。"""
    from ..ingest.mcap_reader import _episode_files

    return {f"ep{idx:06d}": path for idx, path in _episode_files(input_dir)}


def export_mcap_curated(delivery_dir: str, input_dir: str, keep_ids: list[str],
                        relabels: dict[str, str] | None = None,
                        out_name: str = "mcap_curated",
                        episodes: dict[str, dict] | None = None,
                        generated_at: str = "") -> dict:
    """幸存 episode → `<delivery_dir>/<out_name>/` 下的 .mcap 交付集 + index.json。

    keep_ids:  交付条目的 episode_id(`ep000007` 形态),顺序即清单顺序;
    relabels:  {episode_id: 新任务文本} —— **只落 index.json**,文件本体逐字节原样
               (理由见模块 docstring);
    episodes:  {episode_id: {"verdict","instruction","instruction_source"}} 可选补充;
    generated_at: 生成时间串由调用方传入(库代码不调 datetime.now)。

    返回导出统计(与 export_rrd_curated 同形状:调用方按 out_dir 记交付物)。
    """
    relabels = {e: str(t).strip() for e, t in (relabels or {}).items()
                if str(t).strip()}
    episodes = episodes or {}
    out_dir = os.path.join(delivery_dir, out_name)
    _refuse_nonempty(out_dir)

    src_of = _source_files(input_dir)
    unknown = [e for e in keep_ids if e not in src_of]
    if unknown:
        raise KeyError(f"这些 episode 在源目录里找不到对应 .mcap: {unknown[:8]}")

    os.makedirs(out_dir, exist_ok=True)
    records = []
    print(f"[curation] mcap 导出:{len(keep_ids)} 条原样字节拷贝(改标 "
          f"{len(relabels)} 条记入 index.json,文件本体不动)", flush=True)
    for i, eid in enumerate(keep_ids):
        src = src_of[eid]
        fname = os.path.basename(src)            # 保留源文件名 = 保留原 episode 编号
        shutil.copyfile(src, os.path.join(out_dir, fname))   # 顺序整拷,FSX 安全
        meta = episodes.get(eid) or {}
        new_text = relabels.get(eid, "")
        records.append({
            "episode_id": eid,
            "file": fname,
            "verdict": meta.get("verdict") or "通过",
            "instruction": new_text or (meta.get("instruction") or ""),
            "instruction_source": meta.get("instruction_source") or "",
            "relabeled": bool(new_text),
        })
        if (i + 1) % 50 == 0 or i == len(keep_ids) - 1:
            print(f"[curation] mcap 导出 {i + 1}/{len(keep_ids)} 条", flush=True)

    index = {
        "source_format": "mcap",
        "n_kept": len(keep_ids),
        "n_removed": len(src_of) - len(keep_ids),
        "generated_at": generated_at,
        "说明": ("每条 episode 一个自包含 .mcap,与源文件**逐字节一致**,文件名保留"
                 "源编号便于回源对账;被剔除的条目不在本目录中。改标(relabeled=true)"
                 "的新任务文本在本清单的 instruction 字段 —— 文件本体未重写,理由与"
                 "写回方法见交付报告。"),
        "episodes": records,
    }
    from .safe_write import write_json
    write_json(os.path.join(out_dir, INDEX_NAME), index)

    return {"episodes": len(keep_ids), "relabeled": len(relabels),
            "removed": index["n_removed"], "out_dir": out_dir}
