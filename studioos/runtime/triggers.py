"""Helpers for creating new agent runs from various triggers."""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from studioos.logging import get_logger
from studioos.models import Agent, AgentRun, AgentState

log = get_logger(__name__)


async def create_pending_run(
    session: AsyncSession,
    *,
    agent_id: str,
    trigger_type: str,
    trigger_ref: str | None = None,
    correlation_id: UUID | None = None,
    parent_run_id: UUID | None = None,
    priority: int = 50,
    input_snapshot: dict[str, Any] | None = None,
) -> AgentRun:
    """Insert a new PENDING run for the given agent.

    Caller controls commit. Correlation id inherits from parent if not given.
    """
    agent = (
        await session.execute(select(Agent).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    if agent is None:
        raise ValueError(f"Unknown agent_id: {agent_id}")

    if agent.mode == "paused":
        raise RuntimeError(f"Agent {agent_id} is paused; refusing to enqueue run")

    # Ensure agent_state row exists (bootstrap)
    agent_state = (
        await session.execute(
            select(AgentState).where(AgentState.agent_id == agent_id)
        )
    ).scalar_one_or_none()
    if agent_state is None:
        agent_state = AgentState(agent_id=agent_id, state={})
        session.add(agent_state)
        await session.flush()

    run = AgentRun(
        id=uuid4(),
        agent_id=agent_id,
        studio_id=agent.studio_id,
        correlation_id=correlation_id or uuid4(),
        trigger_type=trigger_type,
        trigger_ref=trigger_ref,
        state="pending",
        priority=priority,
        parent_run_id=parent_run_id,
        input_snapshot=input_snapshot,
        workflow_version=None,
    )
    session.add(run)
    await session.flush()

    log.info(
        "run.enqueued",
        run_id=str(run.id),
        agent_id=agent_id,
        trigger_type=trigger_type,
        trigger_ref=trigger_ref,
        correlation_id=str(run.correlation_id),
        priority=priority,
    )
    return run
