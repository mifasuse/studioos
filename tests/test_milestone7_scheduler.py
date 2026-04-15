"""Milestone 7 — agent scheduler (cadence-based triggers).

Covers parser edge cases + a live tick cycle where amz-monitor enqueues a
run on first pass, skips the next tick (cadence not elapsed), then
enqueues again once the clock has moved past the cadence.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from studioos.db import session_scope
from studioos.models import Agent, AgentRun
from studioos.scheduler import parse_schedule, tick_once
from studioos.scheduler.parser import ScheduleError


def test_parse_basic() -> None:
    assert parse_schedule("@every 30s") == timedelta(seconds=30)
    assert parse_schedule("@every 15m") == timedelta(minutes=15)
    assert parse_schedule("@every 2h") == timedelta(hours=2)
    assert parse_schedule("@every 2h30m") == timedelta(hours=2, minutes=30)
    assert parse_schedule("@every 1h15m30s") == timedelta(
        hours=1, minutes=15, seconds=30
    )


def test_parse_rejects_bad_input() -> None:
    for bad in ("", "   ", "30m", "@every", "@every 0s", "@every abc", "@cron *"):
        with pytest.raises(ScheduleError):
            parse_schedule(bad)


@pytest.mark.asyncio
async def test_tick_enqueues_then_idempotent_then_reenqueues(db_session) -> None:
    # Arrange: set amz-monitor's schedule to 1 minute. The studio.yaml ships
    # @every 30m but we want a short cadence for this test.
    async with session_scope() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.id == "amz-monitor"))
        ).scalar_one()
        agent.schedule_cron = "@every 1m"
        agent.last_scheduled_at = None

    t0 = datetime.now(UTC)

    # First tick: never scheduled → due immediately.
    enqueued = await tick_once(now=t0)
    assert enqueued == 1

    # Second tick 10 seconds later: cadence is 1m, not due yet.
    enqueued = await tick_once(now=t0 + timedelta(seconds=10))
    assert enqueued == 0

    # Third tick 70 seconds later: cadence elapsed, due again.
    enqueued = await tick_once(now=t0 + timedelta(seconds=70))
    assert enqueued == 1

    # Verify two pending runs got created with trigger_type='schedule'.
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(AgentRun).where(
                        AgentRun.agent_id == "amz-monitor",
                        AgentRun.trigger_type == "schedule",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2
    assert all(r.trigger_ref == "@every 1m" for r in rows)


@pytest.mark.asyncio
async def test_tick_ignores_paused_agents(db_session) -> None:
    async with session_scope() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.id == "amz-monitor"))
        ).scalar_one()
        agent.schedule_cron = "@every 1m"
        agent.last_scheduled_at = None
        agent.mode = "paused"

    enqueued = await tick_once(now=datetime.now(UTC))
    assert enqueued == 0


@pytest.mark.asyncio
async def test_tick_skips_bad_schedule(db_session) -> None:
    async with session_scope() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.id == "amz-monitor"))
        ).scalar_one()
        agent.schedule_cron = "@every never"
        agent.last_scheduled_at = None

    # Should log a warning and not crash.
    enqueued = await tick_once(now=datetime.now(UTC))
    assert enqueued == 0
