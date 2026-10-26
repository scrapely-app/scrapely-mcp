"""Saved inputs and automations: list-saved-inputs, run-saved-input, list-automations, run-automation.

Thin wrappers. A saved input is an Actor task (`/v2/actor-tasks`); automations live under `/v2/automations`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .. import runs
from ..core import CallContext, ok
from ..errors import ApiError, ToolError
from ..util import compact_json, strip_quotes
from .task_tools import read_task, resolve_ref


def _seg(ref: str) -> str:
    return quote(ref, safe="~")


def _page(args: dict, default_limit: int = 20) -> tuple[int, int]:
    limit = int(args.get("limit") if args.get("limit") is not None else default_limit)
    offset = int(args.get("offset") or 0)
    return max(1, min(limit, 100)), max(0, offset)


def _saved_input_item(t: dict, actor_id: str | None = None) -> dict:
    item = {
        "savedInputId": t.get("id"),
        "name": t.get("name"),
        "title": t.get("title"),
        "actorId": t.get("actId") or actor_id,
        "isDefault": t.get("isDefault"),
        "lastUsedAt": t.get("lastUsedAt"),
        "totalRuns": t.get("totalRuns"),
    }
    return {k: v for k, v in item.items() if v is not None}


async def list_saved_inputs(cc: CallContext, args: dict) -> dict:
    limit, offset = _page(args)
    actor = strip_quotes(str(args.get("actorId") or ""))
    if actor:
        ref, _ = resolve_ref(actor, is_task=False)
        try:
            data = await cc.api.get_json(f"/v2/actors/{_seg(ref)}/saved-inputs")
        except ApiError as exc:
            if exc.status == 404:
                raise ToolError(f"Actor '{actor}' was not found. Check the Actor ID or name.", http_status=404) from exc
            raise
        data = data if isinstance(data, dict) else {}
        all_items = [_saved_input_item(t, actor) for t in data.get("savedInputs") or [] if isinstance(t, dict)]
        items = all_items[offset : offset + limit]
        examples = [
            {k: e.get(k) for k in ("id", "title", "description") if e.get(k) is not None}
            for e in data.get("examples") or []
            if isinstance(e, dict)
        ]
        result: dict[str, Any] = {
            "total": len(all_items),
            "count": len(items),
            "offset": offset,
            "limit": limit,
            "items": items,
            "examples": examples,
        }
        summary = f"{len(all_items)} saved input(s) for Actor {actor}"
        if examples:
            summary += f", plus {len(examples)} public example input(s) (run them with call-actor and their input)"
    else:
        data = await cc.api.get_json("/v2/actor-tasks", {"limit": limit, "offset": offset, "desc": True})
        data = data if isinstance(data, dict) else {}
        items = [_saved_input_item(t) for t in data.get("items") or [] if isinstance(t, dict)]
        result = {
            "total": data.get("total", len(items)),
            "count": len(items),
            "offset": offset,
            "limit": limit,
            "items": items,
        }
        summary = f"{result['total']} saved input(s) in the account"
    return ok(result, [compact_json(result), summary + ". Run one with run-saved-input."])


async def run_saved_input(cc: CallContext, args: dict) -> dict:
    saved_id = str(args.get("savedInputId") or "")
    ref, ambiguous = resolve_ref(saved_id, is_task=True)
    target = ref
    if ambiguous:
        task = await read_task(cc, saved_id)
        if task is None:
            raise ToolError(f"Saved input '{saved_id}' was not found. Check the ID or name.", http_status=404)
        target = str(task.get("id") or ref)
    override = args.get("input") if isinstance(args.get("input"), dict) else None
    try:
        run = await cc.api.post_json(f"/v2/actor-tasks/{_seg(target)}/runs", override or {})
    except ApiError as exc:
        if exc.status == 404:
            raise ToolError(f"Saved input '{saved_id}' was not found. Check the ID or name.", http_status=404) from exc
        raise
    if not isinstance(run, dict) or not run.get("id"):
        raise ToolError("The API did not return the started run.")
    cc.telemetry["run_id"] = run["id"]
    cc.telemetry.setdefault("started_run_ids", []).append(str(run["id"]))
    wait = int(args.get("waitSecs") or 0)
    return await runs.wait_and_respond(cc, str(run["id"]), wait_secs=max(0, min(wait, 45)), unlimited=False)


def _automation_item(a: dict) -> dict:
    opts = a.get("options") if isinstance(a.get("options"), dict) else {}
    steps = a.get("steps") if isinstance(a.get("steps"), list) else []
    last = a.get("lastRun") if isinstance(a.get("lastRun"), dict) else None
    item = {
        "automationId": a.get("id"),
        "name": a.get("name"),
        "title": a.get("title"),
        "trigger": a.get("trigger"),
        "steps": [
            {
                k: s.get(k)
                for k in ("kind", "actorId", "actorName", "savedInputId", "savedInputTitle", "savedInputName")
                if s.get(k) is not None
            }
            for s in steps
            if isinstance(s, dict)
        ],
        "hasAiStep": bool(a.get("ai")),
        "deliveries": [d.get("channel") for d in a.get("deliveries") or [] if isinstance(d, dict)],
        "isEnabled": opts.get("isEnabled"),
        "nextRunAt": a.get("nextRunAt"),
        "lastRun": {k: last.get(k) for k in ("id", "status", "startedAt", "finishedAt")} if last else None,
    }
    return {k: v for k, v in item.items() if v is not None}


def _automations_error(exc: ApiError) -> ToolError | None:
    if exc.type == "feature-not-enabled":
        return ToolError(f"{exc.message} Automations cannot be used with this account yet.", http_status=exc.status)
    return None


async def list_automations(cc: CallContext, args: dict) -> dict:
    limit, offset = _page(args)
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    for key in ("actorId", "savedInputId", "search"):
        value = strip_quotes(str(args.get(key) or ""))
        if value:
            params[key] = value.replace("/", "~") if key == "actorId" else value
    try:
        data = await cc.api.get_json("/v2/automations", params)
    except ApiError as exc:
        mapped = _automations_error(exc)
        if mapped:
            raise mapped from exc
        raise
    data = data if isinstance(data, dict) else {}
    items = [_automation_item(a) for a in data.get("items") or [] if isinstance(a, dict)]
    result = {
        "total": data.get("total", len(items)),
        "count": len(items),
        "offset": data.get("offset", offset),
        "limit": data.get("limit", limit),
        "items": items,
    }
    return ok(result, [compact_json(result), f"{result['total']} automation(s). Start one now with run-automation."])


async def run_automation(cc: CallContext, args: dict) -> dict:
    automation_id = strip_quotes(str(args.get("automationId") or ""))
    try:
        run = await cc.api.post_json(f"/v2/automations/{_seg(automation_id)}/runs")
    except ApiError as exc:
        if exc.status == 404:
            raise ToolError(
                f"Automation '{automation_id}' was not found. Use list-automations to find its ID.", http_status=404
            ) from exc
        mapped = _automations_error(exc)
        if mapped:
            raise mapped from exc
        if exc.type == "automation-busy":
            raise ToolError(f"The automation did not start: {exc.message}", http_status=exc.status) from exc
        raise
    run = run if isinstance(run, dict) else {}
    steps = [
        {k: s.get(k) for k in ("index", "actorId", "actorName", "runId", "status") if s.get(k) is not None}
        for s in run.get("steps") or []
        if isinstance(s, dict)
    ]
    result = {
        "automationRunId": run.get("id"),
        "automationId": run.get("automationId") or automation_id,
        "status": run.get("status"),
        "startedAt": run.get("startedAt"),
        "steps": steps,
    }
    result = {k: v for k, v in result.items() if v is not None}
    run_ids = [s["runId"] for s in steps if s.get("runId")]
    if run_ids:
        cc.telemetry.setdefault("started_run_ids", []).extend(str(r) for r in run_ids)
    hint = (
        f" Follow the Actor runs with get-actor-run ({', '.join(run_ids)})."
        if run_ids and cc.has("get-actor-run")
        else ""
    )
    return ok(result, [compact_json(result), f"Automation run {result.get('automationRunId')} started.{hint}"])
