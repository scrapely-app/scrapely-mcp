"""search-actors, fetch-actor-details, call-actor and their widget variants."""

from __future__ import annotations

from typing import Any

import anyio

from .. import remote, runs
from ..actors import (
    MODE_MCP,
    MODE_STANDBY_WITHOUT_MCP,
    UnsafeMcpUrl,
    details_input_schema,
    get_definition,
    mcp_server_url,
    output_schema_of,
    output_schema_text,
)
from ..cards import ALL_SECTIONS, build_card, success_rate
from ..core import CallContext, ok, run_result, text
from ..errors import ApiError, RemoteUnreachable, ToolError
from ..log import log
from ..util import compact_json, normalize_actor_name

DEFAULT_SECTIONS = {"description", "stats", "pricing", "rating", "metadata", "inputSchema", "readme"}
ALL_OUTPUT_FLAGS = DEFAULT_SECTIONS | {"outputSchema", "mcpTools"}


def _log_name_change(original: str, fixed: str, route: str) -> None:
    if original != fixed:
        log.info("normalised Actor name", original=original, fixed=fixed, route=route)


def not_found_error(cc: CallContext, actor: str) -> ToolError:
    msg = (
        f"Actor '{actor}' was not found. Check that the ID or the full name in the `username/actor-name` "
        "format is correct."
    )
    if cc.has("search-actors"):
        msg += " Search for it by keyword with search-actors instead of guessing the spelling."
    return ToolError(msg, http_status=404, detail="actor-not-found")


# -- search ------------------------------------------------------------------


async def _search(cc: CallContext, args: dict) -> tuple[list[dict], str, str]:
    keywords = args.get("keywords") or ""
    if not isinstance(keywords, str):
        keywords = str(keywords)
    limit = int(args.get("limit") or 5)
    offset = int(args.get("offset") or 0)
    params: dict[str, Any] = {
        "search": keywords,
        "limit": limit,
        "offset": offset,
        "includeInputSchema": True,
        # Agents only see Actors whose owners allow agentic use (server default: allowed).
        "allowsAgenticUsers": "true",
        # The Store's shared ranking (the same as the dashboard chat): words in titles, descriptions,
        # categories, output types and target sites, partial matches, site names and synonyms, with
        # Scrape.ly-maintained and tested Actors first. The default order is plain popularity.
        "sortBy": "recommended",
    }
    items = await cc.api.store_search(params)
    caller = await cc.get_caller()
    return items, keywords, caller.user_plan_tier


async def search_actors(cc: CallContext, args: dict) -> dict:
    items, query, tier = await _search(cc, args)
    if not items:
        instructions = (
            "No Actors matched. Search once more with broader keywords (only the platform name, or only the kind "
            "of data). If that finds nothing suitable either, tell the user that no Scrape.ly Actor does this yet. "
            "Do not guess an Actor name or offer an unrelated Actor instead."
        )
        return ok(
            {"actors": [], "query": query, "count": 0, "instructions": instructions},
            [f"No Actors found for '{query}'. {instructions}"],
        )
    console = cc.console_on()
    caller = await cc.get_caller()
    cards: list[dict] = []
    texts: list[str] = []
    for item in items:
        card, card_text = build_card(
            item,
            cc.settings,
            sections=ALL_SECTIONS,
            pricing_mode="simplified",
            user_tier=tier,
            caller=caller,
            console=console,
            input_schema=item.get("inputSchema"),
        )
        cards.append(card)
        texts.append(card_text)
    footer_parts = []
    if cc.has("fetch-actor-details"):
        footer_parts.append("Use fetch-actor-details to get the README and the full input schema of an Actor.")
    footer_parts.append("Always run a second, broader search before choosing; a better match may exist.")
    footer = " ".join(footer_parts)
    header = f"Found {len(cards)} Actors for '{query}'."
    body = header + "\n\n" + "\n\n".join(texts) + "\n\n" + footer
    if console:
        body += "\nShow the URLs to the user exactly as written."
    structured = {"actors": cards, "query": query, "count": len(cards), "instructions": footer}
    return ok(structured, [body])


