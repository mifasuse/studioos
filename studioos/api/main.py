"""FastAPI app — administrative + introspection endpoints."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import desc, select

from studioos import __version__
from studioos.db import session_scope
from studioos.kpi.store import get_current_state
from studioos.logging import configure_logging, get_logger
from studioos.memory.store import search_memory
from studioos.models import (
    Agent,
    AgentRun,
    Approval,
    Budget,
    Event,
    KpiSnapshot,
    MemorySemantic,
    Studio,
    ToolCall,
)
from studioos.tools import list_tools
# Import builtin tools so registry is populated on API startup.
from studioos.tools import builtin as _builtin_tools  # noqa: F401
from studioos.api.slack_events import router as slack_router

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    log.info("api.starting", version=__version__)
    # Initialize Slack bot user mapping for inbound mention routing
    try:
        from studioos.slack_routing import init_bot_user_map
        await init_bot_user_map()
    except Exception as exc:
        log.warning("api.slack_init_failed", error=str(exc))
    yield
    log.info("api.stopped")


app = FastAPI(
    title="StudioOS",
    version=__version__,
    description="Multi-studio autonomous agent platform",
    lifespan=lifespan,
)
app.include_router(slack_router)


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    from studioos.api.dashboard import DASHBOARD_HTML

    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@app.post("/events/ingest")
async def ingest_event(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Generic event-ingest webhook for external systems (CI, etc).

    Body shape:
        {
          "event_type": "amz.deploy.notification",
          "event_version": 1,
          "studio_id": "amz",
          "payload": {...},
          "source": "github-actions",
          "correlation_id": "<uuid optional>"
        }

    The event lands in the outbox and the publisher routes it to any
    matching subscriptions. No auth on this endpoint yet — it sits
    behind the same docker-internal traefik that fronts the studioos
    API; lock down with a header secret in a future milestone.
    """
    from datetime import UTC, datetime
    from uuid import UUID, uuid4

    from studioos.events.envelope import EventEnvelope, EventSource
    from studioos.events.registry import registry as event_registry
    from studioos.models import Event

    event_type = body.get("event_type")
    if not event_type:
        raise HTTPException(400, "event_type is required")
    version = int(body.get("event_version", 1))
    payload = body.get("payload") or {}

    try:
        event_registry.validate(event_type, version, payload)
    except Exception as exc:
        raise HTTPException(400, f"schema validation failed: {exc}") from exc

    correlation_id_raw = body.get("correlation_id")
    correlation_id = (
        UUID(correlation_id_raw) if correlation_id_raw else uuid4()
    )

    envelope = EventEnvelope(
        event_type=event_type,
        event_version=version,
        payload=payload,
        metadata=body.get("metadata", {}) or {},
        source=EventSource(
            type="external",
            identifier=body.get("source", "external"),
            run_id=None,
        ),
        studio_id=body.get("studio_id"),
        correlation_id=correlation_id,
        causation_id=None,
        idempotency_key=body.get("idempotency_key"),
    )

    async with session_scope() as session:
        row = Event(
            id=envelope.event_id,
            event_type=envelope.event_type,
            event_version=envelope.event_version,
            studio_id=envelope.studio_id,
            correlation_id=envelope.correlation_id,
            causation_id=envelope.causation_id,
            source_type=envelope.source.type,
            source_id=envelope.source.identifier,
            source_run_id=None,
            idempotency_key=envelope.idempotency_key,
            payload=envelope.payload,
            event_metadata=envelope.metadata,
            occurred_at=envelope.occurred_at or datetime.now(UTC),
        )
        session.add(row)

    return {
        "ok": True,
        "event_id": str(envelope.event_id),
        "correlation_id": str(envelope.correlation_id),
    }


