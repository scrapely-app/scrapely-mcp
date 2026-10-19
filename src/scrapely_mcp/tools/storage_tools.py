"""Dataset and key-value store tools (SPEC 7.8 - 7.16)."""

from __future__ import annotations

import base64
import re
from typing import Any

from ..api import path_ref, seg
from ..core import CallContext, ok, storage_result, text
from ..errors import ToolError
from ..signing import record_url
from ..util import compact_json, normalize_fields, strip_quotes, strip_record_key, utf8_len

INLINE_LIMIT = 262_144


def _size_hint(size: Any) -> str:
    if not isinstance(size, (int, float)) or isinstance(size, bool) or size <= 0:
        return ""
    hint = f" The full dataset is roughly {int(size)} bytes."
    if size > 50_000:
        hint += (
            " Reading all of it at once may not fit in your context: select columns with `fields=` or page "
            "with `offset`."
        )
    return hint


def _list_step(tool: str, get_tool: str, offset: int, count: int, total: int, noun: str) -> tuple[str, str]:
    summary = f"Listed {count} of {total} {noun}."
    if offset + count < total:
        return summary, f"Call {tool} again with offset={offset + count} to see more."
    return summary, f"Inspect one of them with {get_tool}."


# -- datasets ------------------------------------------------------------------


async def get_dataset(cc: CallContext, args: dict) -> dict:
    ds = strip_quotes(str(args.get("datasetId") or ""))
    cc.telemetry["dataset_id"] = ds
    meta = await cc.api.get_or_none(f"/v2/datasets/{path_ref(ds)}")
    if not isinstance(meta, dict):
        raise ToolError(f"Dataset '{ds}' was not found. Check the dataset ID.", http_status=404)
    data = {k: v for k, v in meta.items() if k != "schema"}
    if "fields" in data:
        data["fields"] = normalize_fields(data.get("fields"))
    label = data.get("name") or data.get("id") or ds
    count = data.get("itemCount")
    nf = len(data.get("fields") or [])
    summary = f"Dataset {label} has {count if count is not None else 'an unknown number of'} items and {nf} fields."
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    step = f"Call get-dataset-items with datasetId \"{data.get('id') or ds}\" and limit 20.{_size_hint(stats.get('inflatedBytes'))}"
    data.setdefault("id", ds)
    return storage_result(data, summary, step, await cc.console_link(f"/storage/datasets/{data['id']}"))


def _derive_flatten(fields: str | None) -> str | None:
    if not fields:
        return None
    seen: list[str] = []
    for f in fields.split(","):
        f = f.strip()
        idx = f.find(".")
        if idx > 0:
            head = f[:idx]
            if head not in seen:
                seen.append(head)
    return ",".join(seen) if seen else None


def _header_int(headers, name: str) -> int | None:
    for key in (f"x-apify-pagination-{name}", f"x-scrapely-pagination-{name}"):
        raw = headers.get(key)
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                return None
    return None


