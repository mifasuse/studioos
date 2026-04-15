"""FastAPI app — administrative + introspection endpoints."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import desc, select

from studioos import __version__
from studioos.db import session_scope
from studioos.kpi.store import get_current_state
from studioos.logging import configure_logging, get_logger
from studioos.memory.store import search_memory
from studioos.models import (
    Agent,
    AgentRun,
    Event,
    KpiSnapshot,
    MemorySemantic,
    Studio,
    ToolCall,
)
from studioos.tools import list_tools
# Import builtin tools so registry is populated on API startup.
from studioos.tools import builtin as _builtin_tools  # noqa: F401

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    log.info("api.starting", version=__version__)
    yield
    log.info("api.stopped")


app = FastAPI(
    title="StudioOS",
    version=__version__,
    description="Multi-studio autonomous agent platform",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@app.get("/studios")
async def list_studios() -> list[dict[str, Any]]:
    async with session_scope() as session:
        rows = (await session.execute(select(Studio))).scalars().all()
        return [
            {
                "id": s.id,
                "display_name": s.display_name,
                "mission": s.mission,
                "status": s.status,
            }
            for s in rows
        ]


@app.get("/agents")
async def list_agents(studio_id: str | None = None) -> list[dict[str, Any]]:
    async with session_scope() as session:
        stmt = select(Agent)
        if studio_id:
            stmt = stmt.where(Agent.studio_id == studio_id)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": a.id,
                "studio_id": a.studio_id,
                "template_id": a.template_id,
                "template_version": a.template_version,
                "display_name": a.display_name,
                "mode": a.mode,
                "goals": a.goals,
            }
            for a in rows
        ]


@app.get("/runs/{run_id}")
async def get_run(run_id: UUID) -> dict[str, Any]:
    async with session_scope() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise HTTPException(404, "run not found")
        return _serialize_run(run)


@app.get("/runs")
async def list_runs(
    agent_id: str | None = None,
    state: str | None = None,
    correlation_id: UUID | None = None,
    limit: int = Query(default=20, le=200),
) -> list[dict[str, Any]]:
    async with session_scope() as session:
        stmt = select(AgentRun).order_by(desc(AgentRun.created_at)).limit(limit)
        if agent_id:
            stmt = stmt.where(AgentRun.agent_id == agent_id)
        if state:
            stmt = stmt.where(AgentRun.state == state)
        if correlation_id:
            stmt = stmt.where(AgentRun.correlation_id == correlation_id)
        rows = (await session.execute(stmt)).scalars().all()
        return [_serialize_run(r) for r in rows]


@app.get("/events")
async def list_events(
    correlation_id: UUID | None = None,
    event_type: str | None = None,
    limit: int = Query(default=20, le=200),
) -> list[dict[str, Any]]:
    async with session_scope() as session:
        stmt = select(Event).order_by(desc(Event.recorded_at)).limit(limit)
        if correlation_id:
            stmt = stmt.where(Event.correlation_id == correlation_id)
        if event_type:
            stmt = stmt.where(Event.event_type == event_type)
        rows = (await session.execute(stmt)).scalars().all()
        return [_serialize_event(e) for e in rows]


def _serialize_run(run: AgentRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "agent_id": run.agent_id,
        "studio_id": run.studio_id,
        "correlation_id": str(run.correlation_id),
        "state": run.state,
        "priority": run.priority,
        "trigger_type": run.trigger_type,
        "trigger_ref": run.trigger_ref,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "error": run.error,
        "output": run.output_snapshot,
    }


@app.get("/memory")
async def list_memory(
    query: str | None = None,
    agent_id: str | None = None,
    studio_id: str | None = None,
    limit: int = Query(default=10, le=100),
) -> list[dict[str, Any]]:
    """List or semantic-search memories."""
    async with session_scope() as session:
        if query:
            results = await search_memory(
                session,
                query=query,
                agent_id=agent_id,
                studio_id=studio_id,
                limit=limit,
            )
            return [
                {
                    "id": str(r.id),
                    "content": r.content,
                    "tags": r.tags,
                    "importance": r.importance,
                    "score": 1 - r.distance,
                    "created_at": r.created_at.isoformat(),
                    "source_run_id": str(r.source_run_id) if r.source_run_id else None,
                }
                for r in results
            ]
        stmt = select(MemorySemantic).order_by(desc(MemorySemantic.created_at)).limit(limit)
        if agent_id:
            stmt = stmt.where(MemorySemantic.agent_id == agent_id)
        if studio_id:
            stmt = stmt.where(MemorySemantic.studio_id == studio_id)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": str(r.id),
                "agent_id": r.agent_id,
                "studio_id": r.studio_id,
                "content": r.content,
                "tags": r.tags,
                "importance": float(r.importance),
                "created_at": r.created_at.isoformat(),
                "source_run_id": str(r.source_run_id) if r.source_run_id else None,
            }
            for r in rows
        ]


@app.get("/kpi")
async def list_kpi(
    studio_id: str | None = None,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Show current KPI state for a scope."""
    async with session_scope() as session:
        states = await get_current_state(
            session, studio_id=studio_id, agent_id=agent_id
        )
    out: list[dict[str, Any]] = []
    for s in states:
        item: dict[str, Any] = {
            "name": s.name,
            "display_name": s.display_name,
            "target": float(s.target) if s.target is not None else None,
            "current": float(s.current) if s.current is not None else None,
            "direction": s.direction,
            "unit": s.unit,
            "last_recorded_at": s.last_recorded_at.isoformat()
            if s.last_recorded_at
            else None,
        }
        if s.gap is not None:
            item["gap"] = {
                "delta": float(s.gap.delta),
                "reached": s.gap.reached,
            }
        out.append(item)
    return out


