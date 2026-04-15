"""amz_reflector workflow — Milestone 13: daily reflection / learning loop.

Once a day, scan the last 24 hours of agent_runs, events, verdicts,
tool_calls; build a structured digest; ask MiniMax to interpret it
(what worked, what failed, what to change tomorrow); persist the
reflection as a `memory_episodic` row; and send a morning summary
to Telegram.

This is the first agent that consumes its own organization's history
rather than external Amazon data.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import desc, func, select

from studioos.db import session_scope
from studioos.logging import get_logger
from studioos.models import AgentRun, Event, MemoryEpisodic, ToolCall
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class ReflectorState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    # populated during run
    digest: dict[str, Any]
    reflection: str
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


SYSTEM_PROMPT = """You are the daily reflection agent for an autonomous
Amazon TR→US arbitrage studio. You receive a structured digest of the
last 24 hours of activity (which agents ran, what they decided, what
events fired, how much was spent on tools/LLM, what failed) and you
write a short reflection in Turkish.

Your reflection has 4 sections, each ≤ 4 bullet points:

  Çalıştı  — what went well
  Dikkat   — anomalies, signals to watch, near-misses
  Düzelt   — concrete changes for tomorrow (cron cadence, threshold,
             prompt tweak, missing data)
  Sayılar  — 1-2 metrics worth highlighting

