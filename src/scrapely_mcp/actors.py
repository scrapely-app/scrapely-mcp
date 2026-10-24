"""Actor definitions, the ownership-aware cache, tool modes and Actor tools (SPEC 8)."""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from .api import ApiClient
from .identity import get_caller
from .log import log
from .util import TTLCache, compact_json

# About 3,000 tokens: enough for the whole README of most Store Actors (median ~8,400 characters) while
# keeping fetch-actor-details small. Longer READMEs are shortened by section (truncate_readme).
README_LIMIT = 12_000
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


# Sections an agent needs before it runs an Actor: what it costs, what it cannot do, common questions.
# They are always kept (each up to KEPT_SECTION_LIMIT characters), wherever they are in the README.
_KEEP_HEADING = re.compile(
    r"pric|cost|billing|charge|limit|known issue|caveat|not support|faq|frequently asked|question|troubleshoot",
    re.IGNORECASE,
)
KEPT_SECTION_LIMIT = 4_000
# Code blocks and tables longer than these are shortened first: they are examples, rarely needed in full.
CODE_BLOCK_LIMIT = 600
TABLE_ROW_LIMIT = 8
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S")


@dataclass
class _Section:
    heading: str  # the heading line ("" for the text before the first heading)
    body: str  # the whole section, heading line included

    @property
    def keep(self) -> bool:
        return bool(self.heading) and bool(_KEEP_HEADING.search(self.heading))


def _split_sections(text: str) -> list[_Section]:
    """Split Markdown at headings (not at `#` lines inside code fences)."""
    sections: list[_Section] = []
    heading = ""
    lines: list[str] = []
    fenced = False
    for line in text.splitlines(keepends=True):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced and _HEADING.match(line):
            if lines:
                sections.append(_Section(heading, "".join(lines)))
            heading, lines = line.strip(), []
        lines.append(line)
    if lines:
        sections.append(_Section(heading, "".join(lines)))
    return sections


def _shorten_blocks(text: str) -> str:
    """Shorten long code blocks and long tables; prose is left as it is."""
    out: list[str] = []
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            j = i + 1
            while j < len(lines) and not _FENCE.match(lines[j]):
                j += 1
            block = lines[i + 1 : j]
            body = "".join(block)
            out.append(line)
            if len(body) > CODE_BLOCK_LIMIT:
                cut = body[:CODE_BLOCK_LIMIT]
                cut = cut[: cut.rfind("\n") + 1] or cut
                out.append(cut + "... (example shortened)\n")
            else:
                out.append(body)
            if j < len(lines):
                out.append(lines[j])
            i = j + 1
            continue
        if line.lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            rows = lines[i:j]
            # header + separator + TABLE_ROW_LIMIT rows
            if len(rows) > TABLE_ROW_LIMIT + 2:
                out.extend(rows[: TABLE_ROW_LIMIT + 2])
                out.append(f"| ... {len(rows) - TABLE_ROW_LIMIT - 2} more rows |\n")
            else:
                out.extend(rows)
            i = j
            continue
        out.append(line)
        i += 1
    return "".join(out)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    return cut.rstrip() + "\n[section shortened]\n"


