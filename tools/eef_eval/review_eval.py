"""F5.10: how often the VLM review agrees with the truth (design 12 D-E14).

For every episode of a DEMO dataset: the module's CPU part with the ``demo`` profile, then the review's
windows (one point P and at most one axis A each) sent to a model, every call recorded on a tape so
the run can be replayed offline. Only after the model answered does this script read the truth
(``evaluation/truth_pixels``) and grade each answer:

* position - the declared P against the true P over the window's frames: median distance
  <= ``--support-px`` is "support", >= ``--refute-px`` is "refute", in between is not graded;
* tracking - the tracked P (green cross) against the true P, the same two limits;
* orientation - the declared A against the true A (both ends need true pixels, e.g. the finger line):
  median angle <= ``--support-deg`` is "support", >= ``--refute-deg`` is "refute".

Reported per question and per fault kind: graded windows, the model's decisive share (support /
refute, the rest is uncertain or not observable), agreement among decisive answers and the confusion
counts. A real backend::

    PYTHONPATH=backend:tools .venv/bin/python -m eef_eval.review_eval --dataset dataset2 \\
        --endpoint https://ark.cn-beijing.volces.com/api/v3 --model <model> --api-key-env ARK_API_KEY

``--replay reports/review_eval_dataset2.tape.jsonl.gz`` answers from a recorded tape instead;
``--stand-in`` uses the parity fake model (checks the plumbing, not the model).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import numpy as np

from . import truth

QUESTIONS = ("position", "tracking", "orientation")
FIELD = {"position": "position_support", "tracking": "tracking_target_correct", "orientation": "orientation_support"}


def _hooks(mode: str, tape: pathlib.Path, stand_in: bool):
    from parity import vlm_tape as T

    from curation.adapters import vlm_client

    if mode == "replay":
        _, entries = T.read_tape(str(tape))
        hooks = T.TapeHooks("replay", replay_entries=entries)
    else:
        transport = None
        if stand_in:
            from parity.fakevlm import FakeVlm

            transport = FakeVlm("stand-in").transport()
        hooks = T.TapeHooks("record", tape_out=str(tape), transport=transport, tape_meta={"label": "review_eval"})
    hooks.install(vlm_client)
    return hooks


def run(dataset: str, out: pathlib.Path, vlm: dict, episodes: list[int] | None, per_camera: int,
        frames_per_window: int) -> list[dict]:
    """CPU + review per episode; returns ``[{episode_index, sample, detail, review}]`` (no truth yet)."""
    from curation.adapters.vlm_client import SharedGate
    from curation.cli.eef_review import make_asker, review_episode
    from curation.extensions.eef_consistency import load, profile, runner
    from curation.extensions.eef_consistency import review as R
    from curation.pipeline.records import module_dir

    r = load.load_bundle(truth.dataset_dir(dataset) / "trajectory.json", lerobot_root=truth.lerobot_root(dataset),
                         episodes=episodes)
    assert r.ok, r.errors[:3]
    run_dir = str(out / "run" / f"review_eval_{dataset}")
    cfg = runner.RunConfig(lerobot_root=str(truth.lerobot_root(dataset)), seed_root=str(truth.seed_root(dataset)),
                           profile=profile.load("demo"), out_dir=module_dir(run_dir, "eef_video_consistency"),
                           evidence_mode="off")
    ask = make_asker(vlm, float(vlm.get("timeout_s", 120.0)), SharedGate(1))
    rows = []
    for ep, s in sorted(r.samples.items()):
        t0 = time.perf_counter()
        detail, _ = runner.run_episode(s, cfg)
        review = review_episode(s, {"details": detail, "verdict": "abstain"}, run_dir=run_dir,
                                media_root=str(truth.lerobot_root(dataset)), ask=ask, cache=R.Cache(None),
                                model=str(vlm["model"]), per_camera=per_camera, frames_per_window=frames_per_window,
                                out_dir=module_dir(run_dir, "eef_video_review"))
        rows.append({"episode_index": ep, "sample": s, "detail": detail, "review": review})
        n = sum(len(c.get("windows") or []) for c in review["cameras"].values())
        print(f"ep{ep}: {n} windows, {round(time.perf_counter() - t0, 1)} s", file=sys.stderr, flush=True)
    return rows


# ----------------------------------------------------------------------------------- truth (read after)


def _median_dist(a: np.ndarray, b: np.ndarray, frames: list[int]) -> float | None:
    idx = [f for f in frames if np.isfinite(a[f]).all() and np.isfinite(b[f]).all()]
    return float(np.median(np.linalg.norm(a[idx] - b[idx], axis=1))) if idx else None


def _median_angle(a0, a1, b0, b1, frames: list[int], directed: bool) -> float | None:
    out = []
    for f in frames:
        u, v = a1[f] - a0[f], b1[f] - b0[f]
        if not (np.isfinite(u).all() and np.isfinite(v).all()) or np.linalg.norm(u) < 3 or np.linalg.norm(v) < 3:
            continue
        c = float(np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v)))
        ang = float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))
        out.append(ang if directed else min(ang, 180.0 - ang))
    return float(np.median(out)) if out else None


def _grade(value: float | None, support: float, refute: float) -> str | None:
    if value is None:
        return None
    return "support" if value <= support else "refute" if value >= refute else None


def grade(dataset: str, rows: list[dict], a: argparse.Namespace) -> list[dict]:
    """One line per answered window: the model's answer and the truth's, per question."""
    from curation.extensions.eef_consistency import load

    faults = truth.faults(dataset)
    out = []
    for row in rows:
        s, ep = row["sample"], row["episode_index"]
        for cid, cam in row["review"]["cameras"].items():
            try:
                tp = truth.truth_pixels(dataset, s.sample_id, cid, s.n_frames)
            except FileNotFoundError:
                continue
            for w in cam.get("windows") or []:
                if w["status"] != "answered":
                    continue
                ans, frames, pid, aid = w["answer"], w["frames"], w.get("point_id"), w.get("axis_id")
                decl = load.declared_track(s, cid, pid).uv if pid else None
                truth_p = tp.get(pid)
                obs = None
                curves = pathlib.Path(a.out) / "run" / f"review_eval_{dataset}" / "checks" / "eef_video_consistency" / \
                    "curves" / f"{ep:06d}" / f"{cid}.parquet"
                if pid and curves.is_file():
                    import pandas as pd

                    df = pd.read_parquet(curves)
                    if f"obs_u:{pid}" in df:
                        obs = np.stack([df[f"obs_u:{pid}"].to_numpy(float), df[f"obs_v:{pid}"].to_numpy(float)], 1)
                g = {"position": None, "tracking": None, "orientation": None}
                if decl is not None and truth_p is not None:
                    g["position"] = _grade(_median_dist(decl, truth_p, frames), a.support_px, a.refute_px)
                if obs is not None and truth_p is not None:
                    g["tracking"] = _grade(_median_dist(obs, truth_p, frames), a.support_px, a.refute_px)
                ax = (s.axes or {}).get(aid) if aid else None
                if ax and ax["start_point_id"] in tp and ax["end_point_id"] in tp:
                    d0, d1 = (load.declared_track(s, cid, ax[k]) for k in ("start_point_id", "end_point_id"))
                    if d0 is not None and d1 is not None:
                        g["orientation"] = _grade(_median_angle(d0.uv, d1.uv, tp[ax["start_point_id"]],
                                                                tp[ax["end_point_id"]], frames, bool(ax.get("directed"))),
                                                  a.support_deg, a.refute_deg)
                out.append({"episode_index": ep, "fault": faults[ep]["kind"], "camera_id": cid, "kind": w["kind"],
                            "frames": frames, "point_id": pid, "axis_id": aid, "truth": g,
                            "model": {q: ans[FIELD[q]] for q in QUESTIONS}, "explanation": ans.get("explanation")})
    return out


def summarize(lines: list[dict]) -> dict:
    def block(sel):
        res = {}
        for q in QUESTIONS:
            graded = [x for x in sel if x["truth"][q] is not None]
            decisive = [x for x in graded if x["model"][q] in ("support", "refute")]
            agree = [x for x in decisive if x["model"][q] == x["truth"][q]]
            conf = {f"truth_{t}/model_{m}": sum(1 for x in graded if x["truth"][q] == t and x["model"][q] == m)
                    for t in ("support", "refute") for m in ("support", "refute", "uncertain", "not_observable")}
            res[q] = {"graded": len(graded), "decisive": len(decisive),
                      "decisive_share": round(len(decisive) / len(graded), 3) if graded else None,
                      "agreement": round(len(agree) / len(decisive), 3) if decisive else None,
                      "confusion": {k: v for k, v in conf.items() if v}}
        return res

    faults = sorted({x["fault"] for x in lines})
    return {"windows": len(lines), "overall": block(lines), "by_fault": {f: block([x for x in lines if x["fault"] == f])
                                                                         for f in faults}}


def markdown(rep: dict) -> str:
    names = {"position": "位置（红圈是否在 P 上）", "tracking": "跟踪（绿十字是否在 P 上）", "orientation": "朝向（箭头 A 的方向）"}
    lines = [f"# EEF 复核 · 模型对照真值（F5.10）", "",
             f"数据集 `{rep['dataset']}`，模型 `{rep['model']}`，窗口 {rep['summary']['windows']} 个（有答复的）。"
             f"真值判定：距离 ≤ {rep['limits']['support_px']} px 算「在」、≥ {rep['limits']['refute_px']} px 算「不在」；"
             f"角度 ≤ {rep['limits']['support_deg']}° 算「对」、≥ {rep['limits']['refute_deg']}° 算「不对」，之间不计。", "",
             "| 问题 | 可评窗口 | 模型给出明确结论的比例 | 明确结论与真值一致率 |", "|---|---|---|---|"]
    for q in QUESTIONS:
        b = rep["summary"]["overall"][q]
        lines.append(f"| {names[q]} | {b['graded']} | {b['decisive_share']} | {b['agreement']} |")
    lines += ["", "按故障类型（位置 / 跟踪 / 朝向的一致率）：", ""]
    for f, blk in rep["summary"]["by_fault"].items():
        lines.append(f"- {f}：" + " / ".join(f"{blk[q]['agreement']}（{blk[q]['decisive']}/{blk[q]['graded']}）"
                                           for q in QUESTIONS))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="dataset2")
    ap.add_argument("--out", default="tools/eef_eval/reports")
    ap.add_argument("--episodes", type=int, nargs="*", default=None)
    ap.add_argument("--endpoint", default="http://stand-in.invalid/v1")
    ap.add_argument("--model", default="stand-in")
    ap.add_argument("--api-key-env", default=None)
    ap.add_argument("--timeout-s", type=float, default=120.0)
    ap.add_argument("--windows-per-camera", type=int, default=3)
    ap.add_argument("--frames-per-window", type=int, default=6)
    ap.add_argument("--support-px", type=float, default=5.0)
    ap.add_argument("--refute-px", type=float, default=12.0)
    ap.add_argument("--support-deg", type=float, default=5.0)
    ap.add_argument("--refute-deg", type=float, default=15.0)
    ap.add_argument("--replay", default=None, help="answer from this tape instead of the model")
    ap.add_argument("--stand-in", action="store_true", help="the parity fake model (plumbing check)")
    a = ap.parse_args(argv)
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = f"{a.dataset}{'_stand_in' if a.stand_in else ''}"
    tape = pathlib.Path(a.replay) if a.replay else out / f"review_eval_{tag}.tape.jsonl.gz"
    hooks = _hooks("replay" if a.replay else "record", tape, a.stand_in)
    try:
        rows = run(a.dataset, out, {"endpoint": a.endpoint, "model": a.model, "api_key_env": a.api_key_env,
                                    "timeout_s": a.timeout_s}, a.episodes, a.windows_per_camera, a.frames_per_window)
    finally:
        hooks.uninstall()
    lines = grade(a.dataset, rows, a)                # the truth, read after every answer is in
    rep = {"schema_version": "eef-review-eval/1.0", "dataset": a.dataset, "model": a.model,
           "tape": str(tape), "limits": {k: getattr(a, k) for k in ("support_px", "refute_px", "support_deg",
                                                                    "refute_deg")},
           "summary": summarize(lines), "windows": lines}
    (out / f"review_eval_{tag}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str) + "\n")
    (out / f"review_eval_{tag}.md").write_text(markdown(rep), encoding="utf-8")
    print(json.dumps(rep["summary"]["overall"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