async def get_dataset_items(cc: CallContext, args: dict) -> dict:
    ds = strip_quotes(str(args.get("datasetId") or ""))
    cc.telemetry["dataset_id"] = ds
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or 20)
    fields = args.get("fields")
    flatten = args.get("flatten") or _derive_flatten(fields)
    params: dict[str, Any] = {
        "format": "json",
        "clean": args.get("clean"),
        "offset": offset,
        "limit": limit,
        "fields": fields,
        "omit": args.get("omit"),
        "desc": args.get("desc"),
        "flatten": flatten,
    }
    resp = await cc.api.request("GET", f"/v2/datasets/{path_ref(ds)}/items", params=params)
    if resp.status_code == 404:
        raise ToolError(f"Dataset '{ds}' was not found. Check the dataset ID.", http_status=404)
    cc.api.raise_for(resp)
    items = resp.json() if resp.content else []
    if isinstance(items, dict) and isinstance(items.get("items"), list):
        items = items["items"]
    if not isinstance(items, list):
        items = []
    total = _header_int(resp.headers, "total")
    count = max(_header_int(resp.headers, "count") or 0, len(items))
    if total is None:
        total = offset + count
    if offset + count < total:
        summary = f"Fetched {count} of {total} items (offset {offset})."
        step = f"Call get-dataset-items again with offset={offset + count} for the next page."
    else:
        if offset == 0 and count == total:
            summary = f"Fetched all {count} items."
        else:
            summary = f"Fetched {count} of {total} items (offset {offset}); there are no more pages."
        step = (
            f"Call get-dataset with datasetId \"{ds}\" to see the field list."
            if cc.has("get-dataset")
            else "There are no more pages."
        )
    structured: dict[str, Any] = {"datasetId": ds}
    console = await cc.console_link(f"/storage/datasets/{ds}")
    if console:
        structured["scrapelyConsoleUrl"] = console
    structured.update(
        {
            "items": items,
            "itemCount": count,
            "totalItemCount": total,
            "offset": offset,
            "limit": limit,
            "summary": summary,
            "nextStep": step,
        }
    )
    data = {k: v for k, v in structured.items() if k not in ("summary", "nextStep")}
    texts = [compact_json(data), f"{summary}\n{step}"]
    if console:
        texts.append(f"Dashboard: {console} (show this URL to the user exactly as written)")
    return ok(structured, texts)


# schema inference -----------------------------------------------------------

_DATE_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(:\d{2})?(\.\d+)?([Zz]|[+-]\d{2}:?\d{2})?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://\S+$")


def _format(s: str) -> str | None:
    if _DATE_TIME_RE.match(s):
        return "date-time"
    if _DATE_RE.match(s):
        return "date"
    if _UUID_RE.match(s):
        return "uuid"
    if _EMAIL_RE.match(s):
        return "email"
    if _URI_RE.match(s):
        return "uri"
    return None


def infer(value: Any) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "integer"} if value.is_integer() else {"type": "number"}
    if isinstance(value, str):
        out: dict[str, Any] = {"type": "string"}
        fmt = _format(value)
        if fmt:
            out["format"] = fmt
        return out
    if isinstance(value, list):
        out = {"type": "array"}
        merged: dict | None = None
        for item in value:
            s = infer(item)
            merged = s if merged is None else merge(merged, s)
        if merged is not None:
            out["items"] = merged
        return out
    if isinstance(value, dict):
        out = {"type": "object"}
        if value:
            out["properties"] = {k: infer(v) for k, v in value.items()}
        return out
    return {}


def _type_list(s: dict) -> list[str]:
    t = s.get("type")
    if isinstance(t, list):
        return list(t)
    if isinstance(t, str):
        return [t]
    return []


def merge(a: dict, b: dict) -> dict:
    types: list[str] = []
    for t in _type_list(a) + _type_list(b):
        if t not in types:
            types.append(t)
    if "integer" in types and "number" in types:
        types.remove("integer")
    out: dict[str, Any] = {}
    if types:
        out["type"] = types[0] if len(types) == 1 else types
    if a.get("format") and a.get("format") == b.get("format"):
        out["format"] = a["format"]
    pa, pb = a.get("properties"), b.get("properties")
    if pa or pb:
        props: dict[str, Any] = {}
        for k, v in (pa or {}).items():
            props[k] = merge(v, pb[k]) if pb and k in pb else v
        for k, v in (pb or {}).items():
            if k not in props:
                props[k] = v
        out["properties"] = props
    ia, ib = a.get("items"), b.get("items")
    if ia and ib:
        out["items"] = merge(ia, ib)
    elif ia or ib:
        out["items"] = ia or ib
    return out


def _clean_empty_arrays(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean_empty_arrays(v) for k, v in value.items() if v != []}
    if isinstance(value, list):
        return [_clean_empty_arrays(v) for v in value]
    return value


