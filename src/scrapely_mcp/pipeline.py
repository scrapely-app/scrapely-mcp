"""The tools/call pipeline (SPEC 6)."""

from __future__ import annotations

import copy
import json
import time
from typing import Any

import anyio

from . import config, session_log, telemetry
from .core import CallContext, SessionState, ToolDef, make_api, text
from .errors import ApiError, ToolError, ValidationFailure
from .log import log, redact
from .util import json_size

# Spec names plus the equivalents the Scrape.ly API documents publicly.
RUN_LIMIT_TYPES = {"cannot-start-actor-runs", "concurrency-limit-exceeded"}
MEMORY_LIMIT_TYPES = {"memory-limit-exceeded", "limit-exceeded"}

STRONG_NUDGE = (
    "If you cannot resolve this failure yourself, report it with report-problem before telling the user that it "
    "failed."
)
SOFT_NUDGE = (
    "If this looks like a bug in the tool or Actor rather than a mistake in the request, report it with "
    "report-problem."
)


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            if not isinstance(inner, anyio.get_cancelled_exc_class()):
                return _leaf(inner)
    return exc


async def _run_handler(tool: ToolDef, cc: CallContext, args: Any) -> dict:
    try:
        return await tool.handler(cc, args)
    except BaseExceptionGroup as group:
        leaf = _leaf(group)
        if leaf is group or isinstance(leaf, anyio.get_cancelled_exc_class()):
            raise
        raise leaf from None


def find_tool(state: SessionState, name: str) -> ToolDef | None:
    blocked = state.report_problem_blocked()
    if blocked and name == "report-problem":
        return None
    tool = state.tools.get(name)
    if tool is None and "-slash-" in name:
        tool = state.tools.get(name.replace("-slash-", "--"))
    return tool


def too_large_result(name: str, size: int, limit: int) -> dict:
    return {
        "content": [
            text(
                f"The result of tool {name} was {size:,} bytes, over the {limit:,} byte limit for one tool result. "
                "Request less data (a smaller limit, fewer fields or a narrower input), or download large stored "
                "data through the Scrape.ly API instead."
            )
        ],
        "isError": True,
    }


def unknown_tool_result(name: str) -> dict:
    return {
        "content": [text(f"Tool '{name}' was not found. Check the list of available tools and try again.")],
        "isError": True,
    }


def request_token(state: SessionState, meta: dict | None) -> str | None:
    if isinstance(meta, dict):
        override = meta.get("scrapelyToken")
        if isinstance(override, str) and override:
            return override
        if config.ACCEPT_LEGACY_TOKEN_META_ALIAS:
            alias = meta.get("apifyToken")
            if isinstance(alias, str) and alias:
                return alias
    return state.token


def _error_text(tool_name: str, exc: ApiError, settings) -> tuple[str, str, bool]:
    """Return (text, category, user_error) for an API error."""
    status = exc.status
    if exc.type == "full-permission-actor-not-approved":
        msg = exc.message
        approval = exc.data.get("approvalUrl") if isinstance(exc.data, dict) else None
        if approval:
            msg += f"\nApprove the Actor's permissions here: {approval}"
        return msg, "PERMISSION_APPROVAL_REQUIRED", True
    if exc.type in RUN_LIMIT_TYPES or "cannot-start-actor-runs" in str(exc):
        return (
            "You reached the limit of concurrent Actor runs on your plan. Wait for running Actors to finish, or "
            f"upgrade your plan at {settings.console_base_url}/billing/subscription.",
            "INVALID_INPUT",
            True,
        )
    if exc.type in MEMORY_LIMIT_TYPES:
        return (
            "The run would exceed your account's memory quota. Try a smaller callOptions.memory, or wait for "
            "running Actors to finish.",
            "INVALID_INPUT",
            True,
        )
    if exc.type == "cannot-publish-actor-task":
        return (
            f"The API refused the task: {exc.message}. SEO fields are checked on create and update, and all "
            "publication requirements on publish. Fix the stated requirement first.",
            "INVALID_INPUT",
            True,
        )
    type_part = f" ({exc.type})" if exc.type else ""
    if status == 401:
        hint = "Authentication failed: check that your Scrape.ly token is valid."
        category = "AUTH"
    elif status == 403:
        hint = "The resource may be private, or your token does not have access to it."
        category = "AUTH"
    else:
        hint = "Check the tool name and its inputs."
        category = "INVALID_INPUT" if status is not None and status < 500 else "INTERNAL_ERROR"
    user = status is not None and status < 500
    return f"Tool {tool_name} failed: {exc.message.rstrip('.')}{type_part}. {hint}", category, user


