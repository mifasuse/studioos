"""amz_executor workflow — Milestone 11 (phase 1, notify-only).

First "write" agent in the AMZ studio. It subscribes to
`amz.opportunity.confirmed` events emitted by the analyst and
relays them to the human via Telegram. No Amazon writes, no
PriceFinder mutations — just notifications for now. M11 phase 2
can extend this with PriceFinder status=='action_requested' mutations
and, later, direct repricing API calls gated by approvals.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class ExecutorState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    # populated during run
    opportunity: dict[str, Any]
    notification: dict[str, Any]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


def _format_message(opp: dict[str, Any]) -> str:
    asin = opp.get("asin", "???")
    prev = opp.get("previous_price")
    curr = opp.get("current_price")
    delta = opp.get("delta_pct")
    conf = opp.get("confidence")
    rationale = (opp.get("rationale") or "")[:400]
    action = opp.get("recommended_action") or "—"

    lines = [
        "*AMZ Opportunity Confirmed*",
        f"`{asin}` · https://www.amazon.com/dp/{asin}",
        f"Previous → Current: *{prev}* → *{curr}* ({delta:+.2f}%)"
        if prev is not None and curr is not None and delta is not None
        else f"Current: {curr}",
        f"Confidence: *{conf:.2f}*"
        if isinstance(conf, (int, float))
        else "",
        f"Action: _{action}_",
        "",
        f"_{rationale}_" if rationale else "",
    ]
    return "\n".join(line for line in lines if line)


def _extract_opportunity(state: ExecutorState) -> dict[str, Any]:
    event = state.get("input") or {}
    payload = event.get("payload") or {}
    return {
        "asin": payload.get("asin"),
        "marketplace": payload.get("marketplace", "US"),
        "previous_price": payload.get("previous_price"),
        "current_price": payload.get("current_price"),
        "delta_pct": payload.get("delta_pct"),
        "direction": payload.get("direction"),
        "verdict": payload.get("verdict"),
        "confidence": payload.get("confidence"),
        "rationale": payload.get("rationale"),
        "recommended_action": payload.get("recommended_action"),
    }


async def node_read(state: ExecutorState) -> dict[str, Any]:
    opp = _extract_opportunity(state)
    return {"opportunity": opp}


async def node_notify(state: ExecutorState) -> dict[str, Any]:
    opp = state.get("opportunity") or {}
    asin = opp.get("asin") or "unknown"
    text = _format_message(opp)

    result = await invoke_from_state(
        state,
        "telegram.notify",
        {
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
    )

    existing_state = dict(state.get("state") or {})
    notified_total = int(existing_state.get("notified_total", 0)) + 1
    existing_state["notified_total"] = notified_total

    memories: list[dict[str, Any]] = []
    kpi_updates: list[dict[str, Any]] = [
        {"name": "notified_total", "value": notified_total},
    ]

    if result["status"] == "ok":
        memories.append(
            {
                "content": (
                    f"Notified Telegram about confirmed opportunity {asin} "
                    f"(conf={opp.get('confidence')}, action={opp.get('recommended_action')})"
                ),
                "tags": ["amz", "notify", "telegram", "opportunity", asin],
                "importance": 0.5,
            }
        )
        summary = f"Telegram notified for {asin}"
    else:
        log.error(
            "amz_executor.notify_failed",
            asin=asin,
            status=result["status"],
            error=result.get("error"),
        )
        memories.append(
            {
                "content": (
                    f"FAILED Telegram notify for {asin}: "
                    f"{result.get('error', 'unknown')}"
                ),
                "tags": ["amz", "notify", "failed", asin],
                "importance": 0.8,
            }
        )
        summary = f"Telegram notify FAILED for {asin}"

    return {
        "notification": result,
        "memories": memories,
        "kpi_updates": kpi_updates,
        "state": existing_state,
        "summary": summary,
    }


def build_graph() -> Any:
    graph = StateGraph(ExecutorState)
    graph.add_node("read", node_read)
    graph.add_node("notify", node_notify)
    graph.add_edge(START, "read")
    graph.add_edge("read", "notify")
    graph.add_edge("notify", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_executor", 1, compiled)
