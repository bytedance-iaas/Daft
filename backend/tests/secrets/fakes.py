"""Offline stand-ins for W8's two outside worlds.

* :class:`FakeTos` - an in-memory TOS behind the SDK method names the Daemon uses
  (``list_buckets``, ``head_bucket``, ``get_object``, ``put_object``, ``delete_object``,
  ``pre_signed_url``). It knows which key pairs exist and what each may do, records every
  call, and - like a careless provider - quotes the key pair in its error messages, so the
  tests can prove the Daemon scrubs them.
* :class:`VlmStub` - an OpenAI-compatible ``/models`` and ``/chat/completions`` served by the
  standard library on 127.0.0.1, recording each request (headers and JSON body). Its error
  bodies echo the ``Authorization`` header for the same reason.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: Planted secrets: none of them may show up in a response, a log line or the database file.
AK = "AKLTplantedAccessKeyId0001"
SK = "SKplanted/Secret+Value==0001"
AK2 = "AKLTplantedOutputKeyId0002"
SK2 = "SKplantedOutputSecret==0002"
BAD_SK = "SKplantedButWrongSecret0003"
API_KEY = "ark-planted-api-key-000000001"
API_KEY2 = "ark-planted-api-key-000000002"
SECRETS = (SK, SK2, BAD_SK, API_KEY, API_KEY2)
#: Access key ids are not secret the way the others are (a presigned URL has to carry one),
#: but they are never shown either - only their last four characters.
KEY_IDS = (AK, AK2)


class FakeTosError(Exception):
    """Like ``tos.exceptions.TosServerError``: ``status_code``, ``code``, ``message``."""

    def __init__(self, status_code: int, code: str = "", message: str = ""):
        super().__init__(message or code)
        self.status_code = status_code
        self.code = code
        self.message = message


class FakeNetworkError(Exception):
    """Like ``tos.exceptions.TosClientError``: no status (DNS, connect, timeout)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
        self.cause = ConnectionError(message)


@dataclass
class Grant:
    list_buckets: bool = True
    read: set = field(default_factory=set)       # buckets
    write: set = field(default_factory=set)


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, n: int = -1) -> bytes:
        return self._data if n is None or n < 0 else self._data[:n]

    def close(self) -> None:
        pass


class _Presigned:
    def __init__(self, url: str):
        self.signed_url = url


class FakeTos:
    def __init__(self):
        self.buckets: set[str] = {"datasets", "deliveries", "public-mirror"}
        self.objects: dict[tuple[str, str], bytes] = {}
        self.pairs: dict[str, str] = {}              # access key id -> secret
        self.grants: dict[str, Grant] = {}
        self.anonymous_read: set[str] = {"public-mirror"}
        self.calls: list[dict] = []
        self.fail_delete = False
        self.down = False
        self._lock = threading.Lock()

    # -- setup -------------------------------------------------------------------
    def add_key(self, ak: str, sk: str, **grant) -> "FakeTos":
        self.pairs[ak] = sk
        self.grants[ak] = Grant(**grant)
        return self

    def put(self, bucket: str, key: str, data: bytes = b"{}") -> None:
        self.buckets.add(bucket)
        self.objects[(bucket, key)] = data

    def factory(self, endpoint: str, region: str, key):
        return FakeTosClient(self, endpoint, region, key)

    def ops(self, op: str | None = None) -> list[dict]:
        return [c for c in self.calls if op is None or c["op"] == op]


