"""Actor definitions, the ownership-aware cache, tool modes and Actor tools (SPEC 8)."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from .api import ApiClient
from .identity import get_caller
from .log import log
from .util import TTLCache

README_LIMIT = 5000
DESCRIPTION_LIMIT = 500
ENUM_CHAR_LIMIT = 2000
TOOL_NAME_LIMIT = 64

MODE_RUN = "RUN"
MODE_MCP = "MCP"
MODE_STANDBY_WITHOUT_MCP = "STANDBY_WITHOUT_MCP"


@dataclass
class ActorDefinition:
    id: str
    username: str
    name: str
    title: str | None
    description: str | None
    readme_summary: str | None
    default_memory: int | None
    picture_url: str | None
    is_public: bool
    user_id: str | None
    standby_enabled: bool
    input_schema: dict | None
    readme: str
    mcp_path: str | None
    build_tag: str | None
    raw: dict

    @property
    def full_name(self) -> str:
        return f"{self.username}/{self.name}" if self.username else self.name

    @property
    def mode(self) -> str:
        return tool_mode(self.standby_enabled, self.mcp_path, self.input_schema)


def truncate_readme(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= README_LIMIT:
        return text
    head = text[:README_LIMIT]
    rest = text[README_LIMIT:]
    headings = [ln.strip() for ln in rest.splitlines() if ln.lstrip().startswith("#")]
    out = head + "\n\n[README truncated at 5,000 characters.]"
    if headings:
        out += " Remaining sections:\n" + "\n".join(headings)
    return out


def parse_definition(actor: Any) -> ActorDefinition | None:
    if not isinstance(actor, dict):
        return None
    definition = actor.get("defaultBuildActorDefinition")
    if not isinstance(definition, dict):
        return None
    schema = definition.get("input")
    if not (isinstance(schema, dict) and "type" in schema and "properties" in schema):
        schema = None
    standby = actor.get("actorStandby") if isinstance(actor.get("actorStandby"), dict) else {}
    run_opts = actor.get("defaultRunOptions") if isinstance(actor.get("defaultRunOptions"), dict) else {}
    mem = run_opts.get("memoryMbytes")
    return ActorDefinition(
        id=str(actor.get("id") or ""),
        username=str(actor.get("username") or ""),
        name=str(actor.get("name") or ""),
        title=actor.get("title"),
        description=actor.get("description"),
        readme_summary=actor.get("readmeSummary"),
        default_memory=mem if isinstance(mem, int) else None,
        picture_url=actor.get("pictureUrl"),
        is_public=bool(actor.get("isPublic")),
        user_id=actor.get("userId"),
        standby_enabled=standby.get("isEnabled") is True,
        input_schema=schema,
        readme=truncate_readme(definition.get("readme")),
        mcp_path=_pick_mcp_path(definition.get("webServerMcpPath")),
        build_tag=definition.get("buildTag"),
        raw=actor,
    )


def _pick_mcp_path(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return "/mcp" if "/mcp" in parts else parts[0]


def tool_mode(standby: bool, mcp_path: str | None, schema: dict | None) -> str:
    if not standby:
        return MODE_RUN
    if mcp_path:
        return MODE_MCP
    props = (schema or {}).get("properties")
    if isinstance(props, dict) and props:
        return MODE_RUN
    return MODE_STANDBY_WITHOUT_MCP


# -- cache -----------------------------------------------------------------

_cache: TTLCache[str, ActorDefinition] = TTLCache(500, 1800)


def clear_cache() -> None:
    _cache.clear()


async def get_definition(api: ApiClient, ref: str) -> ActorDefinition | None:
    caller = None
    hit = _cache.get(ref)
    if hit is not None:
        caller = await get_caller(api)
        if hit.is_public:
            allowed = api.token is None or (caller.user_id is not None and caller.user_id != hit.user_id)
        else:
            allowed = caller.user_id is not None and caller.user_id == hit.user_id
        if allowed:
            return hit
    actor = await api.get_actor(ref)
    definition = parse_definition(actor)
    if definition is None:
        return None
    if caller is None and api.token:
        caller = await get_caller(api)
    owner_view = "hasSuccessfulBuild" in actor or (
        caller is not None and caller.user_id is not None and caller.user_id == definition.user_id
    )
    if not (definition.is_public and owner_view):
        _cache.set(ref, definition)
    return definition


# -- MCP-server URL ----------------------------------------------------------


class UnsafeMcpUrl(ValueError):
    pass


def mcp_server_url(standby_base: str, username: str, name: str, mcp_path: str) -> str:
    base = f"{standby_base.rstrip('/')}/v2/{username}/{name}/"
    rel = mcp_path[1:] if mcp_path.startswith("/") else mcp_path
    target = urljoin(base, rel)
    b, t = urlsplit(base), urlsplit(target)
    if t.username or t.password or "@" in t.netloc:
        raise UnsafeMcpUrl("MCP path carries user info")
    if (t.scheme, t.netloc.lower()) != (b.scheme, b.netloc.lower()):
        raise UnsafeMcpUrl("MCP path leaves the Actor's standby origin")
    if not (t.path + "/").startswith(b.path) and t.path != b.path.rstrip("/"):
        raise UnsafeMcpUrl("MCP path leaves the Actor's standby path")
    if ".." in t.path.split("/"):
        raise UnsafeMcpUrl("MCP path is not normalised")
    return target


# -- naming ----------------------------------------------------------------


def _hash4(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:4]


def cap_name(name: str, full: str) -> str:
    if len(name) <= TOOL_NAME_LIMIT:
        return name
    return name[:59] + "-" + _hash4(full)


def actor_tool_name(full_name: str) -> str:
    if "/" not in full_name:
        log.warning("Actor reference has no username, using the bare name as tool name", actor=full_name)
        return cap_name(full_name, full_name)
    username, name = full_name.split("/", 1)
    raw = f"{username.replace('.', '-dot-')}--{name}"
    return cap_name(raw, full_name)


def proxied_tool_names(full_name: str, tool_names: list[str]) -> dict[str, str]:
    username, name = full_name.split("/", 1) if "/" in full_name else ("", full_name)
    escaped = username.replace(".", "-dot-")
    plain = {t: f"{escaped}--{name}--{t}" for t in tool_names}
    if all(len(v) <= TOOL_NAME_LIMIT for v in plain.values()):
        return plain
    short = escaped[:8].rstrip("-")
    out: dict[str, str] = {}
    for t in tool_names:
        capped = f"{short}--{name}--{t}"
        full = f"{escaped}--{name}--{t}"
        if len(capped) > TOOL_NAME_LIMIT:
            capped = capped[:59] + "-" + _hash4(full)
        out[t] = capped
    return out


# -- schema shaping ----------------------------------------------------------

_KEEP = ("title", "description", "type", "enum", "default", "prefill", "properties", "items", "required")
_HTTP_METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE", "TRACE", "OPTIONS", "CONNECT", "PATCH"]


def shorten_description(text: Any) -> Any:
    if isinstance(text, str) and len(text) > DESCRIPTION_LIMIT:
        return text[:DESCRIPTION_LIMIT] + "..."
    return text


def shorten_enum(values: Any) -> list | None:
    if not isinstance(values, list):
        return None
    out: list = []
    total = 0
    for v in values:
        if v == "":
            continue
        length = len(str(v))
        if total + length > ENUM_CHAR_LIMIT:
            break
        total += length
        out.append(v)
    return out or None


def reduce_property(p: dict) -> dict:
    out = {k: p[k] for k in _KEEP if k in p and p[k] is not None}
    if "description" in out:
        out["description"] = shorten_description(out["description"])
    if "enum" in out:
        e = shorten_enum(out["enum"])
        if e is None:
            out.pop("enum")
        else:
            out["enum"] = e
    items = out.get("items")
    if isinstance(items, dict) and "enum" in items:
        items = dict(items)
        e = shorten_enum(items["enum"])
        if e is None:
            items.pop("enum")
        else:
            items["enum"] = e
        out["items"] = items
    return out


def details_input_schema(schema: Any) -> dict:
    """Input schema as returned by fetch-actor-details (SPEC 7.3)."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = {k: v for k, v in schema.items() if k not in ("properties", "$schema", "schemaVersion")}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    out["properties"] = {n: reduce_property(p) if isinstance(p, dict) else p for n, p in props.items()}
    out.setdefault("type", "object")
    return out


