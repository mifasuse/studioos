"""amz_admanager workflow — Amazon PPC ad campaign candidates.

Reads pricefinder.db.ad_candidates: ASINs that are economically
worth running PPC ads on (high monthly_sold, decent reviews +
rating, manageable competition). Sends digest. No actual ad
campaign creation — that lives in AdsOptimizer and is the
next milestone, gated by approval.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class AdState(TypedDict, total=False):
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


async def node_scan(state: AdState) -> dict[str, Any]:
    goals = state.get("goals") or {}
    result = await invoke_from_state(
        state,
        "pricefinder.db.ad_candidates",
        {
            "limit": int(goals.get("scan_limit", 15)),
            "min_monthly_sold": int(goals.get("min_monthly_sold", 50)),
            "min_reviews": int(goals.get("min_reviews", 50)),
            "min_rating": float(goals.get("min_rating", 4.0)),
            "max_competitors": int(goals.get("max_competitors", 15)),
        },
    )
    if result["status"] != "ok":
        log.warning("amz_admanager.scan_failed", error=result.get("error"))
        return {"candidates": []}
    return {"candidates": (result["data"] or {}).get("items") or []}


def node_diff(state: AdState) -> dict[str, Any]:
    cands = state.get("candidates") or []
    existing = dict(state.get("state") or {})
    seen = set(existing.get("ad_candidates_seen") or [])
    new_finds = [c for c in cands if c.get("asin") not in seen]
    for c in new_finds:
        seen.add(c["asin"])
    if len(seen) > 500:
        seen = set(list(seen)[-500:])
    existing["ad_candidates_seen"] = sorted(seen)
    existing["scans_total"] = int(existing.get("scans_total", 0)) + 1
    return {"new_finds": new_finds, "state": existing}


def _format_digest(items: list[dict[str, Any]]) -> str:
    lines = [f"*📣 AMZ AdManager — {len(items)} reklam adayı*"]
    for c in items[:10]:
        asin = c.get("asin", "?")
        ms = c.get("monthly_sold")
        rc = c.get("review_count")
        rating = c.get("rating")
        bb = c.get("buybox_usd")
        comp = c.get("fba_offer_count")
        title = (c.get("title") or "")[:50]
        lines.append(
            f"• `{asin}` ${bb:.0f} · {ms}/mo · {rc} rev · ⭐{rating} · "
            f"comp={comp}\n  {title}"
            if isinstance(bb, (int, float))
            else f"• `{asin}` · {ms}/mo · {rc} rev · ⭐{rating}\n  {title}"
        )
    if len(items) > 10:
        lines.append(f"\n_+{len(items) - 10} more_")
    return "\n".join(lines)


async def node_emit(state: AdState) -> dict[str, Any]:
    new_finds = state.get("new_finds") or []
    events: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    kpi_updates: list[dict[str, Any]] = []

    for c in new_finds:
        events.append(
            {
                "event_type": "amz.ad.candidate",
                "event_version": 1,
                "payload": c,
                "idempotency_key": (
                    f"amz_admanager:{state['run_id']}:{c.get('asin')}"
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
                    f"AdManager surfaced {len(new_finds)} new ad candidates; "
                    f"top {new_finds[0].get('asin')} "
                    f"({new_finds[0].get('monthly_sold')}/mo)"
                ),
                "tags": ["amz", "admanager", "ppc"],
                "importance": 0.5,
            }
        )

    kpi_updates.append(
        {"name": "ad_candidates_new", "value": len(new_finds)}
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
    graph = StateGraph(AdState)
    graph.add_node("scan", node_scan)
    graph.add_node("diff", node_diff)
    graph.add_node("emit", node_emit)
    graph.add_edge(START, "scan")
    graph.add_edge("scan", "diff")
    graph.add_edge("diff", "emit")
    graph.add_edge("emit", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_admanager", 1, compiled)
