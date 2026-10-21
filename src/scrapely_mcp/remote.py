"""Client connections to MCP-server Actors (SPEC 8.6).

The connector is a module-level hook so tests can replace it with a fake.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

import anyio

from .errors import RemoteUnreachable
from .log import log

CONNECT_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


def max_response_bytes() -> int:
    """Byte budget for everything one remote connection may send back.

    SCRAPELY_MCP_MAX_REMOTE_RESPONSE_BYTES overrides the 10 MiB default.
    """
    raw = os.environ.get("SCRAPELY_MCP_MAX_REMOTE_RESPONSE_BYTES")
    try:
        value = int(raw) if raw and raw.strip() else DEFAULT_MAX_RESPONSE_BYTES
    except ValueError:
        return DEFAULT_MAX_RESPONSE_BYTES
    return value if value > 0 else DEFAULT_MAX_RESPONSE_BYTES


DEFAULT_MAX_INFLIGHT_BYTES = 64 * 1024 * 1024


def max_inflight_bytes() -> int:
    """Byte budget for what all open remote connections of the process hold together.

    SCRAPELY_MCP_MAX_REMOTE_INFLIGHT_BYTES overrides the 64 MiB default.
    """
    raw = os.environ.get("SCRAPELY_MCP_MAX_REMOTE_INFLIGHT_BYTES")
    try:
        value = int(raw) if raw and raw.strip() else DEFAULT_MAX_INFLIGHT_BYTES
    except ValueError:
        return DEFAULT_MAX_INFLIGHT_BYTES
    return value if value > 0 else DEFAULT_MAX_INFLIGHT_BYTES


class _Inflight:
    """Bytes received by every open remote connection (tool lists, tool results,
    task results still on their way), counted as they arrive and released when
    the connection closes."""

    def __init__(self) -> None:
        self.used = 0


inflight = _Inflight()

# Connections closed inside hold_inflight() stay charged until the block exits.
_held: ContextVar[list | None] = ContextVar("remote_held_budgets", default=None)


def close_budget(budget: "_Budget") -> None:
    """A connection closed: release its bytes, or keep them for the enclosing hold_inflight()."""
    held = _held.get()
    if held is not None:
        held.append(budget)
    else:
        budget.release()


@contextmanager
def hold_inflight():
    """Keep the bytes of remote connections closed in this block charged until it exits.

    A task runner holds them until the task store has taken over the result, so
    a result is counted from its first byte until it is stored.
    """
    held: list = []
    reset = _held.set(held)
    try:
        yield
    finally:
        _held.reset(reset)
        for budget in held:
            budget.release()


# A tighter byte budget for the current connection (set by list_remote_tools).
_connection_limit: ContextVar[int | None] = ContextVar("remote_connection_limit", default=None)


def _connection_budget() -> int:
    limit = max_response_bytes()
    override = _connection_limit.get()
    return min(limit, override) if override else limit


class RemoteResponseTooLarge(RemoteUnreachable):
    """An MCP-server Actor sent more data than the gateway accepts."""


class RemoteBusy(RemoteResponseTooLarge):
    """All remote connections together hold more data than the process-wide budget."""


class RemoteUrlRefused(RemoteUnreachable):
    """An MCP-server Actor tried to send the gateway outside the Actor's own standby path."""


# Every request to an MCP-server Actor carries this header. The API serves a
# request that carries it only from the Actor standby router, never from a
# user-authenticated API route (scrapely-server middleware/mcpRemoteMarker.ts).
MCP_REMOTE_HEADER = "X-Scrapely-Mcp-Remote"

_UNSAFE_PATH_BYTES = (b"%2e", b"%2f", b"%5c", b"\\")


class ActorScope:
    """Where requests of one MCP-server Actor connection may go: the scheme, host
    and port of its MCP URL and the path prefix `/v2/{username}/{name}/`.

    The caller's bearer is attached to every request of the connection, so a
    request anywhere else on the API origin would be made as the caller: the
    legacy SSE transport POSTs to whatever `endpoint` the Actor's server names,
    and both transports follow same-origin 307/308 redirects. The check runs in
    the HTTP transport, under every SDK code path.
    """

    def __init__(self, scheme: str, host: str, port: int | None, prefix: bytes) -> None:
        self.scheme = scheme
        self.host = host
        self.port = port
        self.prefix = prefix

    def allows(self, url: Any) -> bool:
        import httpx2

        try:
            u = url if isinstance(url, httpx2.URL) else httpx2.URL(str(url))
        except Exception:  # noqa: BLE001
            return False
        if (u.scheme, u.host, u.port) != (self.scheme, self.host, self.port) or u.userinfo:
            return False
        path = u.raw_path.split(b"?", 1)[0]
        if not path.startswith(self.prefix):
            return False
        lowered = path.lower()
        if any(bad in lowered for bad in _UNSAFE_PATH_BYTES):
            return False
        # No dot segments and no empty segments (a trailing slash is fine).
        segments = path[len(self.prefix):].split(b"/")
        if any(seg in (b".", b"..") for seg in segments):
            return False
        return all(segments[:-1])


