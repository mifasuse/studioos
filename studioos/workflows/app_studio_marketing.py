"""app_studio_marketing — daily campaign monitoring + country ROI + VoC.

Workflow: START → collect → analyze → voc → report → END

collect:  hub.api.campaigns (action=list) + hub.api.metrics (metric=countries,
          days=7) for each tracked app.
analyze:  flag_underperforming_countries() pure function — ROI < min_roi.
          Flag campaigns with zero spend.
voc:      Weekly Voice of Customer — web search for app reviews/sentiment.
report:   Slack #growth-ops digest + Telegram + Memory + app.marketing.report event.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pure function — no I/O, fully testable
# ---------------------------------------------------------------------------

def flag_underperforming_countries(
    countries_data: list[dict[str, Any]],
    min_roi: float = 0,
) -> list[dict[str, Any]]:
    """Return entries where ROI < min_roi."""
    return [c for c in countries_data if float(c.get("roi", 0)) < min_roi]


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

class MarketingState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    # populated during run
    campaigns: list[dict[str, Any]]
    countries_data: dict[str, list[dict[str, Any]]]  # app_id → country rows
    flagged_countries: list[dict[str, Any]]
    zero_spend_campaigns: list[dict[str, Any]]
    voc_insights: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def node_collect(state: MarketingState) -> dict[str, Any]:
    """Fetch campaign list + country metrics for each tracked app."""
    goals = state.get("goals") or {}
    tracked_apps: list[str] = goals.get("tracked_apps") or []

    # Fetch campaigns
    campaigns_result = await invoke_from_state(
        state, "hub.api.campaigns", {"action": "list"}
    )
    campaigns: list[dict[str, Any]] = []
    if campaigns_result.get("status") == "ok":
        raw = campaigns_result.get("data") or {}
        campaigns = raw if isinstance(raw, list) else raw.get("campaigns", [])

    # Fetch country metrics per app
    countries_data: dict[str, list[dict[str, Any]]] = {}
    for app_id in tracked_apps:
        result = await invoke_from_state(
            state,
            "hub.api.metrics",
            {"app_id": app_id, "metric": "countries", "days": 7},
        )
        if result.get("status") == "ok":
            raw = result.get("data") or {}
            rows = raw if isinstance(raw, list) else raw.get("countries", [])
            countries_data[app_id] = rows

    return {"campaigns": campaigns, "countries_data": countries_data}


async def node_analyze(state: MarketingState) -> dict[str, Any]:
    """Flag underperforming countries and zero-spend campaigns."""
    goals = state.get("goals") or {}
    min_roi: float = float((goals.get("thresholds") or {}).get("min_roi", 0))

    countries_data = state.get("countries_data") or {}
    campaigns = state.get("campaigns") or []

    all_flagged: list[dict[str, Any]] = []
    for app_id, rows in countries_data.items():
        flagged = flag_underperforming_countries(rows, min_roi=min_roi)
        for f in flagged:
            f = dict(f)
            f["app_id"] = app_id
            all_flagged.append(f)

    zero_spend = [c for c in campaigns if float(c.get("spend", 0)) == 0]

    return {
        "flagged_countries": all_flagged,
        "zero_spend_campaigns": zero_spend,
    }


_VOC_APP_NAMES: dict[str, str] = {
    "quit_smoking": "Quit Smoking Now",
    "sms_forward": "SMS Forward",
    "moodmate": "MoodMate",
}


async def node_voc(state: MarketingState) -> dict[str, Any]:
    """Weekly Voice of Customer — search web for app reviews and sentiment.

    Runs every 7th report (weekly cadence on top of daily schedule).
    Searches for recent reviews, complaints, and feature requests.
    """
    state_accum = dict(state.get("state") or {})
    reports_total = int(state_accum.get("reports_total", 0))

    # Only run VoC weekly (every 7th marketing report)
    if reports_total % 7 != 0:
        return {"voc_insights": []}

    goals = state.get("goals") or {}
    tracked_apps: list[str] = goals.get("tracked_apps") or []
    insights: list[dict[str, Any]] = []

    for app_id in tracked_apps:
        app_name = _VOC_APP_NAMES.get(app_id, app_id)
        queries = [
            f'"{app_name}" app review',
            f'"{app_name}" app store rating complaint',
        ]
        for query in queries:
            result = await invoke_from_state(
                state, "web.search", {"query": query, "max_results": 5}
            )
            if result.get("status") == "ok":
                for item in (result.get("data") or {}).get("results", [])[:3]:
                    insights.append({
                        "app_id": app_id,
                        "title": item.get("title", ""),
                        "snippet": item.get("snippet", "")[:200],
                        "url": item.get("url", ""),
                    })

    return {"voc_insights": insights}


async def node_report(state: MarketingState) -> dict[str, Any]:
    """Send Slack + Telegram digest, save memory, emit app.marketing.report."""
    flagged = state.get("flagged_countries") or []
    zero_spend = state.get("zero_spend_campaigns") or []
    campaigns = state.get("campaigns") or []
    countries_data = state.get("countries_data") or {}
    voc_insights = state.get("voc_insights") or []

    # Build message
    lines: list[str] = ["*App Studio Marketing Daily Report*"]
    lines.append(f"Campaigns: {len(campaigns)} total, {len(zero_spend)} zero-spend")
    if zero_spend:
        for c in zero_spend[:3]:
            lines.append(f"  • zero-spend: `{c.get('campaign_id', c.get('name', '?'))}`")
    lines.append(f"Underperforming countries (ROI < threshold): {len(flagged)}")
    for f in flagged[:5]:
        lines.append(
            f"  • {f.get('app_id', '?')} / {f.get('country', '?')} — ROI {f.get('roi', '?')}"
        )
    if voc_insights:
        lines.append(f"\n*VoC (Voice of Customer) — {len(voc_insights)} mentions:*")
        for v in voc_insights[:5]:
            lines.append(
                f"  • [{v.get('app_id')}] {v.get('title', '')[:60]}\n"
                f"    {v.get('snippet', '')[:100]}"
            )

    text = "\n".join(lines)

    slack_result = await invoke_from_state(
        state,
        "slack.notify",
        {"text": text, "mrkdwn": True},
    )
    tg_result = await invoke_from_state(
        state,
        "telegram.notify",
        {"text": text, "parse_mode": "Markdown", "disable_web_page_preview": True},
    )

    # Build events per app
    events: list[dict[str, Any]] = []
    for app_id in (state.get("goals") or {}).get("tracked_apps") or []:
        app_flagged = [f for f in flagged if f.get("app_id") == app_id]
        events.append(
            {
                "event_type": "app.marketing.report",
                "event_version": 1,
                "payload": {
                    "app_id": app_id,
                    "flagged_countries": app_flagged,
                    "campaign_count": len(campaigns),
                    "summary": text[:300],
                },
                "idempotency_key": (
                    f"marketing:{state.get('run_id', '')}:report:{app_id}"
                ),
            }
        )

    memories: list[dict[str, Any]] = [
        {
            "content": (
                f"Marketing daily: {len(campaigns)} campaigns, "
                f"{len(zero_spend)} zero-spend, "
                f"{len(flagged)} underperforming countries"
            ),
            "tags": ["app-studio", "marketing", "daily"],
            "importance": 0.5 if flagged or zero_spend else 0.2,
        }
    ]

    state_accum = dict(state.get("state") or {})
    state_accum["reports_total"] = int(state_accum.get("reports_total", 0)) + 1

    notified = (
        slack_result.get("status") == "ok" or tg_result.get("status") == "ok"
    )
    return {
        "events": events,
        "memories": memories,
        "kpi_updates": [
            {"name": "marketing_zero_spend_campaigns", "value": len(zero_spend)},
            {"name": "marketing_underperforming_countries", "value": len(flagged)},
        ],
        "state": state_accum,
        "summary": (
            f"{len(campaigns)} campaigns, {len(flagged)} flagged countries"
            + (" (notified)" if notified else "")
        ),
    }


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_graph() -> Any:
    graph = StateGraph(MarketingState)
    graph.add_node("collect", node_collect)
    graph.add_node("analyze", node_analyze)
    graph.add_node("voc", node_voc)
    graph.add_node("report", node_report)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "analyze")
    graph.add_edge("analyze", "voc")
    graph.add_edge("voc", "report")
    graph.add_edge("report", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_marketing", 1, compiled)
