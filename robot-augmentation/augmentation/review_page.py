"""生成本地评审页(file:// 直接开):每个增广版本一行,原视频 | 生成 | 半透明叠加 三格同步播放,顶部放并排对比视频。

    python3 -m augmentation.review_page --dir <评审目录> [--out <评审目录>/review.html]

评审目录约定(collect 会照此摆放):
  original.mp4                 源视频
  <name>.mp4 / <name>_triptych.mp4 / <name>_run.json   每个版本的生成视频、三格对照、运行记录
  *_compare.mp4                并排对比视频(compare 生成)
  contact_sheet.png            可选的对照拼图
"""
from __future__ import annotations

import argparse
import html
import json
import os


def _metrics_line(run: dict) -> str:
    m = run.get("metrics") or {}
    c = run.get("cost") or {}
    parts = [f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}" for k, v in m.items()
             if k in ("flow_r", "flow_lag_frames", "motion_iou_mean", "joint_r_orig", "joint_r_gen")]
    if c.get("yuan") is not None:
        parts.append(f"cost={c['yuan']}元/{c.get('draws_total')}抽")
    return " · ".join(parts)


def build(d: str, out: str) -> int:
    names = sorted(f[: -len("_run.json")] for f in os.listdir(d) if f.endswith("_run.json"))
    runs = {n: json.load(open(os.path.join(d, f"{n}_run.json"))) for n in names}
    rel = lambda f: os.path.relpath(os.path.join(d, f), os.path.dirname(os.path.abspath(out)))
    compares = sorted(f for f in os.listdir(d) if f.endswith("_compare.mp4"))
    cmp_rows = "".join(f"""
<section class="row"><h2>{html.escape(f[:-len('_compare.mp4')])} <small>并排对比,各格标签见画面左上角</small></h2>
<video src="{rel(f)}" muted controls playsinline preload="metadata" style="width:100%;max-width:1440px"></video></section>""" for f in compares)
    rows = []
    for n in names:
        if not os.path.exists(os.path.join(d, f"{n}.mp4")):
            continue
        tri = f"{n}_triptych.mp4"
        tri_fig = (f"""<figure><video src="{rel(tri)}" muted controls playsinline preload="metadata" class="wide"></video><figcaption>原 | 生成 | 叠加</figcaption></figure>"""
                   if os.path.exists(os.path.join(d, tri)) else "")
        rows.append(f"""
<section class="row">
  <h2>{html.escape(n)} <small>{html.escape(_metrics_line(runs[n]))}</small></h2>
  <div class="vids">
    <figure><video src="{rel('original.mp4')}" muted controls playsinline preload="metadata"></video><figcaption>原视频</figcaption></figure>
    <figure><video src="{rel(n + '.mp4')}" muted controls playsinline preload="metadata"></video><figcaption>生成</figcaption></figure>
    {tri_fig}
  </div>
</section>""")
    sheet = ('<details style="margin-top:24px"><summary>对照拼图</summary><img src="%s" style="max-width:100%%"></details>'
             % rel("contact_sheet.png")) if os.path.exists(os.path.join(d, "contact_sheet.png")) else ""
    doc = f"""<!doctype html><meta charset="utf-8"><title>增广评审 · {html.escape(os.path.basename(os.path.abspath(d)))}</title>
<style>
body{{font:14px -apple-system,system-ui,sans-serif;margin:20px 28px;color:#1d2129}}
h1{{font-size:18px}} h2{{font-size:15px;margin:18px 0 6px}} h2 small{{font-weight:400;color:#86909c;margin-left:10px}}
.bar{{position:sticky;top:0;background:#fff;padding:8px 0;border-bottom:1px solid #e5e6eb;z-index:2}}
button{{background:#165DFF;color:#fff;border:0;border-radius:2px;padding:6px 14px;margin-right:8px;cursor:pointer}}
button.ghost{{background:#f2f3f5;color:#1d2129}}
.vids{{display:flex;gap:10px;align-items:flex-start}} figure{{margin:0}} video{{width:320px;display:block;background:#000}} video.wide{{width:960px}}
figcaption{{color:#86909c;font-size:12px;margin-top:4px}}
input[type=range]{{width:420px;vertical-align:middle}}
</style>
<div class="bar">
<h1>增广评审 · {html.escape(os.path.basename(os.path.abspath(d)))}</h1>
<button onclick="all(v=>{{v.currentTime=0;v.play()}})">全部从头播放</button>
<button class="ghost" onclick="all(v=>v.pause())">暂停</button>
<button class="ghost" onclick="step(-1)">← 上一帧</button>
<button class="ghost" onclick="step(1)">下一帧 →</button>
<label>拖动定位 <input type="range" id="seek" min="0" max="60" step="0.0333" value="0" oninput="all(v=>{{v.pause();v.currentTime=+this.value}})"></label>
<span id="t"></span>
<div style="color:#86909c;margin-top:6px">每格视频自带播放控件;「全部从头播放」让所有版本同步播;叠加格里机械臂出现重影 = 动作漂了。</div>
</div>
{('<h1 style="font-size:16px;margin-top:18px">对比视频</h1>' + cmp_rows) if compares else ''}
<h1 style="font-size:16px;margin-top:24px">各版本</h1>
{''.join(rows)}
{sheet}
<script>
const vs=[...document.querySelectorAll('video')];
function all(f){{vs.forEach(f)}}
function step(k){{all(v=>{{v.pause();v.currentTime=Math.max(0,v.currentTime+k/30)}})}}
vs[0]&&vs[0].addEventListener('loadedmetadata',()=>{{document.getElementById('seek').max=vs[0].duration.toFixed(2)}});
setInterval(()=>{{if(vs[0]){{document.getElementById('t').textContent=' t='+vs[0].currentTime.toFixed(2)+'s 帧≈'+Math.round(vs[0].currentTime*30);}}}},100);
</script>"""
    open(out, "w").write(doc)
    return len(rows)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True)
    p.add_argument("--out")
    a = p.parse_args(argv)
    out = a.out or os.path.join(a.dir, "review.html")
    n = build(a.dir, out)
    print(out, n, "versions")


if __name__ == "__main__":
    main()