def actor_scope(url: str) -> ActorScope:
    """The scope of an MCP-server Actor URL (`{standby_base}/v2/{username}/{name}/...`).

    Fails closed: a URL that is not an Actor standby path gets no connection.
    """
    import httpx2

    try:
        u = httpx2.URL(url)
    except Exception as exc:  # noqa: BLE001
        raise RemoteUrlRefused("the Actor's MCP server URL is not valid") from exc
    segs = u.raw_path.split(b"?", 1)[0].split(b"/")
    for i, seg in enumerate(segs):
        if seg == b"v2" and len(segs) > i + 3 and segs[i + 1] and segs[i + 2]:
            prefix = b"/".join(segs[: i + 3]) + b"/"
            scope = ActorScope(u.scheme, u.host, u.port, prefix)
            if not scope.allows(u):
                break
            return scope
    raise RemoteUrlRefused("the Actor's MCP server URL is not under the Actor's standby path")


class _Budget:
    """Shared byte counter for one remote connection (all its HTTP responses).

    Every byte is also charged to the process-wide `inflight` counter until
    release() (when the connection closes). On overflow of either budget the
    reading task fails and every registered cancel scope is cancelled, so the
    waiting tool call ends at once instead of at its timeout.
    """

    def __init__(self, limit: int, inflight_limit: int | None = None) -> None:
        self.limit = limit
        self.inflight_limit = inflight_limit if inflight_limit is not None else max_inflight_bytes()
        self.used = 0
        self.charged = 0
        self.exceeded = False
        self.busy = False
        self.refused: RemoteUrlRefused | None = None
        self.scopes: list[anyio.CancelScope] = []

    def consume(self, n: int) -> None:
        self.used += n
        self.charged += n
        inflight.used += n
        if not self.exceeded:
            if self.used > self.limit:
                self.exceeded = True
            elif inflight.used > self.inflight_limit:
                self.exceeded = self.busy = True
            if self.exceeded:
                for scope in self.scopes:
                    scope.cancel()
        if self.exceeded:
            raise self.error()

    def refuse(self, error: RemoteUrlRefused) -> None:
        """End the connection: a request left the Actor's scope. Cancels the waiting call at once."""
        if self.refused is None:
            self.refused = error
        self.exceeded = True
        for scope in self.scopes:
            scope.cancel()

    def release(self) -> None:
        """Give this connection's bytes back to the process-wide counter (idempotent)."""
        inflight.used -= self.charged
        self.charged = 0

    def error(self) -> RemoteUnreachable:
        if self.refused is not None:
            return self.refused
        if self.busy:
            return RemoteBusy(
                "the server is receiving too much data from MCP-server Actors right now; retry in a moment"
            )
        return RemoteResponseTooLarge(self.message())

    def message(self) -> str:
        return f"the Actor's MCP server response exceeded the {self.limit} byte limit"

    def guard(self) -> "_Guard":
        return _Guard(self)


class _Guard:
    def __init__(self, budget: _Budget) -> None:
        self.budget = budget
        self.scope = anyio.CancelScope()

    def __enter__(self) -> anyio.CancelScope:
        self.budget.scopes.append(self.scope)
        self.scope.__enter__()
        return self.scope

    def __exit__(self, *exc: Any) -> bool:
        try:
            suppressed = self.scope.__exit__(*exc)
        finally:
            self.budget.scopes.remove(self.scope)
        if self.budget.exceeded:
            raise self.budget.error()
        return bool(suppressed)


