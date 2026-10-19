"""Built-in tool registry (SPEC 7.0)."""

from __future__ import annotations

from ..core import CallContext, ToolDef, ok
from ..log import log
from ..schemas import input_schema, output_schema
from ..texts import DESCRIPTIONS, TITLES
from ..validation import advertised_schema
from . import actor_tools, docs_tools, run_tools, storage_tools, task_tools

CATEGORIES: dict[str, list[str]] = {
    "actors": ["search-actors", "fetch-actor-details", "call-actor"],
    "docs": ["search-scrapely-docs", "fetch-scrapely-docs"],
    "runs": ["get-actor-run", "get-actor-output", "get-actor-run-list", "get-actor-log", "abort-actor-run"],
    "storage": [
        "get-actor-output",
        "get-dataset",
        "get-dataset-items",
        "get-dataset-schema",
        "get-key-value-store",
        "get-key-value-store-keys",
        "get-key-value-store-record",
        "get-dataset-list",
        "get-key-value-store-list",
    ],
    "tasks": ["create-actor-task", "get-actor-task", "update-actor-task", "publish-actor-task", "unpublish-actor-task"],
    "dev": ["report-problem"],
}
DEFAULT_CATEGORIES = ["actors", "docs", "runs", "storage", "tasks"]
WIDGET_TOOLS = ["search-actors-widget", "fetch-actor-details-widget", "call-actor-widget", "get-actor-run-widget"]
WIDGET_SIBLINGS = {
    "search-actors": "search-actors-widget",
    "fetch-actor-details": "fetch-actor-details-widget",
    "call-actor": "call-actor-widget",
    "get-actor-run": "get-actor-run-widget",
}
RETIRED_SELECTORS = {"add-actor", "experimental", "preview"}
TOKEN_FREE_TOOLS = {"search-actors", "fetch-actor-details", "search-scrapely-docs", "fetch-scrapely-docs"}
TOKEN_FREE_CATEGORIES = {c for c, tools in CATEGORIES.items() if all(t in TOKEN_FREE_TOOLS for t in tools)}
PAYMENT_REQUIRED = {
    "call-actor",
    "call-actor-widget",
    "get-actor-run",
    "get-actor-run-widget",
    "get-actor-run-list",
    "get-actor-log",
    "abort-actor-run",
    "get-actor-output",
    "get-dataset",
    "get-dataset-items",
    "get-dataset-schema",
    "get-key-value-store",
    "get-key-value-store-keys",
    "get-key-value-store-record",
}
AUTO_INJECT = ["get-actor-run", "get-actor-output", "get-dataset-items", "get-key-value-store-record", "abort-actor-run"]

_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_ANNOTATIONS = {
    "call-actor": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    "call-actor-widget": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    "abort-actor-run": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    "create-actor-task": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "update-actor-task": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "publish-actor-task": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "unpublish-actor-task": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "report-problem": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
}


async def report_problem(cc: CallContext, args: dict) -> dict:
    log.info(
        "problem reported",
        message=str(args.get("message") or "")[:2000],
        actorId=args.get("actorId"),
        actorRunId=args.get("actorRunId"),
        relatedTools=args.get("relatedTools"),
        session_id=cc.state.session_id,
        client=cc.state.client.name,
    )
    return ok({"reported": True}, ["Thank you, the problem report was sent to the Scrape.ly team."])


_HANDLERS = {
    "search-actors": actor_tools.search_actors,
    "fetch-actor-details": actor_tools.fetch_actor_details,
    "call-actor": actor_tools.call_actor,
    "get-actor-run": run_tools.get_actor_run,
    "get-actor-run-list": run_tools.get_actor_run_list,
    "get-actor-log": run_tools.get_actor_log,
    "abort-actor-run": run_tools.abort_actor_run,
    "get-dataset": storage_tools.get_dataset,
    "get-actor-output": storage_tools.get_actor_output,
    "get-dataset-items": storage_tools.get_dataset_items,
    "get-dataset-schema": storage_tools.get_dataset_schema,
    "get-dataset-list": storage_tools.get_dataset_list,
    "get-key-value-store": storage_tools.get_key_value_store,
    "get-key-value-store-keys": storage_tools.get_key_value_store_keys,
    "get-key-value-store-record": storage_tools.get_key_value_store_record,
    "get-key-value-store-list": storage_tools.get_key_value_store_list,
    "create-actor-task": task_tools.create_actor_task,
    "get-actor-task": task_tools.get_actor_task,
    "update-actor-task": task_tools.update_actor_task,
    "publish-actor-task": task_tools.publish_actor_task,
    "unpublish-actor-task": task_tools.unpublish_actor_task,
    "search-scrapely-docs": docs_tools.search_docs,
    "fetch-scrapely-docs": docs_tools.fetch_docs,
    "report-problem": report_problem,
    "search-actors-widget": actor_tools.search_actors_widget,
    "fetch-actor-details-widget": actor_tools.fetch_actor_details_widget,
    "call-actor-widget": actor_tools.call_actor_widget,
    "get-actor-run-widget": run_tools.get_actor_run_widget,
}

BUILTIN_NAMES = set(_HANDLERS)


def builtin_tool(name: str) -> ToolDef:
    raw_input = input_schema(name)
    title = TITLES[name]
    annotations = dict(_ANNOTATIONS.get(name, _RO))
    annotations["title"] = title
    tool = ToolDef(
        name=name,
        title=title,
        description=DESCRIPTIONS[name],
        input_schema=advertised_schema(raw_input),
        validation_schema=raw_input,
        output_schema=output_schema(name),
        annotations=annotations,
        handler=_HANDLERS[name],
        payment_required=name in PAYMENT_REQUIRED,
        task_support=name == "call-actor",
        lenient=name == "call-actor",
        token_free=name in TOKEN_FREE_TOOLS,
    )
    if name in WIDGET_TOOLS:
        kind = "run" if name in ("call-actor-widget", "get-actor-run-widget") else "search"
        tool.extra["widget"] = kind
    return tool
