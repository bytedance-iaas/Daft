"""Where an mcap dataset's bytes come from: a local directory, or TOS over ranged reads.

``ingest.mcap_reader`` was written against a local directory - it globs ``*.mcap`` and
opens paths. A ``tos://`` dataset used to be made to fit by downloading every episode
into a local cache first. This module is the seam that removes the download: the reader
asks an :class:`McapObjects` for its episodes and for a seekable stream per episode,
and a remote implementation answers with a :class:`~curation.streams.rangefile.RangeFile`.

Two ways in, on purpose:

* explicit - pass ``objects=`` to the reader's public functions;
* the registry - :func:`bind` a dataset URI once (the v2 ``Source`` does this at start
  and :func:`unbind`\\ s on close), and every reader call for that URI picks it up.

The registry exists because the reader's public signatures take ``dataset_dir: str`` and
are called from a dozen places across v1 and v2; threading an object through all of them
would be a far larger change than the one this seam is for. It is the same shape as the
process-level state the package already keeps (``dsfs.configure``, ``_VIDEO_DIRS``).
"""
from __future__ import annotations

import glob
import os
import re
import threading
from typing import BinaryIO, Protocol, runtime_checkable

#: The v1 naming contract. Kept here as well as in mcap_reader so the numbering rule
#: can run over a listing (no local files) without importing the A-class module.
EPISODE_RE = re.compile(r"episode_(\d+)\.mcap$")


def number_episodes(names: list[str]) -> list[tuple[int, str]]:
    """``[name] -> [(episode index, name)]``, v1's rule, sorted by index.

    Names matching ``episode_<N>.mcap`` are numbered by N and anything else is ignored
    (a stray ``calib.mcap`` must not renumber the dataset, nor be read as an episode).
    With no conforming name at all, the sorted position is the number.

    The caller reports strays; this stays a pure function so the same rule can serve a
    local glob and a remote listing.
    """
    ordered = sorted(names)
    matched = [(EPISODE_RE.search(os.path.basename(n)), n) for n in ordered]
    named = [(int(m.group(1)), n) for m, n in matched if m]
    items = named if named else [(i, n) for i, (_, n) in enumerate(matched)]
    items.sort(key=lambda t: t[0])
    return items


def strays(names: list[str]) -> list[str]:
    """The names :func:`number_episodes` ignores, for the warning the reader prints."""
    matched = [(EPISODE_RE.search(os.path.basename(n)), n) for n in sorted(names)]
    if not any(m for m, _ in matched):
        return []
    return [os.path.basename(n) for m, n in matched if not m]


@runtime_checkable
class McapObjects(Protocol):
    """One mcap dataset's objects, wherever they live."""

    uri: str

    def names(self) -> list[str]:
        """Every ``*.mcap`` name in the dataset, unordered."""

    def open(self, name: str) -> BinaryIO:
        """A seekable, read-only stream over one episode file."""

    def object_uri(self, name: str) -> str:
        """The addressable URI of one episode file (``tos://…`` or a local path)."""

    def identity(self, name: str) -> tuple[int, str]:
        """``(size, content identity)`` - an ETag remotely, the mtime locally."""

    def copy_to(self, name: str, dst: str) -> int:
        """Write one episode file out whole, for the paths that need every byte.

        Delivery copies the source file into ``mcap_curated/`` byte for byte, so it
        cannot be served by ranged reads of the parts a check happens to want. This is
        the one place a streamed dataset still transfers a whole object - into the
        delivery it is producing, not into a cache of the source.
        """


class LocalDirObjects:
    """The behaviour the reader has always had: a directory of ``.mcap`` files."""

    def __init__(self, dataset_dir: str) -> None:
        self.uri = str(dataset_dir)

    def names(self) -> list[str]:
        return [os.path.basename(p) for p in glob.glob(os.path.join(self.uri, "*.mcap"))]

    def open(self, name: str) -> BinaryIO:
        return open(os.path.join(self.uri, name), "rb")

    def object_uri(self, name: str) -> str:
        return os.path.join(self.uri, name)

    def identity(self, name: str) -> tuple[int, str]:
        st = os.stat(os.path.join(self.uri, name))
        return st.st_size, str(st.st_mtime_ns)

    def copy_to(self, name: str, dst: str) -> int:
        import shutil

        src = os.path.join(self.uri, name)
        shutil.copyfile(src, dst)                      # sequential whole copy, FSX-safe
        return os.path.getsize(dst)


# ---------------------------------------------------------------- the registry

_BOUND: dict[str, McapObjects] = {}
_LOCK = threading.Lock()


def _key(dataset_uri: str) -> str:
    """Normalise so a bind and a later lookup agree.

    Only local paths are made absolute: ``os.path.abspath`` on a ``tos://`` URI would
    prefix it with the working directory.
    """
    s = str(dataset_uri)
    return s if "://" in s else os.path.abspath(s)


def bind(dataset_uri: str, objects: McapObjects) -> None:
    with _LOCK:
        _BOUND[_key(dataset_uri)] = objects


def unbind(dataset_uri: str) -> None:
    with _LOCK:
        _BOUND.pop(_key(dataset_uri), None)


def is_bound(dataset_uri: str) -> bool:
    with _LOCK:
        return _key(dataset_uri) in _BOUND


def bound(dataset_uri: str) -> McapObjects | None:
    with _LOCK:
        return _BOUND.get(_key(dataset_uri))


def resolve(dataset_dir: str, objects: McapObjects | None = None) -> McapObjects:
    """What the reader should read from: the explicit object, the bound one, or the dir."""
    if objects is not None:
        return objects
    return bound(dataset_dir) or LocalDirObjects(dataset_dir)


# ---------------------------------------------------------------- one episode file by its path

def join(dataset: str, name: str) -> str:
    """The path of one of a dataset's objects: ``<uri>/<name>`` for a ``tos://`` dataset (a
    ``pathlib`` or ``os.path`` join would fold the scheme's ``//``), an OS path for a local one."""
    s = str(dataset)
    return f"{s.rstrip('/')}/{name}" if "://" in s else os.path.join(s, name)


def locate(path: str) -> tuple[McapObjects, str]:
    """The objects holding the episode file ``path`` and its name among them.

    For code that is handed a file path rather than the dataset (the EEF module's views name
    the episode file). An mcap episode is a top-level object of its dataset (``mcap_keys``),
    so ``path`` is ``<dataset>/<name>``: the dataset bound under that URI, else a local
    directory. A ``tos://`` path whose dataset nobody bound raises ``FileNotFoundError``.
    """
    s = str(path)
    if "://" in s:
        dataset, _, name = s.rpartition("/")
        objs = bound(dataset)
        if objs is None:
            raise FileNotFoundError(f"{s}: its dataset is not open for reading (streams.objects.bind)")
        return objs, name
    dataset, name = os.path.split(os.path.abspath(s))
    return bound(dataset) or LocalDirObjects(dataset), name
