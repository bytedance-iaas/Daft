"""VLM request recording and replay ("tape") for parity runs.

A tape is a gzip JSON-Lines file. Each line is one *entry*: the canonical form
of a request, its hash, and what came back (a response or an exception).

Two kinds of entries mirror the two ways the v1 client talks to the model:

* ``logical`` - one call made through ``vlm_client.hedged_request``. Hedging may
  send the same request twice; only the attempt whose result the caller got is
  recorded, so replay hands back exactly what the pipeline used.
* ``direct`` - one plain ``requests.post``/``requests.get`` attempt (the text
  LLM client retries on its own, the endpoint probe calls ``/models``). Each
  attempt is one entry; replay serves them in recorded order.

The canonical request keeps every prompt character and every model parameter,
and replaces each inline image with the sha256 of its decoded bytes. The hash
of the canonical form is the replay key. Nothing here imports the pipeline, so
the same code hashes requests on the v1 side and on the v2 side.
"""
from __future__ import annotations

import base64
import collections
import gzip
import hashlib
import importlib
import io
import json
import threading
import time
from typing import Any, Callable, Iterable

TAPE_SCHEMA_VERSION = "1.0"

_DATA_URI_PREFIX = "data:"


# ---------------------------------------------------------------------------
# Canonical request and hash
# ---------------------------------------------------------------------------

