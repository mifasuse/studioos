"""analyst_test workflow — Milestone 1 vertical slice consumer.

Behavior:
  START → read_event → evaluate → acknowledge → END

Receives trigger payload from outbox (a test.opportunity.detected event) and
emits a test.opportunity.acknowledged event in response.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.runtime.workflow_registry import register_workflow


class AnalystState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    recent_memories: list[dict[str, Any]]
    kpis: list[dict[str, Any]]
    # populated during run
    received_event: dict[str, Any] | None
    verdict: str
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


def node_read_event(state: AnalystState) -> dict[str, Any]:
    return {"received_event": state.get("input", {})}


def node_evaluate(state: AnalystState) -> dict[str, Any]:
    ev = state.get("received_event") or {}
    payload = ev.get("payload") or {}
    value = float(payload.get("value", 0))
    verdict = "accept" if value >= 50 else "reject"
    return {"verdict": verdict}


def node_acknowledge(state: AnalystState) -> dict[str, Any]:
    ev = state.get("received_event") or {}
    payload = ev.get("payload") or {}
    verdict = state.get("verdict", "unknown")

    events: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    kpi_updates: list[dict[str, Any]] = []

    if payload.get("opportunity_id"):
        events.append(
            {
                "event_type": "test.opportunity.acknowledged",
                "event_version": 1,
                "payload": {
                    "opportunity_id": payload["opportunity_id"],
                    "verdict": verdict,
                    "notes": f"value={payload.get('value')}",
                },
                "causation_id": ev.get("event_id"),
                "idempotency_key": f"analyst:{state['run_id']}:{payload['opportunity_id']}",
            }
        )
        memories.append(
            {
                "content": (
                    f"Verdict {verdict.upper()} for opportunity "
                    f"{payload['opportunity_id']} (value={payload.get('value')})"
                ),
                "tags": ["verdict", verdict, "analyst_test"],
                "importance": 0.6 if verdict == "accept" else 0.4,
            }
        )

    existing_state = dict(state.get("state", {}))
    evaluated = int(existing_state.get("evaluated_total", 0)) + 1
    accept_total = int(existing_state.get("accept_total", 0))
    reject_total = int(existing_state.get("reject_total", 0))
    if verdict == "accept":
        accept_total += 1
    elif verdict == "reject":
        reject_total += 1

    existing_state["evaluated_total"] = evaluated
    existing_state["accept_total"] = accept_total
    existing_state["reject_total"] = reject_total

    kpi_updates.append({"name": "evaluated_total", "value": evaluated})
    if evaluated > 0:
        kpi_updates.append(
            {
                "name": "acceptance_rate",
                "value": round(accept_total / evaluated, 4),
            }
        )

    return {
        "events": events,
        "memories": memories,
        "kpi_updates": kpi_updates,
        "summary": f"Evaluated opportunity → {verdict}",
        "state": existing_state,
    }


def build_graph() -> Any:
    graph = StateGraph(AnalystState)
    graph.add_node("read_event", node_read_event)
    graph.add_node("evaluate", node_evaluate)
    graph.add_node("acknowledge", node_acknowledge)

    graph.add_edge(START, "read_event")
    graph.add_edge("read_event", "evaluate")
    graph.add_edge("evaluate", "acknowledge")
    graph.add_edge("acknowledge", END)

    return graph.compile()


compiled = build_graph()

register_workflow("analyst_test", 1, compiled)
