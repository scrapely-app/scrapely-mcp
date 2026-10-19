"""Actor cards shared by search and details (SPEC 7.2)."""

from __future__ import annotations

import re
from typing import Any

from .config import Settings
from .links import console_url, store_actor_url, store_user_url
from .pricing import complete_pricing, simplified_pricing, simplified_text
from .util import normalize_ts

NO_DESCRIPTION = "No description provided."
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


def input_fields(schema: Any) -> dict | None:
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if not isinstance(props, dict):
        return None
    names = list(props.keys())
    kept = names[:MAX_INPUT_FIELDS]
    out_props: dict[str, Any] = {}
    for n in kept:
        p = props.get(n) if isinstance(props.get(n), dict) else {}
        t = p.get("type")
        if t is None:
            t = "string" if p.get("editor") in ("textfield", "textarea", "select") else "any"
        out_props[n] = {"type": t}
    required = [r for r in (schema.get("required") or []) if r in out_props]
    data: dict[str, Any] = {"type": "object", "properties": out_props, "required": required}
    if len(names) > MAX_INPUT_FIELDS:
        data["_truncated"] = len(names)
    return data


def _type_text(t: Any) -> str:
    if isinstance(t, list):
        return "|".join(str(x) for x in t)
    return str(t)


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
    if "description" in sections:
        card["description"] = actor.get("description") or NO_DESCRIPTION
    else:
        card["description"] = ""
    if "metadata" in sections:
        card["developer"] = {
            "username": username,
            "isOfficial": username.lower() in settings.official_usernames,
            "url": store_user_url(settings, username),
        }
        card["categories"] = [format_category(c) for c in actor.get("categories") or [] if isinstance(c, str)]
    else:
        card["developer"] = {"username": "", "isOfficial": False, "url": ""}
        card["categories"] = []
    pricing_text = ""
    if "pricing" in sections:
        if pricing_mode == "complete":
            card["pricing"], pricing_text = complete_pricing(actor, user_tier)
        else:
            card["pricing"] = simplified_pricing(actor, user_tier)
            pricing_text = simplified_text(card["pricing"])
    stats = actor.get("stats") if isinstance(actor.get("stats"), dict) else {}
    if "stats" in sections and _num(stats.get("totalUsers")) and _num(stats.get("totalUsers30Days")):
        s: dict[str, Any] = {"totalUsers": stats["totalUsers"], "monthlyUsers": stats["totalUsers30Days"]}
        bm = actor.get("bookmarkCount", stats.get("bookmarkCount"))
        if _num(bm):
            s["bookmarks"] = int(bm)
        card["stats"] = s
    if "rating" in sections:
        avg = _to_num(stats.get("actorReviewRating", actor.get("avgRating")))
        count = _to_num(stats.get("actorReviewCount", actor.get("totalReviews")))
        if avg:
            card["rating"] = {"average": round(float(avg), 2), "count": int(count or 0)}
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
    if card["description"]:
        lines.append(f"- Description: {card['description']}")
    if "metadata" in sections:
        dev = card["developer"]
        lines.append(f"- Developer: {dev['username']}{' (official)' if dev['isOfficial'] else ''}")
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
    if "stats" in card:
        st = card["stats"]
        extra = f", {st['bookmarks']} bookmarks" if "bookmarks" in st else ""
        lines.append(f"- Users: {st['totalUsers']} total, {st['monthlyUsers']} in the last 30 days{extra}")
    if "rating" in card:
        lines.append(f"- Rating: {card['rating']['average']} from {card['rating']['count']} reviews")
    if "modifiedAt" in card:
        lines.append(f"- Last modified: {card['modifiedAt']}")
    if card["isDeprecated"]:
        lines.append("- Note: this Actor is deprecated and may stop working.")
    if "inputFields" in card:
        req = set(card["inputFields"].get("required") or [])
        parts = [
            f"{n}{'' if n in req else '?'}: {_type_text(p.get('type'))}"
            for n, p in card["inputFields"]["properties"].items()
        ]
        if card.get("inputFieldsTruncated"):
            parts.append(f"... (+{card['inputFieldsTotalCount'] - MAX_INPUT_FIELDS} more)")
        lines.append(f"- Input: {', '.join(parts) if parts else '(no input fields)'}")
    return card, "\n".join(lines)
