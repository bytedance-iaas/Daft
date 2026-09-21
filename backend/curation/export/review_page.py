"""静态审片页生成器(2026-08-06,复活并转正原 7862 临时审片工具)。

原工具 = 旧 pod /tmp 里手起的 http.server + 一次性切片,pod 一重启全蒸发。
转正方案:本模块把 **索引页 + 逐 episode 页 + 视频片段** 生成为纯静态文件
(落 TOS 持久),由 UI 进程的 `/review` 静态路由服务(见 ui/app.py)——
UI 是 pod 的 PID 1,重启自动回来;同端口走 Basic 锁,公网 APIG 也可达。

页面形态照抄原工具的长处:索引页一屏列全量 episode(百余条直接扫),点进
单条页看多路视频(2 倍速片段),prev/next 串场。全部相对路径,挂哪个前缀
都能用。视频编码复用 evidence._encode_mp4(faststart + 本地盘中转,FSX 安全)。
"""
from __future__ import annotations

import html
import json
import os
import time

from .safe_write import write_text

#: 站点身份文件(2026-08-11 新增)。UI 的 Episodes 页要把"这个交付"对上"哪个审片站",
#: 而站点原先只有一个自由文本标题,对不上——曾出现另一份交付借用了本站片段(同源
#: 数据集、同 episode 号,那次巧对;换个数据集就是给客户放错视频)。有了它,UI 按
#: 源数据集精确匹配,匹配不上宁可不给视频。
SITE_JSON = "site.json"


def write_site_manifest(out_dir: str, source_dataset: str | None,
                        title: str = "") -> str | None:
    """站点根目录落 site.json(源数据集是谁)。没给源数据集就不写——宁可缺这个
    文件让 UI 走降级,也不写一个内容存疑的身份证。重跑覆盖(幂等)。

    ⚠️ FSX:审片站一般落 TOS 挂载,库直写咬过五次 → 本地临时文件写完再整文件拷。
    """
    if not source_dataset:
        return None
    import shutil
    import tempfile
    src = os.path.abspath(source_dataset)
    payload = {"source_dataset": src, "dataset_name": os.path.basename(src),
               "title": title,
               "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, SITE_JSON)
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8",
                                     delete=False) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=1)
        tmp_path = tmp.name
    try:
        shutil.copyfile(tmp_path, dst)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return dst

_INDEX_CSS = """
body{font-family:system-ui,sans-serif;margin:1.5rem;background:#fafafa;color:#222}
table{border-collapse:collapse;width:100%;background:#fff}
th,td{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;font-size:.92rem}
tr:hover td{background:#fff3e0}
a{color:#c60;text-decoration:none} a:hover{text-decoration:underline}
h1{font-size:1.3rem}
.small{color:#888;font-size:.85rem}
"""

_EP_CSS = _INDEX_CSS + """
.vids{display:flex;gap:.8rem;flex-wrap:wrap}
video{max-width:32%;min-width:280px;background:#000;border-radius:6px}
.nav{margin:.8rem 0;display:flex;gap:1.2rem}
"""


