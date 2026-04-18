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

from studioos.budget import preflight_check
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
        agent_id = run.agent_id
        studio_id = run.studio_id

        ok, reason = await preflight_check(
            session, agent_id=agent_id, studio_id=studio_id
        )
        if not ok:
            run.state = "budget_exceeded"
            run.ended_at = datetime.now(UTC)
            run.error = {"type": "BudgetExceeded", "message": reason}
            log.warning(
                "dispatch.budget_exceeded",
                run_id=str(run_id),
                agent_id=agent_id,
                reason=reason,
            )
            return run_id

    # Execute in its own session so state and events commit together
    from studioos.config import settings as _cfg
    timeout = float(_cfg.run_timeout_seconds)

    async with session_scope() as session:
        try:
            await asyncio.wait_for(
                execute_run(session, run_id),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                "dispatch.run_timeout",
                run_id=str(run_id),
                agent_id=agent_id,
                timeout_seconds=timeout,
            )
            await session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(
                    state="timed_out",
                    ended_at=datetime.now(UTC),
                    error={"type": "Timeout", "message": f"Run exceeded {timeout}s"},
                )
            )
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


async def _reap_expired_approvals() -> None:
    from studioos.approvals import expire_stale

    async with session_scope() as session:
        await expire_stale(session)


async def dispatch_loop(stop_event: asyncio.Event, tick_seconds: float) -> None:
    """Run dispatch_once repeatedly until stopped."""
    log.info("dispatcher.started", tick_seconds=tick_seconds)
    reap_every = 30.0
    import time

    last_reap = 0.0
    while not stop_event.is_set():
        try:
            run_id = await dispatch_once()
            now = time.monotonic()
            if now - last_reap > reap_every:
                try:
                    await _reap_expired_approvals()
                except Exception:
                    log.exception("dispatcher.approval_reaper_error")
                last_reap = now
            if run_id is None:
                await asyncio.sleep(tick_seconds)
        except Exception:
            log.exception("dispatcher.tick_error")
            await asyncio.sleep(tick_seconds)
    log.info("dispatcher.stopped")