@app.get("/status")
async def status() -> dict[str, Any]:
    from studioos.status import build_snapshot

    async with session_scope() as session:
        snap = await build_snapshot(session)
    return {
        "as_of": snap.as_of.isoformat(),
        "studios": snap.studios,
        "agents": [
            {
                "id": a.id,
                "studio_id": a.studio_id,
                "mode": a.mode,
                "schedule_cron": a.schedule_cron,
                "last_scheduled_at": a.last_scheduled_at.isoformat()
                if a.last_scheduled_at
                else None,
                "next_due_seconds": a.next_due_seconds,
                "tool_scope": a.tool_scope,
            }
            for a in snap.agents
        ],
        "runs_by_state": snap.runs_by_state,
        "recent_runs": [
            {
                "id": r.id,
                "agent_id": r.agent_id,
                "state": r.state,
                "trigger_type": r.trigger_type,
                "created_at": r.created_at.isoformat(),
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "summary": r.summary,
                "error": r.error,
            }
            for r in snap.recent_runs
        ],
        "failures_last_hour": snap.failures_last_hour,
        "event_type_counts_last_hour": snap.event_type_counts_last_hour,
        "pending_approvals": snap.pending_approvals,
        "budgets": snap.budgets,
        "tool_call_counts_last_hour": snap.tool_call_counts_last_hour,
        "tool_cost_cents_last_hour": snap.tool_cost_cents_last_hour,
    }


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    """Prometheus exposition format — scraped by the monitoring stack."""
    from studioos.status import build_snapshot

    async with session_scope() as session:
        snap = await build_snapshot(session)

    lines: list[str] = []

    # Run state counters
    lines.append("# HELP studioos_runs_by_state Current count of runs by state")
    lines.append("# TYPE studioos_runs_by_state gauge")
    for state, count in (snap.runs_by_state or {}).items():
        lines.append(f'studioos_runs_by_state{{state="{state}"}} {count}')

    # Failures
    lines.append("# HELP studioos_failures_last_hour Failures in last 60m")
    lines.append("# TYPE studioos_failures_last_hour gauge")
    lines.append(f"studioos_failures_last_hour {snap.failures_last_hour}")

    # Pending approvals
    lines.append("# HELP studioos_pending_approvals Pending approval count")
    lines.append("# TYPE studioos_pending_approvals gauge")
    lines.append(f"studioos_pending_approvals {snap.pending_approvals}")

    # Events per type last hour
    lines.append("# HELP studioos_events_last_hour Events by type last hour")
    lines.append("# TYPE studioos_events_last_hour gauge")
    for etype, count in (snap.event_type_counts_last_hour or {}).items():
        lines.append(f'studioos_events_last_hour{{event_type="{etype}"}} {count}')

    # Tool calls last hour
    lines.append("# HELP studioos_tool_calls_last_hour Tool calls by name last hour")
    lines.append("# TYPE studioos_tool_calls_last_hour gauge")
    for tool, count in (snap.tool_call_counts_last_hour or {}).items():
        lines.append(f'studioos_tool_calls_last_hour{{tool="{tool}"}} {count}')

    # Tool cost
    lines.append("# HELP studioos_tool_cost_cents_last_hour Tool cost in cents last hour")
    lines.append("# TYPE studioos_tool_cost_cents_last_hour gauge")
    lines.append(f"studioos_tool_cost_cents_last_hour {snap.tool_cost_cents_last_hour}")

    # Agent mode + next due
    lines.append("# HELP studioos_agent_next_due_seconds Seconds until next scheduled run")
    lines.append("# TYPE studioos_agent_next_due_seconds gauge")
    for a in snap.agents:
        if a.next_due_seconds is not None:
            lines.append(
                f'studioos_agent_next_due_seconds{{agent_id="{a.id}",mode="{a.mode}"}} {a.next_due_seconds}'
            )

    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


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


@app.get("/budgets")
async def list_budgets(
    agent_id: str | None = None,
    studio_id: str | None = None,
) -> list[dict[str, Any]]:
    from studioos.budget import current_budget

    async with session_scope() as session:
        views = await current_budget(
            session, agent_id=agent_id, studio_id=studio_id
        )
    return [
        {
            "scope": v.scope,
            "period": v.period,
            "limit_cents": v.limit_cents,
            "spent_cents": v.spent_cents,
            "remaining_cents": v.remaining_cents,
            "over": v.over,
            "period_start": v.period_start.isoformat(),
            "period_end": v.period_end.isoformat(),
        }
        for v in views
    ]


@app.get("/approvals")
async def list_approvals_endpoint(
    state: str | None = None,
    agent_id: str | None = None,
    limit: int = Query(default=50, le=500),
) -> list[dict[str, Any]]:
    async with session_scope() as session:
        stmt = select(Approval).order_by(desc(Approval.created_at)).limit(limit)
        if state:
            stmt = stmt.where(Approval.state == state)
        if agent_id:
            stmt = stmt.where(Approval.agent_id == agent_id)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": str(r.id),
                "run_id": str(r.run_id),
                "agent_id": r.agent_id,
                "studio_id": r.studio_id,
                "correlation_id": str(r.correlation_id) if r.correlation_id else None,
                "reason": r.reason,
                "payload": r.payload,
                "state": r.state,
                "decided_by": r.decided_by,
                "decision_note": r.decision_note,
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@app.post("/approvals/{approval_id}/decide")
async def decide_approval_endpoint(
    approval_id: UUID,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    from studioos.approvals import decide_approval

    decision = body.get("decision")
    decided_by = body.get("decided_by") or "api"
    note = body.get("note")
    if decision not in ("approved", "denied"):
        raise HTTPException(400, "decision must be 'approved' or 'denied'")
    async with session_scope() as session:
        try:
            row = await decide_approval(
                session,
                approval_id=approval_id,
                decision=decision,
                decided_by=decided_by,
                note=note,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "id": str(row.id),
            "state": row.state,
            "decided_by": row.decided_by,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
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