def build_review_page(rows: list[dict], out_dir: str, title: str = "人工审片",
                      sample_interval_s: float = 0.5, max_side: int = 448,
                      play_fps: int = 4, max_cams: int = 3,
                      on_progress=None, source_dataset: str | None = None) -> int:
    """rows(含 episode_id/instruction/video 指针)→ out_dir 下的静态审片站。

    产物:site.json + index.html + ep/<id>.html + clips/<id>__<cam>.mp4。
    已存在片段的 episode 跳过重编码(重跑只补新增,幂等)。返回生成的片段数。
    source_dataset = 源数据集目录,写进 site.json 供 UI 精确认领(见其注释)。
    """
    from .evidence import write_audit_clips

    os.makedirs(os.path.join(out_dir, "ep"), exist_ok=True)
    write_site_manifest(out_dir, source_dataset, title)
    clip_root = os.path.join(out_dir, "details", "audit_clips")  # 复用同款目录结构

    n_clips = 0
    items = []
    for i, r in enumerate(rows):
        eid = r["episode_id"]
        cams = sorted(r.get("video") or {})[:max_cams]
        have = [c for c in cams
                if os.path.exists(os.path.join(clip_root, f"{eid}__{c.split('.')[-1]}.mp4"))]
        if len(have) < len(cams):
            n_clips += write_audit_clips([eid], {eid: r["video"]}, out_dir,
                                         sample_interval_s=sample_interval_s,
                                         max_side=max_side, play_fps=play_fps,
                                         max_cams=max_cams)
        items.append({"eid": eid, "instruction": str(r.get("instruction") or ""),
                      "cams": [c.split(".")[-1] for c in cams]})
        if on_progress is not None:
            on_progress()

    # 逐 episode 页(prev/next 串场)
    for i, it in enumerate(items):
        eid = it["eid"]
        prev_ep = items[i - 1]["eid"] if i > 0 else None
        next_ep = items[i + 1]["eid"] if i + 1 < len(items) else None
        vids = "\n".join(
            f'<video controls preload="metadata" '
            f'src="../details/audit_clips/{eid}__{c}.mp4"></video>'
            for c in it["cams"])
        nav = '<div class="nav"><a href="../index.html">← 索引</a>'
        if prev_ep:
            nav += f'<a href="{prev_ep}.html">← 上一条 {prev_ep}</a>'
        if next_ep:
            nav += f'<a href="{next_ep}.html">下一条 {next_ep} →</a>'
        nav += "</div>"
        page = (f"<!doctype html><meta charset=utf-8><title>{eid}</title>"
                f"<style>{_EP_CSS}</style>"
                f"{nav}<h1>{eid} <span class=small>({i + 1}/{len(items)})</span></h1>"
                f"<p><b>标注:</b>{html.escape(it['instruction'])}</p>"
                f'<div class="vids">{vids}</div>{nav}')
        # 逐条页不开回读校验:几百个小文件挨个回读,在挂载的可见性延迟下只会
        # 刷几百行误报;索引页与三件套走回验足够代表这一批。
        write_text(os.path.join(out_dir, "ep", f"{eid}.html"), page)

    # 索引页:一屏列全量
    trs = "\n".join(
        f'<tr><td><a href="ep/{it["eid"]}.html">{it["eid"]}</a></td>'
        f"<td>{html.escape(it['instruction'][:90])}</td>"
        f"<td>{len(it['cams'])} 路</td></tr>"
        for it in items)
    index = (f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
             f"<style>{_INDEX_CSS}</style><h1>{html.escape(title)}"
             f" <span class=small>({len(items)} 条,视频为 {play_fps}fps 倍速片段)"
             f"</span></h1>"
             f"<table><tr><th>episode</th><th>标注</th><th>视频</th></tr>{trs}</table>")
    write_text(os.path.join(out_dir, "index.html"), index)
    return n_clips


# ── 切片入交付(2026-08-28,去挂载依赖)─────────────────────────────────

#: 批次里逐条片段的目录名。报告页轻镜像**刻意跳过**它(大文件),播放端按
#: 下面的索引现签 URL;别改名 —— runner.MIRROR_SKIP_DIRS 与 manifest 的
#: batch_clip_paths 都按这个字面量对齐。
CLIPS_DIRNAME = "review_clips"

#: 片段索引(批次内相对路径):{episode_id: [相机短名,…]}。落在 details/ 下
#: 是有意的 —— details/ 会进轻镜像,播放端不用为了"有没有片段"去列桶。
CLIPS_INDEX_RELPATH = "details/review_clips_index.json"


