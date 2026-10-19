"""Caller identity and plan tier (SPEC 10.5)."""

from __future__ import annotations

from dataclasses import dataclass

from .api import ApiClient
from .log import log, token_fingerprint
from .pricing import TIERS
from .util import TTLCache


@dataclass(frozen=True)
class CallerInfo:
    user_id: str | None = None
    user_plan_tier: str = "FREE"
    is_organization: bool = False


UNKNOWN_CALLER = CallerInfo()
_cache: TTLCache[str, CallerInfo] = TTLCache(200, 3600)


def clear_cache() -> None:
    _cache.clear()


async def get_caller(api: ApiClient) -> CallerInfo:
    token = api.token
    if not token:
        return UNKNOWN_CALLER
    key = token_fingerprint(token)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    try:
        me = await api.get_me()
    except Exception as exc:  # noqa: BLE001 - identity lookup is best effort
        log.classified("caller lookup failed", exc)
        return UNKNOWN_CALLER
    if not isinstance(me, dict) or not me.get("id"):
        return UNKNOWN_CALLER
    plan = me.get("plan") if isinstance(me.get("plan"), dict) else {}
    raw_tier = str(plan.get("tier") or "").upper()
    if raw_tier in TIERS:
        tier = raw_tier
    else:
        if raw_tier:
            log.info("unrecognised plan tier, using FREE", tier=raw_tier)
        tier = "FREE"
    info = CallerInfo(
        user_id=str(me["id"]),
        user_plan_tier=tier,
        is_organization=bool(me.get("organizationOwnerUserId")),
    )
    _cache.set(key, info)
    return info
