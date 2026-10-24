"""Pricing presentation (SPEC 10.6): complete and simplified modes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .util import fmt_number, parse_ts

TIERS = ["FREE", "BRONZE", "SILVER", "GOLD", "PLATINUM", "DIAMOND"]


def current_pricing(actor: dict) -> dict | None:
    cur = actor.get("currentPricingInfo")
    newest = _newest_info(actor.get("pricingInfos"))
    if isinstance(cur, dict) and cur:
        # Some API views return only the model in currentPricingInfo; take the details
        # from the matching pricingInfos entry.
        if newest and newest.get("pricingModel") == cur.get("pricingModel"):
            return {**newest, **{k: v for k, v in cur.items() if v is not None}}
        return cur
    return newest


def _newest_info(infos: Any) -> dict | None:
    if not isinstance(infos, list) or not infos:
        return None
    now = datetime.now(timezone.utc)
    best: dict | None = None
    best_ts: datetime | None = None
    for info in infos:
        if not isinstance(info, dict):
            continue
        ts = parse_ts(info.get("startedAt"))
        if ts is not None and ts > now:
            continue
        if best is None or (ts is not None and (best_ts is None or ts >= best_ts)) or (ts is None and best_ts is None):
            best, best_ts = info, ts
    return best


def _num(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _tier_map(raw: Any, key: str) -> list[tuple[str, float | int]]:
    out: list[tuple[str, float | int]] = []
    if not isinstance(raw, dict):
        return out
    for tier, entry in raw.items():
        price = _num(entry.get(key)) if isinstance(entry, dict) else _num(entry)
        if price is not None:
            out.append((str(tier), price))
    return out


def _events(info: dict) -> list[dict]:
    ppe = info.get("pricingPerEvent")
    events = ppe.get("actorChargeEvents") if isinstance(ppe, dict) else None
    out: list[dict] = []
    if isinstance(events, dict):
        for name, ev in events.items():
            if not isinstance(ev, dict):
                continue
            out.append(
                {
                    "title": ev.get("eventTitle") or name,
                    "description": ev.get("eventDescription") or "",
                    "price": _num(ev.get("eventPriceUsd")),
                    "tiers": _tier_map(ev.get("eventTieredPricingUsd"), "tieredEventPriceUsd"),
                }
            )
    return out


def model_of(info: dict | None) -> str:
    if not info:
        return "FREE"
    return str(info.get("pricingModel") or "FREE")


# -- platform usage --------------------------------------------------------
# isPPEPlatformUsagePaidByUser false (official Actors): the event prices include the
# run's compute, proxy and browser usage. Absent or true: usage is billed on top.
# usageEstimate (metered paid Actors): measured usage at Free-plan rates, last 30 days.


def usage_included(actor: dict, info: dict | None = None) -> bool:
    info = current_pricing(actor) if info is None else info
    if model_of(info) != "PAY_PER_EVENT" or not info:
        return False
    cur = actor.get("currentPricingInfo")
    flag = cur.get("isPPEPlatformUsagePaidByUser") if isinstance(cur, dict) else None
    if not isinstance(flag, bool):
        flag = info.get("isPPEPlatformUsagePaidByUser")
    return flag is False


def usage_per_1000(actor: dict) -> float | int | None:
    est = actor.get("usageEstimate")
    value = _num(est.get("usdPer1000Median")) if isinstance(est, dict) else None
    return value if value is not None and value > 0 else None


START_EVENTS = {"scrapely-actor-start", "apify-actor-start"}
RESULT_TITLES = {"result", "results"}


def headline_event(info: dict | None, user_tier: str) -> dict | None:
    """The event a pay-per-event price is quoted by (the primary event), or None.

    Same rule as the server's per-result event (utils/usageCap.perResultEventName):
    the pricing's primaryEventName, else the dataset-item event, else the only priced
    event that is not a start event. ``perResult`` is False when the pricing says the
    event is not one per result (primaryEventPerResult false, a start event, or the
    event's own perResult false), for example a page of a document or a minute of audio.
    Returns {"event", "title", "price", "perResult"}.
    """
    if not info or model_of(info) != "PAY_PER_EVENT":
        return None
    ppe = info.get("pricingPerEvent")
    events = ppe.get("actorChargeEvents") if isinstance(ppe, dict) else None
    if not isinstance(events, dict) or not events:
        return None
    name = info.get("primaryEventName")
    if not (isinstance(name, str) and name in events):
        if "scrapely-default-dataset-item" in events:
            name = "scrapely-default-dataset-item"
        else:
            priced = [
                n for n, e in events.items()
                if n not in START_EVENTS and isinstance(e, dict)
                and ((_num(e.get("eventPriceUsd")) or 0) > 0 or _tier_map(e.get("eventTieredPricingUsd"), "tieredEventPriceUsd"))
            ]
            name = priced[0] if len(priced) == 1 else (next(iter(events)) if len(events) == 1 else None)
    ev = events.get(name) if name else None
    if not isinstance(ev, dict):
        return None
    tiers = _tier_map(ev.get("eventTieredPricingUsd"), "tieredEventPriceUsd")
    price = _choose(tiers, user_tier)[1] if tiers else _num(ev.get("eventPriceUsd"))
    if price is None:
        return None
    per_result = not (info.get("primaryEventPerResult") is False or name in START_EVENTS or ev.get("perResult") is False)
    return {"event": name, "title": str(ev.get("eventTitle") or name), "price": price, "perResult": per_result}


def headline_fields(info: dict | None, user_tier: str) -> dict[str, Any]:
    """chargeUnit, chargedPerResult and usdPer1000Results (per-result events only)."""
    head = headline_event(info, user_tier)
    if head is None:
        return {}
    out: dict[str, Any] = {"chargeUnit": head["title"], "chargedPerResult": head["perResult"]}
    if head["perResult"]:
        out["usdPer1000Results"] = round(head["price"] * 1000, 6)
    return out


def headline_text(fields: dict, *, unit_price: float | int | None = None) -> str:
    """'$2.00 per 1,000 results', with the billed unit when it is not called a result.

    An event that is not one per result (a page, a minute of audio) is quoted in its
    own unit instead: '$0.0015 per Scraped page (not per result)'.
    """
    unit = fields.get("chargeUnit")
    if not unit:
        return ""
    if fields.get("chargedPerResult") and fields.get("usdPer1000Results") is not None:
        text = f"{_usd(fields['usdPer1000Results'])} per 1,000 results"
        if str(unit).strip().lower() not in RESULT_TITLES:
            text += f" (charged per {unit})"
        return text
    if unit_price is None:
        return ""
    return f"{_usd(unit_price)} per {unit} (not per result)"


def _primary_result_price(info: dict, user_tier: str) -> float | int | None:
    """Price of the event charged once per result, for a per-1,000-results total."""
    head = headline_event(info, user_tier)
    if head is None or not head["perResult"]:
        return None
    return head["price"] if head["price"] > 0 else None


def usage_fields(actor: dict, info: dict | None, user_tier: str) -> dict[str, Any]:
    """platformUsageIncluded, estUsageUsdPer1000 and estTotalUsdPer1000 for a pay-per-event Actor."""
    if model_of(info) != "PAY_PER_EVENT" or not info:
        return {}
    included = usage_included(actor, info)
    out: dict[str, Any] = {"platformUsageIncluded": included}
    primary = _primary_result_price(info, user_tier)
    usage = None if included else usage_per_1000(actor)
    if usage is not None:
        out["estUsageUsdPer1000"] = round(usage, 4)
    if primary is not None and (included or usage is not None):
        out["estTotalUsdPer1000"] = round(primary * 1000 + (usage or 0), 4)
    return out


# Platform rates (compute, proxy) differ by plan; Actor prices are list prices.
PLATFORM_AT_PLAN_RATES = "platform usage (compute, storage, proxy) at your plan's rates"
LIST_PRICE_NOTE = "List price, the same on every plan."
# Events with discountEnabled get the caller's plan Store discount at billing (server
# billingServiceSS.getUserStoreDiscount); compute, storage and proxy never do.
DISCOUNT_PRICE_NOTE = "List price; your plan's Store discount, if it has one, comes off these event prices when you are billed."
TIERED_PRICE_NOTE = "This Actor's price depends on the plan; fetch-actor-details lists the price for every plan."
TIERED_PRICE_NOTE_COMPLETE = "This Actor's price depends on the plan; the events below list the price for every plan."


def _discount_enabled(info: dict | None) -> bool:
    ppe = info.get("pricingPerEvent") if isinstance(info, dict) else None
    events = ppe.get("actorChargeEvents") if isinstance(ppe, dict) else None
    return isinstance(events, dict) and any(isinstance(e, dict) and e.get("discountEnabled") is True for e in events.values())


def _usd(value: float | int) -> str:
    if value >= 0.01:
        return f"${value:,.2f}"
    return f"${fmt_number(round(float(value), 6))}"


def usage_text(fields: dict) -> str:
    """Complete mode: one sentence on platform usage for a pay-per-event Actor."""
    if "platformUsageIncluded" not in fields:
        return ""
    total = fields.get("estTotalUsdPer1000")
    if fields["platformUsageIncluded"]:
        text = (
            "Platform usage (compute, proxy, browser) is included in the price; "
            "AI (LLM) tokens, if the Actor uses them, are billed separately."
        )
        if total is not None:
            text += f" About {_usd(total)} per 1,000 results in total."
        return text
    usage = fields.get("estUsageUsdPer1000")
    text = f"Plus {PLATFORM_AT_PLAN_RATES}, billed separately"
    if usage is not None:
        text += f": typically about {_usd(usage)} per 1,000 results"
        if total is not None:
            text += f", so about {_usd(total)} per 1,000 results in total"
    return text + "."


def _usage_short(p: dict) -> str:
    """Simplified mode: the platform usage tail of the one-line price."""
    if "platformUsageIncluded" not in p:
        return ""
    total = p.get("estTotalUsdPer1000")
    if p["platformUsageIncluded"]:
        tail = f" (about {_usd(total)} per 1,000 results in total)" if total is not None else ""
        return "; platform usage included" + tail
    usage = p.get("estUsageUsdPer1000")
    if usage is None:
        return "; plus platform usage at your plan's rates"
    tail = f"; about {_usd(total)} per 1,000 results in total" if total is not None else ""
    return f"; plus platform usage at your plan's rates (typically {_usd(usage)} per 1,000 results{tail})"


# -- complete mode -------------------------------------------------------


def complete_pricing(actor: dict, user_tier: str) -> tuple[dict, str]:
    info = current_pricing(actor)
    model = model_of(info)
    data: dict[str, Any] = {"model": model}
    lines: list[str] = []
    if model == "FREE" or info is None:
        data["model"] = "FREE"
        return data, f"Free to use. Only {PLATFORM_AT_PLAN_RATES} is charged."
    if model in ("PRICE_PER_DATASET_ITEM", "FLAT_PRICE_PER_MONTH"):
        unit = info.get("unitName") or "result"
        price = _num(info.get("pricePerUnitUsd")) or 0
        tiers = _tier_map(info.get("tieredPricing"), "tieredPricePerUnitUsd")
        data["unitName"] = unit
        data["pricePerUnit"] = price
        if tiers:
            data["tieredPricing"] = [{"tier": t, "pricePerUnit": p} for t, p in tiers]
        if model == "PRICE_PER_DATASET_ITEM":
            unit_note = "" if unit.strip().lower() in RESULT_TITLES else f" (charged per {unit})"
            if len(tiers) > 1:
                parts = ", ".join(f"{t}: ${fmt_number(round(p * 1000, 6))}" for t, p in tiers)
                lines.append(f"Pay per result: price per 1,000 results{unit_note} by plan: {parts}.")
            else:
                p = tiers[0][1] if tiers else price
                lines.append(f"Pay per result: ${fmt_number(round(p * 1000, 6))} per 1,000 results{unit_note}. {LIST_PRICE_NOTE}")
        else:
            trial = _num(info.get("trialMinutes"))
            if trial is not None:
                data["trialMinutes"] = trial
            if len(tiers) > 1:
                parts = ", ".join(f"{t}: ${fmt_number(p)}" for t, p in tiers)
                lines.append(f"Monthly rental by plan: {parts}.")
            else:
                p = tiers[0][1] if tiers else price
                lines.append(f"Monthly rental: ${fmt_number(p)} per month. {LIST_PRICE_NOTE}")
            if trial:
                lines.append(f"Trial period: {_trial_text(trial)}.")
        lines.append(f"Plus {PLATFORM_AT_PLAN_RATES}.")
        return data, " ".join(lines)
    if model == "PAY_PER_EVENT":
        events = _events(info)
        tiered = any(ev["tiers"] for ev in events)
        head = headline_fields(info, user_tier)
        data.update(head)
        out_events = []
        for ev in events:
            item: dict[str, Any] = {"title": ev["title"], "description": ev["description"]}
            if len(ev["tiers"]) > 1 or (ev["tiers"] and ev["price"] is None):
                item["tieredPricing"] = [{"tier": t, "priceUsd": p} for t, p in ev["tiers"]]
                text_price = ", ".join(f"{t}: ${fmt_number(p)}" for t, p in ev["tiers"])
            elif ev["price"] is not None or ev["tiers"]:
                price = ev["price"] if ev["price"] is not None else ev["tiers"][0][1]
                item["priceUsd"] = price
                text_price = f"${fmt_number(price)}"
            else:
                text_price = "no price info"
            out_events.append(item)
            desc = f" ({ev['description']})" if ev["description"] else ""
            lines.append(f"- {ev['title']}{desc}: {text_price}")
        data["events"] = out_events
        usage = usage_fields(actor, info, user_tier)
        data.update(usage)
        note = usage_text(usage)
        if not events:
            return data, "Pay per event: event pricing is not available." + (f" {note}" if note else "")
        data["pricingNote"] = (
            TIERED_PRICE_NOTE_COMPLETE if tiered else DISCOUNT_PRICE_NOTE if _discount_enabled(info) else LIST_PRICE_NOTE
        )
        headline = headline_text(head, unit_price=(headline_event(info, user_tier) or {}).get("price"))
        # The card renders pricingNote under the price (cards.build_card), so it is not repeated here.
        first = f"Pay per event: {headline}." if headline else "Pay per event:"
        return data, first + "\n" + "\n".join(lines) + (f"\n{note}" if note else "")
    return data, "Pricing information is not available."


def _trial_text(minutes: float | int) -> str:
    if minutes < 60:
        return f"{fmt_number(minutes)} minutes"
    if minutes < 1440:
        return f"{int(minutes // 60)} hours"
    return f"{int(minutes // 1440)} days"


# -- simplified mode -----------------------------------------------------


def _choose(tiers: list[tuple[str, float | int]], user_tier: str) -> tuple[str, float | int]:
    by = dict(tiers)
    if user_tier in by:
        return user_tier, by[user_tier]
    if "FREE" in by:
        return "FREE", by["FREE"]
    return tiers[0]


def simplified_pricing(actor: dict, user_tier: str) -> dict:
    info = current_pricing(actor)
    model = model_of(info)
    data: dict[str, Any] = {"model": model}
    if info is None or model == "FREE":
        data["model"] = "FREE"
        return data
    chosen: list[str] = []
    multi = False
    if model in ("PRICE_PER_DATASET_ITEM", "FLAT_PRICE_PER_MONTH"):
        data["unitName"] = info.get("unitName") or "result"
        tiers = _tier_map(info.get("tieredPricing"), "tieredPricePerUnitUsd")
        if tiers:
            tier, price = _choose(tiers, user_tier)
            chosen.append(tier)
            multi = multi or len(tiers) > 1
            data["pricePerUnit"] = price
        else:
            data["pricePerUnit"] = _num(info.get("pricePerUnitUsd")) or 0
        if model == "FLAT_PRICE_PER_MONTH" and _num(info.get("trialMinutes")) is not None:
            data["trialMinutes"] = _num(info.get("trialMinutes"))
    elif model == "PAY_PER_EVENT":
        events = _events(info)
        data.update(headline_fields(info, user_tier))
        head = headline_event(info, user_tier)
        if head is not None and not head["perResult"]:
            data["chargeUnitPriceUsd"] = head["price"]
        omit = len(events) > 5
        out = []
        for ev in events:
            item: dict[str, Any] = {"title": ev["title"]}
            if not omit and ev["description"]:
                item["description"] = ev["description"]
            if ev["tiers"]:
                tier, price = _choose(ev["tiers"], user_tier)
                chosen.append(tier)
                multi = multi or len(ev["tiers"]) > 1
                item["priceUsd"] = price
            elif ev["price"] is not None:
                item["priceUsd"] = ev["price"]
            out.append(item)
        data["events"] = out
        data.update(usage_fields(actor, info, user_tier))
        if omit:
            data["eventDescriptionsOmitted"] = True
            data["eventDescriptionsNote"] = (
                "Event descriptions are left out to keep this short. "
                "Call fetch-actor-details with output.pricing for the full event list."
            )
    if multi and chosen and len(set(chosen)) == 1 and user_tier != "DIAMOND":
        data["pricingNote"] = TIERED_PRICE_NOTE
    return data


def simplified_text(p: dict) -> str:
    model = p.get("model")
    if model == "FREE":
        return "free (only platform usage, at your plan's rates)"
    if model == "PRICE_PER_DATASET_ITEM":
        unit = str(p.get("unitName") or "result")
        unit_note = "" if unit.strip().lower() in RESULT_TITLES else f" (charged per {unit})"
        return f"${fmt_number(round((p.get('pricePerUnit') or 0) * 1000, 6))} per 1,000 results{unit_note}; plus platform usage at your plan's rates"
    if model == "FLAT_PRICE_PER_MONTH":
        return f"${fmt_number(p.get('pricePerUnit') or 0)} per month; plus platform usage at your plan's rates"
    if model == "PAY_PER_EVENT":
        evs = p.get("events") or []
        if not evs:
            return "pay per event" + _usage_short(p)
        # Headline first ("$2.00 per 1,000 results"), then the other events.
        headline = headline_text(p, unit_price=p.get("chargeUnitPriceUsd"))
        others = [ev for ev in evs if not headline or ev.get("title") != p.get("chargeUnit")]
        parts = [headline] if headline else []
        for ev in others[: 5 - len(parts)]:
            price = ev.get("priceUsd")
            parts.append(f"{ev.get('title')}: ${fmt_number(price)}" if price is not None else str(ev.get("title")))
        shown = len(parts) - (1 if headline else 0)
        more = f" (+{len(others) - shown} more)" if len(others) > shown else ""
        return "pay per event: " + "; ".join(parts) + more + _usage_short(p)
    return "not available"
