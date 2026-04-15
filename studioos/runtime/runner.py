"""Agent runner — executes one workflow for a single run.

Responsibilities (per run):
  1. Load agent config + state
  2. Resolve workflow from template
  3. Construct workflow input (agent_state + trigger payload + config)
  4. Invoke workflow (LangGraph or plain callable in v1)
  5. Apply output deltas: agent_state update, events to publish
  6. Transactionally commit state + outbox events + run completion

Errors are captured on the run record; retry policy is handled by the dispatcher.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from studioos.events.envelope import EventEnvelope, EventSource
from studioos.events.registry import registry
from studioos.logging import bind_agent, bind_correlation, bind_run, get_logger
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
    run.output_snapshot = {
        "state": new_state,
        "events": events_out,
        "summary": output.get("summary"),
    }

    state_row.state = new_state
    state_row.last_run_id = run.id
    state_row.last_run_at = datetime.now(UTC)
    state_row.updated_at = datetime.now(UTC)

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

    run.state = "completed"
    run.ended_at = datetime.now(UTC)
    log.info(
        "run.completed",
        events_count=len(events_out),
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
