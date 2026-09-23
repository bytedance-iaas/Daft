"""One read/write surface for a local directory and a ``tos://bucket/prefix``.

The v2 commands use it instead of ``ingest.dsfs`` because they need what dsfs
does not offer: an explicit key set per role (input vs. output), ranged reads
(``verify`` looks for the mp4 ``moov`` box without downloading the video),
plain listings with ETags or modification times (``snapshot``), and errors that
map onto exit codes. Keys are always POSIX paths relative to the root.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

from .errors import UsageError, unreachable

TOS_PREFIX = "tos://"
_LIST_PAGE = 1000


class ObjectMissing(Exception):
    """The object does not exist (local ENOENT, TOS 404 / NoSuchKey)."""


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    etag: str | None = None              # TOS
    mtime_ns: int | None = None          # local files, in place of an ETag

    def identity(self) -> str:
        """What says "same content version": the ETag, else the modification time."""
        if self.etag is not None:
            return normalize_etag(self.etag)
        return str(self.mtime_ns)


def normalize_etag(etag: str | None) -> str:
    return str(etag or "").strip().strip('"')


def is_remote(uri: str) -> bool:
    return str(uri or "").startswith(TOS_PREFIX)


class Storage:
    uri: str
    remote: bool

    def list(self) -> dict[str, ObjectInfo]:
        raise NotImplementedError

    def stat(self, key: str) -> ObjectInfo | None:
        raise NotImplementedError

    def read_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    def read_range(self, key: str, start: int, length: int) -> bytes:
        raise NotImplementedError

    def put_bytes(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def download(self, key: str, local_path: str) -> ObjectInfo:
        """Stream one object into ``local_path`` (written in place; the caller renames it).

        Returns what was read: the byte count and, on TOS, the ETag of the response - the
        caller compares them with the listing it trusts (a source cache, D44)."""
        raise NotImplementedError

    def put_file(self, key: str, local_path: str) -> None:
        """Upload a whole local file (streamed; videos can be large)."""
        raise NotImplementedError

    def copy(self, src_key: str, dst_key: str) -> None:
        """Copy an object inside this storage (server side on TOS)."""
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class LocalStorage(Storage):
    remote = False

    def __init__(self, root: str, *, role: str = "input"):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.uri = self.root
        self.role = role

    def _path(self, key: str) -> str:
        return os.path.join(self.root, *key.split("/"))

    def exists(self) -> bool:
        return os.path.isdir(self.root)

    def list(self) -> dict[str, ObjectInfo]:
        if not os.path.isdir(self.root):
            raise unreachable(self.role, f"{self.root} does not exist or is not a directory",
                              {"path": self.root, "role": self.role})
        out: dict[str, ObjectInfo] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in filenames:
                if name.startswith("."):          # .DS_Store, publish temp files
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                key = os.path.relpath(full, self.root).replace(os.sep, "/")
                out[key] = ObjectInfo(key, st.st_size, mtime_ns=st.st_mtime_ns)
        return out

    def stat(self, key: str) -> ObjectInfo | None:
        try:
            st = os.stat(self._path(key))
        except FileNotFoundError:
            return None
        except OSError as e:
            raise unreachable(self.role, f"cannot stat {self._path(key)}: {e}") from None
        return ObjectInfo(key, st.st_size, mtime_ns=st.st_mtime_ns)

    def read_bytes(self, key: str) -> bytes:
        try:
            with open(self._path(key), "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            raise ObjectMissing(key) from None
        except OSError as e:
            raise unreachable(self.role, f"cannot read {self._path(key)}: {e}") from None

    def read_range(self, key: str, start: int, length: int) -> bytes:
        try:
            with open(self._path(key), "rb") as fh:
                fh.seek(start)
                return fh.read(length)
        except FileNotFoundError:
            raise ObjectMissing(key) from None
        except OSError as e:
            raise unreachable(self.role, f"cannot read {self._path(key)}: {e}") from None

    def put_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def download(self, key: str, local_path: str) -> ObjectInfo:
        import shutil

        try:
            shutil.copyfile(self._path(key), local_path)
        except FileNotFoundError:
            raise ObjectMissing(key) from None
        except OSError as e:
            raise unreachable(self.role, f"cannot copy {self._path(key)}: {e}") from None
        return ObjectInfo(key, os.path.getsize(local_path))

    def put_file(self, key: str, local_path: str) -> None:
        import shutil

        path = self._path(key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp-{os.getpid()}"
            shutil.copyfile(local_path, tmp)
            os.replace(tmp, path)
        except OSError as e:
            raise unreachable(self.role, f"cannot write {path}: {e}") from None

    def copy(self, src_key: str, dst_key: str) -> None:
        self.put_file(dst_key, self._path(src_key))

    def delete(self, key: str) -> None:
        try:
            os.unlink(self._path(key))
        except FileNotFoundError:
            pass


def _tos_code(e: Exception) -> str:
    return str(getattr(e, "code", "") or "")


def _is_not_found(e: Exception) -> bool:
    """The object is missing - not the bucket, which is a 404 too but means unreachable."""
    code = _tos_code(e)
    if code == "NoSuchBucket":
        return False
    return code in ("NoSuchKey", "NotFound") or getattr(e, "status_code", None) == 404


def describe_tos_error(e: Exception) -> str:
    """One readable line; the SDK's str() is a whole response dump."""
    code = _tos_code(e)
    msg = str(getattr(e, "message", "") or "")
    plain = {"NoSuchBucket": "the bucket does not exist in this region",
             "AccessDenied": "access denied for this access key",
             "InvalidAccessKeyId": "the access key id is not valid",
             "SignatureDoesNotMatch": "the secret key does not match the access key"}.get(code)
    if plain:
        return f"{plain} ({code})"
    if code or msg:
        return f"{code}{': ' if code and msg else ''}{msg}"
    cause = getattr(e, "cause", None)
    return f"{type(e).__name__}: {cause or e}"[:300]


