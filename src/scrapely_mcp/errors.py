"""Exception types shared across the server."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """An error response from the Scrape.ly API."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        type: str | None = None,  # noqa: A002 - mirrors the API field name
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.type = type
        self.data = data

    def __str__(self) -> str:
        suffix = f" ({self.type})" if self.type else ""
        return f"{self.message}{suffix}"


class ToolError(Exception):
    """A failure that becomes an `isError: true` tool result.

    `user=True` marks a user error (bad input, not found, 4xx): telemetry status
    SOFT_FAIL. `user=False` marks a server error: telemetry status FAILED.
    """

    def __init__(
        self,
        message: str,
        *,
        user: bool = True,
        category: str | None = None,
        http_status: int | None = None,
        detail: str | None = None,
        structured: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.user = user
        self.category = category or ("INVALID_INPUT" if user else "INTERNAL_ERROR")
        self.http_status = http_status
        self.detail = detail
        self.structured = structured


class ValidationFailure(Exception):
    def __init__(self, errors: list[dict]) -> None:
        super().__init__(errors[0]["message"] if errors else "invalid arguments")
        self.errors = errors


class RemoteUnreachable(Exception):
    """An MCP-server Actor could not be reached."""
