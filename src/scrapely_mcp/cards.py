"""Actor cards shared by search and details (SPEC 7.2)."""

from __future__ import annotations

import re
from typing import Any

from .config import Settings
from .links import console_url, store_actor_url, store_user_url
from .pricing import complete_pricing, simplified_pricing, simplified_text
from .util import normalize_ts

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
        req = set(card["inputFields"].get("required") or [])
        parts = [
            f"{n}{'' if n in req else '?'}: {_type_text(p.get('type'))}"
            for n, p in card["inputFields"]["properties"].items()
        ]
        if card.get("inputFieldsTruncated"):
            parts.append(f"... (+{card['inputFieldsTotalCount'] - MAX_INPUT_FIELDS} more)")
        lines.append(f"- Input: {', '.join(parts) if parts else '(no input fields)'}")
    return card, "\n".join(lines)
