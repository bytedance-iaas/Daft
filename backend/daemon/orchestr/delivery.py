"""The delivery directory: batches, ``latest``, publishing one at a time (design doc 06 §1, D29).

```
<delivery>/
├── <run_id>/        one task's batch; never written by another task
│   ├── ...          the run directory, synced as it grows
│   └── _COMPLETE    written last by ``curation verify``
└── latest           the run_id of the last batch published complete (P13)
```

* **run ids** are the start time (``YYYYMMDD-HHMMSS``, site time zone); a name
  already taken gets ``-2``, ``-3``... and is claimed at once by writing its
  ``run.json``, so two tasks never share a batch;
* **sync**: the Daemon uploads the run directory (not the delivered dataset - the
  CLI's ``export --output`` uploads that - and not work in progress: hidden files,
  ``inflight.json``, temporary files, ``_COMPLETE``); a local record of what went up
  keeps each sync to what changed;
* **publishing is serial per delivery directory**: export, sync, verify,
  ``_COMPLETE`` and ``latest`` run under one lock per normalized delivery key;
  ``latest`` moves only when the batch is complete (task succeeded, dataset
  exported and not stale, verified).

Two back ends: TOS through W8's client (the output key), and - experimental, for
debugging and tests only - a local directory standing in for the bucket
(``CURATOR_LOCAL_DELIVERY_ROOT``: ``tos://bucket/prefix`` -> ``<root>/bucket/prefix``).
"""
from __future__ import annotations

import contextlib
import logging
import os
import pathlib
import shutil
import threading
from typing import Callable, Iterator

from ..secrets import tos as T
from .workdir import read_json, write_json_atomic

log = logging.getLogger("daemon.orchestr")

LATEST = "latest"
COMPLETE = "_COMPLETE"
EXPORT_DATASET = "export/lerobot_curated"
#: names in the run directory that are never delivered by the sync (verify skips them too)
_SKIP_NAMES = frozenset({"inflight.json", COMPLETE, LATEST, "_EXPORTING"})
_LIST_PAGE = 1000


class DeliveryError(Exception):
    """The delivery location cannot be read or written; ``message_zh`` is for people."""

    def __init__(self, message_zh: str, cause: str = ""):
        super().__init__(f"{message_zh} ({cause})" if cause else message_zh)
        self.message_zh = message_zh
        self.cause = cause


def _join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


class Delivery:
    """A delivery directory ``uri`` (``tos://bucket/prefix``); paths are relative to it."""

    uri: str
    local: bool = False

    def cli_uri(self, rel: str = "") -> str:
        """What a CLI command gets as ``--output`` for ``rel``."""
        raise NotImplementedError

    def display(self, rel: str = "") -> str:
        return _join(self.uri, rel) if rel else self.uri

    def exists(self, rel_prefix: str) -> bool:
        raise NotImplementedError

    def list(self, rel_prefix: str) -> dict[str, int]:
        """``{relative path: size}`` of every object under ``rel_prefix`` (recursively)."""
        raise NotImplementedError

    def put_file(self, rel: str, path: str) -> None:
        raise NotImplementedError

    def put_bytes(self, rel: str, data: bytes) -> None:
        raise NotImplementedError

    def get_bytes(self, rel: str) -> bytes | None:
        raise NotImplementedError

    def delete(self, rel: str) -> None:
        raise NotImplementedError


