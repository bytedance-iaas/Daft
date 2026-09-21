"""OpenAI-compatible calls made with a VLM backend's API key (design doc 08, section 4; D8).

Two calls, both small:

* ``GET {endpoint}/models`` - list the models. Self-hosted vLLM always answers; Ark may not
  (its model list API wants management AK/SK, which the platform does not take). Not
  listing is not an error: the user types the model ID or the endpoint ID (``ep-...``).
* a **minimal chat call** - one short message, ``max_tokens`` 1 (v1 ``probe_endpoint``).
  It proves the endpoint is reachable, the key is accepted and the model is served, which is
  what a task needs. When the effective reasoning effort is NULL the body has **no**
  ``reasoning_effort`` key at all (08 §4.1 rule 2); otherwise it carries the native value.

The API key goes into the ``Authorization`` header and nowhere else. Whatever the server
answers is scrubbed of the key before it is kept or shown.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .scrub import Scrubber, clip

LIST_TIMEOUT_S = 10.0
CALL_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 5.0
PING = "ping"
#: A listing longer than this is cut (a gateway can list hundreds of models).
MAX_LISTED = 1000
_SUFFIXES = ("/chat/completions", "/completions", "/models")


def normalize_endpoint(raw: str | None) -> str:
    """The OpenAI-compatible base URL (``https://ark.cn-beijing.volces.com/api/v3``).

    A pasted ``.../chat/completions`` or ``.../models`` is cut back to the base. Credentials,
    queries and fragments are refused: the endpoint is stored and shown in the clear, so an
    API key must never ride in it. Raises ``ValueError`` with a Chinese message.
    """
    s = str(raw or "").strip()
    if not s:
        raise ValueError("endpoint 不能为空")
    parts = urlsplit(s)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("endpoint 要以 http:// 或 https:// 开头，形如 "
                         "https://ark.cn-beijing.volces.com/api/v3")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValueError("endpoint 里不能带账号或密码，API Key 请填在「API Key」一栏")
    if parts.query or parts.fragment:
        raise ValueError("endpoint 里不能带 ? 或 # 后面的参数，API Key 请填在「API Key」一栏")
    path = parts.path.rstrip("/")
    for suffix in _SUFFIXES:
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return f"{parts.scheme}://{parts.netloc}{path}"


def minimal_request_body(model: str, reasoning_effort: str | None) -> dict:
    """The body of the minimal call; no ``reasoning_effort`` key when it is None."""
    body: dict[str, Any] = {"model": model, "max_tokens": 1, "temperature": 0,
                            "messages": [{"role": "user", "content": PING}]}
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
    return body


def _headers(api_key: str | None) -> dict:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


@dataclass(frozen=True)
class VlmFailure:
    kind: str           # auth | not_found | unsupported | rejected | rate_limited | server | timeout | unreachable | bad_response
    status: int | None
    reason: str         # one Chinese sentence, scrubbed
    detail: str         # the server's words, scrubbed, one line

    def to_json(self) -> dict:
        return {"kind": self.kind, "http_status": self.status, "reason": self.reason}


@dataclass(frozen=True)
class Listing:
    ok: bool
    models: tuple[str, ...] = ()
    truncated: bool = False
    failure: VlmFailure | None = None


@dataclass(frozen=True)
class CallResult:
    ok: bool
    served_model: str | None = None     # the ``model`` the server says it ran (resolves ep-...)
    failure: VlmFailure | None = None


def _provider_message(resp) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text or ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            parts = [str(err.get(k)) for k in ("code", "message") if err.get(k)]
            if parts:
                return ": ".join(parts)
        elif isinstance(err, str):
            return err
        for key in ("message", "detail", "msg"):
            if data.get(key):
                return str(data[key])
    return json.dumps(data, ensure_ascii=False)[:400]


class VlmConnector:
    """Talks HTTP through ``requests`` (a production dependency); tests use a local stub."""

    def __init__(self, *, list_timeout_s: float = LIST_TIMEOUT_S,
                 call_timeout_s: float = CALL_TIMEOUT_S, http=None):
        self.list_timeout_s = list_timeout_s
        self.call_timeout_s = call_timeout_s
        self._http = http

    def _requests(self):
        if self._http is not None:
            return self._http
        import requests

        return requests

    # -- failures ----------------------------------------------------------------
    def _from_exception(self, exc: BaseException, endpoint: str, timeout_s: float,
                        scrub: Scrubber) -> VlmFailure:
        requests = self._requests()
        detail = clip(scrub(f"{type(exc).__name__}: {exc}"))
        timeout_types = tuple(t for t in (getattr(requests, "Timeout", None),) if t)
        if timeout_types and isinstance(exc, timeout_types):
            return VlmFailure("timeout", None, f"调用超时：{timeout_s:g} 秒内没有响应", detail)
        return VlmFailure("unreachable", None, f"连不上 {endpoint}：检查地址和网络", detail)

    @staticmethod
    def _from_response(resp, *, listing: bool, model: str | None,
                       scrub: Scrubber) -> VlmFailure:
        status = int(resp.status_code)
        detail = clip(scrub(_provider_message(resp)))
        tag = f"HTTP {status}"
        if status in (401, 403):
            return VlmFailure("auth", status, f"API Key 无效，或者没有调用权限（{tag}）", detail)
        if listing and status in (404, 405, 501):
            return VlmFailure("unsupported", status,
                              f"这个服务不提供模型列表（{tag}），请手动填写模型", detail)
        if status == 404:
            return VlmFailure(
                "not_found", status,
                f"模型 {model} 不存在或者没有开通，也可能是 endpoint 的路径不对（{tag}）：{detail}",
                detail)
        if status == 429:
            return VlmFailure("rate_limited", status, f"调用太频繁，或者额度已用完（{tag}）", detail)
        if status >= 500:
            return VlmFailure("server", status, f"模型服务出错（{tag}），请稍后重试：{detail}",
                              detail)
        return VlmFailure("rejected", status, f"请求被拒绝（{tag}）：{detail}", detail)

    # -- calls ---------------------------------------------------------------------
    def list_models(self, endpoint: str, api_key: str | None) -> Listing:
        scrub = Scrubber([api_key])
        requests = self._requests()
        try:
            resp = requests.get(endpoint.rstrip("/") + "/models", headers=_headers(api_key),
                                timeout=(CONNECT_TIMEOUT_S, self.list_timeout_s),
                                allow_redirects=False)
        except Exception as exc:  # noqa: BLE001 - every transport error is a failed listing
            return Listing(False, failure=self._from_exception(exc, endpoint, self.list_timeout_s,
                                                               scrub))
        if not 200 <= resp.status_code < 300:
            return Listing(False, failure=self._from_response(resp, listing=True, model=None,
                                                              scrub=scrub))
        try:
            data = resp.json().get("data")
            ids = [str(m["id"]).strip() for m in data if isinstance(m, dict) and m.get("id")]
        except (ValueError, AttributeError, TypeError):
            return Listing(False, failure=VlmFailure(
                "unsupported", resp.status_code,
                "返回的模型列表不是 OpenAI 兼容的格式，请手动填写模型",
                clip(scrub(resp.text or ""))))
        models = tuple(dict.fromkeys(m for m in ids if m))
        return Listing(True, models[:MAX_LISTED], truncated=len(models) > MAX_LISTED)

    def minimal_call(self, endpoint: str, api_key: str | None, model: str,
                     reasoning_effort: str | None = None, *,
                     timeout_s: float | None = None) -> CallResult:
        scrub = Scrubber([api_key])
        timeout = self.call_timeout_s if timeout_s is None else timeout_s
        requests = self._requests()
        body = minimal_request_body(model, reasoning_effort)
        try:
            resp = requests.post(endpoint.rstrip("/") + "/chat/completions", json=body,
                                 headers=_headers(api_key), timeout=(CONNECT_TIMEOUT_S, timeout),
                                 allow_redirects=False)
        except Exception as exc:  # noqa: BLE001
            return CallResult(False, failure=self._from_exception(exc, endpoint, timeout, scrub))
        if not 200 <= resp.status_code < 300:
            return CallResult(False, failure=self._from_response(resp, listing=False, model=model,
                                                                 scrub=scrub))
        try:
            data = resp.json()
            choices = data.get("choices")
            if not isinstance(choices, list):
                raise TypeError("no choices")
        except (ValueError, AttributeError, TypeError):
            return CallResult(False, failure=VlmFailure(
                "bad_response", resp.status_code, "返回的内容不是 OpenAI 兼容的格式",
                clip(scrub(resp.text or ""))))
        served = data.get("model")
        served = clip(scrub(served), 128) if isinstance(served, str) and served.strip() else None
        return CallResult(True, served_model=served)
