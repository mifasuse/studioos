"""FastAPI app — administrative + introspection endpoints."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import desc, select

from studioos import __version__
from studioos.db import session_scope
from studioos.logging import configure_logging, get_logger
from studioos.models import Agent, AgentRun, Event, Studio

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
