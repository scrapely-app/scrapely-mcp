"""In-memory store for protocol-level tasks (SPEC 9)."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import DEFAULT_MAX_TOOL_RESULT_BYTES
from .util import json_size

INVALID_PARAMS = -32602
TOO_MANY_TASKS = -32000
FINISHED = ("completed", "failed", "cancelled")
MIB = 1024 * 1024


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw and raw.strip() else default
    except ValueError:
        return default
    return value if value > 0 else default


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class TaskError(Exception):
    def __init__(self, message: str, code: int = INVALID_PARAMS) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class TaskRecord:
    task_id: str
    status: str
    created_at: str
    last_updated_at: str
    ttl: int | None
    created_mono: float = field(default_factory=time.monotonic)
    status_message: str | None = None
    result: dict | None = None
    runner: asyncio.Task | None = None
    finished_mono: float | None = None
    delivered_mono: float | None = None
    # Bytes `result` holds, as counted by the store.
    result_bytes: int = 0
    # Why the result is no longer kept ("delivered", "evicted"), if it was released.
    released: str | None = None

    def expired(self) -> bool:
        return self.ttl is not None and (time.monotonic() - self.created_mono) * 1000 > self.ttl

    def stale(self, retention_secs: float, now: float | None = None) -> bool:
        """Expired, or finished longer ago than the retention window."""
        if self.expired():
            return True
        now = time.monotonic() if now is None else now
        if self.status not in FINISHED:
            return False
        finished = self.finished_mono if self.finished_mono is not None else self.created_mono
        return now - finished > retention_secs

    def public(self) -> dict:
        out: dict[str, Any] = {
            "taskId": self.task_id,
            "status": self.status,
            "createdAt": self.created_at,
            "lastUpdatedAt": self.last_updated_at,
            "ttl": self.ttl,
        }
        if self.status_message:
            out["statusMessage"] = self.status_message
        return out


class TaskStore:
    """Tasks per scope (session or credential), bounded per scope and in total.

    Limits (env, safe defaults when unset):
    SCRAPELY_MCP_MAX_TASKS_PER_SCOPE (100), SCRAPELY_MCP_MAX_TASKS_TOTAL (10000),
    SCRAPELY_MCP_TASK_RETENTION_SECS (900: finished tasks are dropped this long
    after they finish, or earlier by their ttl).

    Kept results are byte-bounded: SCRAPELY_MCP_MAX_TOOL_RESULT_BYTES per result
    (4 MiB; a larger result is replaced by an error), and
    SCRAPELY_MCP_MAX_TASK_RESULT_BYTES_PER_SCOPE (16 MiB) and
    SCRAPELY_MCP_MAX_TASK_RESULT_BYTES_TOTAL (128 MiB) for all kept results; over
    either, the results of the oldest finished tasks are released first. A
    result is released SCRAPELY_MCP_TASK_RESULT_GRACE_SECS (60) after
    tasks/result first returned it. A released task keeps its status; asking
    for its result again gets a clear error.

    Expired and stale records are swept on create and on task requests (at most
    once per SWEEP_INTERVAL) and by the HTTP gateway's sweeper. Closing a
    session drops its scope and cancels its running tasks (drop_scope).
    """

    SWEEP_INTERVAL = 30.0

    def __init__(
        self,
        max_per_scope: int | None = None,
        max_total: int | None = None,
        retention_secs: float | None = None,
        *,
        max_result_bytes: int | None = None,
        max_scope_bytes: int | None = None,
        max_total_bytes: int | None = None,
        result_grace_secs: float | None = None,
    ) -> None:
        self._scopes: dict[str, dict[str, TaskRecord]] = {}
        self.max_per_scope = max_per_scope or _env_int("SCRAPELY_MCP_MAX_TASKS_PER_SCOPE", 100)
        self.max_total = max_total or _env_int("SCRAPELY_MCP_MAX_TASKS_TOTAL", 10_000)
        self.retention_secs = float(retention_secs or _env_int("SCRAPELY_MCP_TASK_RETENTION_SECS", 900))
        self.max_total_bytes = max_total_bytes or _env_int("SCRAPELY_MCP_MAX_TASK_RESULT_BYTES_TOTAL", 128 * MIB)
        self.max_scope_bytes = min(
            max_scope_bytes or _env_int("SCRAPELY_MCP_MAX_TASK_RESULT_BYTES_PER_SCOPE", 16 * MIB),
            self.max_total_bytes,
        )
        self.max_result_bytes = min(
            max_result_bytes or _env_int("SCRAPELY_MCP_MAX_TOOL_RESULT_BYTES", DEFAULT_MAX_TOOL_RESULT_BYTES),
            self.max_scope_bytes,
        )
        self.result_grace_secs = float(result_grace_secs or _env_int("SCRAPELY_MCP_TASK_RESULT_GRACE_SECS", 60))
        # Bytes of all kept results.
        self.bytes_total = 0
        self._last_sweep = time.monotonic()

    def total(self) -> int:
        return sum(len(b) for b in self._scopes.values())

    def scope_bytes(self, scope: str) -> int:
        return sum(r.result_bytes for r in self._scopes.get(scope, {}).values())

    # -- release and removal -------------------------------------------------

    def _release(self, rec: TaskRecord, reason: str) -> None:
        if rec.result is not None:
            self.bytes_total -= rec.result_bytes
            rec.result = None
            rec.result_bytes = 0
            rec.released = reason

    def _drop(self, bucket: dict[str, TaskRecord], task_id: str) -> None:
        rec = bucket.pop(task_id, None)
        if rec is None:
            return
        self._release(rec, "dropped")
        if rec.runner is not None and not rec.runner.done():
            rec.runner.cancel()

    def _sweep_bucket(self, bucket: dict[str, TaskRecord], now: float) -> int:
        stale = [tid for tid, r in bucket.items() if r.stale(self.retention_secs, now)]
        for tid in stale:
            self._drop(bucket, tid)
        for r in bucket.values():
            if r.delivered_mono is not None and now - r.delivered_mono > self.result_grace_secs:
                self._release(r, "delivered")
        return len(stale)

    def sweep(self) -> int:
        """Drop expired and stale records and release delivered results; returns records dropped."""
        now = time.monotonic()
        self._last_sweep = now
        removed = 0
        for scope in list(self._scopes):
            bucket = self._scopes[scope]
            removed += self._sweep_bucket(bucket, now)
            if not bucket:
                del self._scopes[scope]
        return removed

    def _maybe_sweep(self) -> None:
        if time.monotonic() - self._last_sweep > self.SWEEP_INTERVAL:
            self.sweep()

    def drop_scope(self, scope: str) -> int:
        """Forget every task of a scope (a closed session) and cancel its running tasks."""
        bucket = self._scopes.pop(scope, None)
        if not bucket:
            return 0
        ids = list(bucket)
        for tid in ids:
            self._drop(bucket, tid)
        return len(ids)

    def _make_room(self, scope: str, size: int) -> None:
        """Release the oldest kept results until `size` more bytes fit the scope and total budgets."""
        over_scope = self.scope_bytes(scope) + size - self.max_scope_bytes
        if over_scope > 0:
            kept = sorted(
                (r for r in self._scopes.get(scope, {}).values() if r.result is not None),
                key=lambda r: r.finished_mono or r.created_mono,
            )
            for r in kept:
                if over_scope <= 0:
                    break
                over_scope -= r.result_bytes
                self._release(r, "evicted")
        if self.bytes_total + size > self.max_total_bytes:
            kept = sorted(
                (r for b in self._scopes.values() for r in b.values() if r.result is not None),
                key=lambda r: r.finished_mono or r.created_mono,
            )
            for r in kept:
                if self.bytes_total + size <= self.max_total_bytes:
                    break
                self._release(r, "evicted")

    # -- task API ------------------------------------------------------------

    def create(self, scope: str, ttl: int | None) -> TaskRecord:
        now_mono = time.monotonic()
        self._maybe_sweep()
        bucket = self._scopes.get(scope, {})
        self._sweep_bucket(bucket, now_mono)
        if len(bucket) >= self.max_per_scope:
            finished = [r for r in bucket.values() if r.status in FINISHED]
            if not finished:
                raise TaskError(
                    f"Too many tasks in progress (limit {self.max_per_scope}). Wait for a task to finish "
                    "or cancel one, then retry.",
                    TOO_MANY_TASKS,
                )
            oldest = min(finished, key=lambda r: r.created_mono)
            self._drop(bucket, oldest.task_id)
        if self.total() >= self.max_total:
            self.sweep()
            if self.total() >= self.max_total:
                raise TaskError("The server has too many open tasks. Retry later.", TOO_MANY_TASKS)
        now = _now_iso()
        rec = TaskRecord(task_id=str(uuid.uuid4()), status="working", created_at=now, last_updated_at=now, ttl=ttl)
        self._scopes.setdefault(scope, bucket)[rec.task_id] = rec
        return rec

    def _get(self, scope: str, task_id: Any) -> TaskRecord:
        if not isinstance(task_id, str) or not task_id:
            raise TaskError("taskId is required.")
        bucket = self._scopes.get(scope, {})
        rec = bucket.get(task_id)
        if rec is None:
            raise TaskError(f"Task {task_id} was not found.")
        if rec.expired():
            self._drop(bucket, task_id)
            raise TaskError(f"Task {task_id} was not found.")
        return rec

    def get(self, scope: str, task_id: Any) -> TaskRecord:
        return self._get(scope, task_id)

    def list(self, scope: str) -> list[TaskRecord]:
        bucket = self._scopes.get(scope, {})
        for tid in [t for t, r in bucket.items() if r.expired()]:
            self._drop(bucket, tid)
        return list(bucket.values())

    def complete(self, scope: str, task_id: str, result: dict) -> None:
        rec = self._scopes.get(scope, {}).get(task_id)
        if rec is None or rec.status == "cancelled":
            return
        size = json_size(result)
        if size > self.max_result_bytes:
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": f"The task result was {size:,} bytes, over the {self.max_result_bytes:,} byte "
                        "limit for one result. Request less data and run the task again.",
                    }
                ],
                "isError": True,
            }
            size = json_size(result)
        self._make_room(scope, size)
        rec.status = "failed" if result.get("isError") else "completed"
        rec.result = result
        rec.result_bytes = size
        self.bytes_total += size
        rec.finished_mono = time.monotonic()
        rec.last_updated_at = _now_iso()

    def cancel(self, scope: str, task_id: Any) -> TaskRecord:
        rec = self._get(scope, task_id)
        if rec.status in ("completed", "failed", "cancelled"):
            raise TaskError(f"Task {task_id} cannot be cancelled because it is already {rec.status}.")
        rec.status = "cancelled"
        rec.status_message = "Cancelled at the client's request."
        rec.last_updated_at = _now_iso()
        rec.finished_mono = time.monotonic()
        if rec.runner is not None and not rec.runner.done():
            rec.runner.cancel()
        return rec

    def handle(self, scope: str, method: str, params: dict | None) -> dict:
        params = params or {}
        self._maybe_sweep()
        if method == "tasks/list":
            return {"tasks": [r.public() for r in self.list(scope)], "nextCursor": None}
        if method == "tasks/get":
            return self._get(scope, params.get("taskId")).public()
        if method == "tasks/result":
            rec = self._get(scope, params.get("taskId"))
            if rec.status in ("completed", "failed") and rec.result is None and rec.released:
                raise TaskError(
                    f"The result of task {rec.task_id} is no longer kept on the server (results are released "
                    "shortly after they are fetched, and the oldest ones when memory is short). Run the tool "
                    "again to get a fresh result."
                )
            if rec.status not in ("completed", "failed") or rec.result is None:
                raise TaskError(f"Task {rec.task_id} is not finished yet; its status is {rec.status}.")
            if rec.delivered_mono is None:
                rec.delivered_mono = time.monotonic()
            result = dict(rec.result)
            meta = dict(result.get("_meta") or {})
            meta["related-task"] = {"taskId": rec.task_id}
            meta["io.modelcontextprotocol/related-task"] = {"taskId": rec.task_id}
            result["_meta"] = meta
            return result
        if method == "tasks/cancel":
            return self.cancel(scope, params.get("taskId")).public()
        raise TaskError(f"Unknown task method {method}.")


TASK_METHODS = {"tasks/list", "tasks/get", "tasks/result", "tasks/cancel"}
store = TaskStore()
