"""Argument validation with the relaxations described in SPEC 6.4."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from jsonschema import Draft7Validator

from .errors import ValidationFailure

_DROP_KEYWORDS = ("pattern", "patternProperties", "format", "$schema")
_SUBSCHEMA_LISTS = ("anyOf", "allOf", "oneOf")
_SUBSCHEMA_MAPS = ("properties", "$defs", "definitions")
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
    for key in ("additionalProperties", "not", "contains", "additionalItems", "if", "then", "else"):
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


def validation_schema(schema: dict) -> dict:
    out = copy.deepcopy(schema)

    def strip(node: dict) -> None:
        for k in _DROP_KEYWORDS:
            node.pop(k, None)

    _walk(out, strip)
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


_MISSING_RE = re.compile(r"'([^']+)' is a required property")
_ADDITIONAL_RE = re.compile(r"\('([^']+)'")


class ArgValidator:
    def __init__(self, schema: dict, *, lenient_top_level: bool = False) -> None:
        self.schema = validation_schema(schema)
        self.lenient_top_level = lenient_top_level
        if lenient_top_level:
            self.schema = dict(self.schema)
            self.schema.pop("additionalProperties", None)
        Draft7Validator.check_schema(self.schema)
        self._validator = Draft7Validator(self.schema)

    def validate(self, args: Any) -> Any:
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
        errors = sorted(self._validator.iter_errors(args), key=lambda e: (len(e.path), list(map(str, e.path))))
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
