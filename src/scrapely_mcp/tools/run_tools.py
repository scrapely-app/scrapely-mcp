"""get-actor-run, get-actor-run-list, get-actor-log, abort-actor-run and the run widget."""

from __future__ import annotations

from typing import Any

from .. import runs
from ..api import seg
from ..core import CallContext, ok, run_result
from ..errors import ApiError, ToolError
from ..util import compact_json, normalize_ts, strip_quotes


async def get_actor_run(cc: CallContext, args: dict, *, force_no_wait: bool = False) -> dict:
    run_id = strip_quotes(str(args.get("runId") or ""))
    cc.telemetry["run_id"] = run_id
    wait = 0 if force_no_wait else int(args.get("waitSecs") if args.get("waitSecs") is not None else 30)
    try:
        return await runs.wait_and_respond(cc, run_id, wait_secs=wait, unlimited=False)
    except ToolError:
        raise
    except ApiError as exc:
        if exc.status == 402:
            raise
        raise ToolError(
            f"Could not read run '{run_id}': {exc.message}. Check that the run ID is correct.",
            http_status=exc.status,
            category="AUTH" if exc.status in (401, 403) else "INVALID_INPUT",
        ) from exc


async def get_actor_run_widget(cc: CallContext, args: dict) -> dict:
    from ..texts import WIDGET_NO_POLL
    from ..widgets import widget_meta

    result = await get_actor_run(cc, args, force_no_wait=True)
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        return result
    structured["nextStep"] = WIDGET_NO_POLL
    meta = dict(result.get("_meta") or {})
    label = structured.get("actorName") or structured.get("runId")
    meta.update(widget_meta(cc.settings, "run", description=f"Live status of {label}"))
    return ok(
        structured,
        [compact_json(structured), f"Run {structured.get('runId')} status {structured.get('status')}, a widget was rendered."],
        meta,
    )


async def get_actor_run_list(cc: CallContext, args: dict) -> dict:
    params: dict[str, Any] = {
        "limit": args.get("limit", 10),
        "offset": args.get("offset", 0),
        "desc": bool(args.get("desc", False)),
    }
    if args.get("status"):
        params["status"] = args["status"]
    data = await cc.api.get_json("/v2/actor-runs", params)
    if not isinstance(data, dict):
        data = {"items": data if isinstance(data, list) else []}
    items = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        for key in ("buildId", "buildNumber"):
            if key in item and item[key] is None:
                del item[key]
        items.append(item)
    data = dict(data)
    data["items"] = items
    data.setdefault("total", len(items))
    data.setdefault("count", len(items))
    data.setdefault("offset", params["offset"])
    data.setdefault("limit", params["limit"])
    return ok(data, [compact_json(data)])


async def get_actor_log(cc: CallContext, args: dict) -> dict:
    run_id = strip_quotes(str(args.get("runId") or ""))
    cc.telemetry["run_id"] = run_id
    lines = args.get("lines", 10)
    resp = await cc.api.request("GET", f"/v2/logs/{seg(run_id)}", headers={"Accept": "text/plain"})
    if resp.status_code == 404:
        raise ToolError(f"Run '{run_id}' was not found, so there is no log. Check the run ID.", http_status=404)
    cc.api.raise_for(resp)
    log_text = resp.text
    if log_text.endswith("\n"):
        log_text = log_text[:-1]
    if log_text:
        all_lines = log_text.split("\n")
        n = int(lines or 0)
        if n > 0:
            all_lines = all_lines[-n:]
        log_text = "\n".join(all_lines)
    return ok({"log": log_text}, [log_text])


async def abort_actor_run(cc: CallContext, args: dict) -> dict:
    run_id = strip_quotes(str(args.get("runId") or ""))
    cc.telemetry["run_id"] = run_id
    try:
        run = await cc.api.abort_run(run_id, args.get("gracefully"))
    except ApiError as exc:
        if exc.status == 404:
            raise ToolError(f"Run '{run_id}' was not found. Check the run ID.", http_status=404) from exc
        raise
    if not isinstance(run, dict):
        raise ToolError(f"Run '{run_id}' was not found. Check the run ID.", http_status=404)
    structured: dict[str, Any] = {
        "runId": run.get("id") or run_id,
        "actorId": run.get("actId"),
        "status": run.get("status"),
    }
    if run.get("statusMessage"):
        structured["statusMessage"] = run["statusMessage"]
    if run.get("startedAt"):
        structured["startedAt"] = normalize_ts(run["startedAt"])
    if run.get("finishedAt"):
        structured["finishedAt"] = normalize_ts(run["finishedAt"])
    structured = {k: v for k, v in structured.items() if v is not None}
    structured["storages"] = await runs.build_storages(cc, run, enrich=False)
    summary, step = runs.summarize(run, structured, output_tool=cc.has("get-actor-output"))
    if run.get("status") in ("ABORTING", "ABORTED"):
        summary = f"Abort requested for run {structured['runId']}; its status is now {run.get('status')}."
        step = f"Call get-actor-run with runId \"{structured['runId']}\" to inspect any partial output."
    structured["summary"] = summary
    structured["nextStep"] = step
    return run_result(structured, runs.usage_meta(run))
