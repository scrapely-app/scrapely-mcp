"""Argument validation with the relaxations described in SPEC 6.4."""

from __future__ import annotations

import copy
import json
import os
import re
import time
from contextvars import ContextVar
from typing import Any

import anyio
from anyio.lowlevel import RunVar
from jsonschema import Draft7Validator, validators
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from .errors import ValidationFailure

_DROP_KEYWORDS = ("pattern", "patternProperties", "format", "$schema")
_SUBSCHEMA_LISTS = ("anyOf", "allOf", "oneOf")
_SUBSCHEMA_MAPS = ("properties", "$defs", "definitions", "dependencies", "dependentSchemas")
MAX_UNTRUSTED_SCHEMA_BYTES = 262_144


def _walk(schema: Any, fn) -> Any:
    if not isinstance(schema, dict):
        return schema
    fn(schema)
    for key in _SUBSCHEMA_MAPS:
        sub = schema.get(key)
        if isinstance(sub, dict):
            for v in sub.values():
                _walk(v, fn)
    items = schema.get("items")
    if isinstance(items, dict):
        _walk(items, fn)
    elif isinstance(items, list):
        for v in items:
            _walk(v, fn)
    for key in ("additionalProperties", "not", "contains", "additionalItems", "if", "then", "else", "propertyNames"):
        if isinstance(schema.get(key), dict):
            _walk(schema[key], fn)
    for key in _SUBSCHEMA_LISTS:
        if isinstance(schema.get(key), list):
            for v in schema[key]:
                _walk(v, fn)
    return schema


def relax_required(schema: dict) -> dict:
    """Drop from `required` every property that has a non-null default (in place)."""

    def fix(node: dict) -> None:
        req = node.get("required")
        props = node.get("properties")
        if isinstance(req, list) and isinstance(props, dict):
            node["required"] = [
                r for r in req if not (isinstance(props.get(r), dict) and props[r].get("default") is not None)
            ]

    return _walk(schema, fix)


def advertised_schema(schema: dict) -> dict:
    out = copy.deepcopy(schema)
    out.pop("$schema", None)
    out.pop("$comment", None)

    def strip_comments(node: dict) -> None:
        node.pop("$comment", None)

    _walk(out, strip_comments)
    return relax_required(out)


def _local_ref_targets(schema: dict) -> list[Any]:
    """Every node a local `#/...` reference in the schema points at."""
    refs: list[str] = []

    def collect(node: dict) -> None:
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            refs.append(ref)

    _walk(schema, collect)
    targets: list[Any] = []
    for ref in refs:
        node: Any = schema
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(part)
        if isinstance(node, dict):
            targets.append(node)
    return targets


def validation_schema(schema: dict) -> dict:
    out = copy.deepcopy(schema)

    def strip(node: dict) -> None:
        for k in _DROP_KEYWORDS:
            node.pop(k, None)

    _walk(out, strip)
    # A `$ref` may point at a location the walk does not descend into (any
    # top-level key, for example); strip regex keywords there too. Untrusted
    # schemas must never reach Python's backtracking regex engine.
    seen: set[int] = set()
    pending = _local_ref_targets(out)
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        _walk(node, strip)
        pending.extend(_local_ref_targets(node))
    return relax_required(out)


def _types(schema: dict) -> list[str]:
    t = schema.get("type")
    if isinstance(t, str):
        return [t]
    if isinstance(t, list):
        return [x for x in t if isinstance(x, str)]
    return []


def _resolve(schema: Any, root: dict) -> Any:
    if isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        ref = schema["$ref"]
        if ref.startswith("#/"):
            node: Any = root
            for part in ref[2:].split("/"):
                if not isinstance(node, dict):
                    return schema
                node = node.get(part)
            return node if isinstance(node, dict) else schema
    return schema


