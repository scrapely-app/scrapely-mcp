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


# -- complete mode -------------------------------------------------------


def complete_pricing(actor: dict, user_tier: str) -> tuple[dict, str]:
    info = current_pricing(actor)
    model = model_of(info)
    data: dict[str, Any] = {"model": model, "userTier": user_tier}
    lines: list[str] = []
    if model == "FREE" or info is None:
        data["model"] = "FREE"
        return data, "Free to use. Only platform usage (compute, storage, proxy) is charged."
    if model in ("PRICE_PER_DATASET_ITEM", "FLAT_PRICE_PER_MONTH"):
        unit = info.get("unitName") or "result"
        price = _num(info.get("pricePerUnitUsd")) or 0
        tiers = _tier_map(info.get("tieredPricing"), "tieredPricePerUnitUsd")
        data["unitName"] = unit
        data["pricePerUnit"] = price
        if tiers:
            data["tieredPricing"] = [{"tier": t, "pricePerUnit": p} for t, p in tiers]
        if model == "PRICE_PER_DATASET_ITEM":
            if len(tiers) > 1:
                parts = ", ".join(f"{t}: ${fmt_number(round(p * 1000, 6))}" for t, p in tiers)
                lines.append(f"Pay per result: price per 1,000 {unit}s by plan tier: {parts}.")
            else:
                p = tiers[0][1] if tiers else price
                lines.append(f"Pay per result: ${fmt_number(round(p * 1000, 6))} per 1,000 {unit}s.")
        else:
            trial = _num(info.get("trialMinutes"))
            if trial is not None:
                data["trialMinutes"] = trial
            if len(tiers) > 1:
                parts = ", ".join(f"{t}: ${fmt_number(p)}" for t, p in tiers)
                lines.append(f"Monthly rental by plan tier: {parts}.")
            else:
                p = tiers[0][1] if tiers else price
                lines.append(f"Monthly rental: ${fmt_number(p)} per month.")
            if trial:
                lines.append(f"Trial period: {_trial_text(trial)}.")
        return data, " ".join(lines)
    if model == "PAY_PER_EVENT":
        events = _events(info)
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
        if not events:
            return data, "Pay per event: event pricing is not available."
        return data, "Pay per event:\n" + "\n".join(lines)
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
        if omit:
            data["eventDescriptionsOmitted"] = True
            data["eventDescriptionsNote"] = (
                "Event descriptions are left out to keep this short. "
                "Call fetch-actor-details with output.pricing for the full event list."
            )
    if multi and chosen and len(set(chosen)) == 1 and user_tier != "DIAMOND":
        data["pricingNote"] = (
            f"Prices shown apply to the {chosen[0]} plan tier. Higher tiers can be cheaper; "
            "fetch-actor-details lists every tier."
        )
    return data


def simplified_text(p: dict) -> str:
    model = p.get("model")
    if model == "FREE":
        return "free (platform usage only)"
    if model == "PRICE_PER_DATASET_ITEM":
        return f"${fmt_number(round((p.get('pricePerUnit') or 0) * 1000, 6))} per 1,000 {p.get('unitName', 'result')}s"
    if model == "FLAT_PRICE_PER_MONTH":
        return f"${fmt_number(p.get('pricePerUnit') or 0)} per month"
    if model == "PAY_PER_EVENT":
        evs = p.get("events") or []
        if not evs:
            return "pay per event"
        parts = []
        for ev in evs[:5]:
            price = ev.get("priceUsd")
            parts.append(f"{ev.get('title')}: ${fmt_number(price)}" if price is not None else str(ev.get("title")))
        more = f" (+{len(evs) - 5} more)" if len(evs) > 5 else ""
        return "pay per event: " + "; ".join(parts) + more
    return "not available"
