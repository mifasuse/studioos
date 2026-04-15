"""Dispatcher — picks the next pending run and executes it.

Run selection:
  - Highest priority first (lowest integer)
  - Then oldest created_at
  - Skips agents in paused mode (already blocked at trigger time, double-check)
  - In degraded mode, only picks priority <= HIGH (20)
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from studioos.db import session_scope
from studioos.logging import get_logger
from studioos.models import Agent, AgentRun
from studioos.runtime.runner import RunExecutionError, execute_run

log = get_logger(__name__)


async def claim_next_run(session: AsyncSession) -> AgentRun | None:
    """Atomically claim the next pending run: mark RUNNING, return it."""
    # Degraded agents only take priority <= 20. Normal agents take all pending.
    stmt = (
        select(AgentRun)
        .join(Agent, Agent.id == AgentRun.agent_id)
        .where(
            AgentRun.state == "pending",
            Agent.mode.in_(("normal", "emergency", "degraded")),
        )
        .order_by(AgentRun.priority.asc(), AgentRun.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    run = result.scalar_one_or_none()
    if run is None:
        return None

    # Load agent mode for degraded filter
    agent = (
        await session.execute(select(Agent).where(Agent.id == run.agent_id))
    ).scalar_one()
    if agent.mode == "degraded" and run.priority > 20:
        # Skip this run (it stays pending; next tick can pick higher priority)
        return None

    run.state = "running"
    run.started_at = datetime.now(UTC)
    await session.flush()
    return run


async def dispatch_once() -> UUID | None:
    """Claim and execute a single run. Returns run_id if one was processed."""
    async with session_scope() as session:
        run = await claim_next_run(session)
        if run is None:
            return None
        run_id = run.id

    # Execute in its own session so state and events commit together
    async with session_scope() as session:
        try:
            await execute_run(session, run_id)
        except RunExecutionError:
            # error already recorded on run
            pass
        except Exception:
            log.exception("dispatch.run_unexpected_error", run_id=str(run_id))
            await session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(
                    state="failed",
                    ended_at=datetime.now(UTC),
                    error={"type": "UnexpectedError"},
                )
            )

    return run_id


async def dispatch_loop(stop_event: asyncio.Event, tick_seconds: float) -> None:
    """Run dispatch_once repeatedly until stopped."""
    log.info("dispatcher.started", tick_seconds=tick_seconds)
    while not stop_event.is_set():
        try:
            run_id = await dispatch_once()
            if run_id is None:
                await asyncio.sleep(tick_seconds)
        except Exception:
            log.exception("dispatcher.tick_error")
            await asyncio.sleep(tick_seconds)
    log.info("dispatcher.stopped")
