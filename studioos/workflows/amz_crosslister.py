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
    new_finds: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


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


def node_diff(state: CrossState) -> dict[str, Any]:
    candidates = state.get("candidates") or []
    existing = dict(state.get("state") or {})
    seen = set(existing.get("crosslisted_asins") or [])

    new_finds = [c for c in candidates if c.get("asin") not in seen]
    for c in new_finds:
        seen.add(c["asin"])

    if len(seen) > 500:
        seen = set(list(seen)[-500:])

    existing["crosslisted_asins"] = sorted(seen)
    existing["scans_total"] = int(existing.get("scans_total", 0)) + 1
    existing["last_new_count"] = len(new_finds)

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
    events: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    kpi_updates: list[dict[str, Any]] = []

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

    return {
        "events": events,
        "memories": memories,
        "kpi_updates": kpi_updates,
        "summary": (
            f"{len(state.get('candidates') or [])} candidates, "
            f"{len(new_finds)} new"
            + (" (notified)" if notified else "")
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(CrossState)
    graph.add_node("scan", node_scan)
    graph.add_node("diff", node_diff)
    graph.add_node("emit", node_emit)
    graph.add_edge(START, "scan")
    graph.add_edge("scan", "diff")
    graph.add_edge("diff", "emit")
    graph.add_edge("emit", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_crosslister", 1, compiled)
