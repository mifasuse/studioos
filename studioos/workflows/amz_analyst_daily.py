"""amz_analyst_daily — Daily top-10 opportunity report.

OpenClaw ANALYST.md spec: "Günlük top10 CEO'ya sun."
Runs on a daily schedule, queries PriceFinder for top opportunities,
computes profit + risk scoring, and sends a formatted digest in the
CEO 9-field format to Slack and Telegram.

Workflow: START → collect → score → report → END
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state
from studioos.workflows.amz_analyst_scoring import (
    compute_profit,
    compute_risk,
    decide,
)

log = get_logger(__name__)


class DailyState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    # populated during run
    top_items: list[dict[str, Any]]
    pf_settings: dict[str, float]
    scored_items: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


async def node_collect(state: DailyState) -> dict[str, Any]:
    """Fetch top opportunities + global settings from PriceFinder."""
    goals = state.get("goals") or {}
    limit = int(goals.get("top_n", 10))

    result = await invoke_from_state(
        state,
        "pricefinder.db.top_opportunities",
        {
            "limit": limit,
            "min_profit_dollars": float(goals.get("min_profit_dollars", 5.0)),
            "min_margin_pct": float(goals.get("min_margin_pct", 20.0)),
        },
    )
    items = (
        (result["data"] or {}).get("items") or []
        if result["status"] == "ok"
        else []
    )

    gs = await invoke_from_state(state, "pricefinder.db.global_settings", {})
    pf_settings = gs["data"] or {} if gs["status"] == "ok" else {}

    return {"top_items": items, "pf_settings": pf_settings}


def node_score(state: DailyState) -> dict[str, Any]:
    """Compute profit + risk + verdict for each item."""
    items = state.get("top_items") or []
    pf_settings = state.get("pf_settings") or {}
    scored: list[dict[str, Any]] = []

    exchange_rate = pf_settings.get("exchange_rate")
    for item in items:
        profit = compute_profit(item, pf_settings)
        risk = compute_risk(item, exchange_rate=exchange_rate)
        verdict = decide(
            risk["total"],
            profit.get("roi_pct"),
            item.get("monthly_sold"),
        )
        scored.append({
            **item,
            "_profit": dict(profit),
            "_risk": dict(risk),
            "_verdict": verdict,
        })

    # Sort by net profit descending
    scored.sort(
        key=lambda x: x.get("_profit", {}).get("net_profit_usd") or 0,
        reverse=True,
    )
    return {"scored_items": scored[:10]}


def _format_9field(item: dict[str, Any]) -> str:
    """Format a single item in CEO 9-field format (ANALYST.md / CEO.md)."""
    asin = item.get("asin", "?")
    profit = item.get("_profit") or {}
    risk = item.get("_risk") or {}
    verdict = item.get("_verdict", "?")

    tr_price = item.get("tr_price")
    tr_src = item.get("tr_source") or item.get("source_url") or "TR"
    tr_str = f"₺{tr_price:.0f}" if isinstance(tr_price, (int, float)) else "—"

    buybox = profit.get("buybox_price")
    bb_str = f"${buybox:.2f}" if isinstance(buybox, (int, float)) else "—"

    rank = item.get("sales_rank")
    cat = item.get("category") or item.get("product_group") or "—"
    rank_str = f"#{rank}" if rank else "—"

    ms = item.get("monthly_sold")
    ms_str = f"{ms}/mo" if ms else "—"

    rc = item.get("review_count")
    rating = item.get("rating")
    rev_str = f"{rc} rev · ⭐{rating}" if rc and rating else "—"

    fba = item.get("fba_offer_count")
    fba_str = f"{fba}" if fba is not None else "—"

    ebay = item.get("ebay_price") or item.get("ebay_new_price")
    ebay_str = f"${ebay:.2f}" if isinstance(ebay, (int, float)) else "—"

    net = profit.get("net_profit_usd")
    roi = profit.get("roi_pct")
    margin = profit.get("margin_pct")
    net_str = f"${net:.2f}" if isinstance(net, (int, float)) else "—"
    roi_str = f"{roi:.0f}%" if isinstance(roi, (int, float)) else "—"
    margin_str = f"{margin:.0f}%" if isinstance(margin, (int, float)) else "—"

    return (
        f"• `{asin}` [{verdict}]\n"
        f"  1. ASIN: amazon.com/dp/{asin}\n"
        f"  2. TR: {tr_src} {tr_str}\n"
        f"  3. US BuyBox: {bb_str}\n"
        f"  4. Rank: {rank_str} / {cat}\n"
        f"  5. Aylık satış: {ms_str}\n"
        f"  6. Review: {rev_str}\n"
        f"  7. FBA satıcı: {fba_str}\n"
        f"  8. eBay: {ebay_str}\n"
        f"  9. Net: {net_str} · ROI: {roi_str} · Margin: {margin_str}"
    )


async def node_report(state: DailyState) -> dict[str, Any]:
    """Send daily top-10 digest to Slack + Telegram."""
    scored = state.get("scored_items") or []

    if not scored:
        return {
            "events": [],
            "memories": [],
            "kpi_updates": [],
            "summary": "No top opportunities to report",
        }

    lines = [f"*📊 AMZ Analyst — Günlük Top {len(scored)} Raporu*\n"]
    for item in scored:
        lines.append(_format_9field(item))

    text = "\n".join(lines)

    slack_res = await invoke_from_state(
        state,
        "slack.notify",
        {"text": text, "mrkdwn": True, "unfurl_links": False},
    )
    tg_res = await invoke_from_state(
        state,
        "telegram.notify",
        {
            "text": text[:3500],
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
    )

    notified = (
        slack_res.get("status") == "ok" or tg_res.get("status") == "ok"
    )

    state_accum = dict(state.get("state") or {})
    state_accum["daily_reports_total"] = (
        int(state_accum.get("daily_reports_total", 0)) + 1
    )

    return {
        "events": [
            {
                "event_type": "amz.analyst.daily_report",
                "event_version": 1,
                "payload": {
                    "item_count": len(scored),
                    "top_asin": scored[0].get("asin") if scored else None,
                },
                "idempotency_key": (
                    f"amz_analyst_daily:{state['run_id']}:report"
                ),
            }
        ],
        "memories": [
            {
                "content": (
                    f"Analyst daily top-{len(scored)}: "
                    f"best {scored[0].get('asin')} "
                    f"ROI {scored[0].get('_profit', {}).get('roi_pct')}%"
                ),
                "tags": ["amz", "analyst", "daily", "top10"],
                "importance": 0.5,
            }
        ],
        "kpi_updates": [
            {"name": "analyst_daily_reports", "value": state_accum["daily_reports_total"]},
        ],
        "state": state_accum,
        "summary": (
            f"Daily top-{len(scored)} report"
            + (" (notified)" if notified else "")
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(DailyState)
    graph.add_node("collect", node_collect)
    graph.add_node("score", node_score)
    graph.add_node("report", node_report)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "score")
    graph.add_edge("score", "report")
    graph.add_edge("report", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_analyst_daily", 1, compiled)
