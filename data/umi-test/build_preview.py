"""Build a static page for the marked MP4s written by render_own_hand.py."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil


HTML = """<!doctype html>
<html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>UMI 本手轨迹预览</title>
<style>
body{font:16px system-ui;background:#f6f8fb;color:#152235;max-width:1350px;margin:32px auto;padding:0 24px}
h1{font-size:26px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,400px),1fr));gap:18px}
article{background:white;padding:18px;border-radius:12px;margin:16px 0}video{width:100%;background:#111}
button{padding:8px 18px;margin:8px 8px 8px 0}p{line-height:1.6}
</style>
<h1>UMI 本手轨迹预览</h1><p id="description"></p>
<p>每路仅叠加本手。圈表示当前夹爪中心，短轴表示朝向，横杆表示开口；历史轨迹投影到当前帧的相机。</p>
<button id="play">同时播放</button><button id="pause">暂停</button><div id="videos" class="grid"></div>
<script>
fetch('results.json',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error(r.status);return r.json()}).then(data=>{
  document.querySelector('#description').textContent =
    `Episode ${data.episode_index} · 历史窗口：过去 ${data.horizon_s} 秒到当前帧`;
  for(const item of data.videos){
    const a=document.createElement('article'), h=document.createElement('h3'), v=document.createElement('video');
    h.textContent=`${item.camera} · ${item.hand}`;
    v.controls=true;v.preload='metadata';v.src=item.file;
    a.append(h,v);document.querySelector('#videos').append(a);
  }
}).catch(e=>{document.querySelector('#description').textContent=`预览加载失败：${e.message}`});
document.querySelector('#play').onclick=()=>document.querySelectorAll('#videos video').forEach(v=>v.play().catch(()=>{}));
document.querySelector('#pause').onclick=()=>document.querySelectorAll('#videos video').forEach(v=>v.pause());
</script></html>
"""


def main() -> None:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=root / "history-hand", help="Directory containing manifest.json")
    parser.add_argument("--out", type=Path, default=root.parents[1] / "frontend/dist/umi-preview")
    args = parser.parse_args()
    manifest = json.loads((args.input / "manifest.json").read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    for item in manifest["videos"]:
        filename = item["file"]
        if Path(filename).name != filename or Path(filename).suffix != ".mp4":
            parser.error(f"Expected an MP4 filename in manifest: {filename}")
        source, target = args.input / filename, args.out / filename
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
    (args.out / "results.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "index.html").write_text(HTML, encoding="utf-8")
    print(f"Preview: {args.out / 'index.html'}")
    print(f"Serve with: python -m http.server 8081 --directory {args.out}")


if __name__ == "__main__":
    main()