Style: terse, factual, concrete. No hype, no filler. Reply in plain
Markdown — no code fences, no JSON, no preamble.
"""


async def _build_digest(window_hours: int = 24) -> dict[str, Any]:
    """Aggregate the last N hours of platform activity."""
    since = datetime.now(UTC) - timedelta(hours=window_hours)
    async with session_scope() as session:
        # Run state histogram by agent
        run_rows = (
            await session.execute(
                select(AgentRun.agent_id, AgentRun.state, func.count())
                .where(AgentRun.created_at >= since)
                .group_by(AgentRun.agent_id, AgentRun.state)
            )
        ).all()

        # Event type histogram
        event_rows = (
            await session.execute(
                select(Event.event_type, func.count())
                .where(Event.recorded_at >= since)
                .group_by(Event.event_type)
            )
        ).all()

        # Tool usage + cost
        tool_rows = (
            await session.execute(
                select(
                    ToolCall.tool_name,
                    func.count(),
                    func.coalesce(func.sum(ToolCall.cost_cents), 0),
                )
                .where(ToolCall.called_at >= since)
                .group_by(ToolCall.tool_name)
            )
        ).all()

        # Failed runs (errors)
        fail_rows = (
            (
                await session.execute(
                    select(AgentRun.agent_id, AgentRun.error)
                    .where(AgentRun.created_at >= since)
                    .where(
                        AgentRun.state.in_(
                            ("failed", "timed_out", "dead", "budget_exceeded")
                        )
                    )
                    .order_by(desc(AgentRun.created_at))
                    .limit(10)
                )
            ).all()
        )

        # Recent confirmed/rejected opportunities
        verdict_rows = (
            (
                await session.execute(
                    select(Event)
                    .where(
                        Event.event_type.in_(
                            (
                                "amz.opportunity.confirmed",
                                "amz.opportunity.rejected",
                                "amz.opportunity.discovered",
                                "amz.reprice.recommended",
                                "amz.price.anomaly_detected",
                            )
                        )
                    )
                    .where(Event.recorded_at >= since)
                    .order_by(desc(Event.recorded_at))
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )

    runs_by_agent: dict[str, dict[str, int]] = {}
    for agent_id, state, count in run_rows:
        runs_by_agent.setdefault(agent_id, {})[state] = int(count)

    return {
        "window_hours": window_hours,
        "runs_by_agent": runs_by_agent,
        "events_by_type": {t: int(c) for t, c in event_rows},
        "tools": {
            name: {"calls": int(c), "cost_cents": int(cost)}
            for name, c, cost in tool_rows
        },
        "total_tool_cost_cents": sum(int(cost) for _, _, cost in tool_rows),
        "failures": [
            {
                "agent_id": aid,
                "type": (err or {}).get("type"),
                "message": (err or {}).get("message", "")[:200],
            }
            for aid, err in fail_rows
        ],
        "verdicts": [
            {
                "type": e.event_type,
                "asin": e.payload.get("asin"),
                "verdict": e.payload.get("verdict"),
                "confidence": e.payload.get("confidence"),
                "rationale": (e.payload.get("rationale") or "")[:160],
                "estimated_profit_usd": e.payload.get("estimated_profit_usd"),
                "roi_pct": e.payload.get("roi_pct"),
            }
            for e in verdict_rows
        ],
    }


async def node_collect(state: ReflectorState) -> dict[str, Any]:
    goals = state.get("goals") or {}
    window = int(goals.get("window_hours", 24))
    digest = await _build_digest(window)
    return {"digest": digest}


async def node_reflect(state: ReflectorState) -> dict[str, Any]:
    digest = state.get("digest") or {}
    user = (
        "Aşağıdaki 24 saat özeti üzerinde reflection yaz.\n\n"
        f"```json\n{json.dumps(digest, ensure_ascii=False, indent=2)[:6000]}\n```"
    )
    result = await invoke_from_state(
        state,
        "llm.chat",
        {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "max_tokens": 1500,
            "temperature": 0.2,
        },
    )
    if result["status"] != "ok":
        log.warning(
            "amz_reflector.llm_failed",
            status=result["status"],
            error=result.get("error"),
        )
        return {"reflection": "_LLM çağrısı başarısız_"}
    content = (result["data"] or {}).get("content", "")
    return {"reflection": content.strip()}


async def node_persist(state: ReflectorState) -> dict[str, Any]:
    digest = state.get("digest") or {}
    reflection = state.get("reflection") or ""
    today = datetime.now(UTC).date()

    # Persist as an episodic memory row (one per day per agent).
    async with session_scope() as session:
        existing = (
            await session.execute(
                select(MemoryEpisodic).where(
                    MemoryEpisodic.agent_id == state["agent_id"],
                    MemoryEpisodic.date == today,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.content = json.dumps(digest, ensure_ascii=False)[:8000]
            existing.summary = reflection[:4000]
            existing.events_count = sum(digest.get("events_by_type", {}).values())
        else:
            session.add(
                MemoryEpisodic(
                    agent_id=state["agent_id"],
                    date=today,
                    content=json.dumps(digest, ensure_ascii=False)[:8000],
                    summary=reflection[:4000],
                    events_count=sum(
                        digest.get("events_by_type", {}).values()
                    ),
                )
            )

    notify_text = (
        f"*🌅 StudioOS Daily Reflection — {today.isoformat()}*\n\n"
        f"{reflection[:3500]}"
    )
    notify = await invoke_from_state(
        state,
        "telegram.notify",
        {
            "text": notify_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
    )
    notified = notify["status"] == "ok"

    state_accum = dict(state.get("state") or {})
    state_accum["reflections_total"] = (
        int(state_accum.get("reflections_total", 0)) + 1
    )
    state_accum["last_reflection_date"] = today.isoformat()

    return {
        "memories": [
            {
                "content": (
                    f"Daily reflection {today.isoformat()}: "
                    + reflection[:300]
                ),
                "tags": ["reflection", "daily", today.isoformat()],
                "importance": 0.7,
            }
        ],
        "kpi_updates": [
            {
                "name": "reflections_total",
                "value": state_accum["reflections_total"],
            }
        ],
        "state": state_accum,
        "summary": (
            f"Reflected on {sum(digest.get('events_by_type', {}).values())} "
            f"events, {sum(sum(v.values()) for v in digest.get('runs_by_agent', {}).values())} runs"
            + (" (notified)" if notified else "")
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(ReflectorState)
    graph.add_node("collect", node_collect)
    graph.add_node("reflect", node_reflect)
    graph.add_node("persist", node_persist)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "reflect")
    graph.add_edge("reflect", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_reflector", 1, compiled)
