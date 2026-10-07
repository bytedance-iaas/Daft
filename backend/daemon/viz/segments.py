"""Episode slices of shared camera mp4s, and faststart copies (design doc 21 §4, D69).

A LeRobot v3 camera (and a Lance blob of one) is one mp4 holding many episodes. The Daemon cuts an
episode out of it at GOP boundaries without re-encoding (:mod:`curation.viz.segment`), reading only the
``moov`` and the episode's bytes - through a :class:`~curation.streams.rangefile.RangeFile` on TOS or a
blob, a plain file when local - and keeps the slice in the disk cache (``segment/<digest>/ep<N>/``):

* **always** as the input of a transcode (a camera the browser cannot play): no whole-file download of a
  500 MB shared file for one episode any more;
* **with ``CURATOR_VIZ_SEGMENT=1``** as what the browser plays (``access: remux``, the ``.mp4`` route with
  ``?segment=1``), and a single-episode mp4 whose ``moov`` sits at its end as a copy with it in front.

A slice's 0 is its first packet's decode time, ``start_s`` in the source: the episode answer places the
episode at ``from_ts - start_s`` in it (D69: times are those of the media ``url`` serves).
"""
from __future__ import annotations

import contextlib
import json
import pathlib
import threading
from typing import Callable, ContextManager

from curation.streams.rangefile import RangeFile
from curation.viz.segment import cut, keyframe_before, moov_at_end

from ..results.files import LRU
from .media import digest
from .source import Access, VizSource

#: a ranged read for the ``moov`` and one packet need not bring megabytes behind it
SMALL_READAHEAD = 256 * 1024

Opener = Callable[..., ContextManager]


class Segments:
    def __init__(self, svc):
        self.svc = svc
        self.enabled = bool(getattr(svc.rt.settings, "viz_segment", False))
        self._starts = LRU(max_items=4096)
        self._tails = LRU(max_items=4096)
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ where a camera's mp4 is read from
    def opener(self, src: VizSource, rel: str) -> Opener:
        """A LeRobot camera file: the local file, or a RangeFile over the TOS object."""

        @contextlib.contextmanager
        def open_(small: bool = False):
            access = Access(self.svc.rt, src)
            if src.is_local:
                with open(access.local_file(rel), "rb") as fh:
                    yield fh
                return
            with access.storage() as st:
                size = src.size_of(rel)
                if size is None:
                    info = st.stat(rel)
                    if info is None:
                        from ..errors import ApiError

                        raise ApiError("not_found", f"数据集里没有 {rel}")
                    size = info.size
                kw = {"readahead": SMALL_READAHEAD} if small else {}
                yield RangeFile(lambda s, n: st.read_range(rel, s, n), int(size), name=rel, **kw)

        return open_

    @staticmethod
    def blob_opener(blob, name: str) -> Opener:
        """A Lance blob (design doc 19 §4.3): read by range like an object."""

        @contextlib.contextmanager
        def open_(small: bool = False):
            kw = {"readahead": SMALL_READAHEAD} if small else {}
            yield RangeFile(blob.read_range, int(blob.size()), name=name, **kw)

        return open_

    # ------------------------------------------------------------ what the episode answer says
    def place(self, src: VizSource, ident: str, open_: Opener, frm: float | None) -> dict | None:
        """Whether the Daemon serves this camera of the episode as a slice (switch on): ``{start_s, whole}``
        - an episode of a shared file starts ``from - start_s`` into its slice; a whole single-episode
        file with its ``moov`` at the end is copied with it in front. None: the browser reads the file."""
        if not self.enabled:
            return None
        if frm is not None:
            key = (src.fingerprint, ident, round(float(frm), 6))
            start = self._starts.get(key)
            if start is None:
                with open_(small=True) as fh:
                    start = keyframe_before(fh, float(frm))
                self._starts.put(key, start)
            return {"start_s": float(start), "whole": False}
        key = (src.fingerprint, ident)
        tail = self._tails.get(key)
        if tail is None:
            with open_(small=True) as fh:
                size = getattr(fh, "size", None)
                if size is None:
                    fh.seek(0, 2)
                    size = fh.tell()

                def read(start: int, n: int) -> bytes:
                    fh.seek(start)
                    return fh.read(n)

                tail = moov_at_end(read, int(size))
            self._tails.put(key, tail)
        return {"start_s": 0.0, "whole": True} if tail else None

    # ------------------------------------------------------------ the slice itself
    def file(self, src: VizSource, index: int, camera: str, open_: Opener, frm: float | None,
             to: float | None) -> tuple[pathlib.Path, dict]:
        """The slice of one camera of an episode (the whole file when ``frm`` is None), cut once."""
        path = self.svc.disk.path("segment", digest(src.scope, src.id, src.fingerprint), f"ep{int(index):06d}",
                                  f"{camera}.mp4")
        meta = path.with_suffix(".json")
        with self._lock:
            lock = self._locks.setdefault(str(path), threading.Lock())
        with lock:
            if self.svc.disk.get(path) is not None and meta.is_file():
                with contextlib.suppress(OSError, ValueError):
                    return path, json.loads(meta.read_text(encoding="utf-8"))
            with open_() as fh:
                seg = cut(fh, str(path), frm, to if frm is not None else None)
            meta.write_text(json.dumps(seg.as_dict()), encoding="utf-8")
            self.svc.disk.added(meta)
            self.svc.disk.added(path, keep=(meta,))
            return path, seg.as_dict()
