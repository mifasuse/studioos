"""Scheduler tick + background loop.

The scheduler is a dumb loop: every `tick_seconds` it wakes up, reads the
agents whose `schedule_cron` is non-null, and for any whose cadence is due,
it enqueues a pending run with `trigger_type="schedule"` and stamps
`last_scheduled_at=NOW()`.

Idempotency: the read + write happen in a single transaction with
`SELECT ... FOR UPDATE`. That's good enough for single-node prod; a
future HA rollout can replace the lock with a leader election.

First-run policy: an agent with a brand-new schedule (last_scheduled_at
IS NULL) is considered due immediately on the next tick. "I just
configured this cron, I want to see it run right away."
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from studioos.db import session_scope
from studioos.logging import bind_agent, get_logger
from studioos.models import Agent
from studioos.runtime.triggers import create_pending_run

from .parser import ScheduleError, parse_schedule

log = get_logger(__name__)


def _is_due(agent: Agent, now: datetime) -> bool:
    if agent.schedule_cron is None:
        return False
    try:
        cadence = parse_schedule(agent.schedule_cron)
    except ScheduleError as exc:
        log.warning(
            "scheduler.bad_schedule",
            agent_id=agent.id,
            schedule=agent.schedule_cron,
            error=str(exc),
        )
        return False
    if agent.last_scheduled_at is None:
        return True
    elapsed = now - agent.last_scheduled_at
    return elapsed >= cadence


async def tick_once(now: datetime | None = None) -> int:
    """Run one scheduler pass. Returns the number of runs enqueued."""
    now = now or datetime.now(UTC)
    enqueued = 0
    async with session_scope() as session:
        stmt = (
            select(Agent)
            .where(Agent.schedule_cron.is_not(None))
            .where(Agent.retired_at.is_(None))
            .where(Agent.mode.in_(("normal", "degraded")))
            .with_for_update(skip_locked=True)
        )
        agents = (await session.execute(stmt)).scalars().all()
        for agent in agents:
            if not _is_due(agent, now):
                continue
            bind_agent(agent.id)
            try:
                await create_pending_run(
                    session,
                    agent_id=agent.id,
                    trigger_type="schedule",
                    trigger_ref=agent.schedule_cron,
                )
            except Exception:
                log.exception(
                    "scheduler.enqueue_failed", agent_id=agent.id
                )
                continue
            agent.last_scheduled_at = now
            enqueued += 1
            log.info(
                "scheduler.enqueued",
                agent_id=agent.id,
                schedule=agent.schedule_cron,
            )
    return enqueued


async def scheduler_loop(
    stop_event: asyncio.Event, tick_seconds: float = 15.0
) -> None:
    log.info("scheduler.started", tick_seconds=tick_seconds)
    while not stop_event.is_set():
        try:
            await tick_once()
        except Exception:
            log.exception("scheduler.tick_error")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass
    log.info("scheduler.stopped")