async def get_dataset_schema(cc: CallContext, args: dict) -> dict:
    ds = strip_quotes(str(args.get("datasetId") or ""))
    cc.telemetry["dataset_id"] = ds
    limit = int(args.get("limit") or 5)
    clean = args.get("clean", True)
    resp = await cc.api.request(
        "GET", f"/v2/datasets/{path_ref(ds)}/items", params={"clean": bool(clean), "limit": limit, "format": "json"}
    )
    if resp.status_code == 404:
        raise ToolError(f"Dataset '{ds}' was not found. Check the dataset ID.", http_status=404)
    cc.api.raise_for(resp)
    items = resp.json() if resp.content else []
    if not isinstance(items, list):
        items = []
    items = items[:limit]
    if not items:
        return storage_result(
            {"datasetId": ds, "schema": {}},
            "The dataset is empty, so no schema could be inferred.",
            f"Call get-dataset with datasetId \"{ds}\" to check its item count.",
        )
    merged: dict | None = None
    for item in items:
        if clean:
            item = _clean_empty_arrays(item)
        s = infer(item)
        merged = s if merged is None else merge(merged, s)
    if not merged:
        raise ToolError("Could not infer a schema from the dataset items.", user=False)
    schema = {"type": "array", "items": merged}
    nfields = len(merged.get("properties") or {})
    return storage_result(
        {"datasetId": ds, "schema": schema},
        f"Inferred the schema from {len(items)} items with {nfields} top-level fields.",
        f"Call get-dataset-items with datasetId \"{ds}\" and `fields=` to read only the fields you need.",
    )


async def _list(cc: CallContext, args: dict, path: str, tool: str, get_tool: str, noun: str, default_limit: int) -> dict:
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or default_limit)
    params = {
        "offset": offset,
        "limit": limit,
        "desc": bool(args.get("desc", False)),
        "unnamed": bool(args.get("unnamed", False)),
    }
    data = await cc.api.get_json(path, params)
    if not isinstance(data, dict):
        data = {"items": data if isinstance(data, list) else []}
    items = [i for i in data.get("items") or [] if isinstance(i, dict)]
    out = {
        "total": data.get("total", len(items)),
        "count": data.get("count", len(items)),
        "offset": data.get("offset", offset),
        "limit": data.get("limit", limit),
        "items": items,
    }
    if "desc" in data:
        out["desc"] = data["desc"]
    summary, step = _list_step(tool, get_tool, offset, len(items), int(out["total"] or 0), noun)
    return storage_result(out, summary, step)


async def get_dataset_list(cc: CallContext, args: dict) -> dict:
    return await _list(cc, args, "/v2/datasets", "get-dataset-list", "get-dataset", "datasets", 10)


async def get_key_value_store_list(cc: CallContext, args: dict) -> dict:
    return await _list(
        cc, args, "/v2/key-value-stores", "get-key-value-store-list", "get-key-value-store", "key-value stores", 10
    )


# -- key-value stores ---------------------------------------------------------


async def get_key_value_store(cc: CallContext, args: dict) -> dict:
    sid = strip_quotes(str(args.get("keyValueStoreId") or ""))
    cc.telemetry["key_value_store_id"] = sid
    meta = await cc.api.get_or_none(f"/v2/key-value-stores/{path_ref(sid)}")
    if not isinstance(meta, dict):
        raise ToolError(f"Key-value store '{sid}' was not found. Check the store ID.", http_status=404)
    data = {k: v for k, v in meta.items() if k != "urlSigningSecretKey"}
    data.setdefault("id", sid)
    label = data.get("name") or data["id"]
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    size = stats.get("storageBytes")
    summary = f"Key-value store {label}" + (f" uses {size} bytes." if size is not None else ".")
    step = f"Call get-key-value-store-keys with keyValueStoreId \"{data['id']}\" to list its keys."
    return storage_result(data, summary, step, await cc.console_link(f"/storage/key-value-stores/{data['id']}"))


