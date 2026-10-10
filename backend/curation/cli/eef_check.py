"""``check --modules eef_video_consistency`` - the EEF-video consistency module (design doc 12, D49).

Opinions with a confidence, never a reject (registry 5.0, design doc 25 §6-§7, D81, D82). For every episode:
the CPU measures (``runner.run_episode``: sub-item statuses, coverage, segments, diagnosis; observations,
curves and evidence under ``checks/eef_video_consistency/``) and the model reviews the windows (``eef_review``,
one point and at most one axis per request) - or, without a gripper reference, the model gives its opinion on
each whole marked clip and a wrist camera's own motion is read; ``channels`` turns each into a verdict and a
confidence per sub-item and camera and ``combine`` merges them (``details.merged``: per cell the largest p, a
conflict, a single source capped; the episode's label, p and grounds). The record always passes; its findings
carry the opinion and a conflict is the only adjudication card (records before 5.0 carry the pass / reject /
person verdict of design doc 12 appendix C.9). An episode the file does not declare "cannot tell"; an episode the CPU fails on gets an error line
(held, D33); a model that fails on an episode leaves the CPU alone, capped. The whole call needs a VLM backend (probed first). A remote LeRobot dataset's
videos are read in place, only each episode's window, through the vlm stage's shared blocks (design doc 23
§3.2); ``--resume`` redoes a line made with another trajectory.json,
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


def _fetch_key(storage, key: str, root: str) -> None:
    """One object of a remote dataset into ``root`` (kept for the call). Streamed in ranges, written to a
    temporary name and renamed when complete."""
    dest = os.path.join(root, *key.split("/"))
    if os.path.isfile(dest):
        return
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


class _RemoteLeRobotRecords:
    """The record of a remote LeRobot dataset (design doc 12 §8.7): ``meta/`` and the episode's data file
    are copied into the judge's scratch directory on first use and read there like a local dataset."""

    def __init__(self, storage, keys, root: str):
        from ..extensions.eef_consistency import record as RC

        self.storage, self.keys, self.root = storage, list(keys), root
        self.local = RC.LeRobotRecords(root)
        self._lock = threading.Lock()
        self._meta = False

    def read(self, sample, specs):
        with self._lock:
            if not self._meta:
                for key in self.keys:
                    if key.startswith("meta/") and key.endswith((".json", ".jsonl", ".parquet")):
                        _fetch_key(self.storage, key, self.root)
                self._meta = True
            info = json.loads(open(os.path.join(self.root, "meta", "info.json"), encoding="utf-8").read())
            ep = int(sample.episode_index)
            if str(info.get("codebase_version", "")).startswith("v3"):
                from ..extensions.eef_consistency.adapters.lerobot_mapping import LeRobot

                row = LeRobot(self.root).episodes.get(ep)
                if row is None:
                    from ..extensions.eef_consistency.record import RecordDataError

                    raise RecordDataError(f"episode {ep} is not in the dataset")
                key = info["data_path"].format(chunk_index=int(row["data/chunk_index"]),
                                               file_index=int(row["data/file_index"]))
            else:
                chunk = ep // int(info.get("chunks_size", 1000))
                key = info["data_path"].format(episode_chunk=chunk, episode_index=ep)
            _fetch_key(self.storage, key, self.root)
        return self.local.read(sample, specs)


class _NoModel(Exception):
    """The model is not asked (VLM switched off, or no backend): why, as a ``single_source.missing`` reason."""


