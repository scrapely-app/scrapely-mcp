"""Unambiguous scalar strings in Actor input become the type the input schema asks for.

Some agent runtimes send every scalar tool argument as a string ("20", "true"), and the API then refuses the
run with "Field input.maxItems must be integer". Before Actor input is sent (call-actor, run-saved-input,
create-actor-task, update-actor-task), a top-level field whose schema type is exactly one of integer, number or
boolean (optionally with null) and whose value is a string in that type's plain JSON spelling is converted:

- integer: "20", "-3" (no sign "+", no spaces, no leading zeros, no decimals, within +-(2**53 - 1));
- number: an integer as above, or "1.5", "-0.25", "2e3" (finite only);
- boolean: exactly "true" or "false".

Anything else is left as it is and the API's validation decides: "20.5" for an integer, "yes" or "1" for a boolean,
" 20", "", a field whose type allows a string, a secret field, nested values. Wrappers such as {"item": [...]} are
never unwrapped.
"""

from __future__ import annotations

import math
import re
from typing import Any

_INT_RE = re.compile(r"-?(?:0|[1-9][0-9]*)")
_NUM_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_MAX_SAFE = 2**53 - 1
_NO = object()


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
