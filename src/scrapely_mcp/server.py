"""Builds one MCP server instance per session (or per stateless request)."""

from __future__ import annotations

import asyncio
import copy
from collections import OrderedDict
from typing import Any

import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.shared.exceptions import MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from . import SERVER_NAME, SERVER_TITLE, __version__, pipeline, resources
from .config import Settings
from .core import ClientContext, SessionState, ToolDef
from .log import log, token_fingerprint
from .payments import PaymentProvider
from .selection import Selection, compose, load_tools
from .task_store import TASK_METHODS, TaskError, store as task_store
from .texts import instructions as build_instructions
from .widgets import widget_meta

UI_EXTENSION = "io.modelcontextprotocol/ui"
UI_MIME = "text/html;profile=mcp-app"


def client_supports_ui(capabilities: dict | None) -> bool:
    if not isinstance(capabilities, dict):
        return False
    ext = capabilities.get("extensions")
    if not isinstance(ext, dict):
        return False
    ui = ext.get(UI_EXTENSION)
    if not isinstance(ui, dict):
        return False
    mimes = ui.get("mimeTypes")
    return isinstance(mimes, list) and UI_MIME in mimes


def resolve_mode(option: str, capabilities: dict | None, widgets_enabled: bool) -> str:
    if not widgets_enabled:
        return "default"
    if option == "apps":
        return "apps"
    if option == "default":
        return "default"
    return "apps" if client_supports_ui(capabilities) else "default"


def recompose(state: SessionState) -> None:
    tools = compose(state.base_tools, state.mode)
    if state.payment is not None:
        tools = OrderedDict((n, state.payment.decorate(t)) for n, t in tools.items())
    state.tools = tools


def instructions_for(state: SessionState) -> str:
    return build_instructions(
        state.tool_names(), state.mode, state.settings.public_api_base_url, state.report_problem_available()
    )


def tool_to_wire(tool: ToolDef, state: SessionState, names: set[str]) -> dict:
    out: dict[str, Any] = {
        "name": tool.name,
        "title": tool.title,
        "description": tool.describe(names, state.mode),
        "inputSchema": tool.input_schema,
        "annotations": tool.annotations,
    }
    if tool.output_schema is not None:
        out["outputSchema"] = tool.output_schema
    if tool.task_support:
        out["execution"] = {"taskSupport": "optional"}
    meta = dict(tool.meta or {})
    if state.mode == "apps" and tool.extra.get("widget"):
        meta.update(widget_meta(state.settings, tool.extra["widget"]))
    if meta:
        out["_meta"] = meta
    return out


def apply_client(state: SessionState, protocol_version: str | None, client_info: Any, capabilities: Any) -> None:
    state.client = ClientContext(
        protocol_version=protocol_version,
        client_info=copy.deepcopy(client_info) if isinstance(client_info, dict) else {},
        capabilities=copy.deepcopy(capabilities) if isinstance(capabilities, dict) else {},
    )
    new_mode = resolve_mode(state.ui_option, state.client.capabilities, state.settings.widgets_enabled)
    log.info(
        "resolved server mode",
        mode=new_mode,
        option=state.ui_option,
        client_supports_ui=client_supports_ui(state.client.capabilities),
    )
    state.mode = new_mode
    recompose(state)


def _modern_client(state: SessionState, ctx: Any) -> None:
    """2026-07-28 requests carry client info per request; refresh mode from it."""
    params = ctx.session.client_params
    caps = ctx.session.client_capabilities
    info = params.client_info.model_dump(by_alias=True, mode="json", exclude_none=True) if params else {}
    caps_d = caps.model_dump(by_alias=True, mode="json", exclude_none=True) if caps is not None else {}
    meta = ctx.params.get("_meta") if isinstance(ctx.params, dict) else None
    if isinstance(meta, dict):
        if not info and isinstance(meta.get("io.modelcontextprotocol/clientInfo"), dict):
            info = meta["io.modelcontextprotocol/clientInfo"]
        if not caps_d and isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict):
            caps_d = meta["io.modelcontextprotocol/clientCapabilities"]
    state.client = ClientContext(protocol_version=ctx.protocol_version, client_info=info, capabilities=caps_d)
    mode = resolve_mode(state.ui_option, caps_d, state.settings.widgets_enabled)
    if mode != state.mode or not state.tools:
        state.mode = mode
        recompose(state)


