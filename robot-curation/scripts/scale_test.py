#!/usr/bin/env python
"""P5.2 规模流式压测:分块过第一遍漏斗,记录 RSS 曲线 + 吞吐(报价/资源估算用)。

验收:RSS 预热后平稳不涨(流式+指针设计的证明);吞吐 episode/min 落盘。
用法: /data03/hao/venv/curation/bin/python scripts/scale_test.py \
        --input /data03/hao/data/droid_lerobot --episodes 10000 [--batch-size 500]
"""
from __future__ import annotations

import argparse
import faulthandler
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HF_HOME", "/data03/hao/.hf_home")


def rss_mb() -> float:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024.0
    return -1.0


def main() -> None:
    faulthandler.enable()
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--episodes", type=int, default=10000)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--embodiment", default=None)
    ap.add_argument("--isolate", action="store_true",
                    help="按批子进程隔离(原生崩溃只损失一批;10k 正式压测用)")
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument("--out", default="/data03/hao/deliveries/scale_test")
    args = ap.parse_args()

    from curation.ingest.lerobot_reader import iter_lerobot_batches, rows_to_daft
    from curation.pipeline.config import load_config
    from curation.pipeline.funnel import run_funnel
    from curation.registry.registry import EmbodimentRegistry

    cfg = load_config()
    cfg["checks"]["task_success"]["enable"] = False      # 第一遍压测不跑 VLM
    reg = EmbodimentRegistry()
    os.makedirs(args.out, exist_ok=True)

    curve, agg = [], {"input": 0, "output": 0}
    t0 = time.time()
    if args.isolate:
        import subprocess

        from curation.pipeline.isolation import run_isolated

        t0 = time.time()

        def runner(s, n):
            """一个批 = 一个子进程;崩溃(无报告文件)→ None。"""
            seg_out = f"{args.out}/seg_{s:06d}_{n}"
            cmd = [sys.executable, os.path.abspath(__file__),
                   "--input", args.input, "--start", str(s),
                   "--episodes", str(n), "--batch-size", str(n), "--out", seg_out]
            if args.embodiment:
                cmd += ["--embodiment", args.embodiment]
            r = subprocess.run(cmd, capture_output=True, text=True)
            try:
                seg = json.load(open(f"{seg_out}/scale_report.json"))
                pt = seg["curve"][0]
                pt["episodes"] = seg["result"]["episodes"]
                pt["survivors"] = seg["result"]["survivors"]
                print(f"[{s}+{n}] 子进程RSS={pt['rss_mb']}MB "
                      f"吞吐={pt['eps_per_min']}eps/min", flush=True)
                return pt
            except Exception:
                print(f"[{s}+{n}] 批崩溃 rc={r.returncode} → 进清算(重试+对折)", flush=True)
                return None

        spans = []
        s, end_ep = args.start, args.start + args.episodes
        while s < end_ep:
            spans.append((s, min(args.batch_size, end_ep - s)))
            s += args.batch_size
        results, unrecoverable = run_isolated(spans, runner, min_split=1)

        total_s = time.time() - t0
        agg = {"input": sum(r["episodes"] for r in results),
               "output": sum(r["survivors"] for r in results)}
        warm = [p["rss_mb"] for p in results[1:]] or [0]
        result = {"dataset": args.input, "episodes": agg["input"],
                  "survivors": agg["output"],
                  "unrecoverable": unrecoverable,
                  "total_min": round(total_s / 60, 1),
                  "throughput_eps_per_min": round(60 * agg["input"] / max(total_s, 1e-9), 1),
                  "rss_warm_min_mb": min(warm), "rss_warm_max_mb": max(warm),
                  "mode": "per-batch subprocess isolation + retry/bisect salvage",
                  "memory_note": "每批独立进程,RSS 恒定性由各批 RSS 同量级证明"}
        json.dump({"result": result, "curve": results},
                  open(f"{args.out}/scale_report.json", "w"), ensure_ascii=False, indent=1)
        print("RESULT:", json.dumps(result, ensure_ascii=False), flush=True)
        return

    from curation.ingest.lerobot_reader import read_lerobot_rows

    def batches():
        s = args.start
        end = args.start + args.episodes
        while s < end:
            rs = read_lerobot_rows(args.input, max_episodes=min(args.batch_size, end - s),
                                   start_episode=s, skip_missing=True,
                                   embodiment_id=args.embodiment)
            if not rs:
                return
            yield s, rs
            s += args.batch_size

    for start, rows in batches():
        rows = [r for r in rows if all(os.path.exists(v["path"]) for v in r["video"].values())]
        tb = time.time()
        _, stats = run_funnel(rows_to_daft(rows), cfg, reg)
        agg["input"] += stats["input"]
        agg["output"] += stats["output"]
        n_rows = len(rows)
        point = {"start": start, "n": n_rows, "rss_mb": round(rss_mb(), 1),
                 "batch_s": round(time.time() - tb, 1),
                 "eps_per_min": round(60 * n_rows / max(time.time() - tb, 1e-9), 1)}
        curve.append(point)
        print(f"[{start + n_rows}/{args.episodes}] RSS={point['rss_mb']}MB "
              f"吞吐={point['eps_per_min']}eps/min", flush=True)
        json.dump({"curve": curve, "agg": agg},
                  open(f"{args.out}/scale_curve.json", "w"))

    total_s = time.time() - t0
    warm = [p["rss_mb"] for p in curve[1:]] or [0]
    result = {
        "dataset": args.input, "episodes": agg["input"], "survivors": agg["output"],
        "total_min": round(total_s / 60, 1),
        "throughput_eps_per_min": round(60 * agg["input"] / total_s, 1),
        "rss_first_batch_mb": curve[0]["rss_mb"] if curve else None,
        "rss_warm_min_mb": min(warm), "rss_warm_max_mb": max(warm),
        "rss_growth_pct": round(100 * (warm[-1] - warm[0]) / max(warm[0], 1), 1)
        if len(warm) > 1 else 0.0,
        "memory_verdict": "恒定" if len(warm) > 1
        and (warm[-1] - warm[0]) / max(warm[0], 1) < 0.2 else "增长需查",
    }
    json.dump({"result": result, "curve": curve}, open(f"{args.out}/scale_report.json", "w"),
              ensure_ascii=False, indent=1)
    print("RESULT:", json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
