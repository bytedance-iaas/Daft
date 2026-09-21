"""A local OpenAI-compatible endpoint answering like ``tools/parity``'s fake model.

Real HTTP on 127.0.0.1, so the CLI runs unchanged (in process or as a subprocess
that gets signals). Answers depend only on the request body (the parity fake's
rule), so a run is reproducible; ``delay_s`` slows every answer down (to stop a
run half way) and ``fail(prompt_text, payload) -> status | None`` makes chosen
requests fail with an HTTP status. ``max_in_flight`` is the most requests it
was ever answering at once.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from parity.fakevlm import FakeVlm, _texts


class FakeVlmServer:
    def __init__(self, *, delay_s: float = 0.0, fail=None, model: str = "fake-vlm"):
        self.delay_s, self.fail, self.model = delay_s, fail, model
        self.fake = FakeVlm(model)
        self.calls: list[dict] = []
        #: requests being answered right now, and the most there ever were at once
        self.in_flight = self.max_in_flight = 0
        self._lock = threading.Lock()
        self._server = None

    def __enter__(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status: int, doc: dict) -> None:
                body = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    return self._send(200, {"object": "list",
                                            "data": [{"id": srv.model, "object": "model"}]})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                with srv._lock:
                    srv.in_flight += 1
                    srv.max_in_flight = max(srv.max_in_flight, srv.in_flight)
                try:
                    self._post()
                finally:
                    with srv._lock:
                        srv.in_flight -= 1

            def _post(self):
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
                text = _texts(payload)
                with srv._lock:
                    srv.calls.append({"path": self.path, "text": text, "payload": payload,
                                      "headers": dict(self.headers)})
                if srv.delay_s:
                    time.sleep(srv.delay_s)
                status = srv.fail(text, payload) if srv.fail else None
                if status:
                    return self._send(int(status), {"error": {"message": "injected"}})
                content = srv.fake.answer(payload)
                self._send(200, {
                    "id": "fake", "object": "chat.completion", "model": payload.get("model"),
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": content}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": len(content) // 4 + 1,
                              "completion_tokens_details": {"reasoning_tokens": 0},
                              "prompt_tokens_details": {"cached_tokens": 0}}})

        class Server(ThreadingHTTPServer):
            request_queue_size = 256            # every call opens a connection (v1's clients)

        self._server = Server(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def count(self, needle: str) -> int:
        with self._lock:
            return sum(1 for c in self.calls if needle in c["text"])
