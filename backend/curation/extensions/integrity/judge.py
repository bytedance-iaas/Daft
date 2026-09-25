"""The data integrity module, one episode at a time (design doc 14, D50-D51).

:class:`IntegrityJudge` is built once per ``check`` process (a persistent pipeline
worker keeps it across batches). :meth:`open` does the dataset-level work - the file
plan of every episode, the LeRobot v3 episode table, files nobody references, byte-equal
files, v1's dark-camera prior, and for mcap the comparison with the other episodes - and
writes ``checks/data_integrity/dataset.json``. :meth:`judge` then gives one episode's
``{passed, score, detail}``: its files' structure (L1) and whole read (L2), v1's row
validation (D51) and, with ``decode_test``, every frame decoded (L3).

A file shared by several episodes (LeRobot v3) is analysed once and remembered; a
finding with a position concerns only the episodes whose window it overlaps.
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import threading
from dataclasses import dataclass

from . import files as F
from .findings import DATASET, REJECT, Finding, affects, ordered, outcome

MODULE_ID = "data_integrity"
DATASET_FILE = "dataset.json"
#: pipeline config section ``integrity`` (design doc 14 §3.5), these are its defaults
DEFAULTS = {"count_tolerance_frames": 1, "majority_ratio": 0.8, "rate_outlier_ratio": 0.8,
            "min_peers": 3}
#: a topic whose episodes carry fewer messages than this (a task text, a calibration) has no rate
STREAM_MIN_MESSAGES = 10


@dataclass
class FileRef:
    key: str
    kind: str                               # parquet | mp4 | mcap
    camera: str | None = None
    window: tuple[float, float] | None = None   # the episode's part of a shared video
    shared: bool = False


def _short(camera: str | None) -> str | None:
    if camera is None:
        return None
    from ...cli.lerobot_meta import short_camera

    return short_camera(camera)


class IntegrityJudge:
    module = MODULE_ID

    def __init__(self, ctx, src, cfg: dict, params: dict, run_dir: str, *,
                 selection: list[int] | None = None, max_episodes: int | None = None):
        self.ctx, self.src, self.run_dir = ctx, src, run_dir
        self.decode_test = bool((params or {}).get("decode_test", False))
        self.params = dict(params or {})
        conf = dict(DEFAULTS, **((cfg or {}).get("integrity") or {}))
        self.tolerance = int(conf["count_tolerance_frames"])
        self.majority = float(conf["majority_ratio"])
        self.rate_ratio = float(conf["rate_outlier_ratio"])
        self.min_peers = int(conf["min_peers"])
        self.selection = selection
        self.max_episodes = max_episodes
        self.plan: dict[int, list[FileRef]] = {}
        self.lengths: dict[int, int] = {}
        self.fps: float | None = None
        self.dataset_findings: list[Finding] = []
        self.episode_findings: dict[int, list[Finding]] = {}
        self._reports: dict[str, F.FileReport] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._rows = None

    # ------------------------------------------------------------ setup

    def rebind(self, src) -> None:
        """A later batch's source (its mcap cache has other episodes local)."""
        self.src = src
        if self._rows is not None:
            self._rows.src = src

    def rows(self, todo):
        from .rows import IntegrityRows

        if self._rows is None:
            episodes = sorted(set(self.plan) | {int(e) for e in todo}) if self.plan else todo
            self._rows = IntegrityRows(self.src, episodes, max_episodes=self.max_episodes)
        return self._rows

    def open(self, episodes: list[int] | None = None) -> None:
        """Dataset-level checks over the task's selection (``--selection``), else over the
        whole dataset: a persistent worker is given its episodes a batch at a time."""
        scope = sorted({int(e) for e in self.selection}) if self.selection else None
        kind = self.src.kind
        if scope is None:
            scope = self._all_episodes(episodes or [])
        if kind == "lerobot":
            self._lerobot(scope)
        elif kind == "mcap":
            self._mcap(scope)
        self._duplicates(scope)
        self._write_dataset_file(scope)

    def _all_episodes(self, fallback: list[int]) -> list[int]:
        if self.src.kind == "mcap":
            return sorted(self.src.numbering())
        if self.src.kind == "lerobot":
            from ...cli import lerobot_meta as lm

            info = lm.load_info(self.src.storage)
            fmt = lm.Format("lerobot", lm.version_of(str(info.get("codebase_version") or "")),
                            str(info.get("codebase_version") or ""))
            return sorted(e.index for e in lm.read_dataset(self.src.storage, self.src.listing,
                                                           info, fmt).episodes)
        return sorted({int(e) for e in fallback})

    def _add(self, ep: int, f: Finding) -> None:
        self.episode_findings.setdefault(int(ep), []).append(f)

    def _lerobot(self, scope: list[int]) -> None:
        from ...cli import lerobot_meta as lm

        storage, listing = self.src.storage, self.src.listing
        info = lm.load_info(storage)
        fmt = lm.Format("lerobot", lm.version_of(str(info.get("codebase_version") or "")),
                        str(info.get("codebase_version") or ""))
        meta = lm.read_dataset(storage, listing, info, fmt)
        fps = info.get("fps")
        self.fps = float(fps) if isinstance(fps, (int, float)) and fps > 0 else None
        wanted = set(scope)
        v3 = fmt.version == "v3"
        windows: dict[int, dict[str, tuple[float, float]]] = {}
        if v3:
            windows = self._v3_table(lm, storage, listing, meta, wanted)
        for ep in meta.episodes:
            self.lengths[ep.index] = int(ep.length)
            if ep.index not in wanted:
                continue
            refs = [FileRef(k, "parquet", shared=v3) for k in ep.data_keys if k in listing]
            for cam, key in ep.video_keys.items():
                if key in listing:
                    refs.append(FileRef(key, "mp4", _short(cam),
                                        windows.get(ep.index, {}).get(cam) if v3 else None, v3))
            self.plan[ep.index] = refs
        referenced = {k for ep in meta.episodes for k in list(ep.data_keys) + list(ep.video_keys.values())}
        orphans = sorted(k for k in listing if k.split("/", 1)[0] in ("data", "videos")
                         and k not in referenced)
        if orphans:
            shown = "、".join(orphans[:5]) + ("…" if len(orphans) > 5 else "")
            self.dataset_findings.append(Finding(
                "orphan_files", f"data/ 与 videos/ 下有 {len(orphans)} 个文件不属于任何 episode：{shown}",
                "dataset", args={"count": len(orphans), "files": orphans[:50]}))
        try:
            from ...ingest.validate import stats_prior_warnings

            for text in stats_prior_warnings(self.src.input_dir):
                self.dataset_findings.append(Finding("dark_camera", text, "dataset"))
        except Exception:  # noqa: BLE001 - a prior, never a verdict
            pass

    def _v3_table(self, lm, storage, listing, meta, wanted) -> dict:
        """The episode table's own consistency, and each episode's video window per camera."""
        table = lm._v3_table(storage, listing)
        out: dict[int, dict[str, tuple[float, float]]] = {}
        fps = self.fps
        spans = []
        for _, row in table.iterrows():
            idx = int(row["episode_index"])
            length = int(row["length"])
            if "dataset_from_index" in table.columns and "dataset_to_index" in table.columns:
                lo, hi = int(row["dataset_from_index"]), int(row["dataset_to_index"])
                spans.append((lo, hi, idx))
                if hi - lo != length and idx in wanted:
                    self._add(idx, Finding("table_inconsistent",
                                           f"episode 表里帧区间 [{lo}, {hi}) 有 {hi - lo} 帧，长度却记 {length}",
                                           "dataset"))
            for cam in meta.cameras:
                a, b = f"videos/{cam}/from_timestamp", f"videos/{cam}/to_timestamp"
                if a not in table.columns or b not in table.columns:
                    continue
                t0, t1 = float(row[a]), float(row[b])
                out.setdefault(idx, {})[cam] = (t0, t1)
                if fps and idx in wanted and abs((t1 - t0) * fps - length) > self.tolerance + 0.5:
                    self._add(idx, Finding(
                        "table_inconsistent",
                        f"{_short(cam)} 相机在 episode 表里的视频时间段 {t1 - t0:.2f} 秒，与 {length} 帧 / {fps:g} fps 不符",
                        "dataset", camera=_short(cam)))
        spans.sort()
        bad = set()
        for (lo, hi, a), (lo2, _hi2, b) in zip(spans, spans[1:]):
            if lo2 != hi:
                bad |= {a, b}
        if spans and spans[0][0] != 0:
            bad.add(spans[0][2])
        if bad:
            shown = ", ".join(str(e) for e in sorted(bad)[:10])
            self.dataset_findings.append(Finding(
                "table_overlap", f"episode 表的帧区间有重叠或空缺，涉及 {len(bad)} 条：{shown}", "dataset",
                args={"episodes": sorted(bad)}))
            for e in sorted(bad & wanted):
                self._add(e, Finding("table_inconsistent", "episode 表里本条的帧区间与相邻条重叠或不连续",
                                     "dataset"))
        return out

    def _mcap(self, scope: list[int]) -> None:
        from ...cli import containers

        num = self.src.numbering()
        for ep in scope:
            if ep in num:
                self.plan[ep] = [FileRef(num[ep], "mcap")]
        keys = [num[e] for e in scope if e in num]
        summaries = containers.mcap_summaries(self.src.storage, self.src.listing, keys)
        facts = {}
        for ep in scope:
            s = summaries.get(num.get(ep, ""))
            if s is None or not s.indexed or s.start_ns is None or s.end_ns is None:
                continue
            seconds = (s.end_ns - s.start_ns) / 1e9
            if seconds <= 0:
                continue
            facts[ep] = {t: ((c or 0), (c or 0) / seconds) for t, c in s.topics.items() if c is not None}
        if len(facts) < self.min_peers:
            return
        topics = sorted({t for rates in facts.values() for t, (n, _r) in rates.items() if n > 0})
        for t in topics:
            having = [ep for ep, rates in facts.items() if rates.get(t, (0, 0))[0] > 0]
            if len(having) / len(facts) < self.majority:
                continue
            median = statistics.median(facts[ep][t][1] for ep in having)
            # a rate means something for a stream only, not for a topic written once or twice
            stream = statistics.median(facts[ep][t][0] for ep in having) >= STREAM_MIN_MESSAGES
            for ep, rates in facts.items():
                count, rate = rates.get(t, (0, 0.0))
                if count <= 0:
                    self._add(ep, Finding("stream_missing",
                                          f"缺 topic {t}（其他 {len(facts) - 1} 条里有 {len(having)} 条都有）",
                                          "peers", file=num[ep], args={"topic": t}))
                elif stream and rate < self.rate_ratio * median:
                    self._add(ep, Finding("rate_outlier",
                                          f"topic {t} 的频率 {rate:.1f} Hz，全数据集中位数 {median:.1f} Hz",
                                          "peers", file=num[ep],
                                          args={"topic": t, "rate_hz": round(rate, 3),
                                                "median_hz": round(median, 3)}))

    def _duplicates(self, scope: list[int]) -> None:
        """Byte-equal video or mcap files of different episodes (LeRobot v2, mcap)."""
        listing = self.src.listing
        by_id: dict[str, list[tuple[int, FileRef]]] = {}
        for ep in scope:
            for ref in self.plan.get(ep, []):
                if ref.shared or ref.kind == "parquet":
                    continue                    # v3 files are shared; parquet copies are dedup's
                ident = self._identity(ref.key, listing.get(ref.key))
                if ident is not None:
                    by_id.setdefault(ident, []).append((ep, ref))
        for group in by_id.values():
            if len(group) < 2:
                continue
            for ep, ref in group:
                others = [(e, r) for e, r in group if r is not ref]
                what = f"{ref.camera} 相机的视频" if ref.camera else "文件"
                whom = "、".join(f"ep {e}" + (f"（{r.camera}）" if r.camera and r.camera != ref.camera else "")
                                for e, r in others[:3])
                self._add(ep, Finding("duplicate_content", f"{what}与 {whom} 的内容完全相同",
                                      "dataset", file=ref.key, camera=ref.camera,
                                      args={"same_as": [r.key for _, r in others]}))

    def _identity(self, key: str, obj) -> str | None:
        if obj is None or int(obj.size) == 0:
            return None
        crc = getattr(obj, "crc64", None)
        if crc:
            return f"crc64:{crc}:{obj.size}"
        etag = str(obj.etag or "").strip('"')
        if etag and "-" not in etag:            # a single-part upload: the MD5 of the bytes
            return f"md5:{etag}:{obj.size}"
        if self.src.remote:
            return None
        path = F.local_path(self.src.storage.root, key)
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                h.update(fh.read(65536))
                if obj.size > 131072:
                    fh.seek(-65536, os.SEEK_END)
                    h.update(fh.read(65536))
        except OSError:
            return None
        return f"head-tail:{h.hexdigest()}:{obj.size}"

    def _write_dataset_file(self, scope: list[int]) -> None:
        from ...pipeline.records import module_dir, write_json_atomic

        doc = {"schema_version": "1.0", "module": MODULE_ID, "format": self.src.kind,
               "episodes_in_scope": len(scope),
               "findings": [f.to_json() for f in ordered(self.dataset_findings)],
               "episode_findings": {str(e): [f.to_json() for f in ordered(fs)]
                                    for e, fs in sorted(self.episode_findings.items())}}
        write_json_atomic(os.path.join(module_dir(self.run_dir, MODULE_ID), DATASET_FILE), doc)

    # ------------------------------------------------------------ one episode

    def _blob(self, key: str) -> F.Blob:
        storage, obj = self.src.storage, self.src.listing[key]
        if not storage.remote:
            return F.Blob(key, int(obj.size), path=F.local_path(storage.root, key))
        return F.Blob(key, int(obj.size), ranged=lambda s, n: storage.read_range(key, s, n))

    def _analyse(self, ep: int, ref: FileRef) -> F.FileReport:
        size = int(self.src.listing[ref.key].size)
        rep = F.FileReport(ref.key, ref.kind, size, camera=ref.camera)
        try:
            if ref.kind == "parquet":
                blob = self._blob(ref.key)
                F.parquet_l1(blob, rep)
                F.parquet_l2(blob, rep)
            elif ref.kind == "mp4":
                blob = self._blob(ref.key)
                F.mp4_l1(blob, rep)
                F.mp4_l2(blob, rep)
            else:
                self._fetch(ep)                 # a remote mcap is read from its local copy
                path = F.local_path(self.src.input_dir, ref.key)
                F.mcap_l1(path, rep)
                F.mcap_l2(path, rep)
        except F.ReadFailure as e:
            rep.read_error = str(e)
        except OSError as e:                    # a local copy that went away
            rep.read_error = f"{type(e).__name__}: {e}"[:300]
        return rep

    def _fetch(self, ep: int) -> None:
        from ...cli.errors import SourceChanged

        try:
            self.src.fetch([ep])
        except SourceChanged:
            raise                               # D27: the command ends, exit 6
        except Exception as e:  # noqa: BLE001 - the download failed: the storage's, not the file's
            raise F.ReadFailure(f"{type(e).__name__}: {e}"[:300]) from e

    def _file(self, ep: int, ref: FileRef) -> F.FileReport:
        if not ref.shared:
            return self._analyse(ep, ref)
        with self._guard:
            lock = self._locks.setdefault(ref.key, threading.Lock())
        with lock:
            rep = self._reports.get(ref.key)
            if rep is None:
                rep = self._analyse(ep, ref)
                if rep.read_error is None:      # a storage failure is tried again next time
                    self._reports[ref.key] = rep
            return rep

    def judge(self, ep: int, row: dict | None, row_error: Exception | None, log) -> dict:
        from ...ingest.validate import IngestValidationError

        ep = int(ep)
        findings = list(self.episode_findings.get(ep, []))
        infra: list[str] = []
        files = []
        video_counts: dict[str, int] = {}
        cut_off = False
        for ref in self.plan.get(ep, []):
            rep = self._file(ep, ref)
            if rep.read_error:
                infra.append(f"{ref.key}: {rep.read_error}")
                continue
            summary = rep.summary()
            if ref.camera:
                summary["camera"] = ref.camera
            if ref.window is not None:
                summary["window_s"] = [round(ref.window[0], 3), round(ref.window[1], 3)]
            files.append(summary)
            mine = [f for f in rep.findings if affects(f, ref.window)]
            findings += mine
            cut_off |= rep.cut_off
            if rep.kind == "mp4" and rep.samples is not None \
                    and not any(f.level == REJECT for f in mine):
                eps = 0.5 / (self.fps or 30.0)
                n = rep.samples.count_in(*ref.window, eps) if ref.window else len(rep.samples)
                video_counts[ref.camera or ref.key] = n
                length = self.lengths.get(ep)
                if length is not None and abs(n - length) > self.tolerance:
                    findings.append(Finding("count_mismatch",
                                            f"{ref.camera} 相机的视频有 {n} 帧，episode 表记 {length} 帧",
                                            "L1", file=ref.key, camera=ref.camera,
                                            args={"frames": n, "length": length}))
        if cut_off:
            findings.append(Finding("cut_off", "录制中断：文件尾没有 mcap 结束标识，读得到的部分完好",
                                    "L1", file=(self.plan.get(ep) or [FileRef("", "mcap")])[0].key))
        rejected = any(f.level == REJECT for f in findings)
        if row_error is not None:
            cause = row_error.__cause__
            if isinstance(cause, IngestValidationError):
                findings.append(Finding("row_invalid", f"数据不合规：{cause}", "L2",
                                        args={"error": str(cause)[:300]}))
            elif rejected:
                pass                            # the files already say why it cannot be read
            elif cut_off:
                findings = [f for f in findings if f.code != "cut_off"]
                why = type(row_error.__cause__ or row_error).__name__
                findings.append(Finding("file_truncated", f"录制中断，且读不出数据（{why}）", "L2",
                                        file=self.plan[ep][0].key, args={"error": str(row_error)[:300]}))
            else:
                infra.append(f"read: {row_error}")
        elif row is not None:
            self._row_checks(ep, row, findings, video_counts)
            if self.decode_test and not any(f.level == REJECT for f in findings):
                self._decode(ep, row, findings, video_counts, infra)
        rejected = any(f.level == REJECT for f in findings)
        if infra and not rejected:
            for what in infra:
                log.add("read", cause=what)
        passed, reason = outcome(findings)
        details = {"outcome": {True: "pass", False: "reject", None: "suspect"}[passed],
                   "reason": reason,
                   "tiers": {"L1": True, "L2": True, "L3": self.decode_test},
                   "findings": [f.to_json() for f in ordered(findings) if f.level != DATASET],
                   "files": files}
        return {"passed": passed, "score": None,
                "detail": json.dumps(details, ensure_ascii=False)}

    def _row_checks(self, ep: int, row: dict, findings: list, video_counts: dict) -> None:
        length = self.lengths.get(ep)
        try:
            n = len(row["action"])
        except Exception:  # noqa: BLE001 - validate_episode_row already passed it
            return
        if length is not None and abs(n - length) > self.tolerance:
            findings.append(Finding("count_mismatch", f"数据有 {n} 帧，episode 表记 {length} 帧", "L2",
                                    args={"rows": n, "length": length}))

    def _decode(self, ep: int, row: dict, findings: list, video_counts: dict, infra: list) -> None:
        from .decode import decode

        length = self.lengths.get(ep)
        counted = {f.camera for f in findings if f.code == "count_mismatch" and f.camera}
        for cam, v in sorted((row.get("video") or {}).items()):
            short = _short(cam)
            try:
                got = decode(str(v["path"]), float(v.get("from_ts") or 0.0), float(v["to_ts"]))
            except F.ReadFailure as e:
                infra.append(f"decode {short}: {e}")
                continue
            if got.error:
                findings.append(Finding("decode_failed", f"{short} 相机解码失败：{got.error}", "L3",
                                        camera=short, args={"frames_decoded": got.frames}))
                continue
            if got.concealed:
                findings.append(Finding("decode_concealed",
                                        f"{short} 相机解码时报了 {len(got.concealed)} 处错误（解码器掩盖后继续）："
                                        f"{got.concealed[0]}", "L3", camera=short,
                                        args={"errors": got.concealed}))
            if self.src.kind == "lerobot" and length is not None and short not in counted \
                    and abs(got.frames - length) > self.tolerance:
                findings.append(Finding("count_mismatch", f"{short} 相机解码出 {got.frames} 帧，episode 表记 {length} 帧",
                                        "L3", camera=short, args={"frames": got.frames, "length": length}))

    # ------------------------------------------------------------ bookkeeping

    def stale(self, current: dict[int, dict]) -> set[int]:
        """Episodes whose line was made with the other ``decode_test`` (redone on --resume)."""
        out = set()
        for ep, rec in current.items():
            tiers = (rec.get("details") or {}).get("tiers") or {}
            if bool(tiers.get("L3")) != self.decode_test:
                out.add(int(ep))
        return out

    def input_digest(self, episodes) -> str:
        text = "".join(f"{int(e)}\n" for e in sorted(set(int(x) for x in episodes)))
        text += json.dumps({"params": self.params, "tolerance": self.tolerance,
                            "majority": self.majority, "rate": self.rate_ratio}, sort_keys=True)
        return "sha256:" + hashlib.sha256(text.encode()).hexdigest()