def truncate_readme(text: str | None, limit: int = README_LIMIT) -> str:
    """The README within about `limit` characters, shortened by section.

    A README that fits is returned as it is. Otherwise long code blocks and tables are shortened first;
    if it is still too long, the pricing, limits and FAQ sections are kept (wherever they are), the
    other sections are kept in order while they fit, and the headings of the sections left out are
    listed at the end, so the agent knows what it did not get.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    sections = [_Section(s.heading, _shorten_blocks(s.body)) for s in _split_sections(text)]
    shortened = "".join(s.body for s in sections)
    note_examples = "Long examples and tables were shortened."
    if len(shortened) <= limit:
        return shortened.rstrip() + f"\n\n[README shortened from {len(text):,} characters. {note_examples}]"
    for s in sections:
        if s.keep:
            s.body = _cap(s.body, KEPT_SECTION_LIMIT)
    budget = limit - sum(len(s.body) for s in sections if s.keep)
    kept: list[bool] = []
    for idx, s in enumerate(sections):
        if s.keep:
            kept.append(True)
            continue
        if len(s.body) <= budget:
            kept.append(True)
            budget -= len(s.body)
        elif idx == 0 and budget > 500:
            # The introduction says what the Actor does: keep its start rather than nothing.
            s.body = _cap(s.body, budget)
            kept.append(True)
            budget -= len(s.body)
        else:
            kept.append(False)
    body = "".join(s.body for s, k in zip(sections, kept) if k)
    left_out = [s.heading for s, k in zip(sections, kept) if not k and s.heading]
    out = body.rstrip() + (
        f"\n\n[README shortened from {len(text):,} characters. {note_examples} "
        "Pricing, limits and FAQ sections are always kept."
    )
    if left_out:
        out += " Sections left out:\n" + "\n".join(left_out)
    return out + "]" if not left_out else out + "\n]"


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


MAX_OUTPUT_FIELDS = 100


def _obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def output_schema_of(definition: Any) -> dict:
    """What a run of the Actor writes, from its build's Actor definition (SPEC 7.3 outputSchema).

    - `dataset`: the dataset item fields as a JSON schema (`properties`), from the dataset schema's
      `fields` and the fields its views show, with their labels and formats.
    - `datasetViews`: the dataset's views (name, title and fields), when it has any.
    - `outputs`: the named outputs of the Actor's output schema (`.actor/output_schema.json`).
    Empty when the Actor publishes none of them.
    """
    definition = _obj(definition)
    out: dict[str, Any] = {}
    dataset = _obj(_obj(definition.get("storages")).get("dataset"))
    fields: dict[str, dict] = {}
    for name, raw in _obj(_obj(dataset.get("fields")).get("properties")).items():
        p = _obj(raw)
        entry = {k: p[k] for k in ("type", "title", "description", "format") if p.get(k) not in (None, "")}
        if "description" in entry:
            entry["description"] = shorten_description(entry["description"])
        fields[str(name)] = entry
    views = []
    for view_name, raw_view in _obj(dataset.get("views")).items():
        view = _obj(raw_view)
        names = [f for f in _obj(view.get("transformation")).get("fields") or [] if isinstance(f, str)]
        display = _obj(_obj(view.get("display")).get("properties"))
        for f in names:
            entry = fields.setdefault(f, {})
            shown = _obj(display.get(f))
            if shown.get("label") and "title" not in entry:
                entry["title"] = shown["label"]
            if shown.get("format") and "format" not in entry:
                entry["format"] = shown["format"]
        if names:
            item: dict[str, Any] = {"name": str(view_name), "fields": names}
            if view.get("title"):
                item["title"] = view["title"]
            views.append(item)
    if fields:
        kept = dict(list(fields.items())[:MAX_OUTPUT_FIELDS])
        out["dataset"] = {"type": "object", "properties": kept}
        if len(fields) > MAX_OUTPUT_FIELDS:
            out["dataset"]["fieldsTruncated"] = len(fields)
    if views:
        out["datasetViews"] = views
    outputs = {}
    for name, raw in _obj(_obj(definition.get("output")).get("properties")).items():
        p = _obj(raw)
        entry = {k: p[k] for k in ("title", "description") if p.get(k)}
        outputs[str(name)] = entry
    if outputs:
        out["outputs"] = outputs
    return out


def output_schema_text(schema: dict) -> str:
    lines = []
    props = _obj(_obj(schema.get("dataset")).get("properties"))
    if props:
        lines.append("Each dataset item has these fields: " + ", ".join(props) + ".")
    lines.append(compact_json(schema))
    return "\n".join(lines)


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