def add_nudge(result: dict, state: SessionState, tool_name: str, category: str | None, http_status: int | None) -> dict:
    if not result.get("isError") or not isinstance(result.get("content"), list):
        return result
    if not state.report_problem_available() or tool_name == "report-problem":
        return result
    if http_status == 402 or category in ("AUTH", "PERMISSION_APPROVAL_REQUIRED"):
        return result
    out = dict(result)
    out["content"] = list(result["content"]) + [text(SOFT_NUDGE if category == "INVALID_INPUT" else STRONG_NUDGE)]
    return out


def _sizes(result: dict) -> dict:
    text_bytes = 0
    file_bytes = 0
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text_bytes += len(str(item.get("text", "")).encode())
        elif item.get("type") in ("image", "audio"):
            file_bytes += len(str(item.get("data", "")))
        elif item.get("type") == "resource":
            file_bytes += len(str((item.get("resource") or {}).get("blob", "")))
    structured = result.get("structuredContent")
    structured_bytes = len(json.dumps(structured, default=str).encode()) if structured is not None else 0
    return {"text_bytes": text_bytes, "structured_bytes": structured_bytes, "file_bytes": file_bytes}


def make_progress_sender(request_ctx: Any, progress_token: Any):
    if request_ctx is None or progress_token is None:
        return None
    import mcp_types as types

    async def send(progress: float, message: str, meta: dict) -> None:
        notification = types.ProgressNotification(
            params=types.ProgressNotificationParams(
                progress_token=progress_token, progress=progress, message=message, meta=meta
            )
        )
        await request_ctx.session.send_notification(notification, related_request_id=request_ctx.request_id)

    return send


async def call_tool(
    state: SessionState,
    name: str,
    arguments: dict | None,
    *,
    meta: dict | None = None,
    request_ctx: Any = None,
    headers: dict[str, str] | None = None,
    task_mode: bool = False,
) -> dict:
    started = time.monotonic()
    tool = find_tool(state, name)
    if tool is None:
        result = unknown_tool_result(name)
        args, context = session_log.take_context(None, arguments)
        session_log.record(state, session_log.tool_call_event(
            name=str(name)[:255], arguments=args, context=context, status="error", started=started, result=result,
            category="UNKNOWN_TOOL", task_mode=task_mode,
        ))
        return result
    args = copy.deepcopy(arguments) if isinstance(arguments, dict) else arguments
    # The optional `context` every tool advertises is for the session log only: never validated, never passed on.
    args, context = session_log.take_context(tool, args)
    logged_args = copy.deepcopy(args) if isinstance(args, dict) else args
    if task_mode and isinstance(args, dict):
        args.pop("waitSecs", None)
    token = request_token(state, meta)
    payment_headers = None
    if state.payment is not None and tool.payment_required and isinstance(args, dict):
        payment_headers, required = await state.payment.prepare(args, meta, headers or {})
        if required is not None:
            session_log.record(state, session_log.tool_call_event(
                name=tool.name, arguments=logged_args, context=context, status="error", started=started,
                result=required, category="PAYMENT_REQUIRED", http_status=402, task_mode=task_mode,
            ))
            return required
    api = make_api(
        state.settings,
        None if payment_headers else token,
        state.request_origin(),
        extra_headers=payment_headers,
    )
    progress_token = None
    if isinstance(meta, dict):
        # The SDK validates inbound `_meta`, which renames progressToken to progress_token.
        progress_token = meta.get("progressToken", meta.get("progress_token"))
    cc = CallContext(
        state=state,
        tool=tool,
        api=api,
        token=None if payment_headers else token,
        request_ctx=request_ctx,
        progress_token=progress_token,
        task_mode=task_mode,
        headers=headers or {},
        notify_progress=None if task_mode else make_progress_sender(request_ctx, progress_token),
    )
    status = "SUCCEEDED"
    category: str | None = None
    http_status: int | None = None
    detail: str | None = None
    validation: dict | None = None
    try:
        try:
            args = await tool.validator.validate_async(args)
        except ValidationFailure as vf:
            first = vf.errors[0]
            path = first["instancePath"] or "(root)"
            result = {
                "content": [text(f"Invalid arguments for tool {tool.name} at {path}: {first['message']}")],
                "isError": True,
            }
            status, category = "SOFT_FAIL", "INVALID_INPUT"
            validation = {
                "keyword": str(first["keyword"])[:120],
                "path": str(first["instancePath"])[:120],
                "count": len(vf.errors),
            }
            for k in ("missingProperty", "additionalProperty"):
                if k in first["params"]:
                    validation[k] = str(first["params"][k])[:120]
            log.soft_fail("tool arguments failed validation", tool=tool.name, error=first["message"][:200])
        else:
            log.debug("calling tool", tool=tool.name, arguments=redact(args))
            try:
                result = await _run_handler(tool, cc, args)
            except ToolError as exc:
                result = {"content": [text(exc.message)], "isError": True}
                if exc.structured is not None:
                    result["structuredContent"] = exc.structured
                status = "SOFT_FAIL" if exc.user else "FAILED"
                category = exc.category
                http_status = exc.http_status
                detail = exc.detail or exc.message
                if exc.user:
                    log.soft_fail(f"tool {tool.name} returned a user error", reason=exc.message[:300])
                else:
                    log.error(f"tool {tool.name} failed", reason=exc.message[:300])
            except ApiError as exc:
                if (
                    exc.status == 402
                    and state.payment is not None
                    and state.payment.kind == "x402"
                    and exc.type not in RUN_LIMIT_TYPES
                ):
                    data = state.payment.payment_required_from_error(exc.data)
                    result = await state.payment._required_result(data)  # type: ignore[attr-defined]
                    status, category, http_status = "SOFT_FAIL", "INVALID_INPUT", 402
                else:
                    msg, category, user = _error_text(tool.name, exc, state.settings)
                    result = {"content": [text(msg)], "isError": True}
                    status = "SOFT_FAIL" if user else "FAILED"
                    http_status = exc.status
                    detail = exc.message
                    log.classified(f"tool {tool.name} API call failed", exc)
            except Exception as exc:  # noqa: BLE001
                msg = f"Tool {tool.name} failed: {str(exc).rstrip('.')}. Check the tool name and its inputs."
                result = {"content": [text(msg)], "isError": True}
                status, category, detail = "FAILED", "INTERNAL_ERROR", str(exc)
                log.error(f"tool {tool.name} raised an unexpected exception", exc=True)
    finally:
        with anyio.CancelScope(shield=True):
            await api.aclose()
    if isinstance(result, dict):
        result = {k: v for k, v in result.items() if not k.startswith("_telemetry")}
        limit = state.settings.max_tool_result_bytes
        size = json_size(result)
        if size > limit:
            log.soft_fail(
                "tool result is over the byte limit, returning an error instead", tool=tool.name, size=size, limit=limit
            )
            result = too_large_result(tool.name, size, limit)
            status, category, detail = "SOFT_FAIL", "INVALID_INPUT", "result-too-large"
        result = add_nudge(result, state, tool.name, category, http_status)
    _emit_telemetry(state, tool, result, status, category, http_status, detail, validation, args, cc, started)
    _record_call(state, tool, logged_args, context, result, status, category, http_status, detail, cc, started, task_mode)
    return result


