"""Signed download URLs for key-value store records (public API docs: Signed URLs)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from .api import ApiClient, seg
from .config import Settings
from .log import log

BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
DEFAULT_EXPIRES_SECS = 24 * 3600


def base62(n: int) -> str:
    if n == 0:
        return "0"
    out = ""
    while n > 0:
        n, r = divmod(n, 62)
        out = BASE62[r] + out
    return out


def hmac_base62(secret: str, message: str) -> str:
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return base62(int(digest[:30], 16))


def sign_record(secret: str, store_id: str, key: str, expires_in: int = DEFAULT_EXPIRES_SECS) -> str:
    expires_at = int(time.time() * 1000) + expires_in * 1000
    path = f"/v2/key-value-stores/{store_id}/records/{seg(key)}"
    # Version 2 also binds the query; a record URL carries none besides `signature`.
    message = f"2.{expires_at}.{store_id}.GET.{path}.{key}."
    token = f"2.{expires_at}.{hmac_base62(secret, message)}"
    return base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")


async def record_url(api: ApiClient, settings: Settings, store_ref: str, key: str) -> str:
    """Downloadable record URL on the public API base, signed when the store exposes a key."""
    unsigned = f"{settings.public_api_base_url}/v2/key-value-stores/{seg(store_ref)}/records/{seg(key)}"
    try:
        store = await api.get_or_none(f"/v2/key-value-stores/{store_ref.replace('/', '~')}")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read store for URL signing", reason=str(exc))
        return unsigned
    if not isinstance(store, dict) or not store.get("urlSigningSecretKey"):
        return unsigned
    store_id = str(store.get("id") or store_ref)
    try:
        sig = sign_record(store["urlSigningSecretKey"], store_id, key)
    except Exception as exc:  # noqa: BLE001
        log.warning("record URL signing failed", reason=str(exc))
        return unsigned
    return f"{settings.public_api_base_url}/v2/key-value-stores/{seg(store_id)}/records/{seg(key)}?signature={sig}"
