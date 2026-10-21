"""Core data structures: tool definitions, session state and per-call context."""

from __future__ import annotations

import copy
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .api import ApiClient
from .config import Settings
from .util import compact_json
from .validation import ArgValidator

if TYPE_CHECKING:
    from .payments import PaymentProvider

Handler = Callable[["CallContext", dict], Awaitable[dict]]
DescriptionFn = Callable[[set[str], str], str]


@dataclass
class ToolDef:
    name: str
    title: str
    description: str | DescriptionFn
    input_schema: dict
    output_schema: dict | None
    annotations: dict
    handler: Handler
    kind: str = "helper"  # helper | actor | proxied
    payment_required: bool = False
    task_support: bool = False
    lenient: bool = False
    token_free: bool = False
    meta: dict | None = None
    actor_full_name: str | None = None
    actor_id: str | None = None
    validation_schema: dict | None = None
    extra: dict = field(default_factory=dict)
    _validator: ArgValidator | None = field(default=None, repr=False)

    def describe(self, names: set[str], mode: str) -> str:
        if callable(self.description):
            return self.description(names, mode)
        return self.description

    @property
    def validator(self) -> ArgValidator:
        if self._validator is None:
            self._validator = ArgValidator(
                self.validation_schema or self.input_schema, lenient_top_level=self.lenient
            )
        return self._validator

    def clone(self, **changes: Any) -> ToolDef:
        new = copy.copy(self)
        new.input_schema = copy.deepcopy(self.input_schema)
        new.output_schema = copy.deepcopy(self.output_schema)
        new.annotations = dict(self.annotations)
        new.meta = copy.deepcopy(self.meta)
        new.extra = dict(self.extra)
        new._validator = None
        for k, v in changes.items():
            setattr(new, k, v)
        return new


@dataclass
class ClientContext:
    protocol_version: str | None = None
    client_info: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)

    @property
    def name(self) -> str | None:
        name = self.client_info.get("name") if isinstance(self.client_info, dict) else None
        return name if isinstance(name, str) and name else None


@dataclass
class SessionState:
    settings: Settings
    token: str | None = None
    payment: PaymentProvider | None = None
    ui_option: str = "auto"
    mode: str = "default"
    telemetry_enabled: bool = False
    transport: str = "http"
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_scope: str | None = None
    client: ClientContext = field(default_factory=ClientContext)
    base_tools: list[ToolDef] = field(default_factory=list)
    tools: OrderedDict[str, ToolDef] = field(default_factory=OrderedDict)
    stateless: bool = False
    # This session's share of the process-wide Actor tool budget (selection.ToolReservation).
    tool_reservation: Any = None

    def release_tools(self) -> None:
        """Give this session's Actor tool bytes back to the process-wide budget (idempotent)."""
        if self.tool_reservation is not None:
            self.tool_reservation.release()

    def tool_names(self) -> set[str]:
        return set(self.tools.keys())

    def request_origin(self) -> str:
        return "SCRAPELY_AI" if self.client.name == "scrapely-console-ai-chat" else "MCP"

    def report_problem_available(self) -> bool:
        if "report-problem" not in self.tools:
            return False
        return not self.report_problem_blocked()

    def report_problem_blocked(self) -> bool:
        name = self.client.name
        if not name:
            return False
        lowered = name.lower()
        return any(s and s in lowered for s in self.settings.report_problem_blocked)

    def visible_tools(self) -> list[ToolDef]:
        blocked = self.report_problem_blocked()
        return [t for t in self.tools.values() if not (blocked and t.name == "report-problem")]


def make_api(settings: Settings, token: str | None, origin: str = "MCP", extra_headers: dict | None = None) -> ApiClient:
    from .log import register_secret

    register_secret(token)
    if token and token.strip().lower() == "your-scrapely-token":
        token = None
    return ApiClient(settings.api_base_url, token, request_origin=origin, extra_headers=extra_headers)


@dataclass
class CallContext:
    state: SessionState
    tool: ToolDef
    api: ApiClient
    token: str | None
    request_ctx: Any = None
    progress_token: str | int | None = None
    task_mode: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    telemetry: dict = field(default_factory=dict)
    notify_progress: Callable[[float, str, dict], Awaitable[None]] | None = None
    caller: Any = None  # cached CallerInfo, filled lazily

    async def get_caller(self):
        if self.caller is None:
            from .identity import get_caller

            self.caller = await get_caller(self.api)
        return self.caller

    def console_on(self) -> bool:
        from .links import console_enabled

        return console_enabled(self.settings, self.token)

    async def console_link(self, path: str) -> str | None:
        if not self.console_on():
            return None
        from .links import console_url

        return console_url(self.settings, await self.get_caller(), path)

    @property
    def settings(self) -> Settings:
        return self.state.settings

    def has(self, tool_name: str) -> bool:
        return tool_name in self.state.tools and not (
            tool_name == "report-problem" and self.state.report_problem_blocked()
        )


# -- result builders -----------------------------------------------------


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def ok(structured: dict | None, texts: list[str], meta: dict | None = None) -> dict:
    result: dict[str, Any] = {"content": [text(t) for t in texts], "isError": False}
    if structured is not None:
        result["structuredContent"] = structured
    if meta:
        result["_meta"] = meta
    return result


def storage_result(data: dict, summary: str, next_step: str | None, console_url: str | None = None) -> dict:
    structured = dict(data)
    structured["summary"] = summary
    if next_step:
        structured["nextStep"] = next_step
    texts = [compact_json(data), summary + (f"\n{next_step}" if next_step else "")]
    if console_url:
        texts.append(f"Dashboard: {console_url} (show this URL to the user exactly as written)")
    return ok(structured, texts)


def run_result(structured: dict, meta: dict | None = None) -> dict:
    texts =[compact_json(structured), f"{structured.get('summary', '')}\n{structured.get('nextStep', '')}".strip()]
    if structured.get("scrapelyConsoleUrl"):
        texts.append(
            f"Dashboard: {structured['scrapelyConsoleUrl']} (show this URL to the user exactly as written)"
        )
    return ok(structured, texts, meta)
