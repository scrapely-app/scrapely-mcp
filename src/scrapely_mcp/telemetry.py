"""Opt-in usage telemetry (SPEC 13.3).

Off unless telemetry is enabled AND a write key is configured. Events go to a
Segment-compatible `track` endpoint (SCRAPELY_TELEMETRY_ENDPOINT). No keys are bundled.
"""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from . import __version__
from .log import log

DEFAULT_ENDPOINT = "https://api.segment.io/v1/track"
EVENT_NAME = "MCP Tool Call"

_pending: set[asyncio.Task] = set()
# Hook for tests: when set, events are appended here instead of being sent.
sink: list[dict] | None = None


def active(enabled: bool, write_key: str | None) -> bool:
    return bool(enabled and write_key)


async def _send(write_key: str, payload: dict) -> None:
    endpoint = os.environ.get("SCRAPELY_TELEMETRY_ENDPOINT") or DEFAULT_ENDPOINT
    auth = base64.b64encode(f"{write_key}:".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(endpoint, json=payload, headers={"Authorization": f"Basic {auth}"})
    except Exception as exc:  # noqa: BLE001 - telemetry failures are ignored
        log.debug("telemetry send failed", reason=str(exc))


def track(write_key: str, session_id: str, properties: dict[str, Any]) -> None:
    payload = {
        "event": EVENT_NAME,
        "anonymousId": session_id or str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "properties": {"app": "scrapely-mcp", "app_version": __version__, **properties},
    }
    if sink is not None:
        sink.append(payload)
        return
    try:
        task = asyncio.get_running_loop().create_task(_send(write_key, payload))
    except RuntimeError:
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def flush(timeout: float = 5.0) -> None:
    if not _pending:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*list(_pending), return_exceptions=True), timeout)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        pass
