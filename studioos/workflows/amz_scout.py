"""amz_scout workflow — Milestone 12 (real OpenClaw scout port).

Mirrors the OpenClaw `amz-scout` agent's job:
  1. Query PriceFinder for products matching the strict filter rules
     (ROI > 20%, sales_rank < 100k, monthly_sold > 30, rating > 3.5,
     review_count > 10, in_stock, valid TR price, etc).
  2. Compare the result to the agent's own state.discovered_asins to
     find the ASINs surfacing for the first time.
  3. Emit `amz.opportunity.discovered` events for the new ones AND
     send a single Telegram digest so the human can act.

Read-only. No PriceFinder mutations. Designed to run on an hours-scale
cadence (6h default) so MiniMax doesn't get spammed — the analyst
remains the per-anomaly evaluator on the price-monitor path.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class ScoutState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    # populated during run
    candidates: list[dict[str, Any]]
    new_finds: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


def _scout_params(state: ScoutState) -> dict[str, Any]:
    goals = state.get("goals") or {}
    return {
        "limit": int(goals.get("scan_limit", 20)),
        "min_roi_pct": float(goals.get("min_roi_pct", 20.0)),
        "max_roi_pct": float(goals.get("max_roi_pct", 1000.0)),
        "max_sales_rank": int(goals.get("max_sales_rank", 100_000)),
        "min_monthly_sold": int(goals.get("min_monthly_sold", 30)),
        "min_rating": float(goals.get("min_rating", 3.5)),
        "min_review_count": int(goals.get("min_review_count", 10)),
        "min_profit_dollars": float(goals.get("min_profit_dollars", 10.0)),
        "min_tr_price": float(goals.get("min_tr_price", 5.0)),
    }


async def node_scan(state: ScoutState) -> dict[str, Any]:
    params = _scout_params(state)
    result = await invoke_from_state(
        state, "pricefinder.db.scout_candidates", params
    )
    if result["status"] != "ok":
        log.warning(
            "amz_scout.scan_failed",
            status=result["status"],
            error=result.get("error"),
        )
        return {"candidates": []}
    items = (result["data"] or {}).get("items") or []
    return {"candidates": items}


def node_diff(state: ScoutState) -> dict[str, Any]:
    candidates = state.get("candidates") or []
    existing_state = dict(state.get("state") or {})
    discovered = set(existing_state.get("discovered_asins") or [])

    new_finds: list[dict[str, Any]] = []
    for c in candidates:
        asin = c.get("asin")
        if not asin or asin in discovered:
            continue
        new_finds.append(c)
        discovered.add(asin)

    # Cap how many ASIN ids we hold to avoid unbounded state growth.
    if len(discovered) > 1000:
        discovered = set(list(discovered)[-1000:])

    existing_state["discovered_asins"] = sorted(discovered)
    existing_state["last_scan_count"] = len(candidates)
    existing_state["last_new_count"] = len(new_finds)
    existing_state["scans_total"] = int(
        existing_state.get("scans_total", 0)
    ) + 1
    existing_state["discoveries_total"] = int(
        existing_state.get("discoveries_total", 0)
    ) + len(new_finds)

    return {"new_finds": new_finds, "state": existing_state}


def _format_digest(new_finds: list[dict[str, Any]]) -> str:
    if not new_finds:
        return ""
    lines = [f"*🔍 AMZ Scout — {len(new_finds)} yeni fırsat*\n"]
    for c in new_finds[:10]:
        asin = c.get("asin", "?")
        title = (c.get("title") or "")[:60]
        roi = c.get("roi_percent")
        profit = c.get("estimated_profit")
        ms = c.get("monthly_sold")
        rank = c.get("sales_rank")
        roi_str = f"{roi:.0f}%" if isinstance(roi, (int, float)) else "—"
        profit_str = f"${profit:.0f}" if isinstance(profit, (int, float)) else "—"
        ms_str = f"{ms}/mo" if ms else "—"
        rank_str = f"#{rank}" if rank else "—"
        lines.append(
            f"• `{asin}` ROI {roi_str} · {profit_str} · {ms_str} · rank {rank_str}\n"
            f"  {title}"
        )
    if len(new_finds) > 10:
        lines.append(f"\n_+{len(new_finds) - 10} more_")
    return "\n".join(lines)


async def node_emit(state: ScoutState) -> dict[str, Any]:
    new_finds = state.get("new_finds") or []
    events: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    kpi_updates: list[dict[str, Any]] = []

    for c in new_finds:
        asin = c.get("asin")
        if not asin:
            continue
        events.append(
            {
                "event_type": "amz.opportunity.discovered",
                "event_version": 1,
                "payload": {
                    "asin": asin,
                    "marketplace": "US",
                    "title": (c.get("title") or "")[:200],
                    "brand": c.get("brand"),
                    "tr_price_try": c.get("tr_price"),
                    "buybox_price_usd": c.get("buybox_price"),
                    "estimated_profit_usd": c.get("estimated_profit"),
                    "profit_margin_pct": c.get("profit_margin_percent"),
                    "roi_pct": c.get("roi_percent"),
                    "monthly_sold": c.get("monthly_sold"),
                    "sales_rank": c.get("sales_rank"),
                    "review_count": c.get("review_count"),
                    "rating": c.get("rating"),
                    "fba_offer_count": c.get("fba_offer_count"),
                },
                "idempotency_key": (
                    f"amz_scout:{state['run_id']}:discovered:{asin}"
                ),
            }
        )
        memories.append(
            {
                "content": (
                    f"Scouted new opportunity {asin}: "
                    f"ROI {c.get('roi_percent')}%, "
                    f"profit ${c.get('estimated_profit')}, "
                    f"{c.get('monthly_sold')}/mo sold"
                ),
                "tags": ["amz", "scouted", asin],
                "importance": 0.6,
            }
        )

    # Single digest notification, only if we actually found something.
    notification_sent = False
    if new_finds:
        text = _format_digest(new_finds)
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
        if not notification_sent:
            log.warning(
                "amz_scout.notify_failed",
                status=notify["status"],
                error=notify.get("error"),
            )

    state_accum = state.get("state") or {}
    kpi_updates.append(
        {
            "name": "scout_discoveries_total",
            "value": int(state_accum.get("discoveries_total", 0)),
        }
    )
    kpi_updates.append(
        {"name": "scout_new_per_run", "value": len(new_finds)}
    )

    summary = (
        f"Scouted {len(state.get('candidates') or [])} candidates, "
        f"{len(new_finds)} new"
        + (" (notified)" if notification_sent else "")
    )

    return {
        "events": events,
        "memories": memories,
        "kpi_updates": kpi_updates,
        "summary": summary,
    }


def build_graph() -> Any:
    graph = StateGraph(ScoutState)
    graph.add_node("scan", node_scan)
    graph.add_node("diff", node_diff)
    graph.add_node("emit", node_emit)
    graph.add_edge(START, "scan")
    graph.add_edge("scan", "diff")
    graph.add_edge("diff", "emit")
    graph.add_edge("emit", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_scout", 1, compiled)
