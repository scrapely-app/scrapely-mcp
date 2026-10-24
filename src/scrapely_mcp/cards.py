"""Actor cards shared by search and details (SPEC 7.2)."""

from __future__ import annotations

import re
from typing import Any

from .config import Settings
from .links import console_url, store_actor_url, store_user_url
from .pricing import complete_pricing, simplified_pricing, simplified_text
from .util import compact_json, normalize_ts

NO_DESCRIPTION = "No description provided."
MAINTAINED_BY = "Maintained by Scrape.ly"
SUCCESS_WINDOWS = {"currentBuild": "current version", "last30Days": "last 30 days"}
MAX_INPUT_FIELDS = 20
ALL_SECTIONS = {"description", "stats", "pricing", "rating", "metadata"}


def format_category(raw: str) -> str:
    words = [w.capitalize() for w in str(raw).replace("-", "_").split("_") if w]
    text = " ".join(words)
    text = re.sub(r"\bMcp Server", "MCP Server", text)
    text = re.sub(r"\bAi\b", "AI", text)
    text = re.sub(r"\bSeo\b", "SEO", text)
    return text


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _to_num(v: Any) -> float | int | None:
    if _num(v):
        return v
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def is_official(actor: dict, settings: Settings, username: str) -> bool:
    """Maintained by Scrape.ly: the API's flag (owner is an official account); the configured usernames
    (SCRAPELY_OFFICIAL_USERNAMES) only for an API that does not send it."""
    flag = actor.get("maintainedByScrapely")
    if isinstance(flag, bool):
        return flag
    return username.lower() in settings.official_usernames


def success_rate(stats: dict) -> dict | None:
    """The public success rate when the API sends one; rate None means not enough runs yet."""
    if "successRate" not in stats:
        return None
    rate = stats.get("successRate")
    if not _num(rate):
        return {"rate": None, "window": None}
    window = stats.get("successRateWindow")
    return {"rate": round(rate), "window": window if isinstance(window, str) else None}


def success_rate_text(sr: dict) -> str:
    if sr.get("rate") is None:
        return "not enough runs yet"
    window = SUCCESS_WINDOWS.get(sr.get("window") or "")
    return f"{sr['rate']}%" + (f" ({window})" if window else "")


# Input fields on a card: what an agent needs to fill the input without fetching the details first,
# kept small (search returns several cards). The server sends a compact schema with
# `includeInputSchema=true`; the same bounds apply whatever the API sends.
INPUT_DESCRIPTION_LIMIT = 100
INPUT_VALUE_LIMIT = 120
INPUT_ENUM_VALUES = 10
INPUT_ENUM_CHARS = 200
# Characters of the card text spent on field details; the remaining fields are listed by name.
INPUT_TEXT_BUDGET = 1500
INPUT_NAMES_BUDGET = 300
_SECRET_VALUE_KEYS = ("default", "prefill")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _small_value(value: Any) -> Any:
    if value is None or value == "":
        return None
    try:
        size = len(compact_json(value))
    except (TypeError, ValueError):
        return None
    return value if size <= INPUT_VALUE_LIMIT else None


def _small_enum(values: Any) -> tuple[list, int | None] | None:
    if not isinstance(values, list) or not values:
        return None
    out: list = []
    chars = 0
    for v in values:
        if len(out) >= INPUT_ENUM_VALUES or isinstance(v, (dict, list)):
            break
        size = len(compact_json(v))
        if chars + size > INPUT_ENUM_CHARS:
            break
        chars += size
        out.append(v)
    if not out:
        return None
    return out, (len(values) if len(out) < len(values) else None)


def _has_secret(node: Any, depth: int = 0) -> bool:
    if depth > 20:
        return True
    if isinstance(node, dict):
        if node.get("isSecret") is True:
            return True
        return any(_has_secret(v, depth + 1) for v in node.values())
    if isinstance(node, list):
        return any(_has_secret(v, depth + 1) for v in node)
    return False


def _input_field(p: dict) -> dict:
    t = p.get("type")
    if t is None:
        t = "string" if p.get("editor") in ("textfield", "textarea", "select") else "any"
    field: dict[str, Any] = {"type": t}
    desc = p.get("description") if isinstance(p.get("description"), str) else None
    desc = desc or (p.get("title") if isinstance(p.get("title"), str) else None)
    if desc and desc.strip():
        field["description"] = _clip(desc, INPUT_DESCRIPTION_LIMIT)
    editor = p.get("editor")
    if editor in ("requestListSources", "stringList", "proxy", "keyValue", "json", "datepicker"):
        field["editor"] = editor
    if p.get("isSecret") is True:
        field["isSecret"] = True
    # A secret field, or one with a secret field beneath it, never shows a value.
    if p.get("isSecret") is True or _has_secret(p):
        return field
    for key in _SECRET_VALUE_KEYS:
        v = _small_value(p.get(key))
        if v is not None:
            field[key] = v
    e = _small_enum(p.get("enum"))
    items = p.get("items") if isinstance(p.get("items"), dict) else None
    if e is None and items is not None:
        e = _small_enum(items.get("enum"))
    if e is not None:
        field["enum"] = e[0]
        if e[1]:
            field["enumTotal"] = e[1]
    if items is not None and isinstance(items.get("type"), str):
        field["itemsType"] = items["type"]
    return field