def _record_call(state, tool, args, context, result, status, category, http_status, detail, cc, started, task_mode):
    if getattr(state, "session_log", None) is None:
        return
    try:
        if tool.kind == "actor":
            secret, in_input = set(tool.extra.get("secret_fields") or []), False
        else:
            secret, in_input = set(cc.telemetry.get("secret_fields") or []), True
        log_status = "ok" if status == "SUCCEEDED" else ("invalid" if category == "INVALID_INPUT" and detail is None else "error")
        session_log.record(state, session_log.tool_call_event(
            name=tool.name,
            arguments=args,
            context=context,
            status=log_status,
            started=started,
            result=result,
            telemetry=cc.telemetry,
            sizes=_sizes(result if isinstance(result, dict) else {}),
            category=category,
            http_status=http_status,
            detail=detail,
            secret=secret,
            secret_in_input=in_input,
            task_mode=task_mode,
        ))
    except Exception:  # noqa: BLE001 - logging never changes a tool result
        pass


def _emit_telemetry(state, tool, result, status, category, http_status, detail, validation, args, cc, started):
    if not telemetry.active(state.telemetry_enabled, state.settings.telemetry_write_key):
        return
    props: dict[str, Any] = {
        "client_name": state.client.name,
        "client_version": (state.client.client_info or {}).get("version"),
        "protocol_version": state.client.protocol_version,
        "client_capabilities": state.client.capabilities,
        "mcp_session_id": state.session_id,
        "transport": state.transport,
        "tool_name": tool.name,
        "tool_status": status,
        "execution_time_ms": int((time.monotonic() - started) * 1000),
        **_sizes(result if isinstance(result, dict) else {}),
    }
    if status != "SUCCEEDED":
        props["failure_category"] = category
        props["http_status"] = http_status
        if detail:
            props["failure_detail"] = str(detail)[:200]
    if validation:
        props["validation"] = validation
    if tool.actor_full_name:
        props["actor_name"] = tool.actor_full_name
        props["actor_id"] = tool.actor_id
    for key in ("actor_name", "actor_id", "run_id", "dataset_id", "key_value_store_id"):
        if cc.telemetry.get(key):
            props[key] = cc.telemetry[key]
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if isinstance(structured, dict):
        if structured.get("runId"):
            props["run_id"] = structured["runId"]
        if structured.get("status") and structured.get("runId"):
            props["run_status"] = structured["status"]
        default_ds = ((structured.get("storages") or {}).get("datasets") or {}).get("default")
        if isinstance(default_ds, dict) and default_ds.get("id"):
            props.setdefault("dataset_id", default_ds["id"])
    telemetry.track(state.settings.telemetry_write_key or "", state.session_id, props)
