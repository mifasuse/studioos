"""amz_pricer workflow — Milestone 12 (real OpenClaw pricer port).

Mirrors the OpenClaw `amz-pricer` agent's reactive logic, but in a
notify-only mode for now: read BuyBoxPricer for listings that have
lost the buy box, compute the suggested reprice (match buy-box minus
1%, clamped to the listing's min_price floor), and send a single
Telegram digest. No reprice writes yet — that's the next milestone
behind an approval gate.

Heartbeat: every 30 minutes via the M7 scheduler.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class PricerState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    # populated during run
    lost: list[dict[str, Any]]
    recommendations: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


def _underbid_pct(state: PricerState) -> float:
    goals = state.get("goals") or {}
    return float(goals.get("underbid_pct", 1.0))


def _scan_limit(state: PricerState) -> int:
    goals = state.get("goals") or {}
    return int(goals.get("scan_limit", 25))


async def node_scan(state: PricerState) -> dict[str, Any]:
    result = await invoke_from_state(
        state, "buyboxpricer.db.lost_buybox", {"limit": _scan_limit(state)}
    )
    if result["status"] != "ok":
        log.warning(
            "amz_pricer.scan_failed",
            status=result["status"],
            error=result.get("error"),
        )
        return {"lost": []}
    items = (result["data"] or {}).get("items") or []
    return {"lost": items}


def node_recommend(state: PricerState) -> dict[str, Any]:
    lost = state.get("lost") or []
    underbid = _underbid_pct(state)
    recs: list[dict[str, Any]] = []

    for listing in lost:
        buybox = listing.get("buy_box_price")
        current = listing.get("current_price")
        floor = listing.get("min_price")
        if buybox is None or current is None:
            continue

        # Match buy box minus underbid%, but never below the floor.
        proposed = round(buybox * (1.0 - underbid / 100.0), 2)
        clamped_to_floor = False
        if floor is not None and proposed < floor:
            proposed = float(floor)
            clamped_to_floor = True

        # Skip if the proposed is not actually cheaper than current
        # (would reprice us upward — pointless).
        if proposed >= current:
            continue

        delta = round(current - proposed, 2)
        recs.append(
            {
                **listing,
                "proposed_price": proposed,
                "delta": delta,
                "clamped_to_floor": clamped_to_floor,
            }
        )

    return {"recommendations": recs}


def _format_digest(recs: list[dict[str, Any]]) -> str:
    lines = [f"*💰 AMZ Pricer — {len(recs)} reprice önerisi*\n"]
    for r in recs[:10]:
        asin = r.get("asin", "?")
        sku = r.get("sku", "?")
        current = r.get("current_price")
        proposed = r.get("proposed_price")
        delta = r.get("delta")
        bbox = r.get("buy_box_price")
        bbox_seller = r.get("buybox_seller_name") or "?"
        flag = " *⚠ floor*" if r.get("clamped_to_floor") else ""
        lines.append(
            f"• `{asin}` ({sku})\n"
            f"  *${current:.2f}* → *${proposed:.2f}* (-${delta:.2f}){flag}\n"
            f"  buybox ${bbox:.2f} @ {bbox_seller}"
        )
    if len(recs) > 10:
        lines.append(f"\n_+{len(recs) - 10} more_")
    return "\n".join(lines)


async def node_emit(state: PricerState) -> dict[str, Any]:
    recs = state.get("recommendations") or []
    events: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    kpi_updates: list[dict[str, Any]] = []

    state_accum = dict(state.get("state") or {})
    state_accum["scans_total"] = int(state_accum.get("scans_total", 0)) + 1
    state_accum["last_lost_count"] = len(state.get("lost") or [])
    state_accum["last_recommendation_count"] = len(recs)

    for r in recs:
        asin = r.get("asin")
        if not asin:
            continue
        events.append(
            {
                "event_type": "amz.reprice.recommended",
                "event_version": 1,
                "payload": {
                    "asin": asin,
                    "sku": r.get("sku"),
                    "listing_id": r.get("listing_id"),
                    "current_price": r.get("current_price"),
                    "proposed_price": r.get("proposed_price"),
                    "buy_box_price": r.get("buy_box_price"),
                    "buybox_seller_name": r.get("buybox_seller_name"),
                    "delta": r.get("delta"),
                    "clamped_to_floor": r.get("clamped_to_floor", False),
                },
                "idempotency_key": (
                    f"amz_pricer:{state['run_id']}:reprice:{asin}"
                ),
            }
        )

    notification_sent = False
    if recs:
        text = _format_digest(recs)
        notify = await invoke_from_state(
            state,
            "telegram.notify",
            {
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )
        notification_sent = notify["status"] == "ok"
        if notification_sent:
            memories.append(
                {
                    "content": (
                        f"Sent reprice digest with {len(recs)} listings; "
                        f"top ASIN {recs[0].get('asin')}"
                    ),
                    "tags": ["amz", "pricer", "digest"],
                    "importance": 0.4,
                }
            )

    kpi_updates.append(
        {"name": "pricer_recommendations", "value": len(recs)}
    )
    kpi_updates.append(
        {
            "name": "pricer_lost_buybox",
            "value": len(state.get("lost") or []),
        }
    )

    summary = (
        f"{len(state.get('lost') or [])} lost-buybox listings, "
        f"{len(recs)} recommendations"
        + (" (notified)" if notification_sent else "")
    )

    return {
        "events": events,
        "memories": memories,
        "kpi_updates": kpi_updates,
        "state": state_accum,
        "summary": summary,
    }


def build_graph() -> Any:
    graph = StateGraph(PricerState)
    graph.add_node("scan", node_scan)
    graph.add_node("recommend", node_recommend)
    graph.add_node("emit", node_emit)
    graph.add_edge(START, "scan")
    graph.add_edge("scan", "recommend")
    graph.add_edge("recommend", "emit")
    graph.add_edge("emit", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_pricer", 1, compiled)
