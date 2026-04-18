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
    auto_listed: list[dict[str, Any]]
    auto_list_failed: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    summary: str


AUTO_LIST_BATCH_MAX = 5

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
    """CROSSLISTER.md rule scans — stranded inventory only.

    Stranded items are the highest-priority cross-list candidates
    (dead Amazon stock). Low-stock monitoring is handled by Veeqo
    (Amazon → eBay inventory sync), no StudioOS intervention needed.
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

    return {"stranded": stranded, "low_stock": []}


def node_diff(state: CrossState) -> dict[str, Any]:
    # User directive: only surface stranded inventory (dead FBA stock).
    # listable_items (regular FBA inventory) and eBay arbitrage candidates
    # are NOT reported — those decisions are handled elsewhere (Veeqo sync)
    # or need real eBay price data we don't yet have.
    stranded = state.get("stranded") or []
    existing = dict(state.get("state") or {})
    seen = set(existing.get("crosslisted_asins") or [])

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

    # Only new stranded items (dedup against previously reported)
    new_finds: list[dict[str, Any]] = []
    for s in stranded_tagged:
        if s["asin"] not in seen:
            seen.add(s["asin"])
            new_finds.append(s)

    if len(seen) > 500:
        seen = set(list(seen)[-500:])

    existing["crosslisted_asins"] = sorted(seen)
    existing["scans_total"] = int(existing.get("scans_total", 0)) + 1
    existing["last_new_count"] = len(new_finds)
    existing["last_stranded_count"] = len(stranded_tagged)

    return {"new_finds": new_finds, "state": existing}


async def node_auto_list(state: CrossState) -> dict[str, Any]:
    """Auto-list stranded items on eBay via create_draft + publish_listing."""
    goals = state.get("goals") or {}
    if not goals.get("auto_list_stranded"):
        return {}

    new_finds = state.get("new_finds") or []
    stranded_items = [it for it in new_finds if it.get("priority") == "stranded"]
    batch = stranded_items[:AUTO_LIST_BATCH_MAX]

    auto_listed: list[dict[str, Any]] = []
    auto_list_failed: list[dict[str, Any]] = []

    for item in batch:
        asin = item.get("asin")
        sku = item.get("sku")
        title = item.get("title", "")
        amazon_price = item.get("amazon_price") or 0.0
        price = item.get("ebay_target_price") or round(amazon_price * 1.175, 2)
        quantity = item.get("fulfillable_quantity") or 1

        try:
            draft_res = await invoke_from_state(
                state,
                "ebaycrosslister.api.create_draft",
                {
                    "title": title,
                    "price": price,
                    "quantity": quantity,
                    "condition": "new",
                    "asin": asin,
                    "sku": sku,
                },
            )
            if draft_res.get("status") != "ok":
                auto_list_failed.append({"asin": asin, "reason": draft_res.get("error")})
                continue

            listing_id = (draft_res.get("data") or {}).get("listing_id")
            if not listing_id:
                auto_list_failed.append({"asin": asin, "reason": "no listing_id in create_draft response"})
                continue

            pub_res = await invoke_from_state(
                state,
                "ebaycrosslister.api.publish_listing",
                {"listing_id": listing_id},
            )
            if pub_res.get("status") == "ok":
                auto_listed.append({**item, "listing_id": listing_id})
            else:
                auto_list_failed.append({"asin": asin, "reason": pub_res.get("error")})

        except Exception as exc:  # noqa: BLE001
            log.warning("amz_crosslister.auto_list_error", asin=asin, error=str(exc))
            auto_list_failed.append({"asin": asin, "reason": str(exc)})

    return {"auto_listed": auto_listed, "auto_list_failed": auto_list_failed}


def _format_digest(items: list[dict[str, Any]]) -> str:
    lines = [f"*🛒 AMZ CrossLister — {len(items)} stranded ürün eBay'e listelendi*"]
    for c in items[:10]:
        asin = c.get("asin", "?")
        bb = c.get("amazon_buybox_usd") or c.get("amazon_price")
        eb = c.get("ebay_new_usd") or c.get("ebay_target_price")
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
    events: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    kpi_updates: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    # Note: low_stock monitoring removed — Veeqo handles Amazon↔eBay stock sync

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

    auto_listed = state.get("auto_listed") or []
    if auto_listed:
        auto_lines = [f"*🚀 Auto-listed {len(auto_listed)} stranded items on eBay*"]
        for it in auto_listed[:10]:
            auto_lines.append(
                f"• `{it.get('asin')}` — listing #{it.get('listing_id')} @ ${it.get('ebay_target_price', '?')}"
            )
        if len(auto_listed) > 10:
            auto_lines.append(f"_+{len(auto_listed) - 10} more_")
        auto_text = "\n".join(auto_lines)
        await invoke_from_state(
            state,
            "telegram.notify",
            {
                "text": auto_text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )
        memories.append(
            {
                "content": (
                    f"CrossLister auto-listed {len(auto_listed)} stranded items on eBay; "
                    f"first ASIN: {auto_listed[0].get('asin')}"
                ),
                "tags": ["amz", "crosslister", "ebay", "auto_list"],
                "importance": 0.7,
            }
        )

    kpi_updates.append(
        {"name": "crosslist_new_per_run", "value": len(new_finds)}
    )
    kpi_updates.append(
        {"name": "crosslist_low_stock_alerts", "value": 0}  # Veeqo handles stock sync
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
            f"(Veeqo handles stock sync)"
            + (" (notified)" if notified else "")
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(CrossState)
    graph.add_node("scan_rules", node_scan_rules)
    graph.add_node("diff", node_diff)
    graph.add_node("auto_list", node_auto_list)
    graph.add_node("emit", node_emit)
    graph.add_edge(START, "scan_rules")
    graph.add_edge("scan_rules", "diff")
    graph.add_edge("diff", "auto_list")
    graph.add_edge("auto_list", "emit")
    graph.add_edge("emit", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_crosslister", 1, compiled)
