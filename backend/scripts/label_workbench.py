#!/usr/bin/env python3
"""未打标数据人工定标工作台(7862,2026-08-07)。

目的:droid-200 里无标注的 episode,人看视频 + 豆包 caption 草稿 → 修正 → 确认,
产出人工真值集(无标注专场考卷的地基;也可回填交付)。

形态:单文件 FastAPI 小服务,内部工具不进产品包。
- 视频:直接复用审片站 /mnt/tos/review/droid200 的现成切片(不重复编码);
- caption:生产 captioner(多视角+注意力引导)现场生成,缓存 JSON,重启不重打;
- 真值:/mnt/tos/calibration/droid200_unlabeled_truth.csv,整文件重写
  (FSX 拒绝追加写)+ 进程内存态为准(单进程,读盘只在启动时);
- 无鉴权:与老 7862 同款,只走 kubectl port-forward 内部访问。

用法(pod 内):python3 scripts/label_workbench.py [--regen-captions]
"""
from __future__ import annotations

import argparse
import csv
import datetime
import html
import json
import os
import sys
import threading

sys.path.insert(0, "/app")

DATASET = "/mnt/tos/datasets/droid_lerobot"
CLIP_SITE = "/mnt/tos/review/droid200"            # 审片站(切片已就位)
CAL_DIR = "/mnt/tos/calibration"
OUT_CSV = os.path.join(CAL_DIR, "droid200_unlabeled_truth.csv")
CAP_JSON = os.path.join(CAL_DIR, "droid200_unlabeled_captions.json")
N_EPISODES = 200
PORT = 7862

_LOCK = threading.Lock()


# ── 数据准备 ────────────────────────────────────────────────────────────────

def find_unlabeled() -> list[dict]:
    from curation.ingest.lerobot_reader import read_lerobot_rows
    rows = read_lerobot_rows(DATASET, episode_indices=set(range(N_EPISODES)),
                             validate=True)
    return [r for r in rows if not (r.get("instruction") or "").strip()]


def ensure_captions(rows: list[dict], regen: bool = False) -> dict:
    caps: dict = {}
    if os.path.exists(CAP_JSON) and not regen:
        caps = json.load(open(CAP_JSON, encoding="utf-8"))
    missing = [r for r in rows if not str(caps.get(r["episode_id"], "")).strip()]
    if missing:
        from curation.dataset_level.caption import (caption_episodes,
                                                    make_vlm_captioner)
        from curation.pipeline.config import load_config
        cfg = load_config(os.environ.get("CURATION_CONFIG"))
        v = cfg["checks"]["task_success"]["vlm"]
        capper = make_vlm_captioner(v["endpoint"], v["model"],
                                    api_key_env=v.get("api_key_env"))
        print(f"[workbench] 现场生成 caption:{len(missing)} 条(并发 32)…",
              flush=True)
        done = [0]

        def _tick():
            done[0] += 1
            if done[0] % 10 == 0 or done[0] == len(missing):
                print(f"[workbench] caption {done[0]}/{len(missing)}", flush=True)

        for r, c in zip(missing, caption_episodes(
                missing, capper, n_frames=8, max_concurrency=32,
                on_progress=_tick)):
            caps[r["episode_id"]] = c or ""
        os.makedirs(CAL_DIR, exist_ok=True)
        with open(CAP_JSON, "w", encoding="utf-8") as f:
            json.dump(caps, f, ensure_ascii=False, indent=1)
        print(f"[workbench] caption 缓存已写 {CAP_JSON}", flush=True)
    return caps


def load_truth() -> dict:
    if not os.path.exists(OUT_CSV):
        return {}
    with open(OUT_CSV, newline="", encoding="utf-8") as f:
        return {r["episode_id"]: r for r in csv.DictReader(f)}


def save_truth(truth: dict) -> None:
    """整文件重写(FSX 无追加);调用方持锁。"""
    os.makedirs(CAL_DIR, exist_ok=True)
    fields = ["episode_id", "human_label", "verdict", "note", "doubao_caption", "edited", "at"]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for eid in sorted(truth):
            w.writerow({k: truth[eid].get(k, "") for k in fields})


def clips_of(eid: str) -> list[str]:
    d = os.path.join(CLIP_SITE, "details", "audit_clips")
    try:
        return sorted(f for f in os.listdir(d) if f.startswith(eid + "__"))
    except OSError:
        return []


# ── 页面 ─────────────────────────────────────────────────────────────────────

