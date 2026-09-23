"""Dataset operations that run the CLI: preflight, the file listing, registration (D36, D37).

* **preflight** - ``curation preflight --json``: metadata only, seconds; the result
  goes to the 30-minute cache (``POST /preflight``) or into a registration.
* **listing** - ``curation snapshot`` over the whole dataset (every episode): the
  full file listing whose summary is the registration's ``source_fingerprint``;
  the listing itself is kept on the data volume (content addressed, under
  ``<data>/datasets/listings/``) so a later check can say which files changed.
* **registration** - get or create by (source, address, region): preflight +
  listing, both fingerprints, and an ``add`` check in the history.
* **recheck** - a new listing compared with the kept one; nothing else changes.
* **repreflight** - preflight and listing again; the registration takes both.

Keys reach the CLI through the environment only (W8's ``build_env``).
"""
from __future__ import annotations

import hashlib
import logging
import pathlib
import tempfile
from dataclasses import dataclass

from ..errors import ApiError
from ..exec import CliCommand, CliOutcome
from ..repo import protocol as P
from ..secrets import Unavailable, build_env
from . import rules
from .workdir import read_json, write_json_atomic

log = logging.getLogger("daemon.orchestr")

LISTINGS = "datasets/listings"


@dataclass(frozen=True)
class Source:
    source: str                          # tos | public | local
    uri: str
    region: str | None = None
    credential_id: str | None = None

    @classmethod
    def of_task(cls, task: P.Task) -> "Source":
        return cls(task.input_source, task.input_uri, task.input_region, task.input_cred_id)

    @classmethod
    def of_dataset(cls, ds: P.Dataset) -> "Source":
        return cls(ds.source, ds.uri, ds.region, ds.credential_id)


def format_supported(preflight: dict | None) -> bool:
    fmt = (preflight or {}).get("format") or {}
    return bool(fmt.get("supported")) and fmt.get("kind") == "lerobot"