async def get_key_value_store_keys(cc: CallContext, args: dict) -> dict:
    sid = strip_quotes(str(args.get("keyValueStoreId") or ""))
    cc.telemetry["key_value_store_id"] = sid
    params = {"exclusiveStartKey": args.get("exclusiveStartKey"), "limit": args.get("limit")}
    listing = await cc.api.get_or_none(f"/v2/key-value-stores/{path_ref(sid)}/keys", params)
    if not isinstance(listing, dict):
        raise ToolError(f"Key-value store '{sid}' was not found. Check the store ID.", http_status=404)
    items = [i for i in listing.get("items") or [] if isinstance(i, dict)]
    for i in items:
        i.setdefault("size", 0)
    data = dict(listing)
    data["keyValueStoreId"] = sid
    data["items"] = items
    data.setdefault("count", len(items))
    data["isTruncated"] = bool(listing.get("isTruncated"))
    data.setdefault("nextExclusiveStartKey", None)
    truncated = data["isTruncated"]
    summary = f"Listed {len(items)} keys" + (", more are available." if truncated else ".")
    next_key = data.get("nextExclusiveStartKey")
    if truncated and next_key:
        step = f"Call get-key-value-store-keys again with exclusiveStartKey=\"{next_key}\"."
    elif items:
        step = f"Call get-key-value-store-record with keyValueStoreId \"{sid}\" and recordKey \"{items[0]['key']}\"."
    else:
        step = f"The store has no keys; call get-key-value-store with keyValueStoreId \"{sid}\" for its details."
    return storage_result(data, summary, step)


_TEXT_TYPES = ("application/xml", "text/xml")


def _kind(content_type: str) -> str:
    base = content_type.split(";", 1)[0].strip().lower()
    if base == "application/json" or base.endswith("+json"):
        return "json"
    if base.startswith("text/") or base in _TEXT_TYPES or base.endswith("+xml"):
        return "text"
    return "binary"


async def get_key_value_store_record(cc: CallContext, args: dict) -> dict:
    sid = strip_quotes(str(args.get("keyValueStoreId") or ""))
    key = strip_record_key(str(args.get("recordKey") or ""))
    cc.telemetry["key_value_store_id"] = sid
    resp = await cc.api.request(
        "GET", f"/v2/key-value-stores/{path_ref(sid)}/records/{seg(key)}", headers={"Accept": "*/*"}
    )
    if resp.status_code == 404:
        store = await cc.api.get_or_none(f"/v2/key-value-stores/{path_ref(sid)}")
        if store is None:
            raise ToolError(f"Key-value store '{sid}' was not found. Check the store ID.", http_status=404)
        raise ToolError(
            f"Record '{key}' was not found in key-value store '{sid}'. List the keys with get-key-value-store-keys.",
            http_status=404,
        )
    cc.api.raise_for(resp)
    content_type = resp.headers.get("content-type") or ""
    body = resp.content
    console = await cc.console_link(f"/storage/key-value-stores/{sid}")
    base_mime = content_type.split(";", 1)[0].strip().lower()
    structured: dict[str, Any] = {"keyValueStoreId": sid, "key": key}
    if not body:
        structured["value"] = ""
        if content_type:
            structured["contentType"] = content_type
        return _record_text_result(structured, f"Read record '{key}': it is empty.", console)
    kind = _kind(content_type)
    if kind in ("json", "text"):
        if kind == "json":
            try:
                value: Any = resp.json()
                size = utf8_len(compact_json(value))
            except ValueError:
                value = resp.text
                size = utf8_len(value)
        else:
            value = resp.text
            size = utf8_len(value)
        structured["value"] = value
        if content_type:
            structured["contentType"] = content_type
        return _record_text_result(structured, f"Read record '{key}' ({base_mime or 'unknown type'}, {size} bytes).", console)
    size = len(body)
    structured["value"] = f"<binary {base_mime or 'application/octet-stream'}, {size} bytes>"
    if content_type:
        structured["contentType"] = content_type
    structured["summary"] = f"Read record '{key}' ({base_mime or 'binary'}, {size} bytes)."
    mime = base_mime or "application/octet-stream"
    if size > INLINE_LIMIT:
        url = await record_url(cc.api, cc.settings, sid, key)
        content: list[dict] = [
            {"type": "resource_link", "uri": url, "name": key, "size": size, "mimeType": mime},
            text(structured["summary"] + " It is too large to inline; download it from the link."),
        ]
    elif mime.startswith("image/"):
        content = [{"type": "image", "data": base64.b64encode(body).decode(), "mimeType": mime}]
    elif mime.startswith("audio/"):
        content = [{"type": "audio", "data": base64.b64encode(body).decode(), "mimeType": mime}]
    else:
        uri = f"{cc.settings.public_api_base_url}/v2/key-value-stores/{seg(sid)}/records/{seg(key)}"
        content = [
            {"type": "resource", "resource": {"uri": uri, "blob": base64.b64encode(body).decode(), "mimeType": mime}}
        ]
    if console:
        content.append(text(f"Dashboard: {console} (show this URL to the user exactly as written)"))
    return {"content": content, "structuredContent": structured, "isError": False}