_CSS = """
body{font-family:system-ui,sans-serif;margin:1.2rem;background:#fafafa;color:#222}
a{color:#c60;text-decoration:none} a:hover{text-decoration:underline}
h1{font-size:1.2rem;margin:.2rem 0}
.small{color:#888;font-size:.85rem}
.done{color:#16a34a;font-weight:700}
.todo{color:#d97706;font-weight:700}
.pager{display:flex;gap:8px;align-items:center;margin:10px 0;flex-wrap:wrap}
.pager a,.pager b{border:1px solid #fdba74;border-radius:8px;padding:4px 14px;background:#fff}
.pager b{background:#ea580c;color:#fff;border-color:#ea580c}
.batch{background:#16a34a;color:#fff;font-weight:700;border:none;border-radius:8px;
       padding:7px 20px;font-size:.95rem;cursor:pointer}
.batch:hover{background:#15803d}
.batchmsg{font-weight:700;color:#16a34a}
.card{background:#fff;border:1px solid #e5e5e5;border-radius:10px;
      padding:10px 14px;margin:14px 0;transition:background .3s}
.card.saved{border-left:6px solid #16a34a;background:#f0fdf4}
.card.todo-card{border-left:6px solid #d97706}
.card.miss{outline:3px solid #fca5a5}
.head{display:flex;gap:12px;align-items:baseline;margin-bottom:6px}
.head b{font-size:1.05rem}
.vids{display:flex;gap:8px;flex-wrap:wrap}
video{width:31%;min-width:250px;background:#000;border-radius:6px}
.editrow{display:flex;gap:10px;margin-top:8px;align-items:stretch}
textarea{flex:1;min-height:44px;font-size:1rem;padding:7px 10px;
         border:2px solid #fdba74;border-radius:8px;box-sizing:border-box}
.btn{background:#ea580c;color:#fff;font-weight:700;border:none;border-radius:8px;
     padding:0 22px;font-size:.95rem;cursor:pointer}
.btn:hover{background:#c2410c}
.vgroup{display:flex;gap:6px;align-items:center}
.vopt{display:flex;gap:5px;align-items:center;border:2px solid #ddd;border-radius:8px;
      padding:6px 12px;cursor:pointer;font-weight:700;background:#fff;white-space:nowrap}
.vopt input{accent-color:#ea580c;margin:0}
.vopt.v-ok{color:#16a34a;border-color:#bbf7d0}
.vopt.v-bad{color:#dc2626;border-color:#fecaca}
.vopt.v-meh{color:#6b7280;border-color:#e5e7eb}
.vopt:has(input:checked){box-shadow:0 0 0 2px currentColor inset;background:#fffbeb}
.note{width:100%;margin-top:6px;border:1px dashed #d4d4d4;border-radius:8px;
      padding:6px 10px;font-size:.9rem;box-sizing:border-box;color:#555}
.note:focus{border-style:solid;border-color:#fdba74;outline:none}
"""

# 就地保存(fetch,不刷新页面):单条「确定」当场变绿;「确认本页全部」串行
# 逐条提交,没选成败的红框跳过。页面零刷新 = 视频不重载、滚动位置不丢。
_JS = """
<script>
async function saveCard(ev, eid){
  ev.preventDefault();
  const fd = new FormData(document.getElementById('f-' + eid));
  if(!fd.get('verdict')){
    document.getElementById(eid).classList.add('miss');
    document.getElementById('st-' + eid).innerHTML =
      '<span class="todo">先选 成功/失败/看不清</span>';
    return false;
  }
  const r = await fetch('/save?ajax=1', {method: 'POST', body: fd});
  const j = await r.json();
  if(j.ok){
    const card = document.getElementById(eid);
    card.classList.remove('todo-card', 'miss');
    card.classList.add('saved');
    document.getElementById('st-' + eid).innerHTML =
      '<span class="done">✓ 已确认 · ' + j.verdict_cn + '</span>';
  }
  return false;
}
async function saveAll(){
  let saved = 0, skipped = 0;
  for(const f of document.querySelectorAll('form.editrow')){
    const eid = f.dataset.eid;
    const fd = new FormData(f);
    if(!fd.get('verdict')){
      skipped++; document.getElementById(eid).classList.add('miss'); continue;
    }
    const r = await fetch('/save?ajax=1', {method: 'POST', body: fd});
    const j = await r.json();
    if(j.ok){
      const card = document.getElementById(eid);
      card.classList.remove('todo-card', 'miss');
      card.classList.add('saved');
      document.getElementById('st-' + eid).innerHTML =
        '<span class="done">✓ 已确认 · ' + j.verdict_cn + '</span>';
      saved++;
    }
  }
  document.querySelectorAll('.batchmsg').forEach(e => e.textContent =
    '本页已保存 ' + saved + ' 条' +
    (skipped ? ',跳过 ' + skipped + ' 条(未选成败,红框标出)' : ''));
}
</script>
"""

PAGE_SIZE = 25
_VD_CN = {"success": "成功", "fail": "失败", "unclear": "看不清"}


