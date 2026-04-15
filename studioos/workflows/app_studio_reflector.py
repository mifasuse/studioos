"""app_studio_reflector — daily reflection scoped to studio_id=app-studio.

Mirrors amz_reflector but every aggregation is filtered by studio_id.
Uses the same playbook procedural memory, just with id='app_studio_playbook'.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import desc, func, select

from studioos.db import session_scope
from studioos.logging import get_logger
from studioos.models import (
    AgentRun,
    Event,
    MemoryEpisodic,
    MemoryProcedural,
    ToolCall,
)
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


_PLAYBOOK_ID = "app_studio_playbook"
_STUDIO_ID = "app-studio"


SYSTEM_PROMPT = """You are the daily reflection agent for an autonomous
mobile App Studio. You receive a structured digest of the last 24 hours
of activity scoped to the App Studio (which agents ran, what events
fired, what changed) plus — when available — the previous day's
reflection.

The studio is in early bootstrap: at this stage there will often be
nothing meaningful to report. That's fine; say so honestly rather
than inventing activity.

Your reflection has 5 sections, each ≤ 4 bullet points:

  Çalıştı  — what went well
  Dikkat   — anomalies, signals to watch
  Düzelt   — concrete changes for tomorrow
  İlerleme — for each "Düzelt" from the previous reflection: ✓ done /
             ✗ open / ~ partial
  Sayılar  — 1-2 metrics worth highlighting (or "henüz veri yok")

Style: terse, factual, Turkish, plain Markdown.
"""


PLAYBOOK_PROMPT = """You are the playbook curator for the App Studio.
You have just received today's reflection AND the current playbook.
Update the playbook — a small set of stable operating rules.

Rules:
- ≤ 12 entries total. Replace conflicting old rules.
- Drop entries that haven't paid off in 5 reflections.
- Specific over generic; reference exact thresholds when possible.

Reply in plain Markdown:

