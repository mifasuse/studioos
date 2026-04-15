"""Agent runner — executes one workflow for a single run.

Responsibilities (per run):
  1. Load agent config + state + recent memories + KPI state
  2. Resolve workflow from template
  3. Construct workflow input (agent_state + memories + kpis + trigger payload)
  4. Invoke workflow (LangGraph or plain callable in v1)
  5. Apply output deltas:
       - agent_state update
       - events to publish (outbox)
       - new memories to persist
       - KPI snapshots to record
  6. Transactionally commit everything together with run completion

Errors are captured on the run record; retry policy is handled by the dispatcher.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from studioos.approvals import create_approval
from studioos.events.envelope import EventEnvelope, EventSource
from studioos.events.registry import registry
from studioos.kpi.store import get_current_state, record_snapshot
from studioos.logging import bind_agent, bind_correlation, bind_run, get_logger
from studioos.memory.store import record_memory, search_memory
from studioos.models import (
    Agent,
    AgentRun,
    AgentState,
    Event,
)
from studioos.runtime.workflow_registry import resolve_workflow

log = get_logger(__name__)


class RunExecutionError(Exception):
    """Raised when the workflow itself fails in a retriable manner."""


async def execute_run(session: AsyncSession, run_id: UUID) -> AgentRun:
    """Execute a single run end-to-end. Caller commits.

    The run must already be marked RUNNING by the dispatcher.
    """
    run = (
        await session.execute(select(AgentRun).where(AgentRun.id == run_id))
    ).scalar_one()

    bind_correlation(run.correlation_id)
    bind_run(run.id)
    bind_agent(run.agent_id)

    agent = (
        await session.execute(select(Agent).where(Agent.id == run.agent_id))
    ).scalar_one()

    state_row = (
        await session.execute(
            select(AgentState).where(AgentState.agent_id == agent.id)
        )
    ).scalar_one()

    log.info(
        "run.started",
        template=agent.template_id,
        template_version=agent.template_version,
    )

    workflow = resolve_workflow(agent.template_id, agent.template_version)

    # Pre-load context: recent memories + current KPI state
    recent_memories: list[dict[str, Any]] = []
    try:
        memory_query = (
            (run.input_snapshot or {}).get("memory_query")
            or run.trigger_ref
            or agent.id
        )
        results = await search_memory(
            session,
            query=str(memory_query),
            agent_id=agent.id,
            limit=5,
        )
        recent_memories = [
            {
                "id": str(r.id),
                "content": r.content,
                "tags": r.tags,
                "importance": r.importance,
                "distance": r.distance,
                "created_at": r.created_at.isoformat(),
            }
            for r in results
        ]
    except Exception:  # noqa: BLE001
        log.exception("runner.memory_load_failed")

    kpi_state: list[dict[str, Any]] = []
    try:
        states = await get_current_state(
            session,
            studio_id=agent.studio_id,
            agent_id=agent.id,
        )
        kpi_state = [
            {
                "name": s.name,
                "display_name": s.display_name,
                "target": float(s.target) if s.target is not None else None,
                "current": float(s.current) if s.current is not None else None,
                "direction": s.direction,
                "unit": s.unit,
                "reached": s.gap.reached if s.gap else None,
                "delta": float(s.gap.delta) if s.gap else None,
            }
            for s in states
        ]
    except Exception:  # noqa: BLE001
        log.exception("runner.kpi_load_failed")

    workflow_input: dict[str, Any] = {
        "agent_id": agent.id,
        "studio_id": agent.studio_id,
        "correlation_id": str(run.correlation_id),
        "run_id": str(run.id),
        "state": dict(state_row.state),
        "trigger_type": run.trigger_type,
        "trigger_ref": run.trigger_ref,
        "input": run.input_snapshot or {},
        "config": agent.heartbeat_config or {},
        "goals": agent.goals or {},
        "recent_memories": recent_memories,
        "kpis": kpi_state,
    }

    try:
        output = await workflow.ainvoke(workflow_input)
    except Exception as exc:
        log.exception("run.failed", error=str(exc))
        run.state = "failed"
        run.ended_at = datetime.now(UTC)
        run.error = {"type": type(exc).__name__, "message": str(exc)}
        raise RunExecutionError(str(exc)) from exc

    # Apply deltas
    new_state = output.get("state", state_row.state)
    events_out: list[dict[str, Any]] = output.get("events", [])
    memories_out: list[dict[str, Any]] = output.get("memories", [])
    kpi_updates_out: list[dict[str, Any]] = output.get("kpi_updates", [])
    approvals_out: list[dict[str, Any]] = output.get("approvals", [])

    run.output_snapshot = {
        "state": new_state,
        "events": events_out,
        "memories": memories_out,
        "kpi_updates": kpi_updates_out,
        "summary": output.get("summary"),
    }

    state_row.state = new_state
    state_row.last_run_id = run.id
    state_row.last_run_at = datetime.now(UTC)
    state_row.updated_at = datetime.now(UTC)

    # Persist new memories
    for mem in memories_out:
        try:
            await record_memory(
                session,
                content=mem["content"],
                agent_id=agent.id,
                studio_id=agent.studio_id,
                tags=mem.get("tags"),
                importance=float(mem.get("importance", 0.5)),
                source_run_id=run.id,
            )
        except Exception:  # noqa: BLE001
            log.exception("runner.memory_persist_failed")

    # Record KPI snapshots
    for kpi in kpi_updates_out:
        try:
            await record_snapshot(
                session,
                name=kpi["name"],
                value=kpi["value"],
                studio_id=agent.studio_id,
                agent_id=agent.id,
                source_run_id=run.id,
                metadata=kpi.get("metadata"),
            )
        except Exception:  # noqa: BLE001
            log.exception("runner.kpi_persist_failed")

    # Write events to outbox
    for ev_data in events_out:
        envelope = _build_envelope(
            ev_data=ev_data,
            agent=agent,
            run=run,
        )
        # schema validation (raises on mismatch)
        registry.validate(envelope.event_type, envelope.event_version, envelope.payload)

        event_row = Event(
            id=envelope.event_id,
            event_type=envelope.event_type,
            event_version=envelope.event_version,
            studio_id=envelope.studio_id,
            correlation_id=envelope.correlation_id,
            causation_id=envelope.causation_id,
            source_type=envelope.source.type,
            source_id=envelope.source.identifier,
            source_run_id=envelope.source.run_id,
            idempotency_key=envelope.idempotency_key,
            payload=envelope.payload,
            event_metadata=envelope.metadata,
            occurred_at=envelope.occurred_at,
        )
        session.add(event_row)

    # If the workflow asked for human approvals, persist them and park the
    # run. The dispatcher re-enqueues it once every approval is settled.
    if approvals_out:
        for appr in approvals_out:
            await create_approval(
                session,
                run_id=run.id,
                agent_id=agent.id,
                studio_id=agent.studio_id,
                correlation_id=run.correlation_id,
                reason=appr.get("reason", "unspecified"),
                payload=appr.get("payload"),
                expires_in_seconds=appr.get("expires_in_seconds"),
            )
        run.state = "awaiting_approval"
        run.ended_at = None
        log.info(
            "run.awaiting_approval",
            approvals_count=len(approvals_out),
            events_count=len(events_out),
        )
        return run

    run.state = "completed"
    run.ended_at = datetime.now(UTC)
    log.info(
        "run.completed",
        events_count=len(events_out),
        memories_count=len(memories_out),
        kpi_updates_count=len(kpi_updates_out),
        approvals_count=len(approvals_out),
        summary=output.get("summary"),
    )
    return run


def _build_envelope(
    *,
    ev_data: dict[str, Any],
    agent: Agent,
    run: AgentRun,
) -> EventEnvelope:
    """Wrap a workflow-emitted event dict into a full envelope."""
    return EventEnvelope(
        event_type=ev_data["event_type"],
        event_version=ev_data.get("event_version", 1),
        payload=ev_data["payload"],
        metadata=ev_data.get("metadata", {}),
        source=EventSource(
            type="agent",
            identifier=agent.id,
            run_id=run.id,
        ),
        studio_id=agent.studio_id,
        correlation_id=run.correlation_id,
        causation_id=ev_data.get("causation_id"),
        idempotency_key=ev_data.get("idempotency_key"),
    )
