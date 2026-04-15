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
    aging: list[dict[str, Any]]
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
    goals = state.get("goals") or {}
    lost_result = await invoke_from_state(
        state, "buyboxpricer.db.lost_buybox", {"limit": _scan_limit(state)}
    )
    lost = (
        (lost_result["data"] or {}).get("items") or []
        if lost_result["status"] == "ok"
        else []
    )
    if lost_result["status"] != "ok":
        log.warning(
            "amz_pricer.lost_scan_failed", error=lost_result.get("error")
        )

    aging_result = await invoke_from_state(
        state,
        "buyboxpricer.db.aging_inventory",
        {
            "limit": int(goals.get("aging_limit", 25)),
            "min_age_days": int(goals.get("min_age_days", 90)),
        },
    )
    aging = (
        (aging_result["data"] or {}).get("items") or []
        if aging_result["status"] == "ok"
        else []
    )
    if aging_result["status"] != "ok":
        log.warning(
            "amz_pricer.aging_scan_failed", error=aging_result.get("error")
        )

    return {"lost": lost, "aging": aging}


def _pick_strategy(listing: dict[str, Any]) -> tuple[str, str]:
    """Return (strategy_id, rationale) for a single listing.

    Three modes mirror the OpenClaw amz-pricer playbook:

      buy_box_win    — competitor active, buybox lost, beat them by
                       underbid% (default mode for fresh stock)
      profit_max     — low competition (≤ 3 offers), recently selling,
                       push price UP toward max_price
      stock_bleed    — old stock (> 90d) sitting on the shelf,
                       aggressive cut to the floor to clear it
    """
    age = listing.get("age_days") or 0
    comp = listing.get("competitor_count") or 0
    has_buybox = bool(listing.get("has_buybox"))

    if age >= 90:
        return "stock_bleed", f"{int(age)}d age — aggressive clearance"
    if comp <= 3 and has_buybox:
        return "profit_max", f"low comp ({comp}) + buybox held — push up"
    return "buy_box_win", "lost buybox to competitor — match-1%"


def _propose_price(
    listing: dict[str, Any],
    strategy: str,
    underbid_pct: float,
) -> tuple[float | None, bool, str]:
    """Compute the proposed price for a listing under the given strategy.

    Returns (proposed_price, clamped_flag, reason).
    """
    buybox = listing.get("buy_box_price")
    current = listing.get("current_price")
    floor = listing.get("min_price")
    ceiling = listing.get("max_price")

    if current is None:
        return None, False, "no current_price"

    if strategy == "buy_box_win":
        if buybox is None:
            return None, False, "no buy_box_price"
        target = round(buybox * (1.0 - underbid_pct / 100.0), 2)
        clamped = False
        if floor is not None and target < floor:
            target = float(floor)
            clamped = True
        if target >= current:
            return None, False, "match would not lower price"
        return target, clamped, "match buybox −1%"

    if strategy == "profit_max":
        target = round(current * 1.05, 2)
        clamped = False
        if ceiling is not None and target > ceiling:
            target = float(ceiling)
            clamped = True
        if target <= current:
            return None, False, "ceiling reached"
        return target, clamped, "+5% on low competition"

    if strategy == "stock_bleed":
        if floor is None:
            return None, False, "no floor for stock bleed"
        target = float(floor)
        if target >= current:
            return None, False, "already at floor"
        return target, True, "drop to floor to clear aging stock"

    return None, False, "unknown strategy"


def node_recommend(state: PricerState) -> dict[str, Any]:
    lost = state.get("lost") or []
    aging = state.get("aging") or []
    underbid = _underbid_pct(state)

    # Combine lost-buybox and aging items, deduping by listing_id.
    by_id: dict[int, dict[str, Any]] = {}
    for listing in lost:
        lid = listing.get("listing_id")
        if lid is not None:
            by_id[lid] = listing
    for listing in aging:
        lid = listing.get("listing_id")
        if lid is None:
            continue
        merged = dict(by_id.get(lid, {}))
        merged.update(listing)
        # carry through buybox info from lost row if both sources hit
        if not merged.get("has_buybox") and "has_buybox" in listing:
            merged["has_buybox"] = listing["has_buybox"]
        by_id[lid] = merged

    recs: list[dict[str, Any]] = []
    for listing in by_id.values():
        strategy, rationale = _pick_strategy(listing)
        proposed, clamped, reason = _propose_price(listing, strategy, underbid)
        if proposed is None:
            continue
        current = listing.get("current_price") or 0
        delta = round(current - proposed, 2)
        recs.append(
            {
                **listing,
                "strategy": strategy,
                "strategy_rationale": rationale,
                "price_reason": reason,
                "proposed_price": proposed,
                "delta": delta,
                "clamped_to_floor": clamped,
            }
        )

    return {"recommendations": recs}


_STRATEGY_ICON = {
    "buy_box_win": "🥊",
    "profit_max": "📈",
    "stock_bleed": "🔥",
}


def _format_digest(recs: list[dict[str, Any]]) -> str:
    by_strategy: dict[str, int] = {}
    for r in recs:
        s = r.get("strategy") or "?"
        by_strategy[s] = by_strategy.get(s, 0) + 1
    breakdown = " · ".join(
        f"{_STRATEGY_ICON.get(k, '•')} {k}={v}" for k, v in by_strategy.items()
    )
    lines = [
        f"*💰 AMZ Pricer — {len(recs)} öneri*",
        f"_{breakdown}_\n",
    ]
    for r in recs[:10]:
        asin = r.get("asin", "?")
        sku = r.get("sku", "?")
        current = r.get("current_price")
        proposed = r.get("proposed_price")
        delta = r.get("delta")
        strat = r.get("strategy", "?")
        icon = _STRATEGY_ICON.get(strat, "•")
        flag = " *⚠ clamped*" if r.get("clamped_to_floor") else ""
        rationale = (r.get("strategy_rationale") or "")[:50]
        lines.append(
            f"{icon} `{asin}` ({sku}) — *{strat}*\n"
            f"  ${current} → ${proposed} ({'-' if delta and delta > 0 else '+'}${abs(delta or 0):.2f}){flag}\n"
            f"  _{rationale}_"
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
                    "strategy": r.get("strategy", "buy_box_win"),
                    "strategy_rationale": r.get("strategy_rationale"),
                    "age_days": r.get("age_days"),
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
