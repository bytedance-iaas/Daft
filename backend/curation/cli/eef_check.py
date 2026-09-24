"""``check --modules eef_video_consistency`` - the EEF-video consistency module (design doc 12, D49).

A vlm-tier hard gate on the funnel's survivors (``input_scope=funnel``). For every episode: the CPU
measures (``runner.run_episode``: sub-item statuses, coverage, segments, diagnosis; observations, curves
and evidence under ``checks/eef_video_consistency/``), the model reviews the windows (``eef_review``,
one point and at most one axis per request) and ``decide.py`` gives the verdict (appendix C.9): ``pass``
(``passed=true``), ``reject`` (``passed=false``, the reason in ``details.reason``) or ``human``
(``passed=null``: an adjudication card). An episode the file does not declare goes to a person; an
episode the CPU fails on gets an error line (held, D33); a model that fails on an episode leaves the CPU
without a second opinion. The whole call needs a VLM backend (probed first). A remote dataset's media
are streamed into a temporary directory; ``--resume`` redoes a line made with another trajectory.json,
other seeds or template, another configuration or another model, and ``input_digest`` covers them.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time

from .errors import ModuleFailed, UsageError

MODULE = "eef_video_consistency"
MOUNTS = {"fixed_external_and_wrist": ("fixed_external", "wrist"), "fixed_external": ("fixed_external",)}


def _unsupported_detail(episode: int, reason: str) -> dict:
    from ..extensions.eef_consistency import contracts as C

    subitems = {k: {"status": C.UNSUPPORTED, "reasons": [reason]} for k in C.SUBITEMS if k != C.VLM_REVIEW}
    return {"schema_version": C.DETAIL_SCHEMA_VERSION, "module_version": C.MODULE_VERSION,
            "assessment_mode": "verdict", "episode_index": episode, "overall": "not_assessable",
            "reasons": [reason], "summary": {k: {"status": v["status"], "cameras_assessable": 0,
                                                 "cameras": 0, "suspect_cameras": []} for k, v in subitems.items()},
            "cameras": {}, "segments": [], "diagnosis": [], "evidence": []}


FETCH_CHUNK = 8 * 1024 * 1024


def _fetch_media(storage, sample, root: str) -> None:
    """A remote dataset's media for one sample into ``root`` (kept for the call: a LeRobot v3 file holds
    many episodes). Streamed in ranges, written to a temporary name and renamed when complete."""
    for cam in sample.cameras.values():
        key = cam.media["uri"]
        dest = os.path.join(root, *key.split("/"))
        if os.path.isfile(dest):
            continue
        info = storage.stat(key)
        if info is None:
            raise FileNotFoundError(f"{storage.uri}/{key}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".partial"
        with open(part, "wb") as fh:
            done = 0
            while done < info.size:
                chunk = storage.read_range(key, done, min(FETCH_CHUNK, info.size - done))
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
        os.replace(part, dest)


class EefJudge:
    """The EEF module's per-episode judge inside the vlm stage (``StageRun``, D49): built once per
    call (file, template, configuration), opened inside the call's VLM session (model, asker, cache),
    then ``judge(ep, log)`` gives the episode's ``{passed, score, detail}`` and evidence. Thread-safe:
    the stage judges several episodes at once."""

    module = MODULE

    def __init__(self, ctx, args, run_dir: str, storage, vcfg: dict, gates: dict):
        from ..contracts import modules as registry
        from ..extensions.eef_consistency import load, profile, runner
        from ..extensions.eef_consistency import template as TP
        from ..extensions.eef_consistency.preflight import seed_dir, template_path
        from ..pipeline.records import module_dir
        from . import eef_review, modparams

        params = modparams.with_defaults(MODULE, modparams.parse(getattr(args, "param", None)).get(MODULE))
        try:
            registry.validate_params(MODULE, params)
        except Exception as e:  # noqa: BLE001 - jsonschema's message names the field
            raise UsageError(f"{MODULE}: {getattr(e, 'message', e)} "
                             f"(pass --param {MODULE}.trajectory_json=PATH)") from None
        traj = os.path.expanduser(params["trajectory_json"])
        if not os.path.isfile(traj):
            raise UsageError(f"{MODULE}: trajectory.json not found: {traj}")
        media_exists = (lambda key: storage.stat(key) is not None) if storage.remote else None
        # every declared episode: a stage worker reuses the judge for the batches and the stream to come
        self.result = load.load_bundle(traj, lerobot_root=None if storage.remote else storage.root,
                                       media_exists=media_exists)
        if not self.result.ok:
            first = self.result.errors[0]
            raise ModuleFailed(f"{MODULE}: trajectory.json is invalid: {first.message}",
                               {"errors": [i.as_dict() for i in self.result.errors[:10]],
                                "sha256": self.result.sha256})
        tpath = template_path(params)
        template = None
        if tpath:
            try:
                template = TP.load_template(tpath)
            except (TP.TemplateError, OSError) as e:
                raise ModuleFailed(f"{MODULE}: gripper template is invalid: {e}", {"path": tpath}) from None
        self.template_sha = template.sha256 if template is not None else None
        self.ctx, self.run_dir, self.storage, self.params = ctx, run_dir, storage, params
        self.out_dir = module_dir(run_dir, MODULE)
        self.scratch = tempfile.TemporaryDirectory(prefix="eef-media-") if storage.remote else None
        self.media_root = self.scratch.name if self.scratch else storage.root
        lag = float(params["lag_search_s"])
        self.cfg = runner.RunConfig(
            lerobot_root=self.media_root, seed_root=seed_dir(params), profile=profile.load(params["threshold_profile"]),
            out_dir=self.out_dir, evidence_mode=params["evidence_mode"], allowed_mounts=MOUNTS[params["camera_mounts"]],
            lag_search_s=(-lag, lag), interpolation_gap_factor=float(params["interpolation_gap_factor"]),
            template=template)
        self.config = runner.config_digest(self.cfg)
        self.vlm = vcfg["checks"]["task_success"]["vlm"]
        self.timeout_s = float((self.vlm.get("timeouts_s") or {}).get(eef_review.TAG) or eef_review.DEFAULT_TIMEOUT_S)
        self.gates = gates
        self.per_camera = int(params["review_windows_per_camera"])
        self.per_window = int(params["review_frames_per_window"])
        self._fetch_lock = threading.Lock()
        self.model = self.review_config = self.ask = self.cache = None

    def open(self) -> None:
        """Inside the VLM session: the model is known (probed or resolved)."""
        from ..adapters.vlm_client import SharedGate
        from ..extensions.eef_consistency import review as R
        from . import eef_review

        self.model = str(self.vlm["model"])
        self.review_config = hashlib.sha256(json.dumps(
            {"windows": self.per_camera, "frames": self.per_window, "model": self.model, "prompt": R.PROMPT_VERSION,
             "schema": R.ANSWER_SCHEMA, "preprocess": R.PREPROCESS}, sort_keys=True).encode()).hexdigest()
        self.ask = eef_review.make_asker(self.vlm, self.timeout_s, SharedGate(max(1, int(self.gates.get("arbitration", 1)))))
        self.cache = R.Cache(os.path.join(self.out_dir, "cache"))
        self.ctx.log("info", f"{MODULE}: trajectory.json sha256 {self.result.sha256[:12]}, "
                             f"{len(self.result.samples)} episode(s) declared, profile {self.params['threshold_profile']}, "
                             f"seeds {self.cfg.seed_root or 'none'}, gripper template "
                             f"{self.template_sha[:12] if self.template_sha else 'none'}, model {self.model}")

    def _seeds(self, sample_id: str) -> str | None:
        from ..extensions.eef_consistency.observations import seeds_digest

        return seeds_digest(self.cfg.seed_root, sample_id)

    def stale(self, episodes: list[int]) -> set[int]:
        """Episodes whose line was made from another file, seeds, template, configuration or model
        (design doc 12 §3.7: another trajectory.json is another input): --resume redoes them."""
        from ..pipeline.records import latest_results

        done = latest_results(self.run_dir, MODULE, episodes)
        out = set()
        for e, rec in done.items():
            d = rec.get("details") or {}
            s = self.result.samples.get(e)
            same = (d.get("input_file_sha256") == self.result.sha256 and d.get("review_config") == self.review_config
                    and (s is None or (d.get("config_hash") == self.config
                                       and d.get("seeds_sha256") == self._seeds(s.sample_id)
                                       and d.get("template_sha256") == self.template_sha)))
            if not same:
                out.add(e)
        return out

    def input_digest(self, episodes: list[int]) -> str:
        from ..pipeline.check_stage import input_digest

        return "sha256:" + hashlib.sha256(json.dumps(
            {"episodes": input_digest(episodes), "trajectory": self.result.sha256, "config": self.config,
             "review": self.review_config, "template": self.template_sha,
             "seeds": [self._seeds(s.sample_id) for _, s in sorted(self.result.samples.items())]},
            sort_keys=True).encode()).hexdigest()

    def judge(self, ep: int, log) -> tuple[dict | None, list[str]]:
        """CPU, then the model, then the verdict (design doc 12 C.9). ``None``: the CPU failed (the
        cause is on ``log``; the record is an error line, held)."""
        from ..extensions.eef_consistency import decide as D
        from ..extensions.eef_consistency import review as R
        from ..extensions.eef_consistency import runner
        from . import eef_review

        sample = self.result.samples.get(int(ep))
        review = None
        evidence: list[str] = []
        if sample is None:
            detail = _unsupported_detail(int(ep), "projection_missing")
            decision = D.decide(None, None)
        else:
            try:
                if self.scratch is not None:
                    with self._fetch_lock:
                        _fetch_media(self.storage, sample, self.scratch.name)
                detail, _ = runner.run_episode(sample, self.cfg)
            except Exception as e:  # noqa: BLE001 - one episode failing never stops the call
                log.add(MODULE, cause=f"{type(e).__name__}: {e}"[:500])
                self.ctx.log("warn", f"{MODULE}: episode {ep} failed: {type(e).__name__}: {e}")
                return None, []
            evidence = [os.path.relpath(os.path.join(self.out_dir, e["path"]), self.run_dir).replace(os.sep, "/")
                        for e in detail.get("evidence", [])]
            t1 = time.perf_counter()
            requests: dict = {}
            try:
                review = eef_review.review_episode(
                    sample, {"details": detail}, run_dir=self.run_dir, media_root=self.media_root, ask=self.ask,
                    cache=self.cache, model=self.model, per_camera=self.per_camera,
                    frames_per_window=self.per_window, out_dir=self.out_dir, requests=requests)
            except Exception as e:  # noqa: BLE001 - no second opinion: the CPU's suspects go to a person
                review = {"status": R.INCOMPLETE, "reasons": ["review_failed"], "cameras": {},
                          "failure": f"{type(e).__name__}: {e}"[:300]}
                self.ctx.log("warn", f"{MODULE}: review of episode {ep} failed: {type(e).__name__}: {e}")
            review["elapsed_s"] = round(time.perf_counter() - t1, 3)
            decision = D.decide(detail, review)
            if decision["outcome"] == D.HUMAN:          # the person's card shows every window
                eef_review.write_card_evidence(review, requests, self.run_dir)
            evidence += list(review.get("evidence") or [])
        detail.update(input_file_sha256=self.result.sha256, review_config=self.review_config,
                      assessment_mode="verdict", review=review or {"status": R.NOT_REVIEWED},
                      decision={k: v for k, v in decision.items() if k != "passed"}, reason=decision["reason"],
                      vlm={"model": self.model, "prompt_version": R.PROMPT_VERSION, "answer_schema": R.ANSWER_SCHEMA,
                           "timeout_s": self.timeout_s, "call_kind": eef_review.TAG})
        return {"passed": decision["passed"], "score": None, "detail": detail}, evidence

    def close(self) -> None:
        if self.scratch is not None:
            self.scratch.cleanup()