def _canonical_image(url: str) -> Any:
    """Replace an inline data URI with the hash of its decoded bytes."""
    if not isinstance(url, str) or not url.startswith(_DATA_URI_PREFIX):
        return url
    head, _, data = url.partition(",")
    mime = head[len(_DATA_URI_PREFIX):].split(";")[0]
    try:
        raw = base64.b64decode(data, validate=False)
    except Exception:  # noqa: BLE001 - malformed data URI: hash the text itself
        raw = data.encode("utf-8", "surrogatepass")
    return {"mime": mime, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _canonical_value(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in ("image_url", "video_url") and isinstance(v, dict) and "url" in v:
                v = dict(v)
                v["url"] = _canonical_image(v["url"])
            elif k in ("image_url", "video_url") and isinstance(v, str):
                v = _canonical_image(v)
            out[k] = _canonical_value(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_canonical_value(v) for v in value]
    return value


def canonical_request(payload: dict, *, method: str = "POST",
                      path: str = "/chat/completions") -> tuple[dict, str]:
    """Return ``(canonical, hash)`` for a request body.

    The canonical form is what the tape stores (no image bytes). The hash
    covers method, path and the canonical body, serialized with sorted keys.
    """
    canonical = {"method": method.upper(), "path": path,
                 "body": _canonical_value(payload) if payload is not None else None}
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=True)
    return canonical, "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def endpoint_path(url: str) -> str:
    """``https://host/api/v3/chat/completions`` -> ``/chat/completions``."""
    tail = str(url).split("?", 1)[0].rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if tail.endswith(suffix):
            return suffix
    return ""


# ---------------------------------------------------------------------------
# Tape file
# ---------------------------------------------------------------------------

def _exc_name(exc: BaseException) -> str:
    cls = type(exc)
    return f"{cls.__module__}.{cls.__qualname__}"


class TapeWriter:
    """Append-only, thread-safe tape writer."""

    def __init__(self, path: str, meta: dict | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._seq = 0
        self._fh = gzip.open(path, "wt", encoding="utf-8")
        header = {"tape_schema_version": TAPE_SCHEMA_VERSION, "kind": "header",
                  "created_at": time.time(), **(meta or {})}
        self._fh.write(json.dumps(header, ensure_ascii=False) + "\n")

    def write(self, entry: dict) -> dict:
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, **entry}
            self._fh.write(json.dumps(entry, ensure_ascii=False, allow_nan=True) + "\n")
            return entry

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()


def read_tape(path: str) -> tuple[dict, list[dict]]:
    """Return ``(header, entries)``; entries keep file order.

    A run that was killed leaves a gzip stream without its trailer and maybe a
    half-written last line; everything before that is still usable (e.g. for a
    replay-record rerun), so reading stops there and ``header["truncated"]``
    is set.
    """
    header: dict = {}
    entries: list[dict] = []
    truncated = False
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        try:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    truncated = True
                    break
                if obj.get("kind") == "header":
                    header = obj
                else:
                    entries.append(obj)
        except (EOFError, gzip.BadGzipFile, OSError):
            truncated = True
    if truncated:
        header = {**header, "truncated": True}
    return header, entries


def make_entry(kind: str, *, canonical: dict, digest: str, tag: str | None,
               response=None, exc: BaseException | None = None,
               t_start: float | None = None, t_end: float | None = None,
               replayed_from: int | None = None) -> dict:
    """Build a tape entry from a ``requests.Response`` or an exception."""
    entry: dict = {"kind": kind, "hash": digest, "tag": tag, "request": canonical,
                   "t_start": t_start, "t_end": t_end}
    if replayed_from is not None:
        entry["replayed_from"] = replayed_from
    if exc is not None:
        entry.update(outcome="exception", exc_type=_exc_name(exc), exc_message=str(exc))
    else:
        body = response.content.decode(response.encoding or "utf-8", "replace") \
            if response.content is not None else ""
        entry.update(outcome="response", status=int(response.status_code),
                     reason=getattr(response, "reason", "") or "", body=body)
    return entry


def tape_failures(entries: Iterable[dict]) -> list[dict]:
    """Calls whose final outcome was not a usable model answer.

    * a ``logical`` (hedged) call failed when it raised or came back non-2xx;
    * a text call through ``llm_ask`` retries on its own, so its individual
      ``direct`` attempts are not failures - the hooks write one ``llm_failure``
      marker when the whole call finally raised.
    """
    return [e for e in entries
            if (e.get("kind") == "logical" and not _entry_ok(e))
            or e.get("kind") == "llm_failure"]


def _entry_ok(e: dict) -> bool:
    return e.get("outcome") == "response" and 200 <= int(e.get("status") or 0) < 300


# ---------------------------------------------------------------------------
# Replay store
# ---------------------------------------------------------------------------

class ReplayMiss(Exception):
    """Raised inside the hooks when a request is not on the tape."""

    def __init__(self, digest: str, canonical: dict, tag: str | None):
        super().__init__(f"request not on tape: {digest} (tag={tag})")
        self.digest = digest
        self.canonical = canonical
        self.tag = tag


class ReplayStore:
    """FIFO queues of recorded outcomes, keyed by (kind, hash).

    ``sticky_tags``: entries with these tags are not consumed and not counted -
    every request with their hash gets the last recorded outcome. The v2 replay uses
    it for ``/models`` endpoint probes: each v2 command probes once, v1 probed twice
    per run, and a probe is not part of the VLM call graph (design doc 04 §4.1).
    """

    def __init__(self, entries: Iterable[dict], *, skip_failures: bool = False,
                 drop_hashes: Iterable[str] = (), sticky_tags: Iterable[str] = ()):
        self._queues: dict[tuple[str, str], collections.deque] = collections.defaultdict(
            collections.deque)
        self._sticky: dict[tuple[str, str], dict] = {}
        sticky = set(sticky_tags)
        drop = set(drop_hashes)
        for e in entries:
            if e.get("kind") not in ("logical", "direct"):
                continue                    # failure markers are bookkeeping only
            if skip_failures and not _entry_ok(e):
                continue
            if e.get("hash") in drop:
                continue
            if e.get("tag") in sticky:
                self._sticky[(e["kind"], e["hash"])] = e
                continue
            self._queues[(e["kind"], e["hash"])].append(e)
        self._lock = threading.Lock()
        self.hits = collections.Counter()
        self.misses: list[dict] = []

    def take(self, kind: str, digest: str) -> dict | None:
        with self._lock:
            q = self._queues.get((kind, digest))
            if q:
                self.hits[kind] += 1
                return q.popleft()
            return self._sticky.get((kind, digest))

    def note_miss(self, kind: str, digest: str, canonical: dict, tag: str | None) -> None:
        with self._lock:
            self.misses.append({"kind": kind, "hash": digest, "tag": tag,
                                "request": canonical, "at": time.time()})

    def remaining(self) -> int:
        with self._lock:
            return sum(len(q) for q in self._queues.values())


def response_from_entry(entry: dict, url: str):
    """Rebuild a ``requests.Response`` from a tape entry."""
    import requests
    from requests.structures import CaseInsensitiveDict

    r = requests.models.Response()
    r.status_code = int(entry.get("status") or 0)
    r.reason = entry.get("reason") or ""
    r._content = (entry.get("body") or "").encode("utf-8")
    r.encoding = "utf-8"
    r.headers = CaseInsensitiveDict({"Content-Type": "application/json"})
    r.url = url
    return r


def exception_from_entry(entry: dict) -> BaseException:
    """Rebuild the recorded exception, falling back to ``ConnectionError``."""
    import requests

    name = str(entry.get("exc_type") or "")
    module, _, qual = name.rpartition(".")
    cls = None
    try:
        cls = getattr(importlib.import_module(module), qual) if module else None
    except Exception:  # noqa: BLE001
        cls = None
    if not (isinstance(cls, type) and issubclass(cls, BaseException)):
        cls = requests.exceptions.ConnectionError
    try:
        return cls(entry.get("exc_message") or name)
    except Exception:  # noqa: BLE001 - exotic constructor signature
        return requests.exceptions.ConnectionError(entry.get("exc_message") or name)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

_TL = threading.local()
_ACTIVE: "TapeHooks | None" = None


def _active() -> "TapeHooks":
    if _ACTIVE is None:
        raise RuntimeError("parity tape hooks are not installed")
    return _ACTIVE


def hooked_post(url, *args, **kw):
    return _active()._post(url, *args, **kw)


def hooked_get(url, *args, **kw):
    return _active()._get(url, *args, **kw)


def hooked_hedged_request(send, *, tag: str, timeout_s: float, gate=None):
    return _active()._hedged(send, tag=tag, timeout_s=timeout_s, gate=gate)


class LlmAskWrapper:
    """Text client wrapper that leaves a marker when a whole call raised.

    Picklable as long as the wrapped client is (v1 keeps its clients picklable
    for daft), because it holds nothing but that client.
    """

    def __init__(self, inner):
        self.inner = inner

    def __call__(self, prompt_text: str) -> str:
        try:
            return self.inner(prompt_text)
        except BaseException as exc:
            if _ACTIVE is not None:
                _ACTIVE.llm_failed(exc, prompt_text)
            raise


def hooked_make_llm_ask(*args, **kw):
    return LlmAskWrapper(_active()._orig["make_llm_ask"](*args, **kw))


class _Call:
    """Per-logical-call state shared by the hedged wrapper and the post hook."""

    __slots__ = ("tag", "mode", "digest", "canonical", "lock", "attempts")

    def __init__(self, tag: str | None, mode: str):
        self.tag = tag
        self.mode = mode            # "lookup" (replay) or "live"
        self.digest: str | None = None
        self.canonical: dict | None = None
        self.lock = threading.Lock()
        self.attempts: list = []


class TapeHooks:
    """Patch ``requests`` and ``vlm_client.hedged_request`` for record/replay.

    mode:
      * ``record``        - forward to the real endpoint, write every call.
      * ``replay``        - serve from ``replay_entries``; a miss raises
                            ``requests.exceptions.ConnectionError`` (the
                            pipeline sees a network failure) and is logged.
      * ``replay-record`` - serve successful recorded outcomes; anything not on
                            the tape (or recorded as a failure) goes live and is
                            written. The output tape is a complete recording of
                            this run.

    ``transport`` replaces the real ``requests.post``/``requests.get`` for live
    calls (tests plug a fake model in here).
    """

    def __init__(self, mode: str, *, tape_out: str | None = None,
                 replay_entries: list[dict] | None = None,
                 transport: dict[str, Callable] | None = None,
                 tape_meta: dict | None = None,
                 drop_hashes: Iterable[str] = (),
                 sticky_tags: Iterable[str] = ()):
        if mode not in ("record", "replay", "replay-record"):
            raise ValueError(f"unknown tape mode {mode!r}")
        if mode != "replay" and not tape_out:
            raise ValueError(f"mode {mode!r} needs tape_out")
        self.mode = mode
        self.writer = TapeWriter(tape_out, tape_meta) if tape_out else None
        self.store = (ReplayStore(replay_entries or [],
                                  skip_failures=(mode == "replay-record"),
                                  drop_hashes=drop_hashes if mode == "replay-record" else (),
                                  sticky_tags=sticky_tags)
                      if mode != "record" else None)
        self._transport = transport or {}
        self._orig: dict = {}
        self.counts = collections.Counter()

    # -- install / uninstall -------------------------------------------------
    # v1 hands its model clients to daft UDFs, and daft cloudpickles every UDF
    # closure up front. The replacements therefore must pickle by reference:
    # they are module-level functions (or a small module-level class) that look
    # up the active hooks at call time, never bound methods holding locks.
    def install(self, vlm_client_module) -> None:
        import requests

        global _ACTIVE
        if _ACTIVE is not None:
            raise RuntimeError("another TapeHooks instance is already installed")
        self._orig = {"post": requests.post, "get": requests.get,
                      "hedged": vlm_client_module.hedged_request,
                      "make_llm_ask": vlm_client_module.make_llm_ask,
                      "vlm_client": vlm_client_module}
        _ACTIVE = self
        requests.post = hooked_post
        requests.get = hooked_get
        vlm_client_module.hedged_request = hooked_hedged_request
        vlm_client_module.make_llm_ask = hooked_make_llm_ask

    def uninstall(self) -> None:
        import requests

        global _ACTIVE
        if not self._orig:
            return
        requests.post = self._orig["post"]
        requests.get = self._orig["get"]
        self._orig["vlm_client"].hedged_request = self._orig["hedged"]
        self._orig["vlm_client"].make_llm_ask = self._orig["make_llm_ask"]
        self._orig = {}
        _ACTIVE = None
        if self.writer:
            self.writer.close()

    def llm_failed(self, exc: BaseException, prompt_text: str) -> None:
        self._write({"kind": "llm_failure", "outcome": "exception", "tag": "llm",
                     "hash": "", "exc_type": _exc_name(exc), "exc_message": str(exc)[:500],
                     "prompt_head": str(prompt_text)[:200], "t_end": time.time()})

    # -- live transport ------------------------------------------------------
    def _live_post(self, url, *args, **kw):
        fn = self._transport.get("post") or self._orig["post"]
        return fn(url, *args, **kw)

    def _live_get(self, url, *args, **kw):
        fn = self._transport.get("get") or self._orig["get"]
        return fn(url, *args, **kw)

    def _write(self, entry: dict) -> None:
        if self.writer:
            self.writer.write(entry)
        self.counts[f"{entry['kind']}:{entry['outcome']}"] += 1

    # -- requests.get --------------------------------------------------------
    def _get(self, url, *args, **kw):
        path = endpoint_path(url)
        if path != "/models":
            return self._live_get(url, *args, **kw)
        canonical, digest = canonical_request(None, method="GET", path=path)
        return self._direct(url, canonical, digest, "models",
                            lambda: self._live_get(url, *args, **kw))

    # -- requests.post -------------------------------------------------------
    def _post(self, url, *args, **kw):
        path = endpoint_path(url)
        payload = kw.get("json")
        if path != "/chat/completions" or not isinstance(payload, dict):
            return self._live_post(url, *args, **kw)
        canonical, digest = canonical_request(payload, path=path)
        call: _Call | None = getattr(_TL, "call", None)
        if call is None:
            return self._direct(url, canonical, digest, None,
                                lambda: self._live_post(url, *args, **kw))
        with call.lock:
            call.digest, call.canonical = digest, canonical
        if call.mode == "lookup":
            entry = self.store.take("logical", digest)
            if entry is None:
                raise ReplayMiss(digest, canonical, call.tag)
            call.attempts.append(entry)
            if entry.get("outcome") == "exception":
                raise exception_from_entry(entry)
            return response_from_entry(entry, url)
        resp = self._live_post(url, *args, **kw)
        with call.lock:
            call.attempts.append(resp)
        return resp

    def _direct(self, url, canonical, digest, tag, live: Callable):
        """One plain attempt: replay it, or run it live and record it."""
        if self.store is not None:
            entry = self.store.take("direct", digest)
            if entry is not None:
                if self.writer:
                    self._write({**{k: v for k, v in entry.items() if k != "seq"},
                                 "replayed_from": entry.get("seq")})
                if entry.get("outcome") == "exception":
                    raise exception_from_entry(entry)
                return response_from_entry(entry, url)
            self.store.note_miss("direct", digest, canonical, tag)
            if self.mode == "replay":
                import requests
                raise requests.exceptions.ConnectionError(
                    f"parity replay: request not on tape ({digest})")
        t0 = time.time()
        try:
            resp = live()
        except BaseException as exc:
            self._write(make_entry("direct", canonical=canonical, digest=digest, tag=tag,
                                   exc=exc, t_start=t0, t_end=time.time()))
            raise
        self._write(make_entry("direct", canonical=canonical, digest=digest, tag=tag,
                               response=resp, t_start=t0, t_end=time.time()))
        return resp

    # -- vlm_client.hedged_request ------------------------------------------
    def _hedged(self, send, *, tag: str, timeout_s: float, gate=None):
        if self.store is not None:
            call = _Call(tag, "lookup")
            try:
                return self._run_send(call, send, timeout_s, record_replay=True)
            except ReplayMiss as miss:
                self.store.note_miss("logical", miss.digest, miss.canonical, tag)
                if self.mode == "replay":
                    import requests
                    raise requests.exceptions.ConnectionError(
                        f"parity replay: request not on tape ({miss.digest})") from None
        call = _Call(tag, "live")

        def send_live(hard):
            _TL.call = call
            try:
                return send(hard)
            finally:
                _TL.call = None

        t0 = time.time()
        try:
            resp = self._orig["hedged"](send_live, tag=tag, timeout_s=timeout_s, gate=gate)
        except BaseException as exc:
            self._write(make_entry("logical", canonical=call.canonical or {},
                                   digest=call.digest or "", tag=tag, exc=exc,
                                   t_start=t0, t_end=time.time()))
            raise
        self._write(make_entry("logical", canonical=call.canonical or {},
                               digest=call.digest or "", tag=tag, response=resp,
                               t_start=t0, t_end=time.time()))
        return resp

    def _run_send(self, call: _Call, send, timeout_s: float, record_replay: bool):
        _TL.call = call
        try:
            result = send(timeout_s)
        except ReplayMiss:
            raise
        except BaseException:
            if record_replay and self.writer and call.attempts:
                entry = call.attempts[-1]
                self._write({**{k: v for k, v in entry.items() if k != "seq"},
                             "replayed_from": entry.get("seq")})
            raise
        finally:
            _TL.call = None
        if record_replay and self.writer and call.attempts:
            entry = call.attempts[-1]
            self._write({**{k: v for k, v in entry.items() if k != "seq"},
                         "replayed_from": entry.get("seq")})
        return result

    # -- stats ---------------------------------------------------------------
    def stats(self) -> dict:
        out: dict = {"mode": self.mode, "written": dict(self.counts)}
        if self.store is not None:
            out.update(hits=dict(self.store.hits), misses=len(self.store.misses),
                       unused=self.store.remaining())
        return out


def summarize_tape(path: str) -> dict:
    """Counts by kind/tag/outcome plus the failures, for reports and checks."""
    header, entries = read_tape(path)
    by = collections.Counter((e.get("kind"), e.get("tag"), e.get("outcome"))
                             for e in entries)
    fails = tape_failures(entries)
    return {"header": header, "entries": len(entries),
            "by_kind_tag_outcome": [
                {"kind": k, "tag": t, "outcome": o, "count": n}
                for (k, t, o), n in sorted(by.items(), key=lambda x: str(x[0]))],
            "failures": [{"seq": e.get("seq"), "kind": e.get("kind"), "tag": e.get("tag"),
                          "hash": e.get("hash"), "status": e.get("status"),
                          "exc_type": e.get("exc_type"),
                          "exc_message": (e.get("exc_message") or "")[:200]}
                         for e in fails]}


def dumps_jsonl(rows: Iterable[dict]) -> str:
    buf = io.StringIO()
    for row in rows:
        buf.write(json.dumps(row, ensure_ascii=False, allow_nan=True) + "\n")
    return buf.getvalue()


def main(argv: list[str]) -> int:
    """``python -m parity tape-summary TAPE [--json]``."""
    import argparse

    p = argparse.ArgumentParser(prog="python -m parity tape-summary")
    p.add_argument("tape")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    s = summarize_tape(args.tape)
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
    else:
        print(f"{args.tape}: {s['entries']} entries, {len(s['failures'])} failed calls")
        for row in s["by_kind_tag_outcome"]:
            print(f"  {row['kind']:12} {str(row['tag']):12} {row['outcome']:9} {row['count']}")
        for f in s["failures"][:20]:
            print(f"  FAILED seq={f['seq']} {f['kind']} {f['tag']} {f['exc_type'] or f['status']}")
    return 0