class TosStorage(Storage):
    remote = True

    def __init__(self, uri: str, client, region: str, *, role: str = "input"):
        from .. import tos_store

        try:
            self.bucket, prefix = tos_store.parse_tos_url(uri)
        except tos_store.TosUrlError as e:
            raise UsageError(str(e)) from None
        self.prefix = prefix.strip("/")
        self.uri = f"{TOS_PREFIX}{self.bucket}" + (f"/{self.prefix}" if self.prefix else "")
        self.region = region
        self.role = role
        self._c = client

    def _full(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _fail(self, what: str, e: Exception):
        return unreachable(
            self.role, f"{what} {self.uri} (region {self.region}): {describe_tos_error(e)}",
            {"uri": self.uri, "region": self.region, "tos_code": _tos_code(e) or None,
             "role": self.role})

    def _iter(self) -> Iterator[tuple[str, int, str]]:
        start = self.prefix + "/" if self.prefix else ""
        token = None
        while True:
            out = self._c.list_objects_type2(self.bucket, prefix=start,
                                             continuation_token=token, max_keys=_LIST_PAGE)
            for obj in getattr(out, "contents", None) or []:
                yield obj.key, int(obj.size), str(getattr(obj, "etag", "") or "")
            if not getattr(out, "is_truncated", False):
                return
            token = out.next_continuation_token

    def list(self) -> dict[str, ObjectInfo]:
        cut = len(self.prefix) + 1 if self.prefix else 0
        out: dict[str, ObjectInfo] = {}
        try:
            for key, size, etag in self._iter():
                rel = key[cut:]
                if not rel or rel.endswith("/"):    # directory marker objects
                    continue
                out[rel] = ObjectInfo(rel, size, etag=etag)
        except Exception as e:  # noqa: BLE001 - SDK and network errors are many
            raise self._fail("cannot list", e) from None
        return out

    def stat(self, key: str) -> ObjectInfo | None:
        try:
            head = self._c.head_object(self.bucket, self._full(key))
        except Exception as e:  # noqa: BLE001
            if _is_not_found(e):
                return None
            raise self._fail(f"cannot stat {key} in", e) from None
        return ObjectInfo(key, int(getattr(head, "content_length", 0) or 0),
                          etag=str(getattr(head, "etag", "") or ""))

    def read_bytes(self, key: str) -> bytes:
        try:
            return self._c.get_object(self.bucket, self._full(key)).read()
        except Exception as e:  # noqa: BLE001
            if _is_not_found(e):
                raise ObjectMissing(key) from None
            raise self._fail(f"cannot read {key} from", e) from None

    def read_range(self, key: str, start: int, length: int) -> bytes:
        if length <= 0:
            return b""
        try:
            out = self._c.get_object(self.bucket, self._full(key), range_start=start,
                                     range_end=start + length - 1)
            return out.read()
        except Exception as e:  # noqa: BLE001
            if _is_not_found(e):
                raise ObjectMissing(key) from None
            if getattr(e, "status_code", None) == 416:     # range beyond the end
                return b""
            raise self._fail(f"cannot read {key} from", e) from None

    def download(self, key: str, local_path: str, *, chunk: int = 1 << 20) -> ObjectInfo:
        size = 0
        try:
            out = self._c.get_object(self.bucket, self._full(key))
            with open(local_path, "wb") as fh:
                while True:
                    block = out.read(chunk)
                    if not block:
                        break
                    fh.write(block)
                    size += len(block)
        except OSError as e:
            raise unreachable(self.role, f"cannot write {local_path}: {e}") from None
        except Exception as e:  # noqa: BLE001 - SDK and network errors are many
            if _is_not_found(e):
                raise ObjectMissing(key) from None
            raise self._fail(f"cannot read {key} from", e) from None
        etag = getattr(out, "etag", None)
        return ObjectInfo(key, size, etag=str(etag) if etag else None)

    def put_bytes(self, key: str, data: bytes) -> None:
        try:
            self._c.put_object(self.bucket, self._full(key), content=data)
        except Exception as e:  # noqa: BLE001
            raise self._fail(f"cannot write {key} to", e) from None

    def put_file(self, key: str, local_path: str) -> None:
        put = getattr(self._c, "put_object_from_file", None)
        try:
            if put is not None:
                put(self.bucket, self._full(key), local_path)
            else:                                   # clients without it (test fakes)
                with open(local_path, "rb") as fh:
                    self._c.put_object(self.bucket, self._full(key), content=fh.read())
        except OSError as e:
            raise unreachable(self.role, f"cannot read {local_path}: {e}") from None
        except Exception as e:  # noqa: BLE001
            raise self._fail(f"cannot write {key} to", e) from None

    def copy(self, src_key: str, dst_key: str) -> None:
        try:
            self._c.copy_object(self.bucket, self._full(dst_key), self.bucket,
                                self._full(src_key))
        except Exception as e:  # noqa: BLE001
            raise self._fail(f"cannot copy {src_key} to {dst_key} in", e) from None

    def delete(self, key: str) -> None:
        try:
            self._c.delete_object(self.bucket, self._full(key))
        except Exception as e:  # noqa: BLE001
            if not _is_not_found(e):
                raise self._fail(f"cannot delete {key} from", e) from None


def open_storage(uri: str, *, role: str, region: str | None = None,
                 anonymous: bool = False) -> Storage:
    """A local directory, or a TOS prefix read with ``role``'s key set."""
    if is_remote(uri):
        from . import creds

        client, want = creds.tos_client(role, region, anonymous=anonymous)
        return TosStorage(uri, client, want, role=role)
    if not str(uri or "").strip():
        raise UsageError("an empty path was given")
    return LocalStorage(uri, role=role)
