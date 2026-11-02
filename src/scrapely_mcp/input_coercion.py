"""Actor input fields that an agent runtime mangled become the type the input schema asks for.

Before Actor input is sent (call-actor, run-saved-input, create-actor-task, update-actor-task), two conversions run on
top-level fields only. Neither changes a value the schema already accepts.

1. Unambiguous scalar strings. Some agent runtimes send every scalar tool argument as a string ("20", "true"), and the
   API then refuses the run with "Field input.maxItems must be integer". A field whose schema type is exactly one of
   integer, number or boolean (optionally with null) and whose value is a string in that type's plain JSON spelling is
   converted:

   - integer: "20", "-3" (no sign "+", no spaces, no leading zeros, no decimals, within +-(2**53 - 1));
   - number: an integer as above, or "1.5", "-0.25", "2e3" (finite only);
   - boolean: exactly "true" or "false".

   Anything else is left as it is and the API's validation decides: "20.5" for an integer, "yes" or "1" for a boolean,
   " 20", "", a field whose type allows a string, a secret field, nested values.

2. List wrappers. Some agent runtimes turn a JSON list into an XML-style wrapper object: ["x"] arrives as {"item": "x"},
   ["a", "b"] as {"item": ["a", "b"]}, and sometimes wrapped twice, {"item": {"item": ["a", "b"]}} (prod, 2026-11-02:
   call-actor, create-actor-task and run-saved-input for twitterHandles, searchTerms, startUrls, chains, traderTypes,
   profiles). A field whose schema type is exactly array (optionally with null) and whose value is an object with the
   single key "item" or "items" is unwrapped (at most 4 levels): a list inside is the value, any other value becomes a
   one-element list. Left as it is: an object with other or more keys, {"item": null}, a field whose type also allows
   an object or a string, a secret field, a field whose list items are objects with an "item" or "items" property
   (there the wrapper could be a real item), wrappers inside lists or nested objects.
"""

from __future__ import annotations

import math
import re
from typing import Any

_INT_RE = re.compile(r"-?(?:0|[1-9][0-9]*)")
_NUM_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_MAX_SAFE = 2**53 - 1
_NO = object()
_WRAPPER_KEYS = ("item", "items")
_MAX_WRAP_DEPTH = 4


def _types(prop: dict) -> list[str]:
    t = prop.get("type")
    if isinstance(t, str):
        return [t]
    if isinstance(t, list) and all(isinstance(x, str) for x in t):
        return list(t)
    return []


def _convert(value: str, kind: str) -> Any:
    if kind == "boolean":
        return {"true": True, "false": False}.get(value, _NO)
    if kind == "integer" or (kind == "number" and _INT_RE.fullmatch(value)):
        if not _INT_RE.fullmatch(value):
            return _NO
        n = int(value)
        return n if abs(n) <= _MAX_SAFE else _NO
    if kind == "number" and _NUM_RE.fullmatch(value):
        f = float(value)
        return f if math.isfinite(f) else _NO
    return _NO


def coerce_scalar_strings(run_input: Any, schema: Any) -> tuple[Any, list[str]]:
    """Return (input, converted field names). The input passed in is never changed; a copy is returned when a
    field was converted."""
    if not isinstance(run_input, dict) or not isinstance(schema, dict):
        return run_input, []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return run_input, []
    out: dict | None = None
    changed: list[str] = []
    for key, value in run_input.items():
        if not isinstance(value, str):
            continue
        prop = props.get(key)
        if not isinstance(prop, dict) or prop.get("isSecret") is True:
            continue
        kinds = [t for t in _types(prop) if t != "null"]
        if len(kinds) != 1 or kinds[0] not in ("integer", "number", "boolean"):
            continue
        new = _convert(value, kinds[0])
        if new is _NO:
            continue
        if out is None:
            out = dict(run_input)
        out[key] = new
        changed.append(key)
    return (out if out is not None else run_input), changed


def _unwrap(value: Any) -> Any:
    """The value inside {"item": ...} / {"items": ...} wrappers, or _NO when it is not one (or wraps nothing)."""
    depth = 0
    while isinstance(value, dict) and len(value) == 1 and next(iter(value)) in _WRAPPER_KEYS:
        if depth == _MAX_WRAP_DEPTH:
            return _NO
        value = next(iter(value.values()))
        depth += 1
    if depth == 0 or value is None:
        return _NO
    return value if isinstance(value, list) else [value]


def _items_use_wrapper_keys(prop: dict) -> bool:
    items = prop.get("items")
    props = items.get("properties") if isinstance(items, dict) else None
    return isinstance(props, dict) and any(k in props for k in _WRAPPER_KEYS)


def unwrap_list_wrappers(run_input: Any, schema: Any) -> tuple[Any, list[str]]:
    """Return (input, unwrapped field names). The input passed in is never changed; a copy is returned when a field
    was unwrapped."""
    if not isinstance(run_input, dict) or not isinstance(schema, dict):
        return run_input, []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return run_input, []
    out: dict | None = None
    changed: list[str] = []
    for key, value in run_input.items():
        if not isinstance(value, dict):
            continue
        prop = props.get(key)
        if not isinstance(prop, dict) or prop.get("isSecret") is True:
            continue
        kinds = [t for t in _types(prop) if t != "null"]
        if kinds != ["array"] or _items_use_wrapper_keys(prop):
            continue
        new = _unwrap(value)
        if new is _NO:
            continue
        if out is None:
            out = dict(run_input)
        out[key] = new
        changed.append(key)
    return (out if out is not None else run_input), changed