def _capped_transport(budget: _Budget, scope: ActorScope | None = None):
    import httpx2

    class _CappedStream(httpx2.AsyncByteStream):
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def __aiter__(self):
            async for chunk in self._inner:
                budget.consume(len(chunk))
                yield chunk

        async def aclose(self) -> None:
            await self._inner.aclose()

    class _CappedTransport(httpx2.AsyncBaseTransport):
        def __init__(self) -> None:
            self._inner = httpx2.AsyncHTTPTransport()

        async def handle_async_request(self, request: Any) -> Any:
            if scope is not None and not scope.allows(request.url):
                error = RemoteUrlRefused(
                    "the Actor's MCP server tried to send the request outside the Actor's own address; "
                    "the gateway refused it"
                )
                log.warning(
                    "refused a request of an MCP-server Actor connection outside the Actor's standby path",
                    method=request.method,
                    path=request.url.raw_path.split(b"?", 1)[0].decode("ascii", "replace")[:300],
                )
                budget.refuse(error)
                raise error
            response = await self._inner.handle_async_request(request)
            return httpx2.Response(
                status_code=response.status_code,
                headers=response.headers,
                stream=_CappedStream(response.stream),
                extensions=response.extensions,
            )

        async def aclose(self) -> None:
            await self._inner.aclose()

    return _CappedTransport()


class RemoteSession(Protocol):
    async def list_tools(self) -> list[dict]: ...

    async def call_tool(self, name: str, arguments: dict) -> dict: ...


class _SdkSession:
    def __init__(self, client: Any, budget: _Budget | None = None) -> None:
        self._client = client
        self._budget = budget or _Budget(_connection_budget())

    async def list_tools(self) -> list[dict]:
        with self._budget.guard():
            result = await self._client.list_tools()
        return [t.model_dump(by_alias=True, mode="json", exclude_none=True) for t in result.tools]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        with self._budget.guard():
            result = await self._client.call_tool(name, arguments)
        return result.model_dump(by_alias=True, mode="json", exclude_none=True)


@asynccontextmanager
async def _sdk_connect(url: str, token: str | None) -> AsyncIterator[RemoteSession]:
    import httpx2
    from mcp import Client
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client

    scope = actor_scope(url)
    headers = {MCP_REMOTE_HEADER: "1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    budget = _Budget(_connection_budget())
    try:

        def capped_client(headers: dict[str, str] | None = None, timeout: Any = None, auth: Any = None) -> Any:
            return httpx2.AsyncClient(
                headers=headers,
                timeout=timeout or httpx2.Timeout(30.0, read=CALL_TIMEOUT),
                auth=auth,
                transport=_capped_transport(budget, scope),
            )

        async def enter(factory) -> Any:
            client = Client(factory(), mode="legacy", cache=None)
            with anyio.fail_after(CONNECT_TIMEOUT), budget.guard():
                await client.__aenter__()
            return client

        client = None
        try:
            http_client = capped_client(headers)
            client = await enter(lambda: streamable_http_client(url, http_client=http_client))
        except (RemoteResponseTooLarge, RemoteUrlRefused):
            raise
        except TimeoutError as exc:
            raise RemoteUnreachable(
                f"MCP server at the Actor did not answer within {int(CONNECT_TIMEOUT)}s"
            ) from exc
        except Exception as first:  # noqa: BLE001 - fall back to legacy SSE
            if budget.exceeded:
                raise budget.error() from first
            log.soft_fail("streamable HTTP connect to MCP-server Actor failed, trying SSE", reason=str(first))
            try:
                client = await enter(lambda: sse_client(url, headers=headers, httpx_client_factory=capped_client))
            except (RemoteResponseTooLarge, RemoteUrlRefused):
                raise
            except TimeoutError as exc:
                raise RemoteUnreachable("MCP server at the Actor did not answer in time") from exc
            except Exception as exc:  # noqa: BLE001
                if budget.exceeded:
                    raise budget.error() from exc
                raise RemoteUnreachable(f"could not connect to the Actor's MCP server: {exc}") from exc
        try:
            yield _SdkSession(client, budget)
        finally:
            try:
                await client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
    finally:
        close_budget(budget)


# Replaceable connector: (url, token) -> async context manager yielding a RemoteSession.
connector = _sdk_connect


async def list_remote_tools(url: str, token: str | None, max_bytes: int | None = None) -> list[dict]:
    """The remote tool list; `max_bytes` lowers the connection's byte budget for it."""
    reset = _connection_limit.set(max_bytes if max_bytes and max_bytes > 0 else None)
    try:
        async with connector(url, token) as session:
            return await session.list_tools()
    finally:
        _connection_limit.reset(reset)


async def call_remote_tool(url: str, token: str | None, name: str, arguments: dict) -> dict:
    with anyio.fail_after(CALL_TIMEOUT):
        async with connector(url, token) as session:
            return await session.call_tool(name, arguments)
