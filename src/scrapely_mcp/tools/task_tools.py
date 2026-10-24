"""Saved Actor task tools (SPEC 7.17 - 7.22)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..core import CallContext, ok
from ..errors import ApiError, ToolError
from ..util import ALNUM17_RE, UUID_RE, compact_json, normalize_ts, strip_quotes

_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")


def _seg(ref: str) -> str:
    return quote(ref, safe="~")


def resolve_ref(value: str, *, is_task: bool) -> tuple[str, bool]:
    """Return (path reference, ambiguous)."""
    v = strip_quotes(value)
    if "~" in v:
        return v, False
    if "/" in v:
        return v.replace("/", "~"), False
    if ALNUM17_RE.match(v):
        return v, True
    if is_task and UUID_RE.match(v):
        return v, True
    return f"~{v}", False


def validate_name(name: Any) -> None:
    if name is None:
        return
    if not isinstance(name, str) or not (3 <= len(name) <= 30) or not _NAME_RE.match(name):
        raise ToolError(
            f"Invalid task name '{name}'. Task names are 3 to 30 characters, use only letters, digits and dashes, "
            "and cannot start or end with a dash."
        )


def task_result(task: dict) -> dict:
    seo_title = task.get("seoTitle")
    seo_desc = task.get("seoDescription")
    public = None
    if seo_title is not None or seo_desc is not None:
        public = {"seoTitle": seo_title, "seoDescription": seo_desc}
    published = task.get("publishedAt")
    return {
        "taskId": task.get("id"),
        "actorId": task.get("actId"),
        "name": task.get("name"),
        "title": task.get("title"),
        "publishedAt": normalize_ts(published) if published else None,
        "publicConfig": public,
        "input": task.get("input"),
    }


def _respond(task: dict, summary: str) -> dict:
    result = task_result(task)
    return ok(result, [compact_json(result), summary])


def _seo(body: dict, public: Any) -> None:
    if isinstance(public, dict):
        if public.get("seoTitle") is not None:
            body["seoTitle"] = public["seoTitle"]
        if public.get("seoDescription") is not None:
            body["seoDescription"] = public["seoDescription"]


def _options(args: dict) -> dict:
    out = {}
    for key in ("build", "timeoutSecs", "memoryMbytes"):
        if args.get(key) is not None:
            out[key] = args[key]
    return out


async def read_task(cc: CallContext, task_id: str) -> dict | None:
    ref, ambiguous = resolve_ref(task_id, is_task=True)
    task = await cc.api.get_or_none(f"/v2/actor-tasks/{_seg(ref)}")
    if task is None and ambiguous:
        task = await cc.api.get_or_none(f"/v2/actor-tasks/{_seg('~' + ref)}")
    return task if isinstance(task, dict) else None


def _not_found(task_id: str) -> ToolError:
    return ToolError(f"Task '{task_id}' was not found. Check the task ID or name.", http_status=404)


def _publish_error(exc: ApiError) -> ToolError:
    return ToolError(
        f"The API refused the task: {exc.message}. SEO fields are checked when a task is created or updated, and "
        "all publication requirements are checked on publish. Fix the stated requirement first.",
        http_status=exc.status,
        detail="cannot-publish-actor-task",
    )


async def create_actor_task(cc: CallContext, args: dict) -> dict:
    validate_name(args.get("name"))
    actor_ref, _ = resolve_ref(str(args.get("actorId") or ""), is_task=False)
    body: dict[str, Any] = {"actId": actor_ref}
    for key in ("name", "title"):
        if args.get(key) is not None:
            body[key] = args[key]
    if isinstance(args.get("input"), dict) and args["input"]:
        body["input"] = args["input"]
    opts = _options(args)
    if opts:
        body["options"] = opts
    _seo(body, args.get("publicConfig"))
    try:
        task = await cc.api.post_json("/v2/actor-tasks", body)
    except ApiError as exc:
        if exc.type == "cannot-publish-actor-task":
            raise _publish_error(exc) from exc
        raise
    return _respond(task, f"Created task {task.get('name')} ({task.get('id')}) for Actor {task.get('actId')}.")


async def get_actor_task(cc: CallContext, args: dict) -> dict:
    task_id = str(args.get("taskId") or "")
    task = await read_task(cc, task_id)
    if task is None:
        raise _not_found(task_id)
    published = " It is published." if task.get("publishedAt") else ""
    return _respond(task, f"Task {task.get('name')} ({task.get('id')}) of Actor {task.get('actId')}.{published}")


async def update_actor_task(cc: CallContext, args: dict) -> dict:
    validate_name(args.get("name"))
    task_id = str(args.get("taskId") or "")
    ref, ambiguous = resolve_ref(task_id, is_task=True)
    opts = _options(args)
    stored_opts: dict = {}
    target = ref
    if opts or ambiguous:
        task = await read_task(cc, task_id)
        if task is None:
            raise _not_found(task_id)
        target = str(task.get("id") or ref)
        stored_opts = task.get("options") if isinstance(task.get("options"), dict) else {}
    body: dict[str, Any] = {}
    for key in ("name", "title"):
        if args.get(key) is not None:
            body[key] = args[key]
    if isinstance(args.get("input"), dict) and args["input"]:
        body["input"] = args["input"]
    if opts:
        merged = {k: v for k, v in stored_opts.items() if v is not None}
        merged.update(opts)
        body["options"] = merged
    _seo(body, args.get("publicConfig"))
    try:
        task = await cc.api.put_json(f"/v2/actor-tasks/{_seg(target)}", body)
    except ApiError as exc:
        if exc.type == "cannot-publish-actor-task":
            raise _publish_error(exc) from exc
        if exc.status == 404:
            raise _not_found(task_id) from exc
        raise
    return _respond(task, f"Updated task {task.get('name')} ({task.get('id')}).")


async def _publication(cc: CallContext, args: dict, action: str) -> dict:
    task_id = str(args.get("taskId") or "")
    ref, ambiguous = resolve_ref(task_id, is_task=True)
    target = ref
    if ambiguous:
        task = await read_task(cc, task_id)
        if task is not None and task.get("id"):
            target = str(task["id"])
    try:
        task = await cc.api.post_json(f"/v2/actor-tasks/{_seg(target)}/{action}")
    except ApiError as exc:
        if exc.type == "cannot-publish-actor-task":
            raise _publish_error(exc) from exc
        raise
    if action == "publish":
        summary = (
            f"Task {task.get('name')} ({task.get('id')}) is published; publishedAt is the confirmed publication "
            "time. The public page link is on the task's Publication tab in the dashboard."
        )
    else:
        summary = f"Task {task.get('name')} ({task.get('id')}) is no longer published."
    return _respond(task, summary)


async def publish_actor_task(cc: CallContext, args: dict) -> dict:
    return await _publication(cc, args, "publish")


async def unpublish_actor_task(cc: CallContext, args: dict) -> dict:
    return await _publication(cc, args, "unpublish")