def _coerce(value: Any, schema: Any, root: dict, lenient_here: bool) -> bool:
    """Apply stripping/array coercion in place. Returns True if anything changed."""
    schema = _resolve(schema, root)
    if not isinstance(schema, dict):
        return False
    changed = False
    if isinstance(value, dict):
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        if schema.get("additionalProperties") is False and not lenient_here:
            for key in [k for k in value if k not in props]:
                del value[key]
                changed = True
        for key, sub in props.items():
            if key not in value:
                continue
            sub = _resolve(sub, root)
            if not isinstance(sub, dict):
                continue
            if "array" in _types(sub) and not isinstance(value[key], list):
                value[key] = [value[key]]
                changed = True
            if isinstance(value[key], (dict, list)):
                changed = _coerce(value[key], sub, root, False) or changed
        addl = schema.get("additionalProperties")
        if isinstance(addl, dict):
            for key in [k for k in value if k not in props]:
                if isinstance(value[key], (dict, list)):
                    changed = _coerce(value[key], addl, root, False) or changed
    elif isinstance(value, list):
        items = _resolve(schema.get("items"), root)
        if isinstance(items, dict):
            for i, item in enumerate(value):
                if "array" in _types(items) and not isinstance(item, list):
                    value[i] = [item]
                    changed = True
                if isinstance(value[i], (dict, list)):
                    changed = _coerce(value[i], items, root, False) or changed
    return changed


# -- bounded validation ---------------------------------------------------------
#
# Tool arguments come from the client and most input schemas from Actor authors,
# and validation runs in the one shared gateway process. Two bounds keep one call
# from holding it: uniqueItems is checked in linear time (jsonschema falls back to
# an O(n^2) pairwise scan whenever items cannot be sorted: objects, mixed types,
# any bool), and every keyword checks a per-call time budget, so no other keyword
# combination can run long either. Validation also runs off the event loop.

DEFAULT_VALIDATION_BUDGET_SECS = 2.0
VALIDATION_THREADS = 4


def validation_budget_secs() -> float:
    """Time one argument validation may take (SCRAPELY_MCP_VALIDATION_BUDGET_SECS, default 2 s)."""
    raw = os.environ.get("SCRAPELY_MCP_VALIDATION_BUDGET_SECS")
    try:
        value = float(raw) if raw and raw.strip() else DEFAULT_VALIDATION_BUDGET_SECS
    except ValueError:
        return DEFAULT_VALIDATION_BUDGET_SECS
    return value if value > 0 else DEFAULT_VALIDATION_BUDGET_SECS


class _BudgetExceeded(Exception):
    pass


_deadline: ContextVar[float | None] = ContextVar("validation_deadline", default=None)


def _timed(keyword_fn):
    def check(validator, value, instance, schema):
        deadline = _deadline.get()
        if deadline is not None and time.monotonic() > deadline:
            raise _BudgetExceeded
        return keyword_fn(validator, value, instance, schema)

    return check


def _unique_key(value: Any) -> Any:
    """A hashable key equal for exactly the values JSON Schema calls equal (1 == 1.0, True != 1)."""
    if value is True or value is False:
        return ("b", value)
    if value is None:
        return ("n",)
    if isinstance(value, (int, float)):
        return ("x", value)
    if isinstance(value, str):
        return ("s", value)
    if isinstance(value, (list, tuple)):
        return ("a", tuple(_unique_key(v) for v in value))
    if isinstance(value, dict):
        return ("o", frozenset((k, _unique_key(v)) for k, v in value.items()))
    return ("?", repr(value))


def _unique_items(validator, unique, instance, schema):
    if unique and validator.is_type(instance, "array"):
        seen: set = set()
        for item in instance:
            key = _unique_key(item)
            if key in seen:
                yield ValidationError("the array has non-unique elements")
                return
            seen.add(key)


_BoundedDraft7Validator = validators.extend(
    Draft7Validator,
    {
        **{keyword: _timed(fn) for keyword, fn in Draft7Validator.VALIDATORS.items()},
        "uniqueItems": _timed(_unique_items),
    },
)