class FakeTosClient:
    def __init__(self, tos: FakeTos, endpoint: str, region: str, key):
        self.tos, self.endpoint, self.region, self.key = tos, endpoint, region, key

    def _call(self, op: str, bucket: str | None = None, key: str | None = None) -> str | None:
        ak = self.key.access_key_id if self.key is not None else None
        with self.tos._lock:
            self.tos.calls.append({"op": op, "endpoint": self.endpoint, "region": self.region,
                                   "ak": ak, "bucket": bucket, "key": key})
        if self.tos.down:
            raise FakeNetworkError(f"Max retries exceeded with url {self.endpoint}")
        if self.key is None:
            return None
        sk = self.tos.pairs.get(ak)
        if sk is None:
            raise FakeTosError(403, "InvalidAccessKeyId",
                               f"The access key id {ak} you provided does not exist")
        if sk != self.key.secret_access_key:
            raise FakeTosError(403, "SignatureDoesNotMatch",
                               f"signature mismatch for {ak} using secret "
                               f"{self.key.secret_access_key}")
        return ak

    def _bucket(self, bucket: str) -> None:
        if bucket not in self.tos.buckets:
            raise FakeTosError(404, "NoSuchBucket", "The specified bucket does not exist.")

    def _allowed(self, ak: str | None, bucket: str, what: str) -> None:
        if ak is None:
            ok = what == "read" and bucket in self.tos.anonymous_read
        else:
            ok = bucket in getattr(self.tos.grants[ak], what)
        if not ok:
            raise FakeTosError(403, "AccessDenied", f"Access Denied for {ak or 'anonymous'}")

    def list_buckets(self):
        ak = self._call("list_buckets")
        if ak is None or not self.tos.grants[ak].list_buckets:
            raise FakeTosError(403, "AccessDenied", "Access Denied")
        return sorted(self.tos.buckets)

    def head_bucket(self, bucket: str):
        ak = self._call("head_bucket", bucket)
        if bucket not in self.tos.buckets:
            raise FakeTosError(404, "", "")                   # HEAD: no body, no code
        grant = self.tos.grants.get(ak) if ak else None
        if grant is None or not (bucket in grant.read or bucket in grant.write):
            raise FakeTosError(403, "", "")
        return {"status": 200}

    def get_object(self, bucket: str, key: str):
        ak = self._call("get_object", bucket, key)
        self._bucket(bucket)
        self._allowed(ak, bucket, "read")
        if (bucket, key) not in self.tos.objects:
            raise FakeTosError(404, "NoSuchKey", "The specified key does not exist.")
        return _Body(self.tos.objects[(bucket, key)])

    def put_object(self, bucket: str, key: str, content: bytes = b""):
        ak = self._call("put_object", bucket, key)
        self._bucket(bucket)
        self._allowed(ak, bucket, "write")
        self.tos.objects[(bucket, key)] = bytes(content)

    def delete_object(self, bucket: str, key: str):
        ak = self._call("delete_object", bucket, key)
        self._bucket(bucket)
        if self.tos.fail_delete:
            raise FakeTosError(403, "AccessDenied", "delete not allowed")
        self._allowed(ak, bucket, "write")
        self.tos.objects.pop((bucket, key), None)

    def pre_signed_url(self, method, bucket: str, key: str, expires: int = 3600):
        # local HMAC in the real SDK: no authentication round trip, only a record
        with self.tos._lock:
            self.tos.calls.append({"op": "presign", "endpoint": self.endpoint,
                                   "region": self.region,
                                   "ak": self.key.access_key_id if self.key else None,
                                   "bucket": bucket, "key": key, "expires": expires})
        host = self.endpoint.split("://", 1)[1]
        ak = self.key.access_key_id if self.key else ""
        return _Presigned(f"https://{bucket}.{host}/{key}?X-Tos-Credential={ak}%2F{self.region}"
                          f"&X-Tos-Expires={expires}&X-Tos-Signature=fake")


# ---------------------------------------------------------------------------
# OpenAI-compatible stub
# ---------------------------------------------------------------------------

@dataclass
class VlmStub:
    url: str = ""
    keys: set | None = None                  # accepted API keys; None = no auth at all
    models: list | None = None               # GET /models answer; None = 404
    served: dict = field(default_factory=dict)   # name -> the name the server says it ran
    unknown_model_status: int = 404
    reject_efforts: set = field(default_factory=set)
    requests: list = field(default_factory=list)
    chat_models: set | None = None           # served for chat; None = everything

    def chats(self) -> list[dict]:
        return [r["body"] for r in self.requests if r["path"].endswith("/chat/completions")]


def _handler(stub: VlmStub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):          # keep test output quiet
            pass

        def _send(self, status: int, payload) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _record(self, body=None) -> str:
            auth = self.headers.get("Authorization", "")
            stub.requests.append({"method": self.command, "path": self.path, "auth": auth,
                                  "body": body})
            return auth

        def _authorized(self, auth: str) -> bool:
            if stub.keys is None:
                return True
            token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
            if token in stub.keys:
                return True
            # a careless server that quotes what it was given
            self._send(401, {"error": {"code": "AuthenticationError",
                                       "message": f"invalid api key: {auth}"}})
            return False

        def do_GET(self):
            auth = self._record()
            if not self.path.endswith("/models"):
                self._send(404, {"error": {"message": "no such route"}})
                return
            if not self._authorized(auth):
                return
            if stub.models is None:
                self._send(404, {"error": {"message": "Not Found"}})
                return
            self._send(200, {"object": "list",
                             "data": [{"id": m, "object": "model"} for m in stub.models]})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            auth = self._record(body)
            if not self.path.endswith("/chat/completions"):
                self._send(404, {"error": {"message": "no such route"}})
                return
            if not self._authorized(auth):
                return
            model = body.get("model")
            if stub.chat_models is not None and model not in stub.chat_models:
                self._send(stub.unknown_model_status,
                           {"error": {"code": "InvalidEndpointOrModel.NotFound",
                                      "message": f"The model {model} does not exist"}})
                return
            if body.get("reasoning_effort") in stub.reject_efforts:
                self._send(400, {"error": {"message": "reasoning_effort not supported"}})
                return
            self._send(200, {"id": "chatcmpl-1", "object": "chat.completion",
                             "model": stub.served.get(model, model),
                             "choices": [{"index": 0, "finish_reason": "length",
                                          "message": {"role": "assistant", "content": "p"}}],
                             "usage": {"prompt_tokens": 3, "completion_tokens": 1}})

    return Handler


class StubServer:
    def __init__(self, stub: VlmStub):
        self.stub = stub
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stub))
        self.httpd.daemon_threads = True
        stub.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)

    def __enter__(self) -> VlmStub:
        self.thread.start()
        return self.stub

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