def _record_text_result(structured: dict, summary: str, console: str | None) -> dict:
    data = dict(structured)
    structured["summary"] = summary
    texts = [compact_json(data), summary]
    if console:
        texts.append(f"Dashboard: {console} (show this URL to the user exactly as written)")
    return ok(structured, texts)


# -- run output (one call: dataset page + OUTPUT record) ------------------------

OUTPUT_DEFAULT_LIMIT = 100
OUTPUT_MAX_LIMIT = 1000
OUTPUT_VALUE_MAX_CHARS = 1000
OUTPUT_ITEMS_MAX_BYTES = 50_000
OUTPUT_RECORD_MAX_BYTES = 20_000
OUTPUT_RECORD_KEY = "OUTPUT"
_TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


def _shorten(value: Any, max_chars: int, counter: list[int]) -> Any:
    """Copy of `value` with every string longer than `max_chars` cut, counting the cuts."""
    if isinstance(value, str):
        if len(value) > max_chars:
            counter[0] += 1
            return value[:max_chars] + f"... [cut {len(value) - max_chars} chars]"
        return value
    if isinstance(value, dict):
        return {k: _shorten(v, max_chars, counter) for k, v in value.items()}
    if isinstance(value, list):
        return [_shorten(v, max_chars, counter) for v in value]
    return value


def _fit_items(items: list, budget: int, counter: list[int]) -> list:
    """Shortened items, stopping before the serialized page exceeds `budget` bytes (always keeps one)."""
    out: list = []
    used = 2
    for item in items:
        short = _shorten(item, OUTPUT_VALUE_MAX_CHARS, counter)
        size = utf8_len(compact_json(short)) + 1
        if out and used + size > budget:
            break
        out.append(short)
        used += size
    return out


async def _read_output_record(cc: CallContext, sid: str, key: str) -> dict | None:
    resp = await cc.api.request(
        "GET", f"/v2/key-value-stores/{path_ref(sid)}/records/{seg(key)}", headers={"Accept": "*/*"}
    )
    if resp.status_code == 404:
        return None
    cc.api.raise_for(resp)
    content_type = resp.headers.get("content-type") or ""
    body = resp.content
    record: dict[str, Any] = {"keyValueStoreId": sid, "key": key, "contentType": content_type, "truncated": False}
    kind = _kind(content_type) if body else "text"
    if kind == "binary":
        record["value"] = f"<binary {content_type.split(';', 1)[0] or 'application/octet-stream'}, {len(body)} bytes>"
        record["truncated"] = True
        return record
    value: Any
    if kind == "json":
        try:
            value = resp.json()
        except ValueError:
            value = resp.text
    else:
        value = resp.text if body else ""
    counter = [0]
    value = _shorten(value, OUTPUT_VALUE_MAX_CHARS * 4, counter)
    serialized = value if isinstance(value, str) else compact_json(value)
    if utf8_len(serialized) > OUTPUT_RECORD_MAX_BYTES:
        value = serialized[:OUTPUT_RECORD_MAX_BYTES] + "... [cut]"
        counter[0] += 1
    record["value"] = value
    record["truncated"] = counter[0] > 0
    return record


