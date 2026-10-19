"""Agentic payments: Skyfire and x402 (SPEC 12)."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import httpx

from .api import USER_AGENT
from .config import Settings
from .core import ToolDef, text
from .log import log, register_secret

SKYFIRE_FIELD = "skyfire-pay-id"
PROTOCOL_HEADER = "x-scrapely-payment-protocol"
LEGACY_PROTOCOL_HEADER = "x-apify-payment-protocol"
X402_META = "x402/payment"
REQUIREMENTS_TTL = 1800.0
REQUIREMENTS_TIMEOUT = 8.0

SKYFIRE_GUIDE = """# Paying for Scrape.ly with Skyfire

This session uses Skyfire for payment instead of a Scrape.ly API token.

1. Create a PAY token with the Skyfire MCP server's create-pay-token tool. Charge it with at least 5 USD.
2. Pass the token (a JWT string that starts with `ey`, not an ID or UUID) in the `skyfire-pay-id` argument of
   every tool that needs payment, such as call-actor, Actor tools, run tools and storage tools.
3. Use the same token again when you read the run's output, for example with get-dataset-items.
"""


def _protocol_headers(settings: Settings, value: str) -> dict[str, str]:
    headers = {PROTOCOL_HEADER: value}
    if settings.send_legacy_payment_header:
        headers[LEGACY_PROTOCOL_HEADER] = value
    return headers


class PaymentProvider:
    kind = "none"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def decorate(self, tool: ToolDef) -> ToolDef:
        return tool

    async def prepare(self, args: dict, meta: dict | None, headers: dict[str, str]) -> tuple[dict | None, dict | None]:
        """Return (payment headers, payment-required result). Removes payment fields from `args`."""
        return None, None

    def payment_required_from_error(self, data: Any) -> dict | None:
        return None


class SkyfireProvider(PaymentProvider):
    kind = "skyfire"

    def decorate(self, tool: ToolDef) -> ToolDef:
        if not tool.payment_required or tool.extra.get("skyfire_decorated"):
            return tool
        new = tool.clone()
        props = new.input_schema.setdefault("properties", {})
        props[SKYFIRE_FIELD] = {
            "type": "string",
            "description": "Skyfire PAY token (a JWT starting with 'ey') that pays for this call.",
        }
        if new.validation_schema is not None:
            import copy

            vs = copy.deepcopy(new.validation_schema)
            vs.setdefault("properties", {})[SKYFIRE_FIELD] = {"type": "string"}
            new.validation_schema = vs
        seller = self.settings.skyfire_seller_service_id
        base_desc = tool.description

        def describe(names: set[str], mode: str) -> str:
            base = base_desc(names, mode) if callable(base_desc) else base_desc
            extra = (
                "\n\nPayment: pass a Skyfire PAY token in `skyfire-pay-id`. It is a JWT string beginning with "
                "'ey' (not an ID or UUID), created with the create-pay-token tool of the Skyfire MCP server and "
                "charged with at least 5 USD. Pass the same token when reading the output with get-dataset-items."
            )
            if seller:
                extra += f" Seller service ID: {seller}."
            return base + extra

        new.description = describe
        new.extra["skyfire_decorated"] = True
        return new

    async def prepare(self, args: dict, meta: dict | None, headers: dict[str, str]) -> tuple[dict | None, dict | None]:
        value = args.pop(SKYFIRE_FIELD, None)
        if not isinstance(value, str) or not value.strip():
            msg = (
                "This tool needs payment. Create a Skyfire PAY token with the Skyfire MCP server's create-pay-token "
                "tool (charge it with at least 5 USD) and pass it in the `skyfire-pay-id` argument."
            )
            return None, {
                "content": [text(msg)],
                "structuredContent": {"paymentRequired": True, "provider": "skyfire", "message": msg},
                "isError": True,
            }
        register_secret(value)
        return {SKYFIRE_FIELD: value, **_protocol_headers(self.settings, "skyfire")}, None


# -- x402 --------------------------------------------------------------------

_req_cache: dict[str, tuple[float, dict]] = {}
_req_inflight: dict[str, asyncio.Future] = {}

PAYMENT_REQUIRED_SCHEMA = {
    "type": "object",
    "properties": {
        "x402Version": {"type": "number"},
        "error": {"type": "string"},
        "resource": {"type": "object"},
        "accepts": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["x402Version", "accepts"],
}


def clear_requirements_cache() -> None:
    _req_cache.clear()


async def fetch_requirements(settings: Settings) -> dict | None:
    key = settings.api_base_url
    hit = _req_cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    pending = _req_inflight.get(key)
    if pending is not None:
        return await asyncio.shield(pending)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _req_inflight[key] = fut
    result: dict | None = None
    try:
        async with httpx.AsyncClient(timeout=REQUIREMENTS_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.api_base_url}/v2/actors/",
                headers={"User-Agent": USER_AGENT, **_protocol_headers(settings, "x402")},
            )
        raw = resp.headers.get("payment-required")
        if raw:
            result = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode())
            if isinstance(result, dict):
                _req_cache[key] = (time.monotonic() + REQUIREMENTS_TTL, result)
            else:
                result = None
    except Exception as exc:  # noqa: BLE001 - failures are not cached
        log.soft_fail("x402 payment requirements unavailable", reason=str(exc))
        result = None
    finally:
        _req_inflight.pop(key, None)
        fut.set_result(result)
    return result


def preferred_accept(req: dict) -> dict | None:
    accepts = [a for a in req.get("accepts") or [] if isinstance(a, dict)]
    for scheme in ("exact", "upto"):
        for a in accepts:
            if a.get("scheme") == scheme:
                return a
    return accepts[0] if accepts else None


class X402Provider(PaymentProvider):
    kind = "x402"

    def __init__(self, settings: Settings, requirements: dict | None = None) -> None:
        super().__init__(settings)
        self.requirements = requirements

    @classmethod
    async def create(cls, settings: Settings) -> X402Provider:
        return cls(settings, await fetch_requirements(settings))

    def decorate(self, tool: ToolDef) -> ToolDef:
        if not tool.payment_required or tool.extra.get("x402_decorated"):
            return tool
        new = tool.clone()
        meta = dict(new.meta or {})
        x: dict[str, Any] = {"paymentRequired": True}
        if self.requirements:
            pref = preferred_accept(self.requirements) or {}
            x.update(pref)
            x["accepts"] = list(self.requirements.get("accepts") or [])
        meta["x402"] = x
        new.meta = meta
        if new.output_schema is not None:
            new.output_schema = {"type": "object", "anyOf": [new.output_schema, PAYMENT_REQUIRED_SCHEMA]}
        base_desc = tool.description

        def describe(names: set[str], mode: str) -> str:
            base = base_desc(names, mode) if callable(base_desc) else base_desc
            return base + (
                "\n\nPayment: this tool needs an x402 payment signature in the request's "
                '`_meta["x402/payment"]`. Your client must support the x402 protocol.'
            )

        new.description = describe
        new.extra["x402_decorated"] = True
        return new

    async def _required_result(self, data: Any = None) -> dict:
        req = data if isinstance(data, dict) and data.get("accepts") is not None else None
        if req is None:
            req = self.requirements or await fetch_requirements(self.settings)
            if req:
                self.requirements = req
        structured = req or {"x402Version": 1, "error": "payment required", "accepts": []}
        return {
            "content": [text(json.dumps(structured)), text("Payment is required: sign an x402 payment and retry.")],
            "structuredContent": structured,
            "isError": True,
        }

    async def prepare(self, args: dict, meta: dict | None, headers: dict[str, str]) -> tuple[dict | None, dict | None]:
        payment = (meta or {}).get(X402_META)
        signature = None
        if isinstance(payment, (dict, list)):
            signature = base64.b64encode(json.dumps(payment, separators=(",", ":")).encode()).decode()
        elif isinstance(payment, str) and payment:
            signature = payment
        if signature is None:
            for k, v in headers.items():
                if k.lower() == "payment-signature" and v:
                    signature = v
                    break
        if not signature:
            return None, await self._required_result()
        register_secret(signature)
        return {"PAYMENT-SIGNATURE": signature, **_protocol_headers(self.settings, "x402")}, None

    def payment_required_from_error(self, data: Any) -> dict | None:
        return data if isinstance(data, dict) else {}


async def make_provider(settings: Settings, raw: str | None) -> PaymentProvider | None:
    value = (raw or "").strip().lower()
    if value == "skyfire":
        return SkyfireProvider(settings)
    if value == "x402":
        return await X402Provider.create(settings)
    return None


def parse_payment(raw: str | None) -> str | None:
    value = (raw or "").strip().lower()
    return value if value in ("skyfire", "x402") else None
