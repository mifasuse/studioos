"""app_studio_pulse — minimal heartbeat for the App Studio.

Phase 1 of the App Studio migration: prove the multi-studio platform
works end-to-end with a real (not stub) workflow. Pulse reads the
studio's tracked_apps goal + any recent runs/events scoped to the
app-studio studio_id, and sends a single Telegram heartbeat once a
day. Real Play Store / RevenueCat / App Store Connect integrations
will land in later milestones as new tools the agent's tool_scope
opts into.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select

from studioos.db import session_scope
from studioos.logging import get_logger
from studioos.models import AgentRun, Event
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class PulseState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    # populated during run
    snapshot: dict[str, Any]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


async def node_collect(state: PulseState) -> dict[str, Any]:
    studio_id = state.get("studio_id") or "app-studio"
    since = datetime.now(UTC) - timedelta(hours=24)
    async with session_scope() as session:
        runs_total = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AgentRun)
                    .where(AgentRun.studio_id == studio_id)
                    .where(AgentRun.created_at >= since)
                )
            ).scalar_one()
        )
        events_total = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Event)
                    .where(Event.studio_id == studio_id)
                    .where(Event.recorded_at >= since)
                )
            ).scalar_one()
        )
    goals = state.get("goals") or {}
    snapshot = {
        "studio_id": studio_id,
        "tracked_apps": goals.get("tracked_apps") or [],
        "runs_last_24h": runs_total,
        "events_last_24h": events_total,
    }
    return {"snapshot": snapshot}


def _format_pulse(snap: dict[str, Any]) -> str:
    apps = snap.get("tracked_apps") or []
    apps_str = ", ".join(apps) if apps else "_none yet_"
    return (
        "*📱 App Studio Pulse*\n"
        f"Tracked apps: {apps_str}\n"
        f"Runs (24h): {snap.get('runs_last_24h', 0)}\n"
        f"Events (24h): {snap.get('events_last_24h', 0)}\n"
        "_Real metric tools land in upcoming milestones_"
    )


async def node_emit(state: PulseState) -> dict[str, Any]:
    snap = state.get("snapshot") or {}
    text = _format_pulse(snap)
    notify = await invoke_from_state(
        state,
        "telegram.notify",
        {
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
    )
    notified = notify["status"] == "ok"
    if not notified:
        log.warning(
            "app_studio_pulse.notify_failed", error=notify.get("error")
        )

    state_accum = dict(state.get("state") or {})
    state_accum["pulses_total"] = int(state_accum.get("pulses_total", 0)) + 1

    return {
        "memories": [
            {
                "content": (
                    f"Pulse: {snap.get('runs_last_24h', 0)} runs, "
                    f"{snap.get('events_last_24h', 0)} events in last 24h"
                ),
                "tags": ["app-studio", "pulse", "heartbeat"],
                "importance": 0.3,
            }
        ],
        "kpi_updates": [
            {"name": "pulses_total", "value": state_accum["pulses_total"]},
            {"name": "runs_last_24h", "value": snap.get("runs_last_24h", 0)},
            {"name": "events_last_24h", "value": snap.get("events_last_24h", 0)},
        ],
        "state": state_accum,
        "summary": (
            f"Pulse: {snap.get('runs_last_24h', 0)} runs, "
            f"{snap.get('events_last_24h', 0)} events"
            + (" (notified)" if notified else "")
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(PulseState)
    graph.add_node("collect", node_collect)
    graph.add_node("emit", node_emit)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "emit")
    graph.add_edge("emit", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_pulse", 1, compiled)
