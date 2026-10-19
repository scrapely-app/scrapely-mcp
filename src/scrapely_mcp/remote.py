"""Client connections to MCP-server Actors (SPEC 8.6).

The connector is a module-level hook so tests can replace it with a fake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

import anyio

from .errors import RemoteUnreachable
from .log import log

CONNECT_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0


class RemoteSession(Protocol):
    async def list_tools(self) -> list[dict]: ...

    async def call_tool(self, name: str, arguments: dict) -> dict: ...


class _SdkSession:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def list_tools(self) -> list[dict]:
        result = await self._client.list_tools()
        return [t.model_dump(by_alias=True, mode="json", exclude_none=True) for t in result.tools]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        result = await self._client.call_tool(name, arguments)
        return result.model_dump(by_alias=True, mode="json", exclude_none=True)


@asynccontextmanager
async def _sdk_connect(url: str, token: str | None) -> AsyncIterator[RemoteSession]:
    import httpx2
    from mcp import Client
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def enter(factory) -> Any:
        client = Client(factory(), mode="legacy", cache=None)
        with anyio.fail_after(CONNECT_TIMEOUT):
            await client.__aenter__()
        return client

    client = None
    try:
        http_client = httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30.0, read=CALL_TIMEOUT))
        client = await enter(lambda: streamable_http_client(url, http_client=http_client))
    except TimeoutError as exc:
        raise RemoteUnreachable(f"MCP server at the Actor did not answer within {int(CONNECT_TIMEOUT)}s") from exc
    except Exception as first:  # noqa: BLE001 - fall back to legacy SSE
        log.soft_fail("streamable HTTP connect to MCP-server Actor failed, trying SSE", reason=str(first))
        try:
            client = await enter(lambda: sse_client(url, headers=headers))
        except TimeoutError as exc:
            raise RemoteUnreachable("MCP server at the Actor did not answer in time") from exc
        except Exception as exc:  # noqa: BLE001
            raise RemoteUnreachable(f"could not connect to the Actor's MCP server: {exc}") from exc
    try:
        yield _SdkSession(client)
    finally:
        try:
            await client.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


# Replaceable connector: (url, token) -> async context manager yielding a RemoteSession.
connector = _sdk_connect


async def list_remote_tools(url: str, token: str | None) -> list[dict]:
    async with connector(url, token) as session:
        return await session.list_tools()


async def call_remote_tool(url: str, token: str | None, name: str, arguments: dict) -> dict:
    with anyio.fail_after(CALL_TIMEOUT):
        async with connector(url, token) as session:
            return await session.call_tool(name, arguments)
