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
from .util import json_size, normalize_actor_name, split_list, truncate_text, utf8_len
from .validation import MAX_UNTRUSTED_SCHEMA_BYTES, ArgValidator, advertised_schema, schema_size


@dataclass
class Selection:
    actors: list[str] | None
    tools: list[str] | None
    max_actor_memory_bytes: int | None = None


def _dedupe(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    return list(OrderedDict.fromkeys(values))


def normalize_selection(actors, tools, max_actor_memory_bytes: int | None = None) -> Selection:
    a = _dedupe(split_list(actors))
    t = _dedupe(split_list(tools))
    if a:
        t = list(a) if t is None else _dedupe(t + list(a))
    return Selection(a, t, max_actor_memory_bytes)


def actor_selectors(sel: Selection) -> list[str]:
    """Distinct selectors that load an Actor (one API lookup each)."""
    out: OrderedDict[str, None] = OrderedDict()
    for s in selectors(sel):
        if s in RETIRED_SELECTORS or s in CATEGORIES or s in BUILTIN_NAMES:
            continue
        out[normalize_actor_name(s)] = None
    return list(out)


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


# Limits for tool definition fields an Actor or its MCP server provides.
MAX_TOOL_TITLE_CHARS = 200
MAX_TOOL_DESCRIPTION_CHARS = 8_000
MAX_REMOTE_TOOL_NAME_CHARS = 128
# Rough fixed cost of one kept tool (ToolDef, handler closure, name map entry).
TOOL_OVERHEAD_BYTES = 512
_ANNOTATION_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


class ToolLedger:
    """Bytes of Actor tool definitions kept by all sessions of this process."""

    def __init__(self) -> None:
        self.used = 0


LEDGER = ToolLedger()


class ToolReservation:
    """One session's share of the process-wide ledger. release() is idempotent."""

    def __init__(self, ledger: ToolLedger | None = None) -> None:
        self.ledger = ledger or LEDGER
        self.bytes = 0

    def add(self, n: int) -> None:
        self.bytes += n
        self.ledger.used += n

    def release(self) -> None:
        if self.bytes:
            self.ledger.used -= self.bytes
            self.bytes = 0


class ToolCapacityError(Exception):
    """The process-wide budget for Actor tool definitions is full."""

    def __init__(self) -> None:
        super().__init__(
            "The server is at capacity for Actor tools right now. Retry in a minute, or select fewer Actors."
        )


class ToolBudget:
    """Bytes of Actor tool definitions one session keeps (SCRAPELY_MCP_MAX_SESSION_TOOL_BYTES),
    within the process-wide limit (SCRAPELY_MCP_MAX_TOTAL_TOOL_BYTES)."""

    def __init__(self, limit: int, total_limit: int | None = None, reservation: ToolReservation | None = None) -> None:
        self.limit = limit
        self.used = 0
        self.total_limit = total_limit
        self.reservation = reservation or ToolReservation()

    @property
    def left(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def total_left(self) -> int | None:
        if self.total_limit is None:
            return None
        return max(0, self.total_limit - self.reservation.ledger.used)


class _OverBudget(Exception):
    pass


def tool_def_bytes(tool: ToolDef) -> int:
    """Approximate bytes a kept Actor or proxied tool definition holds."""
    size = TOOL_OVERHEAD_BYTES + utf8_len(tool.name) + utf8_len(tool.title or "")
    if isinstance(tool.description, str):
        size += utf8_len(tool.description)
    size += schema_size(tool.input_schema)
    if tool.validation_schema is not None and tool.validation_schema is not tool.input_schema:
        size += schema_size(tool.validation_schema)
    if tool.output_schema is not None:
        size += schema_size(tool.output_schema)
    size += json_size(tool.annotations) + json_size(tool.extra)
    return size


class _ActorBytes:
    """Counts one Actor's tool bytes as they are built; raises once over the limit."""

    def __init__(self, actor: str, per_actor: int, session: ToolBudget | None) -> None:
        self.actor = actor
        self.per_actor = per_actor
        self.session = session
        self.used = 0

    def add(self, tool: ToolDef) -> None:
        self.used += tool_def_bytes(tool)
        if self.used > self.per_actor:
            log.soft_fail(
                "Actor tool definitions are over the per-Actor byte limit, skipping the Actor",
                actor=self.actor,
                limit=self.per_actor,
            )
            raise _OverBudget
        if self.session is not None and self.used > self.session.left:
            log.soft_fail(
                "session tool definitions are over the per-session byte limit, skipping the Actor",
                actor=self.actor,
                limit=self.session.limit,
            )
            raise _OverBudget
        total_left = self.session.total_left if self.session is not None else None
        if total_left is not None and self.used > total_left:
            log.soft_fail(
                "process-wide Actor tool budget is full, refusing the session",
                actor=self.actor,
                limit=self.session.total_limit,
            )
            raise ToolCapacityError

    def commit(self) -> None:
        if self.session is not None:
            self.session.used += self.used
            self.session.reservation.add(self.used)


def _clean_title(value, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return truncate_text(value.strip(), MAX_TOOL_TITLE_CHARS)
    return fallback


def _clean_description(value) -> str:
    if not isinstance(value, str):
        return ""
    return truncate_text(value, MAX_TOOL_DESCRIPTION_CHARS, "\n\n[Description truncated.]")


def _clean_annotations(value) -> dict:
    """Only the standard tool annotations, with plain values."""
    out: dict = {}
    if not isinstance(value, dict):
        return out
    if isinstance(value.get("title"), str):
        out["title"] = truncate_text(value["title"], MAX_TOOL_TITLE_CHARS)
    for key in _ANNOTATION_HINTS:
        if isinstance(value.get(key), bool):
            out[key] = value[key]
    return out


async def load_tools(
    settings: Settings,
    sel: Selection,
    api: ApiClient,
    *,
    payment_active: bool = False,
    reservation: ToolReservation | None = None,
) -> list[ToolDef]:
    """Tools for a selection.

    Actors are loaded once each by their normalised name (the set actor_selectors
    counts against max_selected_actors) and once each by Actor id, whatever the
    spelling, and their tool definitions share one per-session byte budget and
    the process-wide one. Their bytes are added to `reservation`, which the
    owner releases when the session ends. When the process-wide budget is
    full, ToolCapacityError is raised and nothing stays reserved.
    """
    out: list[ToolDef] = []
    seen: set[str] = set()
    n_actors = 0
    loaded_ids: set[str] = set()
    reservation = reservation or ToolReservation()
    budget = ToolBudget(settings.max_session_tool_bytes, settings.max_total_tool_bytes, reservation)
    limit = settings.max_selected_actors
    for s in selectors(sel):
        if s in RETIRED_SELECTORS:
            continue
        if s in CATEGORIES:
            if s not in seen:
                seen.add(s)
                out.extend(builtin_tool(n) for n in CATEGORIES[s])
            continue
        if s in BUILTIN_NAMES:
            if s not in WIDGET_TOOLS and s not in seen:
                seen.add(s)
                out.append(builtin_tool(s))
            continue
        name = normalize_actor_name(s)
        key = "actor:" + name
        if key in seen:
            continue
        if n_actors >= limit:
            if n_actors == limit:
                log.soft_fail("too many Actors selected, skipping the rest", limit=limit, actor=name)
                n_actors += 1
            continue
        seen.add(key)
        n_actors += 1
        if name != s:
            log.info("normalised Actor name", original=s, fixed=name, route="tool-selection")
        try:
            out.extend(
                await load_actor_tools(
                    settings,
                    name,
                    api,
                    sel.max_actor_memory_bytes,
                    payment_active,
                    loaded_ids=loaded_ids,
                    budget=budget,
                )
            )
        except ToolCapacityError:
            reservation.release()
            raise
        except Exception as exc:  # noqa: BLE001 - load failures never fail the session
            log.soft_fail("could not load Actor tool, skipping it", actor=name, reason=str(exc))
    return out


async def load_actor_tools(
    settings: Settings,
    name: str,
    api: ApiClient,
    max_mem: int | None,
    payment_active: bool,
    *,
    loaded_ids: set[str] | None = None,
    budget: ToolBudget | None = None,
) -> list[ToolDef]:
    definition = await get_definition(api, name)
    if definition is None:
        log.soft_fail("Actor not found, skipping its tool", actor=name)
        return []
    if loaded_ids is not None and definition.id:
        if definition.id in loaded_ids:
            log.info("Actor already selected under another name, loading it once", actor=name)
            return []
        loaded_ids.add(definition.id)
    mode = definition.mode
    if mode == MODE_STANDBY_WITHOUT_MCP:
        log.soft_fail("standby Actor without MCP server and input schema is not supported", actor=name)
        return []
    if mode == MODE_MCP:
        if payment_active:
            log.info("MCP-server Actor tools are not offered with agentic payment", actor=name)
            return []
        return await _load_proxied(settings, definition, api, budget)
    schema = derive_tool_schema(definition.input_schema)
    if schema_size(schema) > MAX_UNTRUSTED_SCHEMA_BYTES:
        log.soft_fail("Actor input schema is oversized, skipping its tool", actor=name)
        return []
    validator = ArgValidator(schema)
    full = definition.full_name
    title = _clean_title(definition.title, full.rsplit("/", 1)[-1])
    desc = definition.description if isinstance(definition.description, str) and definition.description else ""
    desc = desc or "No description provided."
    if isinstance(definition.readme_summary, str) and definition.readme_summary:
        desc += "\n\n" + definition.readme_summary
    desc = _clean_description(desc)

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

    counter = _ActorBytes(full, settings.max_actor_tool_bytes, budget)
    try:
        counter.add(tool)
    except _OverBudget:
        return []
    counter.commit()
    return [tool]


async def _load_proxied(settings: Settings, definition, api: ApiClient, budget: ToolBudget | None = None) -> list[ToolDef]:
    try:
        url = mcp_server_url(settings.standby_base_url, definition.username, definition.name, definition.mcp_path)
    except UnsafeMcpUrl as exc:
        log.soft_fail("MCP-server Actor has an unusable MCP path", actor=definition.full_name, reason=str(exc))
        return []
    if budget is not None and budget.total_left is not None and budget.total_left <= 0:
        raise ToolCapacityError
    if budget is not None and budget.left <= 0:
        log.soft_fail(
            "session tool definitions are over the per-session byte limit, skipping the Actor",
            actor=definition.full_name,
            limit=budget.limit,
        )
        return []
    try:
        remote_tools = await remote.list_remote_tools(url, api.token, settings.max_remote_tool_list_bytes)
    except Exception as exc:  # noqa: BLE001
        log.soft_fail("MCP-server Actor unreachable", actor=definition.full_name, reason=str(exc))
        return []
    if not isinstance(remote_tools, list):
        return []
    # One entry per valid, distinct remote tool name.
    usable: OrderedDict[str, dict] = OrderedDict()
    for t in remote_tools:
        if not isinstance(t, dict):
            continue
        remote_name = t.get("name")
        if not isinstance(remote_name, str) or not remote_name or len(remote_name) > MAX_REMOTE_TOOL_NAME_CHARS:
            log.soft_fail("remote tool has an invalid name, skipping it", actor=definition.full_name)
            continue
        if remote_name in usable:
            continue
        usable[remote_name] = t
    del remote_tools
    names = proxied_tool_names(definition.full_name, list(usable))
    counter = _ActorBytes(definition.full_name, settings.max_actor_tool_bytes, budget)
    out: list[ToolDef] = []
    for remote_name, t in usable.items():
        schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {"type": "object", "properties": {}}
        if schema_size(schema) > MAX_UNTRUSTED_SCHEMA_BYTES:
            log.soft_fail("remote tool schema is oversized, skipping it", tool=remote_name)
            continue
        output_schema = t.get("outputSchema") if isinstance(t.get("outputSchema"), dict) else None
        if output_schema is not None and schema_size(output_schema) > MAX_UNTRUSTED_SCHEMA_BYTES:
            log.soft_fail("remote tool output schema is oversized, dropping it", tool=remote_name)
            output_schema = None

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

        tool = ToolDef(
            name=names[remote_name],
            title=_clean_title(t.get("title"), remote_name),
            description=_clean_description(t.get("description")),
            input_schema=advertised_schema(schema),
            validation_schema=schema,
            output_schema=output_schema,
            annotations=_clean_annotations(t.get("annotations")),
            handler=make_handler(remote_name),
            kind="proxied",
            actor_full_name=definition.full_name,
            actor_id=definition.id,
            extra={"remote_name": remote_name},
        )
        try:
            counter.add(tool)
        except _OverBudget:
            return []
        out.append(tool)
    counter.commit()
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