def _number(value: Any) -> float | int:
    """A number from the API, which may send DECIMAL columns as strings ('0.0')."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def _widget_stats(stats: dict, item: dict) -> dict:
    out: dict[str, Any] = {
        "actorReviewRating": _number(stats.get("actorReviewRating", item.get("avgRating"))),
        "actorReviewCount": int(_number(stats.get("actorReviewCount", item.get("totalReviews")))),
        "monthlyUsers": stats.get("totalUsers30Days") or 0,
    }
    sr = success_rate(stats)
    if sr is not None:
        out["successRate"] = sr["rate"]
        if sr["window"]:
            out["successRateWindow"] = sr["window"]
    return out


async def search_actors_widget(cc: CallContext, args: dict) -> dict:
    from ..pricing import simplified_pricing

    items, query, tier = await _search(cc, args)
    actors = []
    for item in items:
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        username = item.get("username") or ""
        name = item.get("name") or ""
        actors.append(
            {
                "id": item.get("id") or "",
                "name": name,
                "username": username,
                "fullName": f"{username}/{name}",
                "title": item.get("title") or name,
                "description": item.get("description") or "No description provided.",
                "pictureUrl": item.get("pictureUrl") or "",
                "stats": _widget_stats(stats, item),
                "url": f"{cc.settings.store_url}/{username}/{name}",
                "currentPricingInfo": simplified_pricing(item, tier),
            }
        )
    structured = {"actors": actors, "query": query, "count": len(actors)}
    from ..widgets import widget_meta

    result = ok(structured, [f"Rendered a widget with {len(actors)} Store results for '{query}'."])
    result["_meta"] = widget_meta(cc.settings, "search")
    return result


# -- details -----------------------------------------------------------------


def _flags(output: Any) -> set[str]:
    if not isinstance(output, dict) or not output or all(v is None for v in output.values()):
        return set(DEFAULT_SECTIONS)
    return {k for k, v in output.items() if v is True and k in ALL_OUTPUT_FLAGS}


async def fetch_actor_details(cc: CallContext, args: dict) -> dict:
    original = str(args.get("actor") or "")
    ref = normalize_actor_name(original)
    _log_name_change(original, ref, "fetch-actor-details")
    flags = _flags(args.get("output"))
    store_items: list[dict] = []
    definition = None

    async def load_def() -> None:
        nonlocal definition
        definition = await get_definition(cc.api, ref)

    async def load_store() -> None:
        nonlocal store_items
        try:
            store_items = await cc.api.store_search({"search": ref.rsplit("/", 1)[-1], "limit": 5})
        except Exception as exc:  # noqa: BLE001 - only used for a picture
            log.debug("store lookup for picture failed", reason=str(exc))

    async with anyio.create_task_group() as tg:
        tg.start_soon(load_def)
        tg.start_soon(load_store)
    if definition is None:
        raise not_found_error(cc, ref)
    actor = definition.raw
    raw_def = actor.get("defaultBuildActorDefinition") or {}
    structured: dict[str, Any] = {}
    texts: list[str] = []
    sections = flags & ALL_SECTIONS
    if sections:
        tier = (await cc.get_caller()).user_plan_tier if "pricing" in sections else "FREE"
        picture = next((i.get("pictureUrl") for i in store_items if i.get("id") == actor.get("id")), None)
        card, card_text = build_card(
            actor,
            cc.settings,
            sections=sections,
            pricing_mode="complete",
            user_tier=tier,
            caller=cc.caller,
            console=cc.console_on(),
            picture_url=picture or None,
        )
        structured["actorInfo"] = card
        texts.append(card_text)
    if "readme" in flags:
        summary = actor.get("readmeSummary")
        readme = summary if isinstance(summary, str) and summary.strip() else definition.readme
        readme = readme or "This Actor has no README."
        structured["readme"] = readme
        texts.append("# README\n" + readme)
    if "inputSchema" in flags:
        schema = details_input_schema(raw_def.get("input") if isinstance(raw_def.get("input"), dict) else None)
        structured["inputSchema"] = schema
        texts.append("# Input schema\n" + compact_json(schema))
    if "outputSchema" in flags:
        schema = output_schema_of(raw_def)
        structured["outputSchema"] = schema
        if schema:
            texts.append("# Output schema\n" + output_schema_text(schema))
        else:
            texts.append("# Output schema\nThis Actor publishes no output schema. Run it with a small input to see its output.")
    if "mcpTools" in flags:
        mcp_text = await _mcp_tools_text(cc, definition)
        structured["mcpTools"] = mcp_text
        texts.append("# MCP tools\n" + mcp_text)
    if not texts:
        texts.append(f"Actor {definition.full_name} exists; no sections were requested.")
    return ok(structured, texts)


async def _mcp_tools_text(cc: CallContext, definition) -> str:
    mode = definition.mode
    if mode == MODE_STANDBY_WITHOUT_MCP:
        return (
            "This Actor runs in standby mode without an MCP server and has an empty input schema. "
            "Actors of this kind are not supported yet."
        )
    if mode != MODE_MCP:
        return "This Actor is not an MCP server and exposes no MCP tools."
    if cc.state.payment is not None:
        return "MCP-server Actors cannot be used with third-party agentic payment. Connect with OAuth or a token."
    try:
        url = mcp_server_url(cc.settings.standby_base_url, definition.username, definition.name, definition.mcp_path)
    except UnsafeMcpUrl as exc:
        return f"The Actor's MCP endpoint is not usable: {exc}."
    try:
        tools = await remote.list_remote_tools(url, cc.api.token, cc.settings.max_remote_tool_list_bytes)
    except RemoteUnreachable as exc:
        log.soft_fail("MCP-server Actor unreachable", actor=definition.full_name, reason=str(exc))
        return f"Could not connect to the Actor's MCP server: {exc}"
    except Exception as exc:  # noqa: BLE001
        log.soft_fail("listing remote MCP tools failed", actor=definition.full_name, reason=str(exc))
        return f"Connected to the Actor's MCP server, but listing its tools failed: {exc}"
    if not tools:
        return "The Actor's MCP server lists no tools."
    lines = [f"Call these tools with call-actor using `actor: \"{definition.full_name}:<toolName>\"`."]
    for t in tools:
        lines.append(
            f"\n## {t.get('name')}\n{t.get('description') or ''}\nInput schema: {compact_json(t.get('inputSchema') or {})}"
            f"\nCall as: {definition.full_name}:{t.get('name')}"
        )
    return "\n".join(lines)


async def fetch_actor_details_widget(cc: CallContext, args: dict) -> dict:
    from ..widgets import widget_meta

    result = await fetch_actor_details(cc, args)
    result["_meta"] = widget_meta(cc.settings, "search")
    return result


# -- call-actor --------------------------------------------------------------


def _schema_text(definition) -> str:
    raw_def = definition.raw.get("defaultBuildActorDefinition") or {}
    return compact_json(details_input_schema(raw_def.get("input") if isinstance(raw_def.get("input"), dict) else None))


async def start_run_checked(cc: CallContext, actor_ref: str, run_input: Any, params: dict, definition=None) -> dict:
    try:
        run = await cc.api.start_run(actor_ref, run_input, params)
    except ApiError as exc:
        if exc.type == "invalid-input":
            msg = (
                f"Actor {actor_ref} rejected the input: {exc.message}. Make sure the input matches the "
                "Actor's input schema."
            )
            if definition is not None:
                msg += f"\nInput schema: {_schema_text(definition)}"
            log.soft_fail("Actor rejected input", actor=actor_ref, reason=exc.message)
            raise ToolError(msg, http_status=exc.status, detail="invalid-input") from exc
        raise
    if not isinstance(run, dict) or not run.get("id"):
        raise ToolError(f"Starting Actor {actor_ref} returned no run.", user=False)
    cc.telemetry.setdefault("started_run_ids", []).append(str(run["id"]))
    return run


async def call_actor(cc: CallContext, args: dict, *, force_no_wait: bool = False) -> dict:
    original = str(args.get("actor") or "")
    name = normalize_actor_name(original)
    _log_name_change(original, name, "call-actor")
    tool_name = None
    if ":" in name:
        name, tool_name = name.split(":", 1)
    run_input = args.get("input") if isinstance(args.get("input"), dict) else {}
    definition = await get_definition(cc.api, name)
    if definition is None:
        raise not_found_error(cc, name)
    from ..session_log import secret_fields

    # Input fields the Actor marks secret are masked in the session log.
    cc.telemetry["secret_fields"] = sorted(secret_fields(definition.input_schema))
    mode = definition.mode
    if mode == MODE_MCP:
        if cc.state.payment is not None:
            raise ToolError(
                "Standby Actors (MCP servers) are not supported in agentic payment mode. Connect with OAuth or "
                "an API token to use them.",
                http_status=409,
                detail="standby-payment-not-supported",
            )
        if not tool_name:
            raise ToolError(
                f"{definition.full_name} is an MCP-server Actor. Call one of its tools with "
                f"`actor: \"{definition.full_name}:<toolName>\"`"
                + (" (list them with fetch-actor-details and output.mcpTools)." if cc.has("fetch-actor-details") else "."),
                http_status=400,
            )
        return await _call_remote(cc, definition, tool_name, run_input)
    if mode == MODE_STANDBY_WITHOUT_MCP:
        raise ToolError(
            "This Actor runs in standby mode without an MCP server and has an empty input schema. Actors of this "
            "kind are not supported yet.",
            http_status=409,
            detail="standby-without-mcp-not-supported",
        )
    wait = 0 if force_no_wait else int(args.get("waitSecs", 30) if args.get("waitSecs") is not None else 30)
    params = runs.run_params(args.get("callOptions"))
    cc.telemetry.update({"actor_name": definition.full_name, "actor_id": definition.id})
    run = await start_run_checked(cc, definition.full_name, run_input, params, definition)
    run_id = run["id"]
    cc.telemetry["run_id"] = run_id
    try:
        if wait == 0 and not cc.task_mode:
            structured = await runs.start_response(cc, run, definition.full_name)
            return run_result(structured)
        return await runs.wait_and_respond(
            cc, run_id, wait_secs=wait, actor_name=definition.full_name, unlimited=cc.task_mode
        )
    except anyio.get_cancelled_exc_class():
        await runs.abort_quietly(cc, run_id)
        raise


async def _call_remote(cc: CallContext, definition, tool_name: str, arguments: dict) -> dict:
    try:
        url = mcp_server_url(cc.settings.standby_base_url, definition.username, definition.name, definition.mcp_path)
    except UnsafeMcpUrl as exc:
        raise ToolError(f"The MCP endpoint of {definition.full_name} is not usable: {exc}.", user=False) from exc
    try:
        result = await remote.call_remote_tool(url, cc.api.token, tool_name, arguments)
    except Exception as exc:  # noqa: BLE001
        text_ = str(exc)
        if "cannot-start-actor-runs" in text_:
            raise ToolError(
                "You reached the limit of concurrent Actor runs on your plan. Wait for running Actors to finish "
                f"or upgrade your plan at {cc.settings.console_base_url}/billing/subscription.",
                category="INVALID_INPUT",
            ) from exc
        log.soft_fail("remote MCP tool call failed", actor=definition.full_name, tool=tool_name, reason=text_)
        raise ToolError(
            f"Calling tool '{tool_name}' of Actor {definition.full_name} failed: {text_}. The Actor's MCP server "
            "may be unavailable right now.",
            user=False,
        ) from exc
    out: dict[str, Any] = {"content": result.get("content") or [], "isError": bool(result.get("isError"))}
    if result.get("structuredContent") is not None:
        out["structuredContent"] = result["structuredContent"]
    return out


async def call_actor_widget(cc: CallContext, args: dict) -> dict:
    from ..texts import WIDGET_NO_POLL
    from ..widgets import widget_meta

    if ":" in str(args.get("actor") or ""):
        raise ToolError(
            "Tool calls of MCP-server Actors cannot be shown as widgets. Use call-actor instead.", user=False
        )
    result = await call_actor(cc, args, force_no_wait=True)
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and not result.get("isError"):
        structured["nextStep"] = WIDGET_NO_POLL
        result = run_result(structured, result.get("_meta"))
        meta = dict(result.get("_meta") or {})
        meta.update(widget_meta(cc.settings, "run", description=f"Live progress of {structured.get('actorName')}"))
        result["_meta"] = meta
    return result


__all__ = ["call_actor", "fetch_actor_details", "search_actors", "text"]
