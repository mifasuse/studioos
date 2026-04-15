"""scout_test workflow — Milestone 1 vertical slice producer.

Behavior:
  START → fetch_data (mock) → detect_opportunity → emit_event → END

Output contract (consumed by runner.execute_run):
  {
    "state":   <new agent state>,
    "events":  [ {event_type, event_version, payload, ...}, ... ],
    "summary": <short human text>,
  }
"""
from __future__ import annotations

from random import random
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from studioos.runtime.workflow_registry import register_workflow


class ScoutState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    trigger_ref: str | None
    input: dict[str, Any]
    config: dict[str, Any]
    goals: dict[str, Any]
    # populated during run
    scan_id: str
    mock_data: dict[str, Any]
    opportunity: dict[str, Any] | None
    events: list[dict[str, Any]]
    summary: str


def node_fetch_data(state: ScoutState) -> dict[str, Any]:
    scan_id = uuid4().hex[:8]
    value = round(random() * 100, 2)
    return {
        "scan_id": scan_id,
        "mock_data": {
            "id": f"opp-{scan_id}",
            "value": value,
            "label": f"mock-opportunity-{scan_id}",
        },
    }


def node_detect_opportunity(state: ScoutState) -> dict[str, Any]:
    data = state["mock_data"]
    opportunity = data if data["value"] > 10 else None
    return {"opportunity": opportunity}


def node_emit_event(state: ScoutState) -> dict[str, Any]:
    opportunity = state.get("opportunity")
    events: list[dict[str, Any]] = []
    summary: str

    if opportunity:
        events.append(
            {
                "event_type": "test.opportunity.detected",
                "event_version": 1,
                "payload": {
                    "opportunity_id": opportunity["id"],
                    "value": opportunity["value"],
                    "label": opportunity["label"],
                    "source": "scout_test",
                },
                "idempotency_key": f"scout:{state['run_id']}:{opportunity['id']}",
            }
        )
        summary = (
            f"Detected opportunity {opportunity['id']} (value={opportunity['value']})"
        )
    else:
        summary = "No opportunity this scan"

    # increment counter in agent state
    existing_state = dict(state.get("state", {}))
    existing_state["scans_total"] = int(existing_state.get("scans_total", 0)) + 1
    existing_state["opportunities_found"] = int(
        existing_state.get("opportunities_found", 0)
    ) + (1 if opportunity else 0)
    existing_state["last_scan_id"] = state["scan_id"]

    return {
        "events": events,
        "summary": summary,
        "state": existing_state,
    }


def build_graph() -> Any:
    graph = StateGraph(ScoutState)
    graph.add_node("fetch_data", node_fetch_data)
    graph.add_node("detect_opportunity", node_detect_opportunity)
    graph.add_node("emit_event", node_emit_event)

    graph.add_edge(START, "fetch_data")
    graph.add_edge("fetch_data", "detect_opportunity")
    graph.add_edge("detect_opportunity", "emit_event")
    graph.add_edge("emit_event", END)

    return graph.compile()


compiled = build_graph()

register_workflow("scout_test", 1, compiled)
