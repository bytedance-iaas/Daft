"""The export directory on a filesystem: local disk, or TOS through an FSX mount.

FSX rejects random writes (a PyAV muxer seeking back to patch ``moov`` gets EINVAL,
pyarrow gets EPERM; see ``safe_write`` and ``lerobot_writer._reencode_concat``).
So nothing ever *produces* a file in here.  Encoders and parquet writers write to
local scratch; what lands in the export directory is a whole-file sequential copy
into a temporary name next to the destination, renamed into place.  A reader sees
the old file or the new one, never a partial one, and the mount never sees a seek.

The same holds for the two other things the incremental export does here: renames
(``os.replace`` inside one mount) and deletes.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import tempfile

#: Same prefix as safe_write's staging names, so ``publish`` and ``sync_back`` already
#: skip them and a crashed run's leftovers are recognisable.
TEMP_PREFIX = ".curation-"
_CHUNK = 8 * 1024 * 1024


def sha256_file(path: str) -> tuple[int, str]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(_CHUNK)
            if not b:
                break
            h.update(b)
            n += len(b)
    return n, "sha256:" + h.hexdigest()


def copy_hashing(src: str, dst: str) -> tuple[int, str]:
    """Sequential whole-file copy that hashes on the way (one read of ``src``)."""
    h = hashlib.sha256()
    n = 0
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            b = fi.read(_CHUNK)
            if not b:
                break
            h.update(b)
            fo.write(b)
            n += len(b)
    return n, "sha256:" + h.hexdigest()


class LocalTarget:
    """The export directory (``<run-dir>/export`` or any path, FSX mounts included)."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def path(self, rel: str) -> str:
        rel = rel.replace("\\", "/")
        if rel.startswith("/") or ".." in rel.split("/"):
            raise ValueError(f"not a relative path inside the export directory: {rel}")
        return os.path.join(self.root, *rel.split("/"))

    def exists(self, rel: str) -> bool:
        return os.path.exists(self.path(rel))

    def size(self, rel: str) -> int | None:
        try:
            return os.path.getsize(self.path(rel))
        except OSError:
            return None

    def read_bytes(self, rel: str) -> bytes:
        with open(self.path(rel), "rb") as f:
            return f.read()

    def read_json(self, rel: str):
        return json.loads(self.read_bytes(rel).decode("utf-8"))

    def list_files(self, prefix: str = "") -> dict[str, int]:
        """Every file under ``prefix`` -> size, as root-relative POSIX paths."""
        base = self.path(prefix) if prefix else self.root
        out: dict[str, int] = {}
        if not os.path.isdir(base):
            return out
        for cur, _dirs, names in os.walk(base):
            for name in names:
                full = os.path.join(cur, name)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                try:
                    out[rel] = os.path.getsize(full)
                except OSError:
                    continue
        return out

    # ── writes ──────────────────────────────────────────────────────────────
    def put_file(self, local_src: str, rel: str) -> tuple[int, str]:
        """Copy a finished local file into place; returns (size, sha256).

        Sequential copy to ``.curation-pub-*`` in the destination directory, then an
        atomic rename (the same two steps as ``safe_write._publish``, plus hashing).
        """
        dst = self.path(rel)
        parent = os.path.dirname(dst)
        os.makedirs(parent, exist_ok=True)
        fd, staged = tempfile.mkstemp(prefix=TEMP_PREFIX + "pub-", dir=parent)
        os.close(fd)
        try:
            size, digest = copy_hashing(local_src, staged)
            # mkstemp makes 0600; a delivered file follows its directory instead, the rule
            # safe_write.delivery_dir uses for directories (no process-wide umask probe)
            with contextlib.suppress(OSError):
                os.chmod(staged, stat.S_IMODE(os.stat(parent).st_mode) & 0o666)
            os.replace(staged, dst)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(staged)
            raise
        return size, digest

    def write_json(self, rel: str, payload, *, compact: bool = False) -> tuple[int, str]:
        """JSON through local scratch and ``put_file`` (``safe_write.write_json``'s encoding;
        ``compact`` for big machine-only files)."""
        fd, tmp = tempfile.mkstemp(prefix=TEMP_PREFIX + "json-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                if compact:
                    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
                else:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
            return self.put_file(tmp, rel)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    def rename(self, src_rel: str, dst_rel: str) -> None:
        dst = self.path(dst_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(self.path(src_rel), dst)

    def delete(self, rel: str) -> None:
        path = self.path(rel)
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        self._prune_empty(os.path.dirname(path))

    def remove_tree(self, rel: str) -> None:
        path = self.path(rel)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)

    def make_stage_dir(self, parent_rel: str) -> str:
        """A fresh temporary directory (root-relative) for two-phase renames."""
        parent = self.path(parent_rel)
        os.makedirs(parent, exist_ok=True)
        full = tempfile.mkdtemp(prefix=TEMP_PREFIX + "stage-", dir=parent)
        return os.path.relpath(full, self.root).replace(os.sep, "/")

    def _prune_empty(self, directory: str) -> None:
        """Remove empty directories up to (not including) the root."""
        root = self.root
        cur = os.path.abspath(directory)
        while cur.startswith(root + os.sep):
            try:
                os.rmdir(cur)
            except OSError:
                return
            cur = os.path.dirname(cur)