class DatasetOps:
    def __init__(self, orch):
        self.orch = orch

    # -- running one CLI command ------------------------------------------------------
    def _env(self, src: Source, owner: str) -> tuple[dict, str | None]:
        svc = self.orch.svc
        key = None
        if src.source == "tos":
            try:
                key = svc.tos_key(src.credential_id, owner=owner, role="input")
            except Unavailable as err:
                raise ApiError("validation_failed", err.message_zh,
                               details={"errors": [{"field": "input.credential",
                                                    "problem": err.code}]}) from None
        env = build_env(input_key=key, tos_endpoint=svc.deployment_endpoint)
        region = src.region or (key.region if key is not None else None)
        return env, region

    def _run(self, src: Source, owner: str, argv: list[str], *, stage: str,
             timeout_s: float) -> CliOutcome:
        env, region = self._env(src, owner)
        full = list(argv)
        if region:
            full += ["--input-region", region]
        lines: list[str] = []
        outcome = self.orch.executor.run(
            CliCommand(full, env=env, stage=stage, timeout_s=timeout_s), on_line=lines.append)
        if not outcome.ok:
            log.info("%s of %s: %s (%s)", stage, src.uri, outcome.status, outcome.reason())
        return outcome

    def _input_args(self, src: Source) -> list[str]:
        return ["--input", src.uri, "--source", src.source]

    def _raise(self, outcome: CliOutcome, what: str, src: Source) -> None:
        msg = outcome.message or outcome.reason()
        if outcome.status == "unreachable":
            raise ApiError("validation_failed", f"读不到数据集 {src.uri}：{msg}",
                           details={"errors": [{"field": "input", "problem": msg}]})
        if outcome.status == "usage":
            raise ApiError("validation_failed", f"{what}没法做：{msg}",
                           details={"errors": [{"field": "input", "problem": msg}]})
        raise ApiError("internal", f"{what}出错（{outcome.reason()}）：{msg}")

    # -- preflight and listing -----------------------------------------------------------
    def preflight(self, src: Source, owner: str, *, vlm_backend: str | None = None,
                  embodiment_id: str | None = None, modules: list[str] | None = None,
                  params: list[str] | None = None) -> dict:
        """``modules`` narrows the report; ``params`` are ``--param MODULE.KEY=VALUE`` (registry 1.4)."""
        argv = ["preflight", *self._input_args(src)]
        if vlm_backend:
            argv += ["--vlm-backend", vlm_backend]
        if embodiment_id:
            argv += ["--embodiment-id", embodiment_id]
        if modules:
            argv += ["--modules", ",".join(modules)]
        for item in params or []:
            argv += ["--param", item]
        outcome = self._run(src, owner, argv, stage="preflight",
                            timeout_s=self.orch.cfg.preflight_timeout_s)
        if not outcome.ok:
            self._raise(outcome, "预检", src)
        return outcome.doc

    def listing(self, src: Source, owner: str, out: pathlib.Path) -> dict:
        """``curation snapshot`` of every episode into ``out``; the manifest document."""
        out.parent.mkdir(parents=True, exist_ok=True)
        argv = ["snapshot", *self._input_args(src), "--out", str(out)]
        outcome = self._run(src, owner, argv, stage="snapshot",
                            timeout_s=self.orch.cfg.snapshot_timeout_s)
        if not outcome.ok:
            if outcome.status == "source_changed":
                raise ApiError("source_changed", details=None)
            self._raise(outcome, "列出数据集的文件", src)
        return outcome.doc

    def keep_listing(self, doc: dict) -> str:
        """Keep a listing on the data volume, named by its content; returns the path."""
        digest = str((doc.get("summary") or {}).get("digest") or "")
        name = digest.split(":", 1)[-1] or hashlib.sha256(repr(doc).encode()).hexdigest()
        path = pathlib.Path(self.orch.settings.data_dir) / LISTINGS / f"{name}.json"
        if not path.is_file():
            write_json_atomic(path, doc)
        return str(path)

    def scratch_listing(self) -> pathlib.Path:
        base = pathlib.Path(self.orch.settings.scratch_dir) / "listings"
        base.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="snapshot-", suffix=".json", dir=base)
        import os

        os.close(fd)
        return pathlib.Path(name)

    def fresh_listing(self, src: Source, owner: str) -> dict:
        tmp = self.scratch_listing()
        try:
            return self.listing(src, owner, tmp)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def unsupported_fingerprint(preflight: dict) -> dict:
        """Datasets snapshot cannot list (not LeRobot): the preflight's fingerprint stands in."""
        return {"objects": 0, "bytes": 0, "digest": str(preflight.get("meta_fingerprint") or "")}

    # -- registration ----------------------------------------------------------------
    def register(self, src: Source, owner: str, *, name: str | None = None,
                 note: str | None = None) -> tuple[P.Dataset, bool, dict | None]:
        """Get or create the registration of ``src``; returns (dataset, created, listing)."""
        repo = self.orch.repo
        found = repo.find_dataset(source=src.source, uri=src.uri, region=src.region, owner=owner)
        if found is not None:
            if src.source == "tos" and not found.credential_id and src.credential_id:
                found = repo.update_dataset(found.id, owner=owner,
                                            credential_id=src.credential_id)
            return found, False, None
        now = self.orch.clock()
        preflight = self.preflight(src, owner)
        listing_doc = None
        if format_supported(preflight):
            listing_doc = self.fresh_listing(src, owner)
            fingerprint = rules.fingerprint_of(listing_doc)
            manifest_path = self.keep_listing(listing_doc)
        else:
            fingerprint, manifest_path = self.unsupported_fingerprint(preflight), None
        ds, created = repo.register_dataset(P.Dataset(
            id="", name=name or dataset_name(src.uri), note=note, source=src.source,
            uri=src.uri, region=src.region,
            credential_id=src.credential_id if src.source == "tos" else None,
            preflight=preflight, meta_fingerprint=str(preflight.get("meta_fingerprint") or ""),
            source_fingerprint=fingerprint, manifest_path=manifest_path, preflighted_at=now,
            owner_id=owner))
        if created:
            repo.record_dataset_check(P.DatasetCheck(dataset_id=ds.id, at=now, trigger="add",
                                                     result="same"))
            ds = repo.get_dataset(ds.id, owner=owner)
        return ds, created, listing_doc

    def compare(self, ds: P.Dataset, listing_doc: dict | None, meta_fp: str) -> dict | None:
        """The ``SourceChange`` from what the registration keeps to now; None when the same."""
        kept_digest = str((ds.source_fingerprint or {}).get("digest") or "")
        if listing_doc is not None:
            digest = str((listing_doc.get("summary") or {}).get("digest") or "")
        else:
            digest = meta_fp                        # unsupported formats: the meta listing
        meta_changed = bool(ds.meta_fingerprint) and meta_fp != ds.meta_fingerprint
        if digest == kept_digest and not meta_changed:
            return None
        old = read_json(pathlib.Path(ds.manifest_path), None) if ds.manifest_path else None
        if listing_doc is None:
            return {"meta_changed": True, "added": 0, "removed": 0, "modified": 0,
                    "sample_keys": [], "preflighted_at": int(ds.preflighted_at)}
        return rules.source_change(old if isinstance(old, dict) else None, listing_doc,
                                   meta_changed=meta_changed, preflighted_at=ds.preflighted_at,
                                   old_fingerprint=ds.source_fingerprint)

    def check(self, ds: P.Dataset, owner: str, *, trigger: str) -> tuple[P.DatasetCheck, dict | None]:
        """Compare now with the registration and record it; (the check, the new listing)."""
        src = Source.of_dataset(ds)
        if format_supported(ds.preflight):
            listing_doc = self.fresh_listing(src, owner)
            meta_fp = rules.meta_fingerprint(listing_doc)
        else:
            listing_doc = None
            meta_fp = str(self.preflight(src, owner).get("meta_fingerprint") or "")
        change = self.compare(ds, listing_doc, meta_fp)
        record = self.orch.repo.record_dataset_check(P.DatasetCheck(
            dataset_id=ds.id, at=self.orch.clock(), trigger=trigger,
            result="changed" if change else "same", change=change))
        return record, listing_doc

    def repreflight(self, ds: P.Dataset, owner: str) -> tuple[P.Dataset, dict | None]:
        """Preflight and listing again; the registration keeps the new ones (``check_state`` ok)."""
        src = Source.of_dataset(ds)
        now = self.orch.clock()
        preflight = self.preflight(src, owner)
        listing_doc = None
        if format_supported(preflight):
            listing_doc = self.fresh_listing(src, owner)
            fingerprint, manifest_path = rules.fingerprint_of(listing_doc), \
                self.keep_listing(listing_doc)
        else:
            fingerprint, manifest_path = self.unsupported_fingerprint(preflight), None
        meta_fp = str(preflight.get("meta_fingerprint") or "")
        change = self.compare(ds, listing_doc, meta_fp) if format_supported(preflight) else None
        repo = self.orch.repo
        with repo.transaction():
            repo.record_dataset_check(P.DatasetCheck(
                dataset_id=ds.id, at=now, trigger="repreflight",
                result="changed" if change else "same", change=change))
            updated = repo.update_dataset(ds.id, owner=owner, preflight=preflight,
                                          meta_fingerprint=meta_fp, source_fingerprint=fingerprint,
                                          preflighted_at=now, manifest_path=manifest_path)
        return updated, listing_doc


def dataset_name(uri: str) -> str:
    s = str(uri or "").rstrip("/")
    if "://" in s:
        s = s.split("://", 1)[1]
    return (s.rsplit("/", 1)[-1] if "/" in s else s) or str(uri)