def input_fields(schema: Any) -> dict | None:
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if not isinstance(props, dict):
        return None
    names = [n for n, p in props.items() if not (isinstance(p, dict) and p.get("editor") == "hidden")]
    required_all = [r for r in (schema.get("required") or []) if isinstance(r, str)]
    # Required fields are kept first when there are too many; the card lists them in schema order.
    order = {n: i for i, n in enumerate(names)}
    ranked = sorted(names, key=lambda n: (n not in required_all, order[n]))
    kept = sorted(ranked[:MAX_INPUT_FIELDS], key=order.__getitem__)
    out_props = {n: _input_field(props[n] if isinstance(props.get(n), dict) else {}) for n in kept}
    required = [r for r in required_all if r in out_props]
    data: dict[str, Any] = {"type": "object", "properties": out_props, "required": required}
    total = schema.get("totalProperties")
    total = total if isinstance(total, int) and not isinstance(total, bool) else len(names)
    if max(total, len(names)) > len(kept):
        data["_truncated"] = max(total, len(names))
    return data


def _type_text(t: Any) -> str:
    if isinstance(t, list):
        return "|".join(str(x) for x in t)
    return str(t)


def _field_type_text(f: dict) -> str:
    t = _type_text(f.get("type"))
    editor = f.get("editor")
    if t == "array":
        if editor == "requestListSources":
            t = "array of {url}"
        elif f.get("itemsType"):
            t = f"array of {f['itemsType']}"
        elif editor == "stringList":
            t = "array of string"
    elif editor == "proxy":
        t = "proxy settings object"
    if "enum" in f:
        values = "|".join(v if isinstance(v, str) else compact_json(v) for v in f["enum"])
        t += f": {values}" + ("|\u2026" if f.get("enumTotal") else "")
    if f.get("isSecret"):
        t += ", secret"
    return t


def _field_line(name: str, f: dict, required: bool) -> str:
    line = f"  - {name}{'*' if required else ''} ({_field_type_text(f)})"
    parts = []
    if f.get("description"):
        parts.append(f["description"].rstrip(". "))
    for key, label in (("default", "Default"), ("prefill", "Example")):
        # An example that only repeats the default says nothing new.
        if key in f and not (key == "prefill" and "default" in f and f["prefill"] == f["default"]):
            parts.append(f"{label}: {_clip(compact_json(f[key]), 80)}")
    text = ""
    for part in parts:
        # A shortened description ends with an ellipsis: no period after it.
        text += part if not text else (" " if text.endswith("\u2026") else ". ") + part
    return line + (": " + text if text else "")


def input_fields_text(fields: dict, total: int | None = None) -> str:
    """The card's input section: one line per field within INPUT_TEXT_BUDGET, the rest by name."""
    props: dict = fields.get("properties") or {}
    if not props:
        return "- Input: (no input fields)"
    req = set(fields.get("required") or [])
    lines = {n: _field_line(n, f, n in req) for n, f in props.items()}
    detailed: set[str] = set()
    used = 0
    # Required fields get their details first, then fields with an example or default, then the rest.
    for n in sorted(props, key=lambda n: (n not in req, not ({"default", "prefill"} & set(props[n])))):
        cost = len(lines[n]) + 1
        if used + cost > INPUT_TEXT_BUDGET:
            continue
        used += cost
        detailed.add(n)
    out = ["- Input fields (* = required):"]
    out += [lines[n] for n in props if n in detailed]
    rest = [f"{n}{'*' if n in req else ''}" for n in props if n not in detailed]
    hidden_count = (total - len(props)) if total and total > len(props) else 0
    if rest or hidden_count:
        shown: list[str] = []
        chars = 0
        for n in rest:
            if chars + len(n) + 2 > INPUT_NAMES_BUDGET:
                break
            shown.append(n)
            chars += len(n) + 2
        more = len(rest) - len(shown) + hidden_count
        text = ", ".join(shown)
        if more:
            text += (", " if text else "") + f"... (+{more} more)"
        out.append(f"  - Other fields: {text}")
    return "\n".join(out)