# App Studio Playbook v{next_version}
## Filtreler
- ...
## Kararlar
- ...
## Operasyon
- ...
"""


class ReflectorState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    digest: dict[str, Any]
    previous_reflection: str | None
    current_playbook: str | None
    current_playbook_version: int
    reflection: str
    new_playbook: str | None
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


async def _build_digest(window_hours: int = 24) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=window_hours)
    async with session_scope() as session:
        run_rows = (
            await session.execute(
                select(AgentRun.agent_id, AgentRun.state, func.count())
                .where(AgentRun.studio_id == _STUDIO_ID)
                .where(AgentRun.created_at >= since)
                .group_by(AgentRun.agent_id, AgentRun.state)
            )
        ).all()
        event_rows = (
            await session.execute(
                select(Event.event_type, func.count())
                .where(Event.studio_id == _STUDIO_ID)
                .where(Event.recorded_at >= since)
                .group_by(Event.event_type)
            )
        ).all()
        # Tool calls don't carry studio_id directly; aggregate by agent
        # whose studio is app-studio.
        tool_rows = (
            await session.execute(
                select(
                    ToolCall.tool_name,
                    func.count(),
                    func.coalesce(func.sum(ToolCall.cost_cents), 0),
                )
                .join(AgentRun, AgentRun.id == ToolCall.run_id)
                .where(AgentRun.studio_id == _STUDIO_ID)
                .where(ToolCall.called_at >= since)
                .group_by(ToolCall.tool_name)
            )
        ).all()

    runs_by_agent: dict[str, dict[str, int]] = {}
    for agent_id, state, count in run_rows:
        runs_by_agent.setdefault(agent_id, {})[state] = int(count)

    return {
        "window_hours": window_hours,
        "studio_id": _STUDIO_ID,
        "runs_by_agent": runs_by_agent,
        "events_by_type": {t: int(c) for t, c in event_rows},
        "tools": {
            name: {"calls": int(c), "cost_cents": int(cost)}
            for name, c, cost in tool_rows
        },
        "total_tool_cost_cents": sum(int(cost) for _, _, cost in tool_rows),
    }


async def _load_previous_reflection(agent_id: str) -> str | None:
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    async with session_scope() as session:
        row = (
            await session.execute(
                select(MemoryEpisodic)
                .where(MemoryEpisodic.agent_id == agent_id)
                .where(MemoryEpisodic.date >= yesterday - timedelta(days=2))
                .order_by(desc(MemoryEpisodic.date))
                .limit(1)
            )
        ).scalar_one_or_none()
    return row.summary if row else None


async def _load_active_playbook() -> tuple[str | None, int]:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(MemoryProcedural)
                .where(MemoryProcedural.id == _PLAYBOOK_ID)
                .where(MemoryProcedural.active.is_(True))
                .order_by(desc(MemoryProcedural.version))
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None, 0
        return row.content, int(row.version)


async def node_collect(state: ReflectorState) -> dict[str, Any]:
    goals = state.get("goals") or {}
    window = int(goals.get("window_hours", 24))
    digest = await _build_digest(window)
    prev_reflection = await _load_previous_reflection(state["agent_id"])
    playbook, version = await _load_active_playbook()
    return {
        "digest": digest,
        "previous_reflection": prev_reflection,
        "current_playbook": playbook,
        "current_playbook_version": version,
    }


async def node_reflect(state: ReflectorState) -> dict[str, Any]:
    digest = state.get("digest") or {}
    prev = state.get("previous_reflection")
    playbook = state.get("current_playbook")

    sections: list[str] = []
    if prev:
        sections.append("## Önceki gün reflection:\n" + prev[:3000])
    if playbook:
        sections.append("## Mevcut playbook:\n" + playbook[:2000])
    sections.append(
        "## Bugünün 24 saat özeti:\n```json\n"
        + json.dumps(digest, ensure_ascii=False, indent=2)[:5000]
        + "\n```"
    )
    user = "\n\n".join(sections)
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
            "app_studio_reflector.llm_failed", error=result.get("error")
        )
        return {"reflection": "_LLM çağrısı başarısız_"}
    return {"reflection": (result["data"] or {}).get("content", "").strip()}


async def node_update_playbook(state: ReflectorState) -> dict[str, Any]:
    reflection = state.get("reflection") or ""
    current = state.get("current_playbook") or "(empty)"
    next_version = int(state.get("current_playbook_version", 0)) + 1
    user = (
        f"## Bugünkü reflection\n{reflection[:3000]}\n\n"
        f"## Mevcut playbook\n{current[:3000]}\n\n"
        f"Yeni sürüm v{next_version}."
    )
    result = await invoke_from_state(
        state,
        "llm.chat",
        {
            "messages": [
                {
                    "role": "system",
                    "content": PLAYBOOK_PROMPT.format(next_version=next_version),
                },
                {"role": "user", "content": user},
            ],
            "max_tokens": 1500,
            "temperature": 0.1,
        },
    )
    if result["status"] != "ok":
        return {"new_playbook": None}
    return {"new_playbook": (result["data"] or {}).get("content", "").strip()}


async def node_persist(state: ReflectorState) -> dict[str, Any]:
    digest = state.get("digest") or {}
    reflection = state.get("reflection") or ""
    new_playbook = state.get("new_playbook")
    next_version = int(state.get("current_playbook_version", 0)) + 1
    today = datetime.now(UTC).date()

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
        if new_playbook:
            await session.execute(
                MemoryProcedural.__table__.update()
                .where(MemoryProcedural.id == _PLAYBOOK_ID)
                .where(MemoryProcedural.active.is_(True))
                .values(active=False)
            )
            session.add(
                MemoryProcedural(
                    id=_PLAYBOOK_ID,
                    studio_id=state.get("studio_id"),
                    version=next_version,
                    content=new_playbook[:8000],
                    author=state["agent_id"],
                    change_summary=f"App Studio reflector evolved on {today.isoformat()}",
                    active=True,
                )
            )

    playbook_msg = ""
    if new_playbook:
        playbook_msg = (
            f"\n\n*📋 App Studio Playbook v{next_version}:*\n"
            + new_playbook[:1500]
        )
    notify_text = (
        f"*🌅 App Studio Daily Reflection — {today.isoformat()}*\n\n"
        f"{reflection[:2500]}{playbook_msg}"
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
    state_accum["reflections_total"] = int(state_accum.get("reflections_total", 0)) + 1
    state_accum["last_reflection_date"] = today.isoformat()

    return {
        "memories": [
            {
                "content": f"App Studio reflection {today.isoformat()}: {reflection[:300]}",
                "tags": ["reflection", "daily", "app-studio", today.isoformat()],
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
            f"Reflected on {sum(digest.get('events_by_type', {}).values())} events"
            + (" (notified)" if notified else "")
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(ReflectorState)
    graph.add_node("collect", node_collect)
    graph.add_node("reflect", node_reflect)
    graph.add_node("update_playbook", node_update_playbook)
    graph.add_node("persist", node_persist)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "reflect")
    graph.add_edge("reflect", "update_playbook")
    graph.add_edge("update_playbook", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_reflector", 1, compiled)
