"""Waiting for runs and building run responses (SPEC 7.4.1 - 7.4.3, 10.9)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import time

import anyio
import httpx

from .api import path_ref, seg
from .core import CallContext
from .errors import ApiError, ToolError
from .log import log
from .util import drop_empty, normalize_fields, normalize_ts, parse_ts

TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
PROGRESS_POLL_SECS = 3.0
WAIT_CHUNK_SECS = 20
LAG_PROBE_DELAYS = (0, 1, 2, 2)
KVS_KEYS_LIMIT = 50


async def sleep(seconds: float) -> None:  # patched in tests
    if seconds > 0:
        await asyncio.sleep(seconds)


def is_terminal(status: Any) -> bool:
    return status in TERMINAL


def _alias_map(run: dict, kind: str, default_key: str) -> dict[str, str]:
    out: dict[str, str] = {}
    ids = run.get("storageIds")
    raw = ids.get(kind) if isinstance(ids, dict) else None
    if isinstance(raw, dict):
        for alias, sid in raw.items():
            if isinstance(sid, str) and sid:
                out[str(alias)] = sid
    if "default" not in out and isinstance(run.get(default_key), str) and run.get(default_key):
        out = {"default": run[default_key], **out}
    return out


async def actor_name_for(cc: CallContext, act_id: str | None) -> str | None:
    if not act_id:
        return None
    try:
        actor = await cc.api.get_actor(act_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not resolve Actor name for run", actor_id=act_id, reason=str(exc))
        return None
    if isinstance(actor, dict) and actor.get("username") and actor.get("name"):
        return f"{actor['username']}/{actor['name']}"
    return None


def _run_time(run: dict) -> int:
    stats = run.get("stats") if isinstance(run.get("stats"), dict) else {}
    rt = stats.get("runTimeSecs")
    if isinstance(rt, (int, float)) and not isinstance(rt, bool):
        return int(round(rt))
    started = parse_ts(run.get("startedAt"))
    if started is None:
        return 0
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def _status_line(run: dict) -> str:
    msg = run.get("statusMessage")
    if not isinstance(msg, str):
        return ""
    msg = msg.strip()
    if msg.endswith("."):
        msg = msg[:-1].rstrip()
    return f'"{msg}"' if msg else ""


def _size_hint(d: dict | None) -> str:
    if not d:
        return ""
    size = d.get("inflatedBytes")
    if not isinstance(size, (int, float)) or size <= 0:
        return ""
    hint = f" The full dataset is roughly {int(size)} bytes."
    if size > 50_000:
        hint += (
            " Fetching all of it at once may not fit in your context: select columns with `fields=` "
            "or page through with `offset`."
        )
    return hint


def _fields_hint(d: dict | None) -> str:
    fields = (d or {}).get("fields") or []
    if not fields:
        return ""
    return " Available fields (dot notation, usable in `fields`): " + ", ".join(fields) + "."


def summarize(run: dict, structured: dict, output_tool: bool = False) -> tuple[str, str]:
    status = run.get("status")
    run_id = run.get("id")
    storages = structured.get("storages") or {}
    d = (storages.get("datasets") or {}).get("default")
    k = (storages.get("keyValueStores") or {}).get("default")
    rt = _run_time(run)
    sline = _status_line(run)
    items = d.get("itemCount") if d else None
    so_far = f" {items} results so far." if isinstance(items, (int, float)) and items > 0 else ""

    output_keys: list[str] = []
    kvs_note = ""
    if k and k.get("keys"):
        output_keys = [key for key in k["keys"] if key != "INPUT"]
        if output_keys:
            if k.get("keyCount") is None and len(k["keys"]) >= KVS_KEYS_LIMIT:
                n = KVS_KEYS_LIMIT - (len(k["keys"]) - len(output_keys))
                kvs_note = f" The default key-value store holds at least {n} output keys."
            else:
                n = len(output_keys)
                kvs_note = f" The default key-value store holds {n} output key{'s' if n != 1 else ''}."

    def get_items_step(ds: dict, extra: str = "") -> str:
        return f"Call get-dataset-items with datasetId \"{ds['id']}\" and limit 20.{extra}"

    if status == "SUCCEEDED":
        if d and isinstance(items, (int, float)) and items > 0:
            nf = len(d.get("fields") or [])
            n = int(items)
            fields_part = f" with {nf} field{'s' if nf != 1 else ''}" if nf else ""
            summary = f"Run succeeded in {rt}s and produced {n} dataset item{'s' if n != 1 else ''}{fields_part}.{kvs_note}"
            if output_tool:
                step = (
                    f"Call get-actor-output with runId \"{run_id}\" to fetch the results "
                    f"({n} item{'s' if n != 1 else ''} in total), or get-dataset-items with datasetId "
                    f"\"{d['id']}\" for finer control.{_size_hint(d)}{_fields_hint(d)}"
                )
            else:
                step = (
                    f"Call get-dataset-items with datasetId \"{d['id']}\" and limit 20 "
                    f"({n} item{'s' if n != 1 else ''} in total).{_size_hint(d)}{_fields_hint(d)}"
                )
            return summary, step
        if d and items is None:
            summary = f"Run succeeded in {rt}s, but the dataset metadata could not be read.{(' Status: ' + sline + '.') if sline else ''}{kvs_note}"
            return summary, f"Inspect the output with get-dataset-items (datasetId \"{d['id']}\", limit 20)."
        if d and items == 0:
            summary = (
                f"Run succeeded in {rt}s. The dataset item count reads 0, but counts can lag for a few seconds "
                f"right after a run.{kvs_note}"
            )
            return summary, (
                f"Call get-dataset-items with datasetId \"{d['id']}\" and limit 20 before concluding there is "
                f"no output.{_fields_hint(d)}"
            )
        if output_keys and k:
            summary = f"Run succeeded in {rt}s.{kvs_note}"
            return summary, (
                f"Call get-key-value-store-record with keyValueStoreId \"{k['id']}\" and one of these keys: "
                + ", ".join(output_keys[:20])
                + "."
            )
        summary = f"Run succeeded in {rt}s. No output storages were found."
        return summary, (
            "The Actor may produce no stored output or may serve its output through its web server; "
            "check its README with fetch-actor-details."
        )
    if status == "FAILED":
        exit_code = run.get("exitCode")
        parts = [f"Run failed after {rt}s"]
        if exit_code is not None:
            parts.append(f"with exit code {exit_code}")
        summary = " ".join(parts) + "."
        if sline:
            summary += f" Status message: {sline}."
        summary += so_far
        return summary, f"Call get-actor-log with runId \"{run_id}\" to see what went wrong."
    if status == "ABORTED":
        summary = f"Run was aborted after {rt}s." + (f" Status message: {sline}." if sline else "") + so_far
        return summary, f"Call get-actor-run with runId \"{run_id}\" to inspect any partial output."
    if status == "TIMED-OUT":
        if d and isinstance(items, (int, float)) and items > 0:
            return (
                f"Run timed out after {rt}s with {int(items)} items of partial output.",
                get_items_step(d, " These are partial results."),
            )
        if d:
            return (
                f"Run timed out after {rt}s.",
                f"Inspect the dataset with get-dataset-items (datasetId \"{d['id']}\"); the run may be "
                "retryable with a longer timeout.",
            )
        return f"Run timed out after {rt}s. No output storages were found.", (
            "Consider running it again with a longer callOptions.timeout."
        )
    summary = f"Run {run_id} is {status}.{so_far}"
    return summary, f"Call get-actor-run with runId \"{run_id}\" and waitSecs=30 to check the current state."


async def _dataset_entry(cc: CallContext, ds_id: str, succeeded: bool) -> dict:
    entry: dict[str, Any] = {"id": ds_id}
    try:
        meta = await cc.api.get_or_none(f"/v2/datasets/{seg(ds_id)}")
    except Exception as exc:  # noqa: BLE001
        log.warning("dataset metadata unavailable", dataset_id=ds_id, reason=str(exc))
        return entry
    if not isinstance(meta, dict):
        return entry
    entry["name"] = meta.get("name")
    fields = normalize_fields(meta.get("fields"))
    if fields:
        entry["fields"] = fields
    stats = meta.get("stats") if isinstance(meta.get("stats"), dict) else {}
    inflated = meta.get("inflatedBytes", stats.get("inflatedBytes"))
    if isinstance(inflated, (int, float)) and not isinstance(inflated, bool):
        entry["inflatedBytes"] = inflated
    count = meta.get("itemCount")
    if isinstance(count, (int, float)) and not isinstance(count, bool):
        if succeeded and count == 0:
            count = await _probe_items(cc, ds_id)
        entry["itemCount"] = count
    console = await cc.console_link(f"/storage/datasets/{ds_id}")
    if console:
        entry["scrapelyConsoleUrl"] = console
    return drop_empty(entry)


async def _probe_items(cc: CallContext, ds_id: str) -> int:
    for delay in LAG_PROBE_DELAYS:
        await sleep(delay)
        try:
            items = await cc.api.get_json(f"/v2/datasets/{seg(ds_id)}/items", {"limit": 1, "format": "json"})
        except Exception:  # noqa: BLE001 - probe errors are ignored
            continue
        if isinstance(items, list) and items:
            return 1
    return 0


async def _kvs_entry(cc: CallContext, kvs_id: str, terminal: bool) -> dict:
    entry: dict[str, Any] = {"id": kvs_id}
    console = await cc.console_link(f"/storage/key-value-stores/{kvs_id}")
    if console:
        entry["scrapelyConsoleUrl"] = console
    if not terminal:
        return entry
    try:
        listing = await cc.api.get_or_none(f"/v2/key-value-stores/{seg(kvs_id)}/keys", {"limit": KVS_KEYS_LIMIT})
    except Exception as exc:  # noqa: BLE001
        log.warning("key-value store keys unavailable", store_id=kvs_id, reason=str(exc))
        return entry
    if not isinstance(listing, dict):
        return entry
    keys = [i.get("key") for i in listing.get("items") or [] if isinstance(i, dict) and isinstance(i.get("key"), str)]
    keys = keys[:KVS_KEYS_LIMIT]
    truncated = bool(listing.get("isTruncated"))
    if keys:
        entry["keys"] = keys
        if not truncated:
            entry["keyCount"] = len(keys)
    return drop_empty(entry)


async def build_storages(cc: CallContext, run: dict, enrich: bool) -> dict:
    datasets = _alias_map(run, "datasets", "defaultDatasetId")
    stores = _alias_map(run, "keyValueStores", "defaultKeyValueStoreId")
    out: dict[str, Any] = {}
    if not enrich:
        if datasets:
            out["datasets"] = {a: {"id": i} for a, i in datasets.items()}
        if stores:
            out["keyValueStores"] = {a: {"id": i} for a, i in stores.items()}
        return out
    succeeded = run.get("status") == "SUCCEEDED"
    terminal = is_terminal(run.get("status"))
    ds_results: dict[str, dict] = {}
    kv_results: dict[str, dict] = {}

    async def do_ds(alias: str, sid: str) -> None:
        ds_results[alias] = await _dataset_entry(cc, sid, succeeded)

    async def do_kv(alias: str, sid: str) -> None:
        kv_results[alias] = await _kvs_entry(cc, sid, terminal)

    async with anyio.create_task_group() as tg:
        for alias, sid in datasets.items():
            tg.start_soon(do_ds, alias, sid)
        for alias, sid in stores.items():
            tg.start_soon(do_kv, alias, sid)
    if datasets:
        out["datasets"] = {a: ds_results.get(a, {"id": i}) for a, i in datasets.items()}
    if stores:
        out["keyValueStores"] = {a: kv_results.get(a, {"id": i}) for a, i in stores.items()}
    return out


def usage_meta(run: dict) -> dict | None:
    total = run.get("usageTotalUsd")
    if total is None:
        return None
    inner: dict[str, Any] = {"usageTotalUsd": total}
    if isinstance(run.get("usageUsd"), dict):
        inner["usageUsd"] = run["usageUsd"]
    return {"com.scrapely/ActorRun": inner}


async def start_response(cc: CallContext, run: dict, actor_name: str | None) -> dict:
    structured: dict[str, Any] = {
        "runId": run.get("id"),
        "actorId": run.get("actId"),
        "actorName": actor_name,
        "status": run.get("status"),
        "startedAt": normalize_ts(run.get("startedAt")),
    }
    console = await cc.console_link(f"/actors/runs/{run.get('id')}")
    if console:
        structured["scrapelyConsoleUrl"] = console
    structured = {k: v for k, v in structured.items() if v is not None}
    structured["storages"] = await build_storages(cc, run, enrich=False)
    summary, step = summarize(run, structured, output_tool=cc.has("get-actor-output"))
    structured["summary"] = summary
    structured["nextStep"] = step
    return structured


def _progress_message(actor_label: str, run: dict) -> str:
    status = run.get("status") or "UNKNOWN"
    msg = f"{actor_label}: {status}"
    sm = run.get("statusMessage")
    show = isinstance(sm, str) and sm.strip() and (not is_terminal(status) or run.get("isStatusMessageTerminal"))
    if show:
        msg += f" — {sm.strip()}"
    return msg


class _Progress:
    def __init__(self, cc: CallContext, run_id: str, label: str) -> None:
        self.cc = cc
        self.run_id = run_id
        self.label = label
        self.counter = 0
        self.last: str | None = None

    async def emit(self, run: dict) -> None:
        if self.cc.notify_progress is None:
            return
        message = _progress_message(self.label, run)
        if message == self.last:
            return
        self.last = message
        self.counter += 1
        try:
            await self.cc.notify_progress(self.counter, message, {"com.scrapely/ActorRun": {"runId": self.run_id}})
        except Exception:  # noqa: BLE001 - progress errors are silent
            pass

    async def poll(self) -> None:
        while True:
            await anyio.sleep(PROGRESS_POLL_SECS)
            try:
                run = await self.cc.api.get_run(self.run_id)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(run, dict):
                await self.emit(run)
                if is_terminal(run.get("status")):
                    return


async def wait_and_respond(
    cc: CallContext,
    run_id: str,
    *,
    wait_secs: int,
    actor_name: str | None = None,
    unlimited: bool = False,
) -> dict:
    run = await cc.api.get_run(run_id)
    if not isinstance(run, dict):
        raise ToolError(f"Run '{run_id}' was not found. Check the run ID.", http_status=404)
    if actor_name is None:
        actor_name = await actor_name_for(cc, run.get("actId"))
    label = actor_name or run_id
    progress = _Progress(cc, run_id, label) if (cc.notify_progress and not cc.task_mode) else None

    if (wait_secs > 0 or unlimited) and not is_terminal(run.get("status")):
        if progress:
            await progress.emit(run)

        async def waiter() -> dict:
            # Wait in short server-side chunks: long-held requests can be cut by proxies in front of the API.
            current = run
            deadline = None if unlimited else time.monotonic() + wait_secs
            while True:
                remaining = WAIT_CHUNK_SECS if deadline is None else int(round(deadline - time.monotonic()))
                if remaining <= 0:
                    return current
                try:
                    fresh = await cc.api.get_run(run_id, wait_secs=min(WAIT_CHUNK_SECS, remaining))
                except httpx.TransportError as exc:
                    log.soft_fail("waiting for run interrupted, polling instead", run_id=run_id, reason=str(exc))
                    await anyio.sleep(1)
                    fresh = await cc.api.get_run(run_id)
                if isinstance(fresh, dict):
                    current = fresh
                if is_terminal(current.get("status")):
                    return current

        if progress:
            result: dict = {}
            async with anyio.create_task_group() as tg:
                tg.start_soon(progress.poll)
                result = await waiter()
                tg.cancel_scope.cancel()
            run = result
        else:
            run = await waiter()
        if is_terminal(run.get("status")):
            again = await cc.api.get_run(run_id)
            if isinstance(again, dict):
                run = again
        if progress:
            await progress.emit(run)

    structured: dict[str, Any] = {
        "runId": run.get("id") or run_id,
        "actorId": run.get("actId"),
        "actorName": actor_name,
        "status": run.get("status"),
    }
    console = await cc.console_link(f"/actors/runs/{structured['runId']}")
    if console:
        structured["scrapelyConsoleUrl"] = console
    sm = run.get("statusMessage")
    if isinstance(sm, str) and sm.strip():
        structured["statusMessage"] = sm
    if isinstance(run.get("exitCode"), (int, float)) and not isinstance(run.get("exitCode"), bool):
        structured["exitCode"] = run["exitCode"]
    if run.get("startedAt"):
        structured["startedAt"] = normalize_ts(run.get("startedAt"))
    if run.get("finishedAt"):
        structured["finishedAt"] = normalize_ts(run.get("finishedAt"))
    stats = run.get("stats") if isinstance(run.get("stats"), dict) else {}
    st = {
        k: stats[k]
        for k in ("runTimeSecs", "computeUnits", "memMaxBytes")
        if isinstance(stats.get(k), (int, float)) and not isinstance(stats.get(k), bool)
    }
    if st:
        structured["stats"] = st
    structured = {k: v for k, v in structured.items() if v is not None}
    structured["storages"] = await build_storages(cc, run, enrich=True)
    summary, step = summarize(run, structured, output_tool=cc.has("get-actor-output"))
    structured["summary"] = summary
    structured["nextStep"] = step
    from .core import run_result

    return run_result(structured, usage_meta(run))


async def abort_quietly(cc: CallContext, run_id: str) -> None:
    with anyio.CancelScope(shield=True):
        try:
            with anyio.fail_after(10):
                await cc.api.abort_run(run_id)
            log.info("aborted run after client cancellation", run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not abort run after cancellation", run_id=run_id, reason=str(exc))


def run_params(call_options: dict | None) -> dict[str, Any]:
    opts = call_options or {}
    params: dict[str, Any] = {}
    if opts.get("build"):
        params["build"] = opts["build"]
    if opts.get("memory") is not None:
        params["memory"] = int(opts["memory"])
    if opts.get("timeout") is not None:
        params["timeout"] = int(opts["timeout"])
    if opts.get("maxItems") is not None:
        params["maxItems"] = int(opts["maxItems"])
    if opts.get("maxTotalChargeUsd") is not None:
        params["maxTotalChargeUsd"] = opts["maxTotalChargeUsd"]
    return params


__all__ = [
    "ApiError",
    "abort_quietly",
    "actor_name_for",
    "is_terminal",
    "path_ref",
    "run_params",
    "start_response",
    "summarize",
    "usage_meta",
    "wait_and_respond",
]