def build_delivery_clips(rows: list[dict], delivery: str, *, region: str | None = None,
                         sample_interval_s: float = 0.5, max_side: int = 448,
                         play_fps: int = 4, max_cams: int = 3,
                         on_progress=None) -> tuple[int, str]:
    """逐条片段进**交付批次**:<批次>/review_clips/<ep>__<cam>.mp4 + 索引。

    落点跟着交付走,与挂载无关:本地/挂载交付直接落批次目录;桶交付逐条
    上传、传完即删本地 —— pod 盘上同时只有正在切的这一条,与数据集大小无关。
    幂等按索引:已记录的相机不重编码,重跑只补新增。
    返回 (新编码段数, 批次位置)。
    """
    import shutil
    import tempfile

    from .evidence import write_audit_clips

    if str(delivery).startswith("tos://"):
        from .. import tos_store
        run_url = tos_store.resolve_run_url(str(delivery), region)
        bucket, run_prefix = tos_store.parse_tos_url(run_url)
        st = tos_store.make_store_for(bucket, region)

        def _read_index() -> dict:
            try:
                return json.loads(st.get_bytes(
                    bucket, f"{run_prefix}/{CLIPS_INDEX_RELPATH}").decode("utf-8"))
            except Exception:  # noqa: BLE001 没有索引 = 还没切过
                return {}

        def _put_clip(local: str, fname: str) -> None:
            st.upload(local, bucket, f"{run_prefix}/{CLIPS_DIRNAME}/{fname}")
            os.remove(local)

        def _flush(idx: dict) -> None:
            tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                              encoding="utf-8")
            json.dump(idx, tmp, ensure_ascii=False, indent=0)
            tmp.close()
            st.upload(tmp.name, bucket, f"{run_prefix}/{CLIPS_INDEX_RELPATH}")
            os.remove(tmp.name)

        where = run_url
    else:
        from ..delivery import resolve_run
        from .safe_write import write_json
        run_dir = resolve_run(str(delivery))

        def _read_index() -> dict:
            try:
                with open(os.path.join(run_dir, CLIPS_INDEX_RELPATH),
                          encoding="utf-8") as f:
                    return json.load(f)
            except Exception:  # noqa: BLE001
                return {}

        def _put_clip(local: str, fname: str) -> None:
            dst = os.path.join(run_dir, CLIPS_DIRNAME, fname)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(local, dst)

        def _flush(idx: dict) -> None:
            write_json(os.path.join(run_dir, CLIPS_INDEX_RELPATH), idx,
                       ensure_ascii=False)

        where = run_dir

    idx = _read_index()
    n_new = 0
    dirty = False
    changed = False
    for i, r in enumerate(rows):
        eid = str(r["episode_id"])
        cams = sorted(r.get("video") or {})[:max_cams]
        short = {c.split(".")[-1] for c in cams}
        if short and short <= set(idx.get(eid) or []):
            if on_progress is not None:
                on_progress()
            continue
        with tempfile.TemporaryDirectory(prefix="clips-") as td:
            n_new += write_audit_clips([eid], {eid: r.get("video") or {}}, td,
                                       sample_interval_s=sample_interval_s,
                                       max_side=max_side, play_fps=play_fps,
                                       max_cams=max_cams)
            produced_dir = os.path.join(td, "details", "audit_clips")
            produced = (sorted(os.listdir(produced_dir))
                        if os.path.isdir(produced_dir) else [])
            for fn in produced:
                _put_clip(os.path.join(produced_dir, fn), fn)
            got = [fn[len(eid) + 2:-4] for fn in produced
                   if fn.startswith(eid + "__") and fn.endswith(".mp4")]
            if got:
                idx[eid] = sorted(set(idx.get(eid) or []) | set(got))
                dirty = changed = True
        if dirty and (i + 1) % 10 == 0:
            _flush(idx)
            dirty = False
        if on_progress is not None:
            on_progress()
    if dirty or changed:
        _flush(idx)
    return n_new, where


def prune_delivery_clips(batch_dir: str, keep_eids, *, remote_url: str | None = None,
                         region: str | None = None) -> int:
    """剔除条目的逐条片段清理(rejudge 收尾用):索引减行 + 片段文件删除。

    索引在 details/ 下,改本地那份即可(直连交付由 sync_back 一并写回);
    片段本体被镜像/写回**双向跳过**,得点名删 —— 本地有就删本地,remote_url
    给了就顺带删桶里的。返回剔掉的 episode 数;没有索引 = 没切过,0。
    """
    idx_p = os.path.join(batch_dir, CLIPS_INDEX_RELPATH)
    try:
        with open(idx_p, encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:  # noqa: BLE001 没切过片段
        return 0
    keep = {str(e) for e in keep_eids}
    drop = [e for e in idx if e not in keep]
    if not drop:
        return 0
    st = bucket = prefix = None
    if remote_url:
        from .. import tos_store
        bucket, prefix = tos_store.parse_tos_url(remote_url)
        prefix = prefix.strip("/")
        st = tos_store.make_store_for(bucket, region)
    for eid in drop:
        for cam in idx.get(eid) or []:
            fname = f"{eid}__{cam}.mp4"
            lp = os.path.join(batch_dir, CLIPS_DIRNAME, fname)
            if os.path.exists(lp):
                os.remove(lp)
            if st is not None:
                try:
                    st.delete(bucket, f"{prefix}/{CLIPS_DIRNAME}/{fname}")
                except Exception:  # noqa: BLE001 远端本来就没有 → 目的已达成
                    pass
        idx.pop(eid, None)
    from .safe_write import write_json
    write_json(idx_p, idx, ensure_ascii=False)
    return len(drop)
