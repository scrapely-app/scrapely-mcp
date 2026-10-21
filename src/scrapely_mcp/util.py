"""Small helpers: input normalisation, timestamps, caches, JSON."""

from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

_QUOTES = "`\"'“”‘’"
_QUOTES_NO_APOS = "`\"“”‘’"

ALNUM17_RE = re.compile(r"^[A-Za-z0-9]{17}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def strip_quotes(value: str) -> str:
    return value.strip().strip(_QUOTES).strip()


def strip_record_key(value: str) -> str:
    return value.strip().strip(_QUOTES_NO_APOS).strip()


def normalize_actor_name(value: str) -> str:
    """Quote stripping, `~` -> `/`, legacy `-slash-` -> `/` (SPEC 8.1)."""
    fixed = strip_quotes(value).replace("~", "/").replace("-slash-", "/")
    return fixed


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def normalize_ts(value: Any) -> Any:
    """UTC ISO-8601 with millisecond precision and a Z suffix."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return value
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return value
    else:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def normalize_fields(fields: Any) -> list[str]:
    """`a/b/0/c` -> `a.b.c`, drop pure-digit segments and empties, de-duplicate."""
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(fields, list):
        return out
    for f in fields:
        if not isinstance(f, str):
            continue
        parts = [p for p in f.replace("/", ".").split(".") if p and not p.isdigit()]
        name = ".".join(parts)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def drop_empty(d: dict) -> dict:
    """Remove None, empty strings, empty lists and empty dicts (keep 0 / False)."""
    return {k: v for k, v in d.items() if v is not None and v != "" and v != [] and v != {}}


def fmt_number(value: float | int) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return f"{value:.10f}".rstrip("0").rstrip(".")
    return str(value)


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def json_size(value: Any) -> int:
    """UTF-8 size of the compact JSON form of `value` (what it costs to keep or send)."""
    return utf8_len(compact_json(value))


def truncate_text(value: str, limit: int, note: str = " [truncated]") -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + note


K = TypeVar("K")
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """A small LRU cache with per-entry expiry."""

    def __init__(self, maxsize: int, ttl: float) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K) -> V | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: K, value: V) -> None:
        self._data[key] = (time.monotonic() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def pop(self, key: K) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


def split_list(value: Any) -> list[str] | None:
    """Normalise a list-ish selector value. None = absent, [] = explicitly empty."""
    if value is None:
        return None
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if item is None:
                continue
            for p in str(item).split(","):
                if p.strip():
                    out.append(p.strip())
        return out
    return None