@app.get("/kpi/snapshots")
async def list_kpi_snapshots(
    name: str | None = None,
    agent_id: str | None = None,
    studio_id: str | None = None,
    limit: int = Query(default=50, le=500),
) -> list[dict[str, Any]]:
    """Return time-series of KPI snapshot values."""
    async with session_scope() as session:
        stmt = select(KpiSnapshot).order_by(desc(KpiSnapshot.recorded_at)).limit(limit)
        if name:
            stmt = stmt.where(KpiSnapshot.name == name)
        if agent_id:
            stmt = stmt.where(KpiSnapshot.agent_id == agent_id)
        if studio_id:
            stmt = stmt.where(KpiSnapshot.studio_id == studio_id)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id,
                "name": r.name,
                "value": float(r.value),
                "studio_id": r.studio_id,
                "agent_id": r.agent_id,
                "source_run_id": str(r.source_run_id) if r.source_run_id else None,
                "recorded_at": r.recorded_at.isoformat(),
            }
            for r in rows
        ]


@app.get("/tools")
async def list_tools_endpoint() -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "category": t.category,
            "requires_network": t.requires_network,
            "input_schema": t.input_schema,
        }
        for t in list_tools()
    ]


@app.get("/tool-calls")
async def list_tool_calls(
    tool_name: str | None = None,
    agent_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, le=500),
) -> list[dict[str, Any]]:
    async with session_scope() as session:
        stmt = (
            select(ToolCall).order_by(desc(ToolCall.called_at)).limit(limit)
        )
        if tool_name:
            stmt = stmt.where(ToolCall.tool_name == tool_name)
        if agent_id:
            stmt = stmt.where(ToolCall.agent_id == agent_id)
        if status:
            stmt = stmt.where(ToolCall.status == status)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": str(r.id),
                "tool_name": r.tool_name,
                "agent_id": r.agent_id,
                "run_id": str(r.run_id) if r.run_id else None,
                "correlation_id": str(r.correlation_id)
                if r.correlation_id
                else None,
                "args": r.args,
                "result": r.result,
                "error": r.error,
                "status": r.status,
                "duration_ms": r.duration_ms,
                "called_at": r.called_at.isoformat(),
            }
            for r in rows
        ]


def _serialize_event(event: Event) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "event_type": event.event_type,
        "event_version": event.event_version,
        "studio_id": event.studio_id,
        "correlation_id": str(event.correlation_id),
        "causation_id": str(event.causation_id) if event.causation_id else None,
        "source_type": event.source_type,
        "source_id": event.source_id,
        "source_run_id": str(event.source_run_id) if event.source_run_id else None,
        "payload": event.payload,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "recorded_at": event.recorded_at.isoformat() if event.recorded_at else None,
        "published_at": event.published_at.isoformat()
        if event.published_at
        else None,
    }