class LocalDelivery(Delivery):
    local = True

    def __init__(self, root: pathlib.Path, uri: str):
        self.root = pathlib.Path(root)
        self.uri = uri

    def _p(self, rel: str) -> pathlib.Path:
        path = (self.root / rel).resolve() if rel else self.root.resolve()
        base = self.root.resolve()
        if path != base and base not in path.parents:
            raise DeliveryError("交付目录之外的路径", rel)
        return path

    def cli_uri(self, rel: str = "") -> str:
        return str(self._p(rel))

    def exists(self, rel_prefix: str) -> bool:
        p = self._p(rel_prefix)
        if p.is_file():
            return True
        return p.is_dir() and any(p.iterdir())

    def list(self, rel_prefix: str) -> dict[str, int]:
        base = self._p(rel_prefix)
        out: dict[str, int] = {}
        if base.is_file():
            return {rel_prefix: base.stat().st_size}
        if not base.is_dir():
            return out
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                full = pathlib.Path(dirpath) / name
                rel = full.relative_to(self.root.resolve()).as_posix()
                try:
                    out[rel] = full.stat().st_size
                except OSError:
                    continue
        return out

    def put_file(self, rel: str, path: str) -> None:
        dst = self._p(rel)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(f".{dst.name}.part-{os.getpid()}-{threading.get_ident()}")
            shutil.copyfile(path, tmp)
            os.replace(tmp, dst)
        except OSError as err:
            raise DeliveryError("交付目录写不进去", f"{dst}: {err.strerror or err}") from None

    def put_bytes(self, rel: str, data: bytes) -> None:
        dst = self._p(rel)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(f".{dst.name}.part-{os.getpid()}-{threading.get_ident()}")
            tmp.write_bytes(data)
            os.replace(tmp, dst)
        except OSError as err:
            raise DeliveryError("交付目录写不进去", f"{dst}: {err.strerror or err}") from None

    def get_bytes(self, rel: str) -> bytes | None:
        try:
            return self._p(rel).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as err:
            raise DeliveryError("交付目录读不了", f"{rel}: {err.strerror or err}") from None

    def delete(self, rel: str) -> None:
        p = self._p(rel)
        try:
            p.unlink()
        except FileNotFoundError:
            return
        except OSError as err:
            raise DeliveryError("交付目录里的文件删不掉", f"{rel}: {err.strerror or err}") \
                from None
        parent = p.parent
        base = self.root.resolve()
        while parent != base and base in parent.parents:
            try:
                parent.rmdir()                  # tidy empty directories, like a bucket
            except OSError:
                break
            parent = parent.parent


class TosDelivery(Delivery):
    def __init__(self, client, uri: str):
        self.client = client
        self.uri = uri
        self.bucket, self.prefix = T.split_uri(uri)

    def _key(self, rel: str) -> str:
        return _join(self.prefix, rel)

    def cli_uri(self, rel: str = "") -> str:
        return _join(self.uri, rel) if rel else self.uri

    def _fail(self, what: str, err: Exception) -> DeliveryError:
        from curation.cli.storage import describe_tos_error

        return DeliveryError(what, describe_tos_error(err))

    def _iter(self, prefix: str, *, max_keys: int = _LIST_PAGE):
        token = None
        while True:
            out = self.client.list_objects_type2(self.bucket, prefix=prefix,
                                                 continuation_token=token, max_keys=max_keys)
            yield from getattr(out, "contents", None) or []
            if not getattr(out, "is_truncated", False):
                return
            token = out.next_continuation_token

    def exists(self, rel_prefix: str) -> bool:
        key = self._key(rel_prefix)
        try:
            for _ in self._iter(key + "/", max_keys=1):
                return True
            return False
        except Exception as err:  # noqa: BLE001 - the SDK raises many kinds
            raise self._fail("交付目录读不了", err) from None

    def list(self, rel_prefix: str) -> dict[str, int]:
        key = self._key(rel_prefix)
        cut = len(self.prefix.strip("/")) + 1 if self.prefix.strip("/") else 0
        out: dict[str, int] = {}
        try:
            for obj in self._iter(key + "/" if key else ""):
                rel = obj.key[cut:]
                if rel and not rel.endswith("/"):
                    out[rel] = int(obj.size)
        except Exception as err:  # noqa: BLE001
            raise self._fail("交付目录读不了", err) from None
        return out

    def put_file(self, rel: str, path: str) -> None:
        try:
            put = getattr(self.client, "put_object_from_file", None)
            if put is not None:
                put(self.bucket, self._key(rel), path)
            else:
                with open(path, "rb") as fh:
                    self.client.put_object(self.bucket, self._key(rel), content=fh.read())
        except OSError as err:
            raise DeliveryError("本地文件读不了", f"{path}: {err.strerror or err}") from None
        except Exception as err:  # noqa: BLE001
            raise self._fail("交付目录写不进去", err) from None

    def put_bytes(self, rel: str, data: bytes) -> None:
        try:
            self.client.put_object(self.bucket, self._key(rel), content=data)
        except Exception as err:  # noqa: BLE001
            raise self._fail("交付目录写不进去", err) from None

    def get_bytes(self, rel: str) -> bytes | None:
        try:
            return self.client.get_object(self.bucket, self._key(rel)).read()
        except Exception as err:  # noqa: BLE001
            code = str(getattr(err, "code", "") or "")
            if code in ("NoSuchKey", "NotFound") or getattr(err, "status_code", None) == 404:
                return None
            raise self._fail("交付目录读不了", err) from None

    def delete(self, rel: str) -> None:
        try:
            self.client.delete_object(self.bucket, self._key(rel))
        except Exception as err:  # noqa: BLE001
            code = str(getattr(err, "code", "") or "")
            if code in ("NoSuchKey", "NotFound") or getattr(err, "status_code", None) == 404:
                return
            raise self._fail("交付目录里的文件删不掉", err) from None