def _json_type(value: Any) -> str | None:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    return None


def _request_item(first_key: str, first_desc: str) -> dict:
    return {
        "type": "object",
        "title": "Request",
        "properties": {
            first_key: {"type": "string", "title": first_key, "description": first_desc},
            "method": {"type": "string", "title": "HTTP method", "enum": _HTTP_METHODS, "default": "GET"},
            "payload": {"type": "string", "title": "Payload", "description": "Request body for POST/PUT requests."},
            "userData": {"type": "object", "title": "User data", "description": "Custom data passed along with the request."},
            "headers": {"type": "object", "title": "Headers", "description": "HTTP headers to send with the request."},
        },
        "required": [first_key],
    }


def _add_editor_structure(p: dict) -> None:
    editor = p.get("editor")
    typ = p.get("type")
    if typ == "object" and editor == "proxy":
        p["properties"] = {
            "useScrapelyProxy": {
                "type": "boolean",
                "title": "Use Scrape.ly Proxy",
                "description": "Route requests through Scrape.ly Proxy.",
                "default": True,
            },
            "scrapelyProxyGroups": {
                "type": "array",
                "title": "Proxy groups",
                "description": "Proxy groups to use, for example RESIDENTIAL.",
                "items": {"type": "string", "enum": ["RESIDENTIAL", "DATACENTER"]},
            },
            "proxyUrls": {
                "type": "array",
                "title": "Custom proxy URLs",
                "description": "Your own proxy URLs, used instead of Scrape.ly Proxy.",
                "items": {"type": "string"},
            },
        }
        p["required"] = ["useScrapelyProxy"]
    elif typ == "array":
        if editor == "requestListSources":
            p["items"] = _request_item("url", "URL to visit.")
        elif editor == "pseudoUrls":
            p["items"] = _request_item("purl", "Pseudo-URL pattern, for example https://example.com/[.*].")
        elif editor == "globs":
            p["items"] = _request_item("glob", "Glob pattern, for example https://example.com/**.")
        elif editor == "keyValue":
            p["items"] = {
                "type": "object",
                "title": "Entry",
                "properties": {
                    "key": {"type": "string", "title": "Key"},
                    "value": {"type": "string", "title": "Value"},
                },
            }
        elif editor == "resourcePicker":
            p["items"] = {
                "type": "string",
                "title": "Resource ID",
                "description": "ID of a dataset, key-value store or request list.",
            }
        items = p.get("items") if isinstance(p.get("items"), dict) else None
        if items is None or not items.get("type"):
            inferred = None
            if items and items.get("type"):
                inferred = items["type"]
            if inferred is None and isinstance(p.get("prefill"), list) and p["prefill"]:
                inferred = _json_type(p["prefill"][0])
            if inferred is None and isinstance(p.get("default"), list) and p["default"]:
                inferred = _json_type(p["default"][0])
            if inferred is None:
                inferred = {
                    "requestListSources": "object",
                    "json": "object",
                    "globs": "object",
                    "stringList": "string",
                    "select": "string",
                }.get(str(editor))
            if inferred:
                new_items = dict(items or {})
                new_items["type"] = inferred
                new_items.setdefault("title", "Item")
                new_items.setdefault("description", f"One entry of the {p.get('title') or 'list'}.")
                p["items"] = new_items


