"""Public Store links and optional personalised dashboard links (SPEC 10.7)."""

from __future__ import annotations

from .config import Settings
from .identity import CallerInfo


def console_enabled(settings: Settings, token: str | None) -> bool:
    prefix = settings.console_token_prefix
    return bool(prefix and token and token.startswith(prefix))


def console_url(settings: Settings, caller: CallerInfo | None, path: str) -> str:
    base = settings.console_base_url.rstrip("/")
    if caller is not None and caller.is_organization and caller.user_id:
        base = f"{base}/organization/{caller.user_id}"
    return f"{base}{path}"


def store_actor_url(settings: Settings, username: str, name: str) -> str:
    return f"{settings.store_url.rstrip('/')}/{username}/{name}"


def store_user_url(settings: Settings, username: str) -> str:
    return f"{settings.store_url.rstrip('/')}/{username}"