def check_schema(schema: dict) -> None:
    """Draft7Validator.check_schema (first error wins) under the same bounds: the
    schema itself is author-written, and the meta-schema has uniqueItems on
    `required` and `type`. Defence in depth: today the `items` type error stops
    jsonschema's check before uniqueItems on a mixed-type list."""
    budget = validation_budget_secs()
    reset = _deadline.set(time.monotonic() + budget)
    try:
        error = next(_BoundedDraft7Validator(Draft7Validator.META_SCHEMA).iter_errors(schema), None)
    except (_BudgetExceeded, RecursionError):
        raise SchemaError(f"the input schema is too large or complex to check within {budget:g}s") from None
    finally:
        _deadline.reset(reset)
    if error is not None:
        raise SchemaError.create_from(error)


_threads: RunVar[anyio.CapacityLimiter] = RunVar("validation_threads")


def _thread_limiter() -> anyio.CapacityLimiter:
    try:
        return _threads.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(VALIDATION_THREADS)
        _threads.set(limiter)
        return limiter


def _too_complex(budget: float) -> ValidationFailure:
    return ValidationFailure(
        [
            {
                "keyword": "maxValidationTime",
                "instancePath": "",
                "schemaPath": "#",
                "params": {},
                "message": f"the arguments are too large or complex to check against this tool's input schema "
                f"within {budget:g}s; send a smaller input",
            }
        ]
    )


_MISSING_RE = re.compile(r"'([^']+)' is a required property")
_ADDITIONAL_RE = re.compile(r"\('([^']+)'")


class ArgValidator:
    def __init__(self, schema: dict, *, lenient_top_level: bool = False) -> None:
        self.schema = validation_schema(schema)
        self.lenient_top_level = lenient_top_level
        if lenient_top_level:
            self.schema = dict(self.schema)
            self.schema.pop("additionalProperties", None)
        check_schema(self.schema)
        # An empty registry: only references inside the schema resolve. Without
        # it jsonschema fetches remote `$ref` URLs over HTTP from the gateway.
        self._validator = _BoundedDraft7Validator(self.schema, registry=Registry())

    async def validate_async(self, args: Any) -> Any:
        """validate() in a worker thread, so a slow validation does not hold the event loop."""
        return await anyio.to_thread.run_sync(self.validate, args, limiter=_thread_limiter())

    def validate(self, args: Any) -> Any:
        budget = validation_budget_secs()
        reset = _deadline.set(time.monotonic() + budget)
        try:
            return self._validate(args)
        except (_BudgetExceeded, RecursionError):
            raise _too_complex(budget) from None
        finally:
            _deadline.reset(reset)

    def _validate(self, args: Any) -> Any:
        if not isinstance(args, dict):
            raise ValidationFailure(
                [
                    {
                        "keyword": "type",
                        "instancePath": "",
                        "schemaPath": "#/type",
                        "params": {"type": "object"},
                        "message": "arguments must be an object",
                    }
                ]
            )
        for _ in range(5):
            if not _coerce(args, self.schema, self.schema, self.lenient_top_level):
                break
        try:
            errors = sorted(self._validator.iter_errors(args), key=lambda e: (len(e.path), list(map(str, e.path))))
        except Unresolvable as exc:
            raise ValidationFailure(
                [
                    {
                        "keyword": "$ref",
                        "instancePath": "",
                        "schemaPath": "#",
                        "params": {},
                        "message": f"the tool's input schema has an unresolvable reference: {exc}",
                    }
                ]
            ) from exc
        if errors:
            raise ValidationFailure([_to_dict(e) for e in errors])
        return args


def _to_dict(err) -> dict:
    path = "".join(f"/{p}" for p in err.absolute_path)
    params: dict[str, Any] = {}
    if err.validator == "required":
        m = _MISSING_RE.search(err.message)
        if m:
            params["missingProperty"] = m.group(1)
    elif err.validator == "additionalProperties":
        m = _ADDITIONAL_RE.search(err.message)
        if m:
            params["additionalProperty"] = m.group(1)
    else:
        params[str(err.validator)] = err.validator_value if not isinstance(err.validator_value, dict) else None
    return {
        "keyword": err.validator,
        "instancePath": path,
        "schemaPath": "#/" + "/".join(str(p) for p in err.absolute_schema_path),
        "params": params,
        "message": err.message,
    }


def schema_size(schema: dict) -> int:
    return len(json.dumps(schema, separators=(",", ":")).encode())