def derive_tool_schema(schema: dict | None) -> dict:
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict) or not schema["properties"]:
        out: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    else:
        src = copy.deepcopy(schema)
        required = [r for r in src.get("required") or [] if isinstance(r, str)]
        props: dict[str, Any] = {}
        for name, raw in src["properties"].items():
            p = dict(raw) if isinstance(raw, dict) else {}
            _add_editor_structure(p)
            p = reduce_property(p)
            desc = p.get("description") or ""
            enum = p.get("enum")
            if not enum and isinstance(p.get("items"), dict):
                enum = p["items"].get("enum")
            extra = []
            if enum:
                extra.append("Possible values: " + ", ".join(str(v) for v in enum[:20]))
            example = p.get("prefill", p.get("default"))
            if example not in (None, "", [], {}):
                p["examples"] = example if isinstance(example, list) else [example]
                extra.append(f"Example: {_short_json(example)}")
            if extra:
                desc = (desc + " " if desc else "") + ". ".join(extra) + "."
            if name in required:
                desc = "**REQUIRED** " + desc
            if desc:
                p["description"] = desc
            props[name.replace(".", "-dot-")] = p
        out = {k: v for k, v in src.items() if k not in ("properties", "required", "schemaVersion", "$ref", "$schema")}
        out["type"] = "object"
        out["properties"] = props
        out["required"] = [r.replace(".", "-dot-") for r in required]
    out["properties"]["waitSecs"] = {
        "type": "integer",
        "minimum": 0,
        "maximum": 45,
        "description": (
            "Seconds to wait for the run to finish before returning (0 to 45, default 30). "
            "Not passed to the Actor."
        ),
    }
    return out


def _short_json(value: Any) -> str:
    import json

    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 200 else text[:200] + "..."


def decode_dot_keys(args: dict) -> dict:
    return {k.replace("-dot-", "."): v for k, v in args.items()}


def actor_memory_cap(definition: ActorDefinition, max_actor_memory_bytes: int | None) -> int:
    """Computed for information only; never sent as a run option (product decision)."""
    cap = max_actor_memory_bytes // 1_048_576 if max_actor_memory_bytes else 4096
    return min(cap, definition.default_memory or 1024)