class EefJudge:
    """The EEF module's per-episode judge inside the vlm stage (``StageRun``, D49): built once per
    call (file, template, configuration), opened inside the call's VLM session (model, asker, cache),
    then ``judge(ep, log)`` gives the episode's ``{passed, score, detail}`` and evidence. Thread-safe:
    the stage judges several episodes at once."""

    module = MODULE

    def __init__(self, ctx, args, run_dir: str, source, vcfg: dict, gates: dict):
        """``source``: the command's input (``runctx.Source``; a bare ``Storage`` reads LeRobot). An
        mcap dataset (F5.13) is read where the funnel's readers read it: a local directory, or on TOS
        the dataset the source streams (``streams.objects``, ranged reads - nothing to fetch first)."""
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
            raise UsageError(f"{MODULE}: {getattr(e, 'message', e)}") from None
        storage = getattr(source, "storage", source)
        self.src = source if source is not storage else None
        self.mcap = self.src is not None and self.src.kind == "mcap"
        self.decl = self._declaration(args)
        given = (params.get("trajectory_json") or "").strip()
        traj = None
        #: the trajectory had episode by episode: a handheld gripper's recording (design doc 22 §5.4, F5.20), a robot
        #: arm's pose record by the dataset declaration (design doc 25 §4.1)
        self.derived = None
        self.source_kind = None
        if given:
            traj = os.path.expanduser(given)
            if not os.path.isfile(traj):
                raise UsageError(f"{MODULE}: trajectory.json not found: {traj}")
            self.source_kind = "upload"
        else:                                          # design docs 24, 25: had from the dataset, kept in the run
            from ..extensions.eef_consistency import derive

            listing = getattr(source, "listing", None) or storage.list()
            kind = self.src.kind if self.src is not None else "lerobot"
            cameras = self._cameras(storage, kind)
            plan = derive.plan_source(listing=listing, upload=False, handheld=self.mcap and self._handheld(),
                                      decl=self.decl, drafted=False, cameras=cameras, kind=kind)
            self.source_kind = plan["kind"]
            if plan["kind"] in (derive.SESSION, derive.GENERATE, derive.DATASET_FILE) and "readiness" not in plan:
                try:
                    traj = derive.to_run(storage, listing, run_dir)
                except Exception as e:  # noqa: BLE001
                    raise ModuleFailed(f"{MODULE}: the trajectory could not be generated from the dataset: {e}"[:300],
                                       {"reason": "trajectory_invalid"}) from None
            elif plan["kind"] == derive.MCAP_DERIVE:
                self.derived = self._derived(params, run_dir)
            elif plan["kind"] == derive.GENERATE:
                self.derived = self._generated(storage, listing, run_dir, cameras)
            elif plan["kind"] == derive.MISSING_DECLARATION:
                names = ", ".join(f"{m['field']} ({m['code']})" for m in plan["missing"][:6])
                raise ModuleFailed(f"{MODULE}: the dataset records the pose, but its declaration lacks {names}: complete "
                                   f"it (--declaration FILE) or pass --param {MODULE}.trajectory_json=PATH",
                                   {"reason": "declaration_incomplete", "missing": plan["missing"]})
            else:
                raise ModuleFailed(f"{MODULE}: the dataset records no end-effector poses with the cameras' calibration, "
                                   f"so the trajectory cannot be computed: pass --param {MODULE}.trajectory_json=PATH",
                                   {"reason": "trajectory_missing"})
        if traj:
            if self.mcap:                              # the episode files, from the one listing
                listing = self.src.listing
                media_exists = listing.__contains__
            else:
                media_exists = (lambda key: storage.stat(key) is not None) if storage.remote else None
            # every declared episode: a stage worker reuses the judge for the batches and the stream to come
            self.result = load.load_bundle(traj, lerobot_root=None if storage.remote else storage.root,
                                           media_exists=media_exists)
            if not self.result.ok:
                first = self.result.errors[0]
                raise ModuleFailed(f"{MODULE}: trajectory.json is invalid: {first.message}",
                                   {"errors": [i.as_dict() for i in self.result.errors[:10]],
                                    "sha256": self.result.sha256})
            self.umi = any(s.hand_poses for s in self.result.samples.values())
        else:
            self.result = self.derived
            self.umi = self.source_kind == derive.MCAP_DERIVE
        tpath = template_path(params)
        if self.umi and (seed_dir(params) or tpath):
            raise UsageError("UMI action overlays use advisory video opinion; omit gripper seeds/templates")
        template = None
        if tpath:
            try:
                template = TP.load_template(tpath)
            except (TP.TemplateError, OSError) as e:
                raise ModuleFailed(f"{MODULE}: gripper template is invalid: {e}", {"path": tpath}) from None
        self.template_sha = template.sha256 if template is not None else None
        self.record_mapping = None
        self.record_note = None
        rpath = (params.get("record_mapping") or "").strip()
        from ..extensions.eef_consistency import record as RC

        if rpath:                                  # an old task's record mapping (design doc 12 §8.7)
            try:
                self.record_mapping = RC.load_mapping(os.path.expanduser(rpath))
            except (RC.RecordMappingError, OSError, ValueError) as e:
                raise UsageError(f"{MODULE}: the record mapping is invalid: {e}") from None
        elif self.decl is not None:                # the declaration's records (design doc 25 §6.1)
            from ..extensions.eef_consistency import declared

            block = declared.record_block(self.decl)
            if block is not None:
                try:
                    self.record_mapping = RC.parse_mapping(block, sha256=declared.sha256(self.decl))
                except RC.RecordMappingError as e:
                    self.record_note = f"the declaration's records cannot be compared: {e}"
        self.ctx, self.run_dir, self.storage, self.params = ctx, run_dir, storage, params
        #: design doc 25 D84: 「使用 VLM 辅助」 - off, the model is never asked; on without a backend, its channel is
        #: missing (``vlm_missing``: set by the stage, which knows whether an endpoint is configured)
        self.use_vlm = bool(params.get("use_vlm", True))
        self.vlm_missing: str | None = None
        #: the half of the module this call runs (registry 5.3, design doc 23 §2.1, design doc 25 F5.24b): ``full`` -
        #: all of it (a call of its own, a plan from before 5.3); ``prep`` - the CPU half in vlm_prep: everything that
        #: asks no model, the requests built and kept in the episode's package (``package``), or the record itself when
        #: no model is asked; ``ask`` - the model half in vlm: the package read back, asked, merged into the record
        self.mode = "full"
        self.out_dir = module_dir(run_dir, MODULE)
        # a remote LeRobot dataset's videos are read where they are, only the episode's window, through the
        # vlm stage's shared blocks (design doc 23 §3.2); the scratch directory keeps the record's meta and data
        self.scratch = tempfile.TemporaryDirectory(prefix="eef-media-") if storage.remote and not self.mcap else None
        self.media_root = self.src.input_dir if self.mcap else storage.uri if storage.remote else storage.root
        lag = float(params["lag_search_s"])
        seeds = seed_dir(params)
        if seeds and self.source_kind == "generate" and self.derived is not None:
            from ..extensions.eef_consistency import declared

            # the platform names a generated trajectory's samples: the seeds follow them by episode (design doc 25)
            seeds = declared.remap_seeds(seeds, self.derived, os.path.join(run_dir, "inputs", "eef", "seeds"))
        self.cfg = runner.RunConfig(
            lerobot_root=self.media_root, seed_root=seeds, profile=profile.load(params["threshold_profile"]),
            out_dir=self.out_dir, evidence_mode=params["evidence_mode"], allowed_mounts=MOUNTS[params["camera_mounts"]],
            lag_search_s=(-lag, lag), interpolation_gap_factor=float(params["interpolation_gap_factor"]),
            template=template, record=self._record(), ego_motion_window_s=float(params["ego_motion_window_s"]),
            # a trajectory had from the dataset's records is not compared with them (design doc 25 §6.1)
            record_internal_only=self.source_kind not in ("upload", "dataset_file"))
        self.config = runner.config_digest(self.cfg)
        # no gripper reference (design doc 12 §10.5, D-E15): no CPU measurement, the model's advisory opinion
        self.opinion = self.cfg.seed_root is None and template is None
        self.vlm = vcfg["checks"]["task_success"]["vlm"]
        self.timeout_s = float((self.vlm.get("timeouts_s") or {}).get(eef_review.TAG) or eef_review.DEFAULT_TIMEOUT_S)
        self.gates = gates
        self.per_camera = int(params["review_windows_per_camera"])
        self.per_window = int(params["review_frames_per_window"])
        self._fetch_lock = threading.Lock()
        self.model = self.review_config = self.ask = self.cache = None

    @staticmethod
    def _declaration(args) -> dict | None:
        """``--declaration``: the dataset declaration the task froze (design doc 25 §3), normalized."""
        path = getattr(args, "declaration", None)
        if not path:
            return None
        from .. import declaration as DCL

        try:
            return DCL.load(os.path.expanduser(path))
        except (OSError, ValueError) as e:
            raise UsageError(f"--declaration: {e}") from None

    def _cameras(self, storage, kind: str) -> list[str]:
        """The dataset's camera sources (LeRobot video keys; an mcap declaration's camera topics)."""
        if kind == "mcap":
            return [c["topic"] for c in (self.decl or {}).get("cameras") or []]
        if kind != "lerobot" or self.decl is None:
            return []
        from ..extensions.eef_consistency import derive

        try:
            info = json.loads(derive._read(storage, "meta/info.json"))
        except (OSError, ValueError, KeyError):
            return []
        self._info = info
        return [k for k, f in (info.get("features") or {}).items() if isinstance(f, dict)
                and f.get("dtype") in ("video", "image") and "depth" not in k.lower()]

    def _generated(self, storage, listing, run_dir: str, cameras: list[str]):
        """A robot arm's trajectory generated episode by episode from the declaration (design doc 25 §4.1)."""
        from ..declaration.checks import Facts
        from ..extensions.eef_consistency import declared
        from ..extensions.eef_consistency.adapters.lerobot_mapping import MappingError
        from ..pipeline.records import module_dir

        name = os.path.basename(str(storage.uri).rstrip("/")) or "dataset"
        try:
            if self.mcap:                           # the pose and camera topics of each episode file
                return declared.GeneratedMcap(self.decl, root=self.src.input_dir, numbering=self.src.numbering(),
                                              out_dir=module_dir(run_dir, MODULE), dataset_id=name, cameras=cameras)
            sizes = Facts.lerobot(getattr(self, "_info", {}) or {}).cameras
            return declared.Generated(self.decl, storage=storage, listing=listing, out_dir=module_dir(run_dir, MODULE),
                                      dataset_id=name, cameras=cameras, sizes=sizes)
        except (MappingError, OSError, ValueError, KeyError) as e:
            raise ModuleFailed(f"{MODULE}: the trajectory cannot be generated from the declaration: {e}"[:300],
                               {"reason": "trajectory_invalid"}) from None

    def _handheld(self) -> bool:
        """Whether the mcap dataset is a handheld gripper's: its first episode in the built-in UMI layout."""
        from ..extensions.eef_consistency import derive_mcap

        numbering = self.src.numbering()
        return bool(numbering) and derive_mcap.handheld(self.src.input_dir, numbering[min(numbering)])

    def _derived(self, params: dict, run_dir: str):
        """The trajectory a handheld gripper's raw mcap carries (design doc 22 §5.4): derived episode by episode with
        the task's gripper calibration or the built-in DAS DEMO one - the calibration the recording lacks."""
        from ..extensions.eef_consistency import derive_mcap
        from ..extensions.eef_consistency.adapters.umi_mcap import ExportError
        from ..pipeline.records import module_dir

        cal = os.path.expanduser((params.get("gripper_calibration") or "").strip()) or None
        if cal and not os.path.isfile(cal):
            raise UsageError(f"{MODULE}: gripper calibration not found: {cal}")
        hh = (((self.decl or {}).get("calibration") or {}).get("handheld") or {}).get("calibration")
        name = os.path.basename(str(getattr(self.src, "uri", "") or self.src.input_dir).rstrip("/")) or "dataset"
        try:
            return derive_mcap.Derived(root=self.src.input_dir, numbering=self.src.numbering(), calibration=cal,
                                       out_dir=module_dir(run_dir, MODULE), dataset_id=name, calibration_doc=hh)
        except (ExportError, OSError) as e:
            raise ModuleFailed(f"{MODULE}: the gripper calibration is invalid: {e}", {"path": cal}) from None

    def _sample(self, ep: int):
        """(the episode's sample or None, how its trajectory was had: None for an uploaded file)."""
        if self.derived is not None:
            return self.derived.sample(int(ep))
        return self.result.samples.get(int(ep)), None

    def _record(self):
        """(mapping, reader) of the record comparison, or None without a mapping (design doc 12 §8.7)."""
        from ..extensions.eef_consistency import record as RC

        if self.record_mapping is None:
            return None
        if self.mcap:
            reader = RC.McapRecords(self.media_root, numbering=self.src.numbering())
        elif self.storage.remote:
            keys = self.src.listing if self.src is not None else self.storage.list()
            reader = _RemoteLeRobotRecords(self.storage, keys, self.scratch.name)
        else:
            reader = RC.LeRobotRecords(self.storage.root)
        return self.record_mapping, reader

    def rebind(self, source) -> None:
        """A stage worker reuses the judge for its next batch, which opened its own source: an mcap
        dataset is read from that one (the first batch's local copy may be gone with its command)."""
        if self.mcap and getattr(source, "kind", None) == "mcap":
            self.src, self.media_root = source, source.input_dir
            self.cfg.lerobot_root = self.media_root
            self.cfg.record = self._record()
            if self.derived is not None and hasattr(self.derived, "root"):   # what is derived or generated reads it too
                self.derived.root = str(source.input_dir)

    def open(self) -> None:
        """Inside the VLM session: the model is known (probed or resolved)."""
        from ..adapters.vlm_client import SharedGate
        from ..extensions.eef_consistency import opinion as OP
        from ..extensions.eef_consistency import review as R
        from ..extensions.eef_consistency.umi import PROMPT_VERSION as UMI_PROMPT
        from . import eef_review

        self.model = None if self.vlm_missing else str(self.vlm["model"])
        self.review_config = hashlib.sha256(json.dumps(
            {"windows": self.per_camera, "frames": self.per_window, "model": self.model, "prompt": R.PROMPT_VERSION,
             **({"vlm_missing": self.vlm_missing} if self.vlm_missing else {}),
             "schema": R.ANSWER_SCHEMA, "preprocess": R.PREPROCESS,
             "video_protocol": "eef-video-review/1", "video": self.vlm.get("video") or {},
             **({"opinion": [OP.PROTOCOL, OP.PROMPT_VERSION, OP.ANSWER_SCHEMA, OP.MAX_CLIP_S]} if self.opinion else {}),
             **({"umi_prompt": UMI_PROMPT} if self.umi else {})},
            sort_keys=True).encode()).hexdigest()
        self.ask = None if self.vlm_missing else \
            eef_review.make_asker(self.vlm, self.timeout_s, SharedGate(max(1, int(self.gates.get("arbitration", 1)))))
        self.cache = R.Cache(os.path.join(self.out_dir, "cache"))
        where = (self.derived.describe() if self.derived is not None
                 else f"trajectory.json sha256 {self.result.sha256[:12]}, {len(self.result.samples)} episode(s) declared")
        if self.record_note:
            self.ctx.log("warning", f"{MODULE}: {self.record_note}")
        self.ctx.log("info", f"{MODULE}: {where}, profile {self.params['threshold_profile']}, "
                             f"seeds {self.cfg.seed_root or 'none'}, gripper template "
                             f"{self.template_sha[:12] if self.template_sha else 'none'}, model "
                             f"{self.model or {'vlm_off': 'none (VLM switched off)', 'no_vlm_backend': 'none (no backend)'}.get(self.vlm_missing, 'none')}"
                             + ("; no gripper reference: the model's opinion only, no verdict (design doc 12 §10.5)"
                                if self.opinion else ""))

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
            if self.derived is not None:               # no seeds, no template; nothing to derive to tell
                same = (d.get("input_file_sha256") == self.result.sha256 and d.get("review_config") == self.review_config
                        and d.get("config_hash") in (self.config, None))
                if not same:
                    out.add(e)
                continue
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
             "seeds": [] if self.derived is not None else [self._seeds(s.sample_id) for _, s in sorted(self.result.samples.items())]},
            sort_keys=True).encode()).hexdigest()

    def judge(self, ep: int, log) -> tuple[dict | None, list[str]]:
        """The channels, then their merge (design doc 25 §6-§7): an opinion with a confidence, never a reject.
        ``None``: the CPU failed (the cause is on ``log``; the record is an error line, held). In ``prep`` mode with
        a model to ask, everything goes into the episode's package and the struct only says so
        (``check_stage.PREPARED``); in ``ask`` mode the package makes the record."""
        if self.mode == "ask":
            return self._answer(int(ep), log)
        keep = self.mode == "prep" and self.vlm_missing is None
        t0 = time.perf_counter()
        sample, source = self._sample(ep)
        if sample is None:                          # nothing to compare: "cannot tell", nobody asked (§7.5)
            if self.opinion:
                got = self._unassessed(int(ep), source), []
            else:
                why = "trajectory.json 里没有这一条" if source is None else \
                    f"这一条生成不出轨迹：{source.get('message') or source.get('reason')}"
                got = self._judged(self._merged(why=why), _unsupported_detail(int(ep), "projection_missing"), None, [])
                if source is not None:
                    got[0]["detail"]["trajectory_source"] = source
            if keep:                               # nothing to ask: the record is made, kept for the model half
                prep_s = round(time.perf_counter() - t0, 3)
                got[0]["detail"]["halves"] = {"vlm_prep": prep_s}
                return self._keep(ep, {"final": got[0], "evidence": got[1], "prep_s": prep_s}, [])
            return got
        try:
            if keep:
                return self._prepare(int(ep), sample, source, log, t0)
            got, evidence = self._judge(ep, sample, log)
            if got is not None and source is not None:   # generated or derived: how (design doc 25 §4.1)
                got["detail"].setdefault("trajectory_source", source)
            if got is not None and self.mode == "prep":   # the whole module in the CPU block: its time is that stage's
                got["detail"]["halves"] = {"vlm_prep": round(time.perf_counter() - t0, 3)}
            return got, evidence
        finally:
            if self.mcap:                                 # the episode's topic videos are done with
                from ..extensions.eef_consistency import mcap_media as MM
                from ..extensions.eef_consistency import observations as O

                for cid in sample.cameras:
                    MM.drop(O.media_path(sample, cid, self.media_root))

    # ------------------------------------------------------------ the two halves (design doc 23 §2.1)
    def _package(self, ep: int) -> str:
        from ..extensions.eef_consistency import package

        return package.path(self.run_dir, MODULE, ep)

    def _package_config(self) -> dict:
        """What a kept half was made with: another file, configuration, template or model makes it again."""
        return {"input": self.result.sha256, "config": self.config, "review": self.review_config,
                "template": self.template_sha}

    def _keep(self, ep: int, prep: dict, requests: list) -> tuple[dict, list[str]]:
        from ..extensions.eef_consistency import package
        from ..pipeline.check_stage import PREPARED

        package.write(self._package(ep), {**prep, "episode_index": int(ep), "made_with": self._package_config()},
                      requests)
        return {PREPARED: True}, []

    def kept(self, episodes: list[int]) -> set[int]:
        """The episodes whose CPU half is kept for the model half, made with this call's inputs (``--resume``)."""
        from ..extensions.eef_consistency import package

        out = set()
        for e in episodes:
            prep = package.read_prep(self._package(e))
            if prep is not None and prep.get("made_with") == self._package_config():
                out.add(int(e))
        return out

    def committed(self, ep: int, record: dict | None) -> None:
        """The record the model half made is on disk: its package goes (D77); an error keeps it for a retry."""
        from ..extensions.eef_consistency import package
        from ..pipeline.records import is_error

        if self.mode == "ask" and record is not None and not is_error(record):
            package.remove(self._package(ep))

    def _prepare(self, ep: int, sample, source, log, t0: float) -> tuple[dict, list[str]]:
        """The CPU half (design doc 23 §2.1): the measurement, the wrist cameras' own motion, the record comparison
        and every request rendered - kept in the package with the partial record; nobody asked."""
        from ..extensions.eef_consistency import opinion as OP
        from ..extensions.eef_consistency import runner
        from . import eef_review

        options = getattr(self.ask, "video_options", {})
        if self.opinion:
            bridged = None
            if sample.hand_poses:          # short pose gaps bridged with the default (design doc 22 §5.2), and said so
                from ..extensions.eef_consistency import umi

                bridged = umi.fill_gaps(sample)
            plan, failure = None, None
            try:
                self._fetch(sample)
                plan = OP.plan_opinion(sample, media_root=self.media_root, model=self.model,
                                       allowed_mounts=self.cfg.allowed_mounts, options=options)
            except Exception as e:  # noqa: BLE001 - an opinion that could not be had changes nothing
                failure = f"{type(e).__name__}: {e}"[:300]
                self.ctx.log("warn", f"{MODULE}: the opinion on episode {ep} failed: {failure}")
            ego = self._ego_motion(ep, sample)
            detail = {"sample_id": sample.sample_id, "episode_index": int(ep), "assessment_mode": "vlm_opinion",
                      "overall": "opinion", "config_hash": self.config, "seeds_sha256": None,
                      "template_sha256": None, "input_file_sha256": self.result.sha256,
                      "review_config": self.review_config}
            if ego is not None:
                detail["ego_motion"] = ego
            if self.derived is not None:
                detail["trajectory_source"] = self.derived.sample(int(ep))[1]
            evidence = self._attach_record(detail, sample)
            prep = {"kind": "opinion", "bridged": bridged, "failure": failure,
                    "plan": None if plan is None else {
                        "cameras": plan["cameras"], "times": plan["times"], "prompt_version": plan["prompt_version"],
                        "order": [[c, i] for c, i, _ in plan["requests"]]}}
            requests = [] if plan is None else [r for _, _, r in plan["requests"]]
        else:
            try:
                self._fetch(sample)
                detail, _ = runner.run_episode(sample, self.cfg)
            except Exception as e:  # noqa: BLE001 - one episode failing never stops the call
                cause = f"{type(e).__name__}: {e}"[:500]
                self.ctx.log("warn", f"{MODULE}: episode {ep} failed: {cause}")
                return self._keep(ep, {"error": cause}, [])
            evidence = [self._rel(e["path"]) for e in detail.get("evidence", [])]
            self._record_paths(detail)
            plan, failure = None, None
            try:
                plan = eef_review.plan_review(sample, {"details": detail}, run_dir=self.run_dir,
                                              media_root=self.media_root, model=self.model,
                                              per_camera=self.per_camera, frames_per_window=self.per_window,
                                              video=True, video_options=options)
            except Exception as e:  # noqa: BLE001 - no second opinion: the CPU's reading alone
                failure = f"{type(e).__name__}: {e}"[:300]
                self.ctx.log("warn", f"{MODULE}: review of episode {ep} failed: {failure}")
            prep = {"kind": "review", "failure": failure,
                    "plan": None if plan is None else {
                        "cameras": plan["cameras"], "truncated": plan["truncated"], "episode": plan["episode"],
                        "order": [[c, i] for c, i, _ in plan["requests"]]}}
            requests = [] if plan is None else [r for _, _, r in plan["requests"]]
        if source is not None:                     # generated or derived: how (design doc 25 §4.1)
            detail.setdefault("trajectory_source", source)
        prep.update(detail=detail, evidence=evidence, prep_s=round(time.perf_counter() - t0, 3))
        return self._keep(ep, prep, requests)

    def _answer(self, ep: int, log) -> tuple[dict | None, list[str]]:
        """The model half (design doc 23 §2.1): the package read back, its requests asked, the channels merged."""
        from ..extensions.eef_consistency import opinion as OP
        from ..extensions.eef_consistency import package
        from ..extensions.eef_consistency import review as R
        from . import eef_review

        where = self._package(ep)
        prep = package.read_prep(where)
        if prep is None:
            log.add(MODULE, cause="the CPU half kept nothing for this episode (vlm_prep did not finish it)")
            return None, []
        if prep.get("made_with") != self._package_config():
            log.add(MODULE, cause="the CPU half was kept with other inputs (file, configuration or model)")
            return None, []
        if prep.get("error"):
            log.add(MODULE, cause=str(prep["error"]))
            return None, []
        if "final" in prep:                        # nothing to ask: the CPU half made the record
            return prep["final"], list(prep.get("evidence") or [])
        if self.vlm_missing:
            log.add(MODULE, cause=f"the kept requests have no model to ask ({self.vlm_missing})")
            return None, []
        requests = package.read_requests(where)
        detail, evidence = prep["detail"], list(prep.get("evidence") or [])
        order = (prep.get("plan") or {}).get("order") or []
        t1 = time.perf_counter()
        if prep["kind"] == "opinion":
            if prep["plan"] is None:
                op = {"protocol": OP.PROTOCOL, "prompt_version": OP.PROMPT_VERSION, "status": "failed", "cameras": {},
                      "segments": 0, "flagged": False, "max_confidence": None, "requests": 0,
                      "failure": prep.get("failure")}
            else:
                plan = {**prep["plan"], "requests": [(c, i, r) for (c, i), r in zip(order, requests)]}
                try:
                    op = OP.answer_opinion(plan, self.ask, self.cache)
                except Exception as e:  # noqa: BLE001 - an opinion that could not be had changes nothing
                    op = {"protocol": OP.PROTOCOL, "prompt_version": prep["plan"]["prompt_version"], "status": "failed",
                          "cameras": {}, "segments": 0, "flagged": False, "max_confidence": None, "requests": 0,
                          "failure": f"{type(e).__name__}: {e}"[:300]}
                    self.ctx.log("warn", f"{MODULE}: the opinion on episode {ep} failed: {type(e).__name__}: {e}")
            op["elapsed_s"] = round(time.perf_counter() - t1, 3)
            if prep.get("bridged") is not None:
                op["interpolation"] = prep["bridged"]
            merged = self._merged(opinion=op, ego=detail.get("ego_motion"))
            detail.update(opinion=op, merged=merged, reason=merged["episode"]["reason"],
                          vlm={"model": self.model, "prompt_version": op.get("prompt_version", OP.PROMPT_VERSION),
                               "answer_schema": OP.ANSWER_SCHEMA, "timeout_s": self.timeout_s,
                               "call_kind": eef_review.TAG},
                          halves={"vlm_prep": prep.get("prep_s"), "vlm": round(time.perf_counter() - t1, 3)})
            return {"passed": True, "score": None, "detail": detail}, evidence
        asked: dict = {}
        if prep["plan"] is None:
            review = {"status": R.INCOMPLETE, "reasons": ["review_failed"], "cameras": {}, "failure": prep.get("failure")}
        else:
            plan = {**prep["plan"], "requests": [(c, i, r) for (c, i), r in zip(order, requests)]}
            try:
                review = eef_review.answer_review(plan, {"details": detail}, run_dir=self.run_dir, ask=self.ask,
                                                  cache=self.cache, out_dir=self.out_dir, requests=asked)
            except Exception as e:  # noqa: BLE001 - no second opinion: the CPU's suspects go to a person
                review = {"status": R.INCOMPLETE, "reasons": ["review_failed"], "cameras": {},
                          "failure": f"{type(e).__name__}: {e}"[:300]}
                self.ctx.log("warn", f"{MODULE}: review of episode {ep} failed: {type(e).__name__}: {e}")
        review["elapsed_s"] = round(time.perf_counter() - t1, 3)
        merged = self._merged(detail=detail, review=review)
        if merged["episode"]["conflicts"]:          # the person's card shows every window (a conflict is the only card)
            eef_review.write_card_evidence(review, asked, self.run_dir)
        evidence += list(review.get("evidence") or [])
        detail["halves"] = {"vlm_prep": prep.get("prep_s"), "vlm": round(time.perf_counter() - t1, 3)}
        return self._judged(merged, detail, review, evidence)

    def _fetch(self, sample) -> None:
        if self.mcap and self.src.cache is not None:   # a streamed TOS dataset has no cache: read in place
            with self._fetch_lock:                     # the episode's .mcap into the source cache
                keys = {c.media["uri"] for c in sample.cameras.values()}
                if self.record_mapping is not None:    # the record's topics live in the episode's file
                    keys |= set(self.src.episode_keys([sample.episode_index]))
                self.src.cache.fetch(sorted(keys))

    def _opinion(self, ep: int, sample) -> tuple[dict, list[str]]:
        """No gripper reference (design doc 12 §10.5, D-E15): each camera's whole clip, marked, goes to the
        model, which lists the stretches it finds mismatched with a confidence each. Advisory: the record
        passes whatever the model says, and a failure is recorded in the opinion, never as an error line."""
        from ..extensions.eef_consistency import opinion as OP
        from . import eef_review

        t1 = time.perf_counter()
        bridged = None
        if sample.hand_poses:              # short pose gaps bridged with the default (design doc 22 §5.2), and said so
            from ..extensions.eef_consistency import umi

            bridged = umi.fill_gaps(sample)
        try:
            if self.vlm_missing:                 # nobody to ask: the channel is missing, said why (design doc 25 §7.1)
                raise _NoModel(self.vlm_missing)
            self._fetch(sample)
            op = OP.opinion_episode(sample, media_root=self.media_root, ask=self.ask, cache=self.cache,
                                    model=self.model,
                                    allowed_mounts=self.cfg.allowed_mounts,
                                    options=getattr(self.ask, "video_options", {}))
        except _NoModel as e:
            op = {"protocol": OP.PROTOCOL, "prompt_version": OP.PROMPT_VERSION, "status": "not_asked", "cameras": {},
                  "segments": 0, "flagged": False, "max_confidence": None, "requests": 0, "missing": str(e)}
        except Exception as e:  # noqa: BLE001 - an opinion that could not be had changes nothing
            op = {"protocol": OP.PROTOCOL, "prompt_version": OP.PROMPT_VERSION, "status": "failed", "cameras": {},
                  "segments": 0, "flagged": False, "max_confidence": None, "requests": 0,
                  "failure": f"{type(e).__name__}: {e}"[:300]}
            self.ctx.log("warn", f"{MODULE}: the opinion on episode {ep} failed: {type(e).__name__}: {e}")
        op["elapsed_s"] = round(time.perf_counter() - t1, 3)
        if bridged is not None:
            op["interpolation"] = bridged
        ego = self._ego_motion(ep, sample)
        evidence: list[str] = []                                  # the overlay is drawn live
        nobody = None
        if self.vlm_missing:
            from ..extensions.eef_consistency import combine as CB

            nobody = f"没给夹爪参考，模型也没问（{CB.MISSING_ZH.get(self.vlm_missing, self.vlm_missing)}）"
        merged = self._merged(opinion=op, ego=ego, why=nobody)
        detail = {"sample_id": sample.sample_id, "episode_index": int(ep), "assessment_mode": "vlm_opinion",
                  "overall": "opinion", "opinion": op, "config_hash": self.config, "seeds_sha256": None,
                  "template_sha256": None, "input_file_sha256": self.result.sha256, "review_config": self.review_config,
                  "merged": merged, "reason": merged["episode"]["reason"],
                  "vlm": {"model": self.model, "prompt_version": op.get("prompt_version", OP.PROMPT_VERSION), "answer_schema": OP.ANSWER_SCHEMA,
                          "timeout_s": self.timeout_s, "call_kind": eef_review.TAG}}
        if ego is not None:
            detail["ego_motion"] = ego
        if self.derived is not None:
            detail["trajectory_source"] = self.derived.sample(int(ep))[1]
        evidence += self._attach_record(detail, sample)           # needs no gripper reference (§8.7)
        return {"passed": True, "score": None, "detail": detail}, evidence

    def _unassessed(self, ep: int, source: dict | None) -> dict:
        """The opinion's record of an episode without a trajectory: not in the uploaded file, or not derivable from
        its recording (``source`` says why). It passes like every opinion and asks nobody."""
        from ..extensions.eef_consistency import opinion as OP

        if source is None:
            why = "trajectory.json 里没有这一条"
        else:
            cams = [f"{h}：{c.get('reason')}" for h, c in sorted((source.get("cameras") or {}).items())
                    if c.get("status") != "ok" and c.get("reason")]
            why = "；".join(["这一条推不出轨迹", *cams] if cams else ["这一条推不出轨迹", str(source.get("message") or source.get("reason"))])
        op = {"protocol": OP.PROTOCOL, "prompt_version": OP.PROMPT_VERSION, "status": "not_assessable", "cameras": {},
              "segments": 0, "flagged": False, "max_confidence": None, "requests": 0, "failure": why}
        merged = self._merged(why=why)
        detail = {"sample_id": None, "episode_index": int(ep), "assessment_mode": "vlm_opinion", "overall": "opinion",
                  "opinion": op, "config_hash": self.config, "seeds_sha256": None, "template_sha256": None,
                  "input_file_sha256": self.result.sha256, "review_config": self.review_config,
                  "merged": merged, "reason": ""}
        if source is not None:
            detail["trajectory_source"] = source
        return {"passed": True, "score": None, "detail": detail}

    def _ego_motion(self, ep: int, sample) -> dict | None:
        """Each wrist camera's own motion in its pictures against its recorded poses (design doc 22 §5.3):
        a reading for the opinion, never a verdict; None without a wrist camera. A camera that fails is
        reported as such, the others still count."""
        from ..extensions.eef_consistency import egomotion as EM
        from ..extensions.eef_consistency import observations as O

        t1 = time.perf_counter()
        cameras: dict[str, dict] = {}
        for cid, cam in sample.cameras.items():
            if cam.mount not in self.cfg.allowed_mounts:
                continue
            try:
                got = EM.camera_ego_motion(sample, cid, lambda cid=cid: O.view_frames(sample, cid, self.media_root),
                                           window_s=self.cfg.ego_motion_window_s, lag_search_s=self.cfg.lag_search_s[1],
                                           profile=self.cfg.profile)
            except Exception as e:  # noqa: BLE001 - a reading only: the episode goes on without it
                self.ctx.log("warn", f"{MODULE}: the ego-motion of episode {ep} camera {cid} failed: {type(e).__name__}: {e}")
                got = {"status": "unknown", "reason": "failed", "message": f"{type(e).__name__}: {e}"[:200], "metrics": {},
                       "segments": [], "unmatched": [], "lag": None}
            if got is not None:
                cameras[cid] = got
        if not cameras:
            return None
        assumed = next((n for n in sample.sample.get("notes") or [] if str(n).startswith("按假设值")), None)
        out = EM.summarize(cameras, assumed=assumed, profile=self.cfg.profile)
        out["window_s"] = self.cfg.ego_motion_window_s
        out["elapsed_s"] = round(time.perf_counter() - t1, 3)
        return out

    def _attach_record(self, detail: dict, sample) -> list[str]:
        """The record comparison of an opinion-only episode: reported, its overlay evidence run-relative."""
        from ..extensions.eef_consistency import runner

        try:
            shown = runner.attach_record(detail, sample, self.cfg)
        except Exception as e:  # noqa: BLE001 - a report only: its failure never touches the episode
            self.ctx.log("warn", f"{MODULE}: the record comparison of episode {sample.episode_index} failed: "
                                 f"{type(e).__name__}: {e}")
            return []
        self._record_paths(detail)
        return [self._rel(e["path"]) for e in shown]

    def _rel(self, path: str) -> str:
        """A path under the module's output as the record's evidence names it: relative to the run."""
        return os.path.relpath(os.path.join(self.out_dir, path), self.run_dir).replace(os.sep, "/")

    def _record_paths(self, detail: dict) -> None:
        for e in (detail.get("record") or {}).get("evidence") or []:
            e["path"] = self._rel(e["path"])

    def _merged(self, *, detail: dict | None = None, review: dict | None = None, opinion: dict | None = None,
                ego: dict | None = None, why: str | None = None) -> dict:
        """The episode's channels merged (design doc 25 §6-§7, ``combine``): ``details.merged``."""
        from ..extensions.eef_consistency import channels as CH
        from ..extensions.eef_consistency import combine as CB

        prof = self.cfg.profile
        cfg = CB.settings(prof)
        cpu = CH.cpu_channel(detail, prof, full_at=cfg["full_at"]) if detail else None
        rev = CH.review_channel(review, detail) if review is not None else None
        vlm_missing = self.vlm_missing or (CB.MODEL_NO_ANSWER if review is not None and not (review.get("cameras") or {})
                                           else None)
        op = CH.opinion_channel(opinion, umi=self.umi) if opinion is not None else None
        eg = CH.ego_channel(ego, getattr(prof, "ego_motion", None), full_at=cfg["full_at"]) if ego else None
        merged = CB.merge(cpu=cpu, review=rev, opinion=op, ego=eg, cfg=cfg, vlm_missing=vlm_missing,
                          hypotheses=(detail or {}).get("diagnosis"))
        if rev is not None and rev[1]:
            merged["tracking"] = rev[1]
        if why and merged["episode"]["p"] is None:
            merged["episode"]["reason"] = f"判断不了：{why}"
        merged["profile"] = {"calibrated": bool(getattr(prof, "calibrated", False)),
                             "name": getattr(prof, "name", None)}
        return merged

    def _judge(self, ep: int, sample, log) -> tuple[dict | None, list[str]]:
        from ..extensions.eef_consistency import review as R
        from ..extensions.eef_consistency import runner
        from . import eef_review

        if self.opinion:
            return self._opinion(ep, sample)
        review = None
        evidence: list[str] = []
        try:
            self._fetch(sample)
            detail, _ = runner.run_episode(sample, self.cfg)
        except Exception as e:  # noqa: BLE001 - one episode failing never stops the call
            log.add(MODULE, cause=f"{type(e).__name__}: {e}"[:500])
            self.ctx.log("warn", f"{MODULE}: episode {ep} failed: {type(e).__name__}: {e}")
            return None, []
        evidence = [self._rel(e["path"]) for e in detail.get("evidence", [])]
        self._record_paths(detail)
        if self.vlm_missing:                         # no model (switched off, or no backend): the CPU alone
            merged = self._merged(detail=detail)
            return self._judged(merged, detail, None, evidence)
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
        merged = self._merged(detail=detail, review=review)
        if merged["episode"]["conflicts"]:          # the person's card shows every window (a conflict is the only card)
            eef_review.write_card_evidence(review, requests, self.run_dir)
        evidence += list(review.get("evidence") or [])
        return self._judged(merged, detail, review, evidence)

    def _judged(self, merged: dict, detail: dict, review: dict | None, evidence: list[str]):
        """The record of a measured episode: it never fails (D81) - the findings carry the opinion."""
        from ..extensions.eef_consistency import review as R
        from . import eef_review

        detail.update(input_file_sha256=self.result.sha256, review_config=self.review_config,
                      assessment_mode="verdict", review=review or {"status": R.NOT_REVIEWED},
                      merged=merged, reason=merged["episode"]["reason"],
                      vlm={"model": self.model, "prompt_version": R.PROMPT_VERSION, "answer_schema": R.ANSWER_SCHEMA,
                           "timeout_s": self.timeout_s, "call_kind": eef_review.TAG})
        return {"passed": True, "score": None, "detail": detail}, evidence

    def close(self) -> None:
        if self.scratch is not None:
            self.scratch.cleanup()
        if self.mcap:
            from ..extensions.eef_consistency import mcap_media as MM

            MM.close()