def _headers_of(ctx: Any) -> dict[str, str]:
    req = getattr(ctx, "request", None)
    headers = getattr(req, "headers", None)
    if headers is None:
        return {}
    try:
        return {k.lower(): v for k, v in headers.items()}
    except Exception:  # noqa: BLE001
        return {}


def task_scope_for(state: SessionState, ctx: Any) -> str:
    sid = _headers_of(ctx).get("mcp-session-id")
    if sid:
        return sid
    if state.task_scope:
        return state.task_scope
    return f"token:{token_fingerprint(state.token)}" if state.token else "anonymous"


class ScrapelyServer(Server):
    def __init__(self, state: SessionState) -> None:
        self.state = state
        super().__init__(
            SERVER_NAME,
            version=__version__,
            title=SERVER_TITLE,
            instructions=instructions_for(state),
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            on_list_resources=self._list_resources,
            on_list_resource_templates=self._list_templates,
            on_read_resource=self._read_resource,
            on_list_prompts=self._list_prompts,
        )
        for method in TASK_METHODS:
            self.add_request_handler(method, types.RequestParams, self._task_method)
        # `logging` is advertised for handshake clients; accept setLevel as a no-op.
        self.add_request_handler("logging/setLevel", types.SetLevelRequestParams, self._set_level)
        self.middleware.append(self._middleware)

    def get_capabilities(self, notification_options=None, experimental_capabilities=None, extensions=None, *, protocol_version=None):  # noqa: D102
        caps = super().get_capabilities(
            notification_options, experimental_capabilities, extensions, protocol_version=protocol_version
        )
        caps.logging = types.LoggingCapability()
        if protocol_version not in MODERN_PROTOCOL_VERSIONS:
            caps.tasks = types.ServerTasksCapability(
                list=types.TasksListCapability(),
                cancel=types.TasksCancelCapability(),
                requests=types.ServerTasksRequestsCapability(
                    tools=types.TasksToolsCapability(call=types.TasksCallCapability())
                ),
            )
        return caps

    # -- middleware ------------------------------------------------------------

    async def _middleware(self, ctx: Any, call_next):
        state = self.state
        if ctx.method == "initialize" and isinstance(ctx.params, dict):
            p = ctx.params
            apply_client(state, p.get("protocolVersion"), p.get("clientInfo"), p.get("capabilities"))
            self.instructions = instructions_for(state)
            return await call_next(ctx)
        if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS and ctx.request_id is not None:
            try:
                _modern_client(state, ctx)
                self.instructions = instructions_for(state)
            except Exception:  # noqa: BLE001
                pass
        if (
            ctx.method == "tools/call"
            and isinstance(ctx.params, dict)
            and ctx.params.get("task") is not None
            and ctx.protocol_version not in MODERN_PROTOCOL_VERSIONS
        ):
            return await self._start_task(ctx)
        return await call_next(ctx)

    async def _start_task(self, ctx: Any) -> dict:
        state = self.state
        params = ctx.params
        name = str(params.get("name") or "")
        tool = pipeline.find_tool(state, name)
        if tool is None:
            return pipeline.unknown_tool_result(name)
        if not tool.task_support:
            raise MCPError(
                code=-32602,
                message=f"Tool {name} does not support long-running tasks. Remove the `task` field and call it again.",
            )
        task = params.get("task") if isinstance(params.get("task"), dict) else {}
        ttl = task.get("ttl") if isinstance(task.get("ttl"), int) else None
        scope = task_scope_for(state, ctx)
        record = task_store.create(scope, ttl)
        headers = _headers_of(ctx)
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
        arguments = params.get("arguments")

        async def run() -> None:
            if record.expired():
                log.soft_fail("task expired before it started", task_id=record.task_id)
                return
            try:
                result = await pipeline.call_tool(
                    state, name, arguments, meta=meta, headers=headers, task_mode=True
                )
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                result = {"content": [{"type": "text", "text": f"Task failed: {exc}"}], "isError": True}
            task_store.complete(scope, record.task_id, result)

        record.runner = asyncio.get_running_loop().create_task(run())
        return {"task": record.public()}

    # -- handlers --------------------------------------------------------------

    async def _list_tools(self, ctx: Any, params: Any) -> types.ListToolsResult:
        state = self.state
        names = {t.name for t in state.visible_tools()}
        wire = [types.Tool.model_validate(tool_to_wire(t, state, names)) for t in state.visible_tools()]
        return types.ListToolsResult(tools=wire)

    async def _call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> Any:
        meta = dict(params.meta) if params.meta else None
        result = await pipeline.call_tool(
            self.state,
            params.name,
            params.arguments,
            meta=meta,
            request_ctx=ctx,
            headers=_headers_of(ctx),
        )
        return types.CallToolResult.model_validate(result)

    async def _list_resources(self, ctx: Any, params: Any) -> types.ListResourcesResult:
        return types.ListResourcesResult.model_validate({"resources": resources.list_resources(self.state)})

    async def _list_templates(self, ctx: Any, params: Any) -> types.ListResourceTemplatesResult:
        return types.ListResourceTemplatesResult.model_validate(
            {"resourceTemplates": resources.list_templates(self.state)}
        )

    async def _read_resource(self, ctx: Any, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        meta = dict(params.meta) if params.meta else None
        token = pipeline.request_token(self.state, meta)
        if token and token.strip().lower() == "your-scrapely-token":
            token = None
        modern = ctx.protocol_version in MODERN_PROTOCOL_VERSIONS
        try:
            contents = await resources.read_resource(self.state, str(params.uri), token, modern=modern)
        except resources.ResourceError as exc:
            raise MCPError(code=exc.code, message=str(exc)) from exc
        return types.ReadResourceResult.model_validate({"contents": contents})

    async def _set_level(self, ctx: Any, params: Any) -> types.EmptyResult:
        return types.EmptyResult()

    async def _list_prompts(self, ctx: Any, params: Any) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=[])

    async def _task_method(self, ctx: Any, params: Any) -> dict:
        raw = ctx.params if isinstance(ctx.params, dict) else {}
        try:
            return task_store.handle(task_scope_for(self.state, ctx), ctx.method, raw)
        except TaskError as exc:
            raise MCPError(code=exc.code, message=exc.message) from exc


async def build_state(
    settings: Settings,
    *,
    token: str | None,
    payment: PaymentProvider | None,
    ui_option: str,
    telemetry_enabled: bool,
    transport: str,
    selection: Selection | None,
    preloaded: list[ToolDef] | None = None,
    session_id: str | None = None,
    task_scope: str | None = None,
    stateless: bool = False,
) -> SessionState:
    from .core import make_api

    state = SessionState(
        settings=settings,
        token=token,
        payment=payment,
        ui_option=ui_option,
        telemetry_enabled=telemetry_enabled,
        transport=transport,
        task_scope=task_scope,
        stateless=stateless,
    )
    if session_id:
        state.session_id = session_id
    if preloaded is not None and selection is None:
        state.base_tools = [t for t in preloaded if not (payment is not None and t.kind == "proxied")]
    else:
        api = make_api(settings, token, "MCP")
        try:
            state.base_tools = await load_tools(
                settings, selection or Selection(None, None), api, payment_active=payment is not None
            )
        finally:
            await api.aclose()
    state.mode = resolve_mode(ui_option, None, settings.widgets_enabled) if ui_option != "auto" else "default"
    recompose(state)
    return state