def build_card(
    actor: dict,
    settings: Settings,
    *,
    sections: set[str],
    pricing_mode: str,
    user_tier: str,
    caller=None,
    console: bool = False,
    input_schema: Any = None,
    picture_url: str | None = None,
) -> tuple[dict, str]:
    username = str(actor.get("username") or "")
    name = str(actor.get("name") or "")
    full_name = f"{username}/{name}" if username else name
    actor_id = str(actor.get("id") or "")
    title = actor.get("title") or name
    if console:
        url = console_url(settings, caller, f"/actors/{actor_id}")
    else:
        url = store_actor_url(settings, username, name)
    card: dict[str, Any] = {"title": title, "url": url, "id": actor_id, "fullName": full_name}
    pic = picture_url or actor.get("pictureUrl")
    if isinstance(pic, str) and pic:
        card["pictureUrl"] = pic
    # Description, developer and categories are always filled from the loaded Actor, whichever sections
    # were asked for: they cost nothing and empty placeholders made agents report "no description".
    card["description"] = actor.get("description") or NO_DESCRIPTION
    official = is_official(actor, settings, username)
    developer: dict[str, Any] = {
        "username": username,
        "isOfficial": official,
        "url": store_user_url(settings, username),
    }
    owner_name = actor.get("ownerName")
    if isinstance(owner_name, str) and owner_name.strip():
        developer["name"] = owner_name.strip()
    if official:
        developer["maintainedBy"] = "Scrape.ly"
    card["developer"] = developer
    card["categories"] = [format_category(c) for c in actor.get("categories") or [] if isinstance(c, str)]
    pricing_text = ""
    if "pricing" in sections:
        if pricing_mode == "complete":
            card["pricing"], pricing_text = complete_pricing(actor, user_tier)
        else:
            card["pricing"] = simplified_pricing(actor, user_tier)
            pricing_text = simplified_text(card["pricing"])
    stats = actor.get("stats") if isinstance(actor.get("stats"), dict) else {}
    # Monthly active users and the success rate are the public numbers (no run, total user or bookmark counts).
    if "stats" in sections:
        card_stats: dict[str, Any] = {}
        if _num(stats.get("totalUsers30Days")):
            card_stats["monthlyUsers"] = stats["totalUsers30Days"]
        sr = success_rate(stats)
        if sr is not None:
            card_stats["successRate"] = sr["rate"]
            if sr["window"]:
                card_stats["successRateWindow"] = sr["window"]
        if card_stats:
            card["stats"] = card_stats
    if "rating" in sections:
        avg = _to_num(stats.get("actorReviewRating", actor.get("avgRating")))
        count = _to_num(stats.get("actorReviewCount", actor.get("totalReviews")))
        # Only real reviews: no Store Actor has any yet, and "0.0 from 0 reviews" says nothing.
        if avg and count:
            card["rating"] = {"average": round(float(avg), 2), "count": int(count)}
    if "metadata" in sections and actor.get("modifiedAt"):
        card["modifiedAt"] = normalize_ts(actor.get("modifiedAt"))
    card["isDeprecated"] = bool(actor.get("isDeprecated"))
    fields = input_fields(input_schema)
    if fields is not None:
        truncated = fields.pop("_truncated", None)
        card["inputFields"] = fields
        if truncated:
            card["inputFieldsTruncated"] = True
            card["inputFieldsTotalCount"] = truncated

    # text form
    lines = [f"## {title} ({full_name})", f"- URL: {url}"]
    if "description" in sections:
        lines.append(f"- Description: {card['description']}")
    if "metadata" in sections:
        dev = card["developer"]
        if dev["isOfficial"]:
            brand = dev.get("name") or dev["username"]
            lines.append(f"- Developer: {MAINTAINED_BY} ({brand})")
        else:
            lines.append(f"- Developer: {dev.get('name') or dev['username']}")
        if card["categories"]:
            lines.append(f"- Categories: {', '.join(card['categories'])}")
    if pricing_text:
        if "\n" in pricing_text:
            lines.append("- Pricing:\n" + "\n".join("  " + ln for ln in pricing_text.splitlines()))
        else:
            lines.append(f"- Pricing: {pricing_text}")
        note = card.get("pricing", {}).get("pricingNote")
        if note:
            lines.append(f"  {note}")
    if "monthlyUsers" in card.get("stats", {}):
        lines.append(f"- Monthly users: {card['stats']['monthlyUsers']}")
    if "successRate" in card.get("stats", {}):
        sr = {"rate": card["stats"]["successRate"], "window": card["stats"].get("successRateWindow")}
        lines.append(f"- Success rate: {success_rate_text(sr)}")
    if "rating" in card:
        lines.append(f"- Rating: {card['rating']['average']} from {card['rating']['count']} reviews")
    if "modifiedAt" in card:
        lines.append(f"- Last modified: {card['modifiedAt']}")
    if card["isDeprecated"]:
        lines.append("- Note: this Actor is deprecated and may stop working.")
    if "inputFields" in card:
        lines.append(input_fields_text(card["inputFields"], card.get("inputFieldsTotalCount")))
    return card, "\n".join(lines)
