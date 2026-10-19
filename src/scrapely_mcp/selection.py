"""Tool selection, Actor tool loading and final tool list composition (SPEC 5.1 - 5.3, 8)."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from . import remote
from .actors import (
    MODE_MCP,
    MODE_STANDBY_WITHOUT_MCP,
    UnsafeMcpUrl,
    actor_memory_cap,
    actor_tool_name,
    derive_tool_schema,
    get_definition,
    mcp_server_url,
    proxied_tool_names,
)
from .api import ApiClient
from .config import Settings
from .core import CallContext, ToolDef
from .log import log
from .schemas import run_response_schema
from .tools import (
    AUTO_INJECT,
    BUILTIN_NAMES,
    CATEGORIES,
    DEFAULT_CATEGORIES,
    RETIRED_SELECTORS,
    TOKEN_FREE_CATEGORIES,
    TOKEN_FREE_TOOLS,
    WIDGET_SIBLINGS,
    WIDGET_TOOLS,
    builtin_tool,
)
from .util import normalize_actor_name, split_list
from .validation import MAX_UNTRUSTED_SCHEMA_BYTES, ArgValidator, advertised_schema, schema_size


@dataclass
class Selection:
    actors: list[str] | None
    tools: list[str] | None
    max_actor_memory_bytes: int | None = None


def normalize_selection(actors, tools, max_actor_memory_bytes: int | None = None) -> Selection:
    a = split_list(actors)
    t = split_list(tools)
    if a:
        t = list(a) if t is None else t + list(a)
    return Selection(a, t, max_actor_memory_bytes)


def selectors(sel: Selection) -> list[str]:
    if sel.tools is None:
        if sel.actors is not None and len(sel.actors) == 0:
            return []
        return list(DEFAULT_CATEGORIES)
    return list(sel.tools)


def token_required(sel: Selection) -> bool:
    active = [s for s in selectors(sel) if s not in RETIRED_SELECTORS]
    if not active:
        return True
    for s in active:
        if s in TOKEN_FREE_TOOLS or s in TOKEN_FREE_CATEGORIES:
            continue
        return True
    return False


async def load_tools(
    settings: Settings,
    sel: Selection,
    api: ApiClient,
    *,
    payment_active: bool = False,
) -> list[ToolDef]:
    out: list[ToolDef] = []
    for s in selectors(sel):
        if s in RETIRED_SELECTORS:
            continue
        if s in CATEGORIES:
            out.extend(builtin_tool(n) for n in CATEGORIES[s])
            continue
        if s in BUILTIN_NAMES:
            if s not in WIDGET_TOOLS:
                out.append(builtin_tool(s))
            continue
        name = normalize_actor_name(s)
        if name != s:
            log.info("normalised Actor name", original=s, fixed=name, route="tool-selection")
        try:
            out.extend(await load_actor_tools(settings, name, api, sel.max_actor_memory_bytes, payment_active))
        except Exception as exc:  # noqa: BLE001 - load failures never fail the session
            log.soft_fail("could not load Actor tool, skipping it", actor=name, reason=str(exc))
    return out


async def load_actor_tools(
    settings: Settings, name: str, api: ApiClient, max_mem: int | None, payment_active: bool
) -> list[ToolDef]:
    definition = await get_definition(api, name)
    if definition is None:
        log.soft_fail("Actor not found, skipping its tool", actor=name)
        return []
    mode = definition.mode
    if mode == MODE_STANDBY_WITHOUT_MCP:
        log.soft_fail("standby Actor without MCP server and input schema is not supported", actor=name)
        return []
    if mode == MODE_MCP:
        if payment_active:
            log.info("MCP-server Actor tools are not offered with agentic payment", actor=name)
            return []
        return await _load_proxied(settings, definition, api)
    schema = derive_tool_schema(definition.input_schema)
    if schema_size(schema) > MAX_UNTRUSTED_SCHEMA_BYTES:
        log.soft_fail("Actor input schema is oversized, skipping its tool", actor=name)
        return []
    validator = ArgValidator(schema)
    full = definition.full_name
    title = definition.title or full.rsplit("/", 1)[-1]
    desc = definition.description or "No description provided."
    if definition.readme_summary:
        desc += "\n\n" + definition.readme_summary

    async def handler(cc: CallContext, args: dict) -> dict:
        from .tools.actor_tools import start_run_checked
        from . import runs
        import anyio
        from .actors import decode_dot_keys
        from .core import run_result

        args = dict(args)
        wait = args.pop("waitSecs", None)
        wait = 30 if wait is None else int(wait)
        run_input = decode_dot_keys(args)
        cc.telemetry.update({"actor_name": full, "actor_id": definition.id})
        run = await start_run_checked(cc, full, run_input, {}, definition)
        run_id = run["id"]
        cc.telemetry["run_id"] = run_id
        try:
            if wait == 0 and not cc.task_mode:
                return run_result(await runs.start_response(cc, run, full))
            return await runs.wait_and_respond(cc, run_id, wait_secs=wait, actor_name=full, unlimited=cc.task_mode)
        except anyio.get_cancelled_exc_class():
            await runs.abort_quietly(cc, run_id)
            raise

    annotations = {
        "title": title,
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    tool = ToolDef(
        name=actor_tool_name(full),
        title=title,
        description=desc,
        input_schema=advertised_schema(schema),
        validation_schema=schema,
        output_schema=run_response_schema(),
        annotations=annotations,
        handler=handler,
        kind="actor",
        payment_required=True,
        task_support=False,
        actor_full_name=full,
        actor_id=definition.id,
    )
    tool._validator = validator
    tool.extra["memoryMbytes"] = actor_memory_cap(definition, max_mem)
    return [tool]


async def _load_proxied(settings: Settings, definition, api: ApiClient) -> list[ToolDef]:
    try:
        url = mcp_server_url(settings.standby_base_url, definition.username, definition.name, definition.mcp_path)
    except UnsafeMcpUrl as exc:
        log.soft_fail("MCP-server Actor has an unusable MCP path", actor=definition.full_name, reason=str(exc))
        return []
    try:
        remote_tools = await remote.list_remote_tools(url, api.token)
    except Exception as exc:  # noqa: BLE001
        log.soft_fail("MCP-server Actor unreachable", actor=definition.full_name, reason=str(exc))
        return []
    names = proxied_tool_names(definition.full_name, [str(t.get("name")) for t in remote_tools])
    out: list[ToolDef] = []
    for t in remote_tools:
        remote_name = str(t.get("name"))
        schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {"type": "object", "properties": {}}
        if schema_size(schema) > MAX_UNTRUSTED_SCHEMA_BYTES:
            log.soft_fail("remote tool schema is oversized, skipping it", tool=remote_name)
            continue

        def make_handler(rname: str):
            async def handler(cc: CallContext, args: dict) -> dict:
                try:
                    result = await remote.call_remote_tool(url, cc.api.token, rname, args)
                except Exception as exc:  # noqa: BLE001
                    from .errors import ToolError

                    raise ToolError(
                        f"Calling tool '{rname}' of Actor {definition.full_name} failed: {exc}. "
                        "The Actor's MCP server may be unavailable right now.",
                        user=False,
                    ) from exc
                out_: dict = {"content": result.get("content") or [], "isError": bool(result.get("isError"))}
                if result.get("structuredContent") is not None:
                    out_["structuredContent"] = result["structuredContent"]
                return out_

            return handler

        annotations = dict(t.get("annotations") or {})
        out.append(
            ToolDef(
                name=names[remote_name],
                title=t.get("title") or remote_name,
                description=t.get("description") or "",
                input_schema=advertised_schema(schema),
                validation_schema=schema,
                output_schema=t.get("outputSchema") if isinstance(t.get("outputSchema"), dict) else None,
                annotations=annotations,
                handler=make_handler(remote_name),
                kind="proxied",
                actor_full_name=definition.full_name,
                actor_id=definition.id,
                extra={"remote_name": remote_name},
            )
        )
    return out


def compose(base: list[ToolDef], mode: str) -> OrderedDict[str, ToolDef]:
    tools = list(base)
    names = [t.name for t in tools]
    if "call-actor" in names or "get-actor-run" in names or any(t.kind == "actor" for t in tools):
        inject = [builtin_tool(n) for n in AUTO_INJECT]
        if "call-actor" in names:
            idx = names.index("call-actor") + 1
            tools = tools[:idx] + inject + tools[idx:]
        else:
            tools = tools + inject
    if mode == "apps":
        present = {t.name for t in tools}
        for base_name, widget in WIDGET_SIBLINGS.items():
            if base_name in present:
                tools.append(builtin_tool(widget))
    else:
        tools = [t for t in tools if t.name not in WIDGET_TOOLS]
    out: OrderedDict[str, ToolDef] = OrderedDict()
    for t in tools:
        if t.name not in out:
            out[t.name] = t
    return out