async def get_actor_output(cc: CallContext, args: dict) -> dict:
    ds = strip_quotes(str(args.get("datasetId") or ""))
    run_id = strip_quotes(str(args.get("runId") or ""))
    kvs = strip_quotes(str(args.get("keyValueStoreId") or ""))
    if not ds and not run_id:
        raise ToolError("Pass a datasetId, or the runId of a run whose output you want.", http_status=400)
    run_status = None
    if run_id:
        cc.telemetry["run_id"] = run_id
        run = await cc.api.get_run(run_id)
        if not isinstance(run, dict):
            raise ToolError(f"Run '{run_id}' was not found. Check the run ID.", http_status=404)
        run_status = run.get("status")
        ds = ds or str(run.get("defaultDatasetId") or "")
        kvs = kvs or str(run.get("defaultKeyValueStoreId") or "")
        if not ds:
            raise ToolError(f"Run '{run_id}' has no default dataset.", http_status=404)
    cc.telemetry["dataset_id"] = ds
    include_record = args.get("includeOutputRecord")
    if include_record is None:
        include_record = bool(kvs)
    if include_record and not kvs:
        raise ToolError("includeOutputRecord needs a runId or a keyValueStoreId.", http_status=400)

    offset = max(int(args.get("offset") or 0), 0)
    limit = min(max(int(args.get("limit") or OUTPUT_DEFAULT_LIMIT), 1), OUTPUT_MAX_LIMIT)
    fields = args.get("fields") or None
    params: dict[str, Any] = {
        "format": "json",
        "clean": True,
        "offset": offset,
        "limit": limit,
        "fields": fields,
        "flatten": _derive_flatten(fields),
    }
    resp = await cc.api.request("GET", f"/v2/datasets/{path_ref(ds)}/items", params=params)
    if resp.status_code == 404:
        raise ToolError(f"Dataset '{ds}' was not found. Check the dataset ID.", http_status=404)
    cc.api.raise_for(resp)
    raw = resp.json() if resp.content else []
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        raw = raw["items"]
    if not isinstance(raw, list):
        raw = []
    total = _header_int(resp.headers, "total")
    if total is None:
        total = offset + len(raw)
    counter = [0]
    items = _fit_items(raw, OUTPUT_ITEMS_MAX_BYTES, counter)
    count = len(items)
    cut_by_size = count < len(raw)
    has_more = offset + count < total
    next_offset = offset + count if has_more else None

    record = None
    if include_record:
        record = await _read_output_record(cc, kvs, OUTPUT_RECORD_KEY)

    parts = []
    if count == 0 and total == 0:
        parts.append("The dataset has no items yet.")
    else:
        parts.append(f"Returned items {offset + 1 if count else offset} to {offset + count} of {total}.")
    if cut_by_size:
        parts.append(f"The page was shortened to {count} items to stay compact.")
    if counter[0]:
        parts.append(f"{counter[0]} long text values were cut.")
    if include_record:
        parts.append(
            "The OUTPUT record was not found." if record is None
            else ("The OUTPUT record is included (shortened)." if record.get("truncated") else "The OUTPUT record is included.")
        )
    if run_status and run_status not in _TERMINAL:
        parts.append(f"The run is still {run_status}, so more output may follow.")
    summary = " ".join(parts)
    if has_more:
        step = f"Call get-actor-output again with offset={next_offset} for the next page"
        step += "." if fields else ", or pass `fields` to read only the columns you need."
    elif record is not None and record.get("truncated"):
        step = f"Read the full record with get-key-value-store-record (keyValueStoreId \"{kvs}\", recordKey \"OUTPUT\")."
    else:
        step = "That is all the output."

    structured: dict[str, Any] = {"datasetId": ds}
    if run_id:
        structured["runId"] = run_id
    if run_status:
        structured["runStatus"] = run_status
    structured.update(
        {
            "items": items,
            "itemCount": count,
            "totalItemCount": total,
            "offset": offset,
            "limit": limit,
            "hasMore": has_more,
            "nextOffset": next_offset,
            "truncated": cut_by_size or counter[0] > 0,
        }
    )
    if include_record:
        structured["outputRecord"] = record
    console = await cc.console_link(f"/storage/datasets/{ds}")
    if console:
        structured["scrapelyConsoleUrl"] = console
    data = dict(structured)
    structured["summary"] = summary
    structured["nextStep"] = step
    texts = [compact_json(data), f"{summary}\n{step}"]
    if console:
        texts.append(f"Dashboard: {console} (show this URL to the user exactly as written)")
    return ok(structured, texts)
