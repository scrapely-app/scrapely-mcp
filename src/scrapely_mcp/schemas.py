"""Binding input/output schemas (package data), with `$ref`s inlined for clients."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from importlib import resources
from typing import Any


@lru_cache(maxsize=4)
def _raw(name: str) -> dict:
    return json.loads(resources.files("scrapely_mcp.data").joinpath(name).read_text("utf-8"))


def _inline(node: Any, root: dict, depth: int = 0) -> Any:
    if depth > 20:
        return node
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            target: Any = root
            for part in ref[2:].split("/"):
                target = target[part]
            merged = copy.deepcopy(target)
            for k, v in node.items():
                if k not in ("$ref", "$comment"):
                    merged[k] = v
            return _inline(merged, root, depth + 1)
        return {k: _inline(v, root, depth + 1) for k, v in node.items() if k != "$comment"}
    if isinstance(node, list):
        return [_inline(v, root, depth + 1) for v in node]
    return node


def input_schema(tool: str) -> dict:
    raw = _raw("tool-inputs.json")
    alias = {"search-actors-widget": "search-actors", "get-actor-run-widget": "get-actor-run"}
    node = raw[alias.get(tool, tool)]
    out = _inline(node, raw)
    out.pop("$defs", None)
    return out


def output_schema(tool: str) -> dict:
    raw = _raw("tool-outputs.json")
    alias = {
        "fetch-actor-details-widget": "fetch-actor-details",
        "get-key-value-store-list": "get-dataset-list",
    }
    node = raw[alias.get(tool, tool)]
    out = _inline(node, raw)
    if "allOf" in out and len(out["allOf"]) == 1 and isinstance(out["allOf"][0], dict):
        base = out.pop("allOf")[0]
        base.update(out)
        out = base
    return out


def run_response_schema() -> dict:
    return output_schema("call-actor")


def pricing_schema() -> dict:
    raw = _raw("tool-outputs.json")
    return _inline(raw["$defs"]["Pricing"], raw)
