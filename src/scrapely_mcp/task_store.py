"""In-memory store for protocol-level tasks (SPEC 9)."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

INVALID_PARAMS = -32602


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

    def expired(self) -> bool:
        return self.ttl is not None and (time.monotonic() - self.created_mono) * 1000 > self.ttl

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
    def __init__(self) -> None:
        self._scopes: dict[str, dict[str, TaskRecord]] = {}

    def create(self, scope: str, ttl: int | None) -> TaskRecord:
        now = _now_iso()
        rec = TaskRecord(task_id=str(uuid.uuid4()), status="working", created_at=now, last_updated_at=now, ttl=ttl)
        self._scopes.setdefault(scope, {})[rec.task_id] = rec
        return rec

    def _get(self, scope: str, task_id: Any) -> TaskRecord:
        if not isinstance(task_id, str) or not task_id:
            raise TaskError("taskId is required.")
        bucket = self._scopes.get(scope, {})
        rec = bucket.get(task_id)
        if rec is None:
            raise TaskError(f"Task {task_id} was not found.")
        if rec.expired():
            bucket.pop(task_id, None)
            raise TaskError(f"Task {task_id} was not found.")
        return rec

    def get(self, scope: str, task_id: Any) -> TaskRecord:
        return self._get(scope, task_id)

    def list(self, scope: str) -> list[TaskRecord]:
        bucket = self._scopes.get(scope, {})
        for tid in [t for t, r in bucket.items() if r.expired()]:
            bucket.pop(tid, None)
        return list(bucket.values())

    def complete(self, scope: str, task_id: str, result: dict) -> None:
        rec = self._scopes.get(scope, {}).get(task_id)
        if rec is None or rec.status == "cancelled":
            return
        rec.status = "failed" if result.get("isError") else "completed"
        rec.result = result
        rec.last_updated_at = _now_iso()

    def cancel(self, scope: str, task_id: Any) -> TaskRecord:
        rec = self._get(scope, task_id)
        if rec.status in ("completed", "failed", "cancelled"):
            raise TaskError(f"Task {task_id} cannot be cancelled because it is already {rec.status}.")
        rec.status = "cancelled"
        rec.status_message = "Cancelled at the client's request."
        rec.last_updated_at = _now_iso()
        if rec.runner is not None and not rec.runner.done():
            rec.runner.cancel()
        return rec

    def handle(self, scope: str, method: str, params: dict | None) -> dict:
        params = params or {}
        if method == "tasks/list":
            return {"tasks": [r.public() for r in self.list(scope)], "nextCursor": None}
        if method == "tasks/get":
            return self._get(scope, params.get("taskId")).public()
        if method == "tasks/result":
            rec = self._get(scope, params.get("taskId"))
            if rec.status not in ("completed", "failed") or rec.result is None:
                raise TaskError(f"Task {rec.task_id} is not finished yet; its status is {rec.status}.")
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
