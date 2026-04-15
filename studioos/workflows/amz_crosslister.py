"""amz_crosslister workflow — eBay arbitrage candidate discovery.

Reads pricefinder.db.crosslist_candidates: ASINs where eBay's
new-price is meaningfully above Amazon's buy-box. Sends a
Telegram + Slack digest. No eBay writes — listing the items
on eBay is a separate human/agent action behind an approval gate
in a future milestone.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class CrossState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    candidates: list[dict[str, Any]]
    stranded: list[dict[str, Any]]
    low_stock: list[dict[str, Any]]
    new_finds: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    summary: str


# CROSSLISTER.md pricing (lines 26–30):
#   ebay_new_price varsa → %5–10 altı
#   yoksa → buybox_price * 1.15–1.20
# MCF fee (Amazon Multi-Channel Fulfillment, rough standard rate):
#   ~$6 baseline + $0.5/kg for Small/Standard; approximate — actual
#   fee comes from SP-API later.
EBAY_UNDERCUT_PCT = 0.07
EBAY_MARKUP_PCT = 0.175


def _mcf_fee_estimate(weight_g: float | None) -> float:
    if not weight_g:
        return 6.0
    kg = weight_g / 1000.0
    return round(6.0 + 0.5 * kg, 2)


def _ebay_target_price(
    ebay_new: float | None,
    amazon_buybox: float | None,
    weight_g: float | None,
) -> dict[str, Any]:
    target: float | None = None
    source = "none"
    if ebay_new is not None:
        target = round(ebay_new * (1.0 - EBAY_UNDERCUT_PCT), 2)
        source = "ebay_undercut"
    elif amazon_buybox is not None:
        target = round(amazon_buybox * (1.0 + EBAY_MARKUP_PCT), 2)
        source = "amazon_markup"
    mcf_fee = _mcf_fee_estimate(weight_g)
    return {
        "ebay_target_price": target,
        "price_source": source,
        "mcf_fee_est": mcf_fee,
    }


async def node_scan(state: CrossState) -> dict[str, Any]:
    """Pull listable items from the EbayCrossLister inventory directly.

    EbayCrossLister already keeps an up-to-date view of which Amazon
    inventory rows are listed on eBay. That's a stronger signal than
    PriceFinder's premium calculation, so we use it as the primary
    source. PriceFinder remains a useful future fallback when ebay
    inventory data is empty.
    """
    goals = state.get("goals") or {}
    primary = await invoke_from_state(
        state,
        "ebaycrosslister.db.listable_items",
        {"limit": int(goals.get("scan_limit", 30))},
    )
    if primary["status"] == "ok":
        items = (primary["data"] or {}).get("items") or []
        # Normalize to a shared shape with the pricefinder fallback.
        normalized = [
            {
                "asin": it.get("asin"),
                "title": it.get("title"),
                "brand": None,
                "amazon_buybox_usd": it.get("amazon_price"),
                "ebay_new_usd": None,
                "premium_pct": None,
                "monthly_sold": None,
                "fba_offer_count": None,
                "sales_rank": None,
                "fulfillable_quantity": it.get("fulfillable_quantity"),
                "sku": it.get("sku"),
                "source": "ebaycrosslister",
            }
            for it in items
        ]
        if normalized:
            return {"candidates": normalized}
        # Empty inventory → fall through to pricefinder
    else:
        log.warning(
            "amz_crosslister.ebay_scan_failed",
            error=primary.get("error"),
        )

    fallback = await invoke_from_state(
        state,
        "pricefinder.db.crosslist_candidates",
        {
            "limit": int(goals.get("scan_limit", 15)),
            "min_premium": float(goals.get("min_premium", 1.15)),
            "min_monthly_sold": int(goals.get("min_monthly_sold", 30)),
        },
    )
    if fallback["status"] != "ok":
        log.warning(
            "amz_crosslister.fallback_failed", error=fallback.get("error")
        )
        return {"candidates": []}
    items = (fallback["data"] or {}).get("items") or []
    for it in items:
        it["source"] = "pricefinder"
    return {"candidates": items}


async def node_scan_rules(state: CrossState) -> dict[str, Any]:
    """CROSSLISTER.md rule scans — stranded inventory + low-stock listings.

    Stranded items are the highest-priority cross-list candidates
    (dead Amazon stock). Low-stock listings are active eBay listings
    whose Amazon inventory has dropped below 3 — they need to be
    paused before we oversell.
    """
    goals = state.get("goals") or {}
    stranded_res = await invoke_from_state(
        state,
        "ebaycrosslister.db.stranded_inventory",
        {"limit": int(goals.get("stranded_limit", 30))},
    )
    stranded = (
        (stranded_res["data"] or {}).get("items") or []
        if stranded_res["status"] == "ok"
        else []
    )
    if stranded_res["status"] != "ok":
        log.warning(
            "amz_crosslister.stranded_failed", error=stranded_res.get("error")
        )

    low_stock_res = await invoke_from_state(
        state,
        "ebaycrosslister.db.low_stock_listings",
        {
            "min_stock": int(goals.get("min_amazon_stock", 3)),
            "limit": int(goals.get("low_stock_limit", 50)),
        },
    )
    low_stock = (
        (low_stock_res["data"] or {}).get("items") or []
        if low_stock_res["status"] == "ok"
        else []
    )
    if low_stock_res["status"] != "ok":
        log.warning(
            "amz_crosslister.low_stock_failed",
            error=low_stock_res.get("error"),
        )

    return {"stranded": stranded, "low_stock": low_stock}


def node_diff(state: CrossState) -> dict[str, Any]:
    candidates = state.get("candidates") or []
    stranded = state.get("stranded") or []
    existing = dict(state.get("state") or {})
    seen = set(existing.get("crosslisted_asins") or [])

    # Stranded items are the priority bucket — surface them first and
    # always tag priority=stranded even if we've seen the ASIN before.
    stranded_tagged = []
    for s in stranded:
        asin = s.get("asin")
        if not asin:
            continue
        pricing = _ebay_target_price(None, s.get("amazon_price"), None)
        stranded_tagged.append(
            {
                **s,
                **pricing,
                "priority": "stranded",
                "source": "ebaycrosslister_stranded",
            }
        )

    new_normal = []
    for c in candidates:
        asin = c.get("asin")
        if not asin or asin in seen:
            continue
        pricing = _ebay_target_price(
            c.get("ebay_new_usd"), c.get("amazon_buybox_usd") or c.get("amazon_price"), None
        )
        new_normal.append({**c, **pricing, "priority": "normal"})
        seen.add(asin)

    # Always include stranded in new_finds (dedup against seen set too).
    new_finds: list[dict[str, Any]] = []
    for s in stranded_tagged:
        if s["asin"] not in seen:
            seen.add(s["asin"])
        new_finds.append(s)
    new_finds.extend(new_normal)

    if len(seen) > 500:
        seen = set(list(seen)[-500:])

    existing["crosslisted_asins"] = sorted(seen)
    existing["scans_total"] = int(existing.get("scans_total", 0)) + 1
    existing["last_new_count"] = len(new_finds)
    existing["last_stranded_count"] = len(stranded_tagged)

    return {"new_finds": new_finds, "state": existing}


def _format_digest(items: list[dict[str, Any]]) -> str:
    lines = [f"*🛒 AMZ CrossLister — {len(items)} eBay fırsatı*"]
    for c in items[:10]:
        asin = c.get("asin", "?")
        bb = c.get("amazon_buybox_usd")
        eb = c.get("ebay_new_usd")
        prem = c.get("premium_pct")
        ms = c.get("monthly_sold")
        title = (c.get("title") or "")[:50]
        bb_str = f"${bb:.0f}" if isinstance(bb, (int, float)) else "—"
        eb_str = f"${eb:.0f}" if isinstance(eb, (int, float)) else "—"
        prem_str = f"+{prem:.0f}%" if isinstance(prem, (int, float)) else "—"
        ms_str = f"{ms}/mo" if ms else "—"
        lines.append(
            f"• `{asin}` Amz {bb_str} → eBay {eb_str} ({prem_str}) · {ms_str}\n"
            f"  {title}"
        )
    if len(items) > 10:
        lines.append(f"\n_+{len(items) - 10} more_")
    return "\n".join(lines)


async def node_emit(state: CrossState) -> dict[str, Any]:
    new_finds = state.get("new_finds") or []
    low_stock = state.get("low_stock") or []
    events: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    kpi_updates: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []

    for row in low_stock:
        approvals.append(
            {
                "reason": (
                    f"Stop eBay listing — Amazon stock "
                    f"{row.get('fulfillable_quantity', 0)} < 3 for "
                    f"{row.get('asin')} (listing {row.get('listing_id')})"
                ),
                "payload": {
                    "kind": "stop_listing_low_stock",
                    "listing_id": row.get("listing_id"),
                    "ebay_item_id": row.get("ebay_item_id"),
                    "asin": row.get("asin"),
                    "sku": row.get("sku"),
                    "fulfillable_quantity": row.get("fulfillable_quantity"),
                },
                "expires_in_seconds": 60 * 60 * 6,
            }
        )

    for c in new_finds:
        events.append(
            {
                "event_type": "amz.crosslist.candidate",
                "event_version": 1,
                "payload": c,
                "idempotency_key": (
                    f"amz_crosslister:{state['run_id']}:{c.get('asin')}"
                ),
            }
        )

    notified = False
    if new_finds:
        text = _format_digest(new_finds)
        notify_tg = await invoke_from_state(
            state,
            "telegram.notify",
            {
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )
        notify_slack = await invoke_from_state(
            state, "slack.notify", {"text": text, "mrkdwn": True}
        )
        notified = (
            notify_tg["status"] == "ok" or notify_slack["status"] == "ok"
        )
        memories.append(
            {
                "content": (
                    f"CrossLister found {len(new_finds)} eBay arbitrage candidates; "
                    f"top {new_finds[0].get('asin')}"
                ),
                "tags": ["amz", "crosslister", "ebay"],
                "importance": 0.5,
            }
        )

    kpi_updates.append(
        {"name": "crosslist_new_per_run", "value": len(new_finds)}
    )
    kpi_updates.append(
        {"name": "crosslist_low_stock_alerts", "value": len(low_stock)}
    )

    return {
        "events": events,
        "memories": memories,
        "kpi_updates": kpi_updates,
        "approvals": approvals,
        "summary": (
            f"{len(state.get('candidates') or [])} candidates, "
            f"{len(new_finds)} new "
            f"({len(state.get('stranded') or [])} stranded), "
            f"{len(low_stock)} low-stock"
            + (" (notified)" if notified else "")
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(CrossState)
    graph.add_node("scan", node_scan)
    graph.add_node("scan_rules", node_scan_rules)
    graph.add_node("diff", node_diff)
    graph.add_node("emit", node_emit)
    graph.add_edge(START, "scan")
    graph.add_edge("scan", "scan_rules")
    graph.add_edge("scan_rules", "diff")
    graph.add_edge("diff", "emit")
    graph.add_edge("emit", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_crosslister", 1, compiled)
