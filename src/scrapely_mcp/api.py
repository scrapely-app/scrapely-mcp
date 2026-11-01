"""Minimal async client for the Scrape.ly REST API (httpx based)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx

from . import __version__
from .errors import ApiError
from .log import register_secret

USER_AGENT = f"scrapely-mcp/{__version__} (Origin/mcp-server; python)"
DEFAULT_TIMEOUT = 30.0
_RETRY_STATUSES = {429, 502, 503, 504}
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
X402_SESSION_HEADER = "x-scrapely-x402-session"


def path_ref(ref: str) -> str:
    """Encode an Actor/storage/task reference as one URL path segment.

    `username/name` becomes `username~name` (the API's form for names).
    """
    return quote(ref.replace("/", "~"), safe="~")


def seg(value: str) -> str:
    return quote(value, safe="")


class CappedRead(NamedTuple):
    """A GET response whose body was read up to a byte budget."""

    response: httpx.Response  # holds at most the budget's bytes of (decoded) body
    truncated: bool  # the body had more bytes than the budget
    declared_length: int | None  # the Content-Length the API sent, if any


# Headers that describe the body as sent, not the decoded bytes kept in a CappedRead.
_BODY_FRAMING_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}
_ERROR_BODY_MAX_BYTES = 65_536


class ApiClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        request_origin: str = "MCP",
        extra_headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        headers = {
            "User-Agent": USER_AGENT,
            "X-Scrapely-Request-Origin": request_origin,
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra_headers:
            headers.update(extra_headers)
        self.headers = headers
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, headers=self.headers, timeout=self._timeout, follow_redirects=True
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> ApiClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- low level -------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        clean_params = _clean_params(params)
        attempts = 3 if method == "GET" else 1
        delay = 0.25
        last: httpx.Response | None = None
        for attempt in range(attempts):
            kwargs: dict[str, Any] = {"params": clean_params}
            if json_body is not None:
                kwargs["content"] = json.dumps(json_body)
                kwargs["headers"] = {"Content-Type": "application/json", **(headers or {})}
            elif headers:
                kwargs["headers"] = headers
            if timeout is not None:
                kwargs["timeout"] = timeout
            last = await self.client.request(method, path, **kwargs)
            self._adopt_x402_session(last)
            if last.status_code in _RETRY_STATUSES and attempt + 1 < attempts:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return last
        if last is None:
            raise RuntimeError("request loop made no attempts")
        return last

    async def get_capped(
        self,
        path: str,
        *,
        max_bytes: int,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> CappedRead:
        """GET read as a stream that stops after `max_bytes` decoded bytes.

        For bodies whose size the caller does not control (dataset pages,
        records): the API streams whatever the request selects, so the gateway
        never holds more than `max_bytes` of one response. Error bodies are read
        up to 64 KB.
        """
        clean_params = _clean_params(params)
        delay = 0.25
        attempt = 0
        while True:
            attempt += 1
            async with self.client.stream("GET", path, params=clean_params, headers=headers) as resp:
                self._adopt_x402_session(resp)
                if not (resp.status_code in _RETRY_STATUSES and attempt < 3):
                    limit = max_bytes if resp.status_code < 400 else min(max_bytes, _ERROR_BODY_MAX_BYTES)
                    chunks: list[bytes] = []
                    total = 0
                    truncated = False
                    async for chunk in resp.aiter_bytes():
                        room = limit - total
                        if len(chunk) > room:
                            chunks.append(chunk[:room])
                            truncated = True
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                    try:
                        declared = int(resp.headers.get("content-length") or "")
                    except ValueError:
                        declared = None
                    kept = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in _BODY_FRAMING_HEADERS]
                    out = httpx.Response(resp.status_code, headers=kept, content=b"".join(chunks), request=resp.request)
                    return CappedRead(out, truncated, declared)
            await asyncio.sleep(delay)
            delay *= 2

    def _adopt_x402_session(self, resp: httpx.Response) -> None:
        """An x402 payment is single-use: the API returns a session token on the
        request it authenticated, and the remaining requests of this tool call use it."""
        session = resp.headers.get(X402_SESSION_HEADER)
        if not session or PAYMENT_SIGNATURE_HEADER not in self.headers:
            return
        register_secret(session)
        self.headers.pop(PAYMENT_SIGNATURE_HEADER, None)
        self.headers[X402_SESSION_HEADER] = session
        if self._client is not None:
            self._client.headers.pop(PAYMENT_SIGNATURE_HEADER, None)
            self._client.headers[X402_SESSION_HEADER] = session

    @staticmethod
    def raise_for(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        err_type = None
        message = f"HTTP {resp.status_code}"
        data = None
        try:
            body = resp.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                err_type = err.get("type")
                message = err.get("message") or message
                data = err.get("data")
            elif isinstance(body, dict) and body.get("message"):
                message = str(body["message"])
        except (ValueError, AttributeError):
            # A non-JSON body (e.g. a proxy's HTML error page) is not echoed to the client.
            pass
        raise ApiError(message, status=resp.status_code, type=err_type, data=data)

    @staticmethod
    def unwrap(resp: httpx.Response) -> Any:
        if not resp.content:
            return None
        try:
            body = resp.json()
        except ValueError:
            return resp.text
        if isinstance(body, dict) and "data" in body and len(body) <= 2 and set(body) <= {"data", "meta"}:
            return body["data"]
        return body

    async def get_json(self, path: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        resp = await self.request("GET", path, params=params, **kw)
        self.raise_for(resp)
        return self.unwrap(resp)

    async def get_or_none(self, path: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        """GET returning None on 404 (and on 400 when `none_on_400`)."""
        none_on_400 = kw.pop("none_on_400", False)
        resp = await self.request("GET", path, params=params, **kw)
        if resp.status_code == 404 or (none_on_400 and resp.status_code == 400):
            return None
        self.raise_for(resp)
        data = self.unwrap(resp)
        return data

    async def post_json(self, path: str, body: Any = None, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        resp = await self.request("POST", path, params=params, json_body=body, **kw)
        self.raise_for(resp)
        return self.unwrap(resp)

    async def put_json(self, path: str, body: Any, **kw: Any) -> Any:
        resp = await self.request("PUT", path, json_body=body, **kw)
        self.raise_for(resp)
        return self.unwrap(resp)

    # -- endpoints -------------------------------------------------------

    async def get_actor(self, ref: str) -> Any:
        return await self.get_or_none(f"/v2/actors/{path_ref(ref)}", none_on_400=True)

    async def store_search(self, params: dict[str, Any]) -> list[dict]:
        data = await self.get_json("/v2/store/actors", params)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if isinstance(data, dict):
            items = data.get("items") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
        return [i for i in items if isinstance(i, dict)]

    async def start_run(self, ref: str, run_input: Any, params: dict[str, Any]) -> dict:
        return await self.post_json(f"/v2/actors/{path_ref(ref)}/runs", run_input if run_input is not None else {}, params)

    async def get_run(self, run_id: str, wait_secs: int | None = None) -> dict | None:
        params = {"waitForFinish": wait_secs} if wait_secs else None
        timeout = (wait_secs + 30) if wait_secs else None
        return await self.get_or_none(f"/v2/actor-runs/{seg(run_id)}", params, timeout=timeout)

    async def abort_run(self, run_id: str, gracefully: bool | None = None) -> dict:
        params = {"gracefully": gracefully} if gracefully is not None else None
        return await self.post_json(f"/v2/actor-runs/{seg(run_id)}/abort", None, params)

    async def get_me(self) -> dict | None:
        return await self.get_json("/v2/users/me")


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    out: dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, float) and v.is_integer():
            out[k] = str(int(v))
        else:
            out[k] = v
    return out