def build_app(rows: list[dict], caps: dict):
    from fastapi import FastAPI, Form
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles

    order = [r["episode_id"] for r in rows]
    truth = load_truth()
    n_pages = (len(order) + PAGE_SIZE - 1) // PAGE_SIZE
    app = FastAPI()
    app.mount("/clips", StaticFiles(directory=os.path.join(
        CLIP_SITE, "details", "audit_clips")), name="clips")

    def _pager(p: int) -> str:
        parts = []
        for k in range(1, n_pages + 1):
            lo, hi = (k - 1) * PAGE_SIZE, min(k * PAGE_SIZE, len(order))
            n_done = sum(1 for e in order[lo:hi] if e in truth)
            tag = f"第{k}页({n_done}/{hi - lo})"
            parts.append(f"<b>{tag}</b>" if k == p else f'<a href="/?p={k}">{tag}</a>')
        parts.append('<button class="batch" onclick="saveAll()">'
                     "✓✓ 确认本页全部</button>")
        parts.append('<span class="batchmsg"></span>')
        return '<div class="pager">' + "".join(parts) + "</div>"

    @app.get("/", response_class=HTMLResponse)
    def index(p: int = 1):
        p = max(1, min(n_pages, p))
        lo, hi = (p - 1) * PAGE_SIZE, min(p * PAGE_SIZE, len(order))
        n_done = sum(1 for e in order if e in truth)
        cards = []
        for i in range(lo, hi):
            eid = order[i]
            t = truth.get(eid)
            text = (t["human_label"] if t else caps.get(eid, "")).strip()
            vd_cn = _VD_CN.get((t or {}).get("verdict", ""), "")
            st = ((f'<span class="done">✓ 已确认 · {vd_cn}</span>' if vd_cn
                   else '<span class="done">✓ 已确认</span>') if t
                  else '<span class="todo">待定标</span>')
            vids = "\n".join(
                f'<video controls preload="metadata" src="/clips/{f}"></video>'
                for f in clips_of(eid)) or '<p class="small">(无切片)</p>'
            radios = "".join(
                f'<label class="vopt {c}"><input type="radio" name="verdict" '
                f'value="{v}"{" checked" if (t or {}).get("verdict") == v else ""}>'
                f"{cn}</label>"
                for v, cn, c in (("success", "成功", "v-ok"),
                                 ("fail", "失败", "v-bad"),
                                 ("unclear", "看不清", "v-meh")))
            cards.append(
                f'<div class="card {"saved" if t else "todo-card"}" id="{eid}">'
                f'<div class="head"><b>{eid}</b>'
                f'<span class="small">#{i + 1}/{len(order)}</span>'
                f'<span id="st-{eid}">{st}</span></div>'
                f'<div class="vids">{vids}</div>'
                f'<form class="editrow" id="f-{eid}" data-eid="{eid}" '
                f'onsubmit="return saveCard(event, \'{eid}\')">'
                f'<input type="hidden" name="eid" value="{eid}">'
                f'<textarea name="label" placeholder="看视频后填写/修正标注…">'
                f"{html.escape(text)}</textarea>"
                f'<div class="vgroup">{radios}</div>'
                f'<button class="btn" type="submit">✓ 确定</button></form>'
                f'<input class="note" name="note" form="f-{eid}" '
                f'placeholder="备注(选填):边界依据/恢复经过/看不清原因/数据异常…" '
                f'value="{html.escape((t or {}).get("note", ""))}"></div>')
        return (f"<style>{_CSS}</style><title>未打标定标台 · 第{p}页</title>{_JS}"
                f"<h1>droid-200 未打标数据 · 人工定标 "
                f'<span class="small">共 {len(order)} 条,已确认 '
                f'<b class="done">{n_done}</b></span></h1>'
                f'<p class="small">每条:看三路视频 → 核对/修改草稿 → 选成败 → '
                f"点「确定」(就地保存变绿,页面不刷新);或整页选完点"
                f"「✓✓ 确认本页全部」。真值落 {OUT_CSV}</p>"
                + _pager(p) + "".join(cards) + _pager(p))

    @app.get("/ep/{eid}")
    def ep_redirect(eid: str):
        if eid in order:
            p = order.index(eid) // PAGE_SIZE + 1
            return RedirectResponse(f"/?p={p}#{eid}", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.post("/save")
    def save(eid: str = Form(...), label: str = Form(...),
             verdict: str = Form(""), note: str = Form("")):
        label = label.strip()
        if eid in order and label and verdict in _VD_CN:
            with _LOCK:
                truth[eid] = {
                    "episode_id": eid, "human_label": label, "verdict": verdict,
                    "note": note.strip(),
                    "doubao_caption": caps.get(eid, ""),
                    "edited": str(label != caps.get(eid, "").strip()).lower(),
                    "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                save_truth(truth)
            return JSONResponse({"ok": True, "eid": eid,
                                 "verdict_cn": _VD_CN[verdict]})
        return JSONResponse({"ok": False, "eid": eid})

    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regen-captions", action="store_true",
                    help="忽略缓存重新生成全部 caption")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    rows = find_unlabeled()
    print(f"[workbench] droid-200 未打标 episode:{len(rows)} 条", flush=True)
    caps = ensure_captions(rows, regen=args.regen_captions)
    app = build_app(rows, caps)
    import uvicorn
    print(f"[workbench] 定标台就绪 → http://127.0.0.1:{args.port}/", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