# ---------------------------------------------------------------- opening one

def local_root_for(root: pathlib.Path, uri: str) -> pathlib.Path:
    bucket, prefix = T.split_uri(uri)
    return pathlib.Path(root) / bucket / prefix if prefix else pathlib.Path(root) / bucket


@contextlib.contextmanager
def open_delivery(uri: str, *, local_root: pathlib.Path | None, svc=None, key=None,
                  region: str | None = None) -> Iterator[Delivery]:
    """The delivery directory ``uri`` - on the local stand-in when configured, else on TOS
    with the output key (``svc.tos`` closes the client afterwards)."""
    if local_root is not None:
        yield LocalDelivery(local_root_for(local_root, uri), uri)
        return
    if svc is None or key is None:
        raise DeliveryError("没有交付目录的访问密钥")
    with svc.tos(key, region) as (client, _ends):
        yield TosDelivery(client, uri)


# ---------------------------------------------------------------- batches

def allocate_run_id(delivery: Delivery, base: str, *, taken: Callable[[str], bool] = lambda _: False
                    ) -> str:
    """``base``, or ``base-2``, ``base-3``... - the first name nobody has used."""
    n = 1
    while True:
        name = base if n == 1 else f"{base}-{n}"
        if not taken(name) and not delivery.exists(name):
            return name
        n += 1
        if n > 1000:
            raise DeliveryError("交付目录下同名的批次太多，换不出新的批次名")


def _delivered(rel: str) -> bool:
    parts = rel.split("/")
    if any(p.startswith(".") for p in parts):
        return False
    if rel == EXPORT_DATASET or rel.startswith(EXPORT_DATASET + "/"):
        return False                      # the CLI's export uploads the dataset itself
    name = parts[-1]
    if name in _SKIP_NAMES:
        return False
    return not (name.endswith((".tmp", ".partial", ".lock")) or ".tmp-" in name)


def sync_run_dir(delivery: Delivery, run_id: str, root: pathlib.Path, state_path: pathlib.Path,
                 *, check_stop: Callable[[], None] | None = None,
                 progress: Callable[[int, int], None] | None = None) -> dict:
    """Upload what changed in the run directory to ``<delivery>/<run_id>/``."""
    state = read_json(state_path, {}) or {}
    if state.get("run_id") != run_id:
        state = {"run_id": run_id, "files": {}}
    files: dict = state.setdefault("files", {})
    todo: list[tuple[str, pathlib.Path, list]] = []
    root = pathlib.Path(root)
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            full = pathlib.Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if not _delivered(rel):
                continue
            try:
                st = full.stat()
            except OSError:
                continue
            sig = [st.st_size, st.st_mtime_ns]
            if files.get(rel) != sig:
                todo.append((rel, full, sig))
    for n, (rel, full, sig) in enumerate(todo, 1):
        if check_stop is not None:
            check_stop()
        delivery.put_file(_join(run_id, rel), str(full))
        files[rel] = sig
        if n % 50 == 0:
            write_json_atomic(state_path, state)
        if progress is not None:
            progress(n, len(todo))
    write_json_atomic(state_path, state)
    return {"uploaded": len(todo)}


def forget_sync(state_path: pathlib.Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        state_path.unlink()


def read_latest(delivery: Delivery) -> str:
    raw = delivery.get_bytes(LATEST)
    return raw.decode("utf-8", "replace").strip() if raw else ""


def write_latest(delivery: Delivery, run_id: str) -> None:
    delivery.put_bytes(LATEST, f"{run_id}\n".encode())


def purge(delivery: Delivery, run_id: str) -> tuple[list[str], int]:
    """The objects of one batch and their bytes (the caller deletes them)."""
    listing = delivery.list(run_id)
    return sorted(listing), sum(listing.values())


class DeliveryLocks:
    """One lock per normalized delivery directory: publishing there is serial (D29)."""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())
