"""resources/* handlers: API proxy, templates, payment guide and widgets (SPEC 10.3, 14.1)."""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from . import widgets
from .api import USER_AGENT
from .config import INLINE_LIMIT_BYTES, normalized_origin
from .core import SessionState, make_api
from .log import log
from .payments import SKYFIRE_GUIDE
from .signing import record_url

GUIDE_URI = "file://readme.md"
_RECORD_PATH = re.compile(r"^/v2/key-value-stores/([^/]+)/records/([^/]+)$")
_TEXTUAL = ("application/json", "application/xml", "application/javascript")


class ResourceError(Exception):
    def __init__(self, message: str, code: int = -32602) -> None:
        super().__init__(message)
        self.code = code


def list_resources(state: SessionState) -> list[dict]:
    out: list[dict] = []
    if state.payment is not None and state.payment.kind == "skyfire":
        out.append(
            {
                "uri": GUIDE_URI,
                "name": "readme",
                "description": "How to pay for Scrape.ly tools with Skyfire in this session.",
                "mimeType": "text/markdown",
            }
        )
    for kind, w in widgets.WIDGETS.items():
        if widgets.asset(state.settings, kind) is not None:
            out.append(
                {
                    "uri": w["uri"],
                    "name": w["name"],
                    "mimeType": widgets.WIDGET_MIME,
                    "_meta": widgets.widget_meta(state.settings, kind),
                }
            )
    return out


def list_templates(state: SessionState) -> list[dict]:
    api = state.settings.public_api_base_url
    note = (
        "Read with resources/read; the server adds your token. Bodies above 256 KB return a download URL instead "
        "of inline content."
    )
    return [
        {
            "uriTemplate": f"{api}/v2/datasets/{{datasetId}}/items{{?limit,offset,clean,fields,format}}",
            "name": "dataset-items",
            "description": "Items of a dataset. Page with limit and offset; `format` picks json, jsonl, xml, html, "
            "csv, xlsx or rss. " + note,
        },
        {
            "uriTemplate": f"{api}/v2/key-value-stores/{{storeId}}/records/{{recordKey}}",
            "name": "key-value-store-record",
            "description": "One record of a key-value store, in its stored content type. " + note,
        },
        {
            "uriTemplate": f"{api}/v2/key-value-stores/{{storeId}}/keys{{?limit,exclusiveStartKey}}",
            "name": "key-value-store-keys",
            "description": "Keys of a key-value store; continue with exclusiveStartKey. " + note,
            "mimeType": "application/json",
        },
        {
            "uriTemplate": f"{api}/v2/actor-runs/{{runId}}",
            "name": "actor-run",
            "description": "One run object with status and storage IDs. " + note,
            "mimeType": "application/json",
        },
        {
            "uriTemplate": f"{api}/v2/logs/{{runId}}",
            "name": "run-log",
            "description": "The full log of a run as plain text. " + note,
            "mimeType": "text/plain",
        },
    ]


def _proxy_allowed(state: SessionState, uri: str) -> bool:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or parts.username or parts.password or "@" in parts.netloc:
        return False
    try:
        origin = normalized_origin(uri)
    except ValueError:
        return False
    return origin in state.settings.api_origins


def _is_textual(ct: str) -> bool:
    base = ct.split(";", 1)[0].strip().lower()
    return base.startswith("text/") or base in _TEXTUAL or base.endswith("+json") or base.endswith("+xml")


async def read_resource(state: SessionState, uri: str, token: str | None, modern: bool = False) -> list[dict]:
    if uri == GUIDE_URI and state.payment is not None and state.payment.kind == "skyfire":
        return [{"uri": uri, "mimeType": "text/markdown", "text": SKYFIRE_GUIDE}]
    if uri.startswith("ui://widget/"):
        kind = widgets.by_uri(uri)
        if kind is None:
            return [{"uri": uri, "mimeType": "text/plain", "text": "This widget is not available."}]
        html = widgets.asset(state.settings, kind)
        if html is None:
            return []
        return [
            {
                "uri": uri,
                "mimeType": widgets.WIDGET_MIME,
                "text": html,
                "_meta": widgets.widget_meta(state.settings, kind),
            }
        ]
    if _proxy_allowed(state, uri):
        if not token:
            raise ResourceError(
                "Reading Scrape.ly API resources needs an API token; payment-only sessions cannot read resources.",
                code=-32001,
            )
        return await _proxy(state, uri, token)
    log.warning("resources/read for an unknown or disallowed resource", uri=uri.split("?", 1)[0])
    if modern:
        raise ResourceError(f"Unknown resource: {uri.split('?', 1)[0]}")
    return []


async def _proxy(state: SessionState, uri: str, token: str) -> list[dict]:
    parts = urlsplit(uri)
    internal = urlsplit(state.settings.api_base_url)
    fetch_url = urlunsplit((internal.scheme, internal.netloc, parts.path, parts.query, ""))
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-Scrapely-Request-Origin": state.request_origin(),
    }
    chunks: list[bytes] = []
    total = 0
    too_large = False
    try:
        # Redirects are not followed: the target path is caller-chosen, and an
        # API route that redirects would send the gateway to any host.
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
            async with client.stream("GET", fetch_url, headers=headers) as resp:
                status = resp.status_code
                ct = resp.headers.get("content-type") or ""
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > INLINE_LIMIT_BYTES:
                        too_large = True
                        break
    except httpx.HTTPError as exc:
        return [{"uri": uri, "mimeType": "text/plain", "text": f"Failed to read {parts.path}: {exc}"}]
    body = b"".join(chunks)
    if status >= 300:
        snippet = body[:2000].decode("utf-8", "replace")[:200]
        return [{"uri": uri, "mimeType": "text/plain", "text": f"Failed to read {parts.path}: HTTP {status}. {snippet}"}]
    if too_large:
        m = _RECORD_PATH.match(parts.path)
        if m:
            api = make_api(state.settings, token, state.request_origin())
            try:
                url = await record_url(api, state.settings, unquote(m.group(1)), unquote(m.group(2)))
            finally:
                await api.aclose()
            msg = f"The body is larger than the inline limit of 256 KB. Download it from: {url}"
        else:
            msg = (
                f"The body is larger than the inline limit of 256 KB. Download it from: {uri} "
                "or read it again with a smaller limit or an offset."
            )
        return [{"uri": uri, "mimeType": "text/plain", "text": msg}]
    if not body:
        return [{"uri": uri, "mimeType": ct or "text/plain", "text": ""}]
    if _is_textual(ct):
        charset = "utf-8"
        m = re.search(r"charset=([\w\-]+)", ct, re.I)
        if m:
            charset = m.group(1)
        try:
            decoded = body.decode(charset, "replace")
        except LookupError:
            decoded = body.decode("utf-8", "replace")
        return [{"uri": uri, "mimeType": ct, "text": decoded}]
    base_mime = ct.split(";", 1)[0].strip() or "application/octet-stream"
    return [{"uri": uri, "mimeType": base_mime, "blob": base64.b64encode(body).decode()}]


__all__: list[Any] = ["list_resources", "list_templates", "read_resource", "ResourceError"]
