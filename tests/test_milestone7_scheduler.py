"""Milestone 7 — agent scheduler (cadence-based triggers).

Covers parser edge cases + a live tick cycle where amz-monitor enqueues a
run on first pass, skips the next tick (cadence not elapsed), then
enqueues again once the clock has moved past the cadence.

Because this test suite runs against the same database the prod runtime
uses, every test that mutates amz-monitor restores its schedule_cron,
mode, and last_scheduled_at back to the yaml-declared values in a
finally block — otherwise the live scheduler picks up junk like
"@every never" and starts spitting parse errors.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from studioos.db import session_scope
from studioos.models import Agent, AgentRun
from studioos.scheduler import parse_schedule, tick_once
from studioos.scheduler.parser import ScheduleError


@asynccontextmanager
async def _restore_amz_monitor():
    """Snapshot amz-monitor's mutable fields and put them back on exit."""
    async with session_scope() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.id == "amz-monitor"))
        ).scalar_one()
        snapshot = {
            "schedule_cron": agent.schedule_cron,
            "last_scheduled_at": agent.last_scheduled_at,
            "mode": agent.mode,
        }
    try:
        yield
    finally:
        async with session_scope() as session:
            agent = (
                await session.execute(
                    select(Agent).where(Agent.id == "amz-monitor")
                )
            ).scalar_one()
            agent.schedule_cron = snapshot["schedule_cron"]
            agent.last_scheduled_at = snapshot["last_scheduled_at"]
            agent.mode = snapshot["mode"]


def test_parse_basic() -> None:
    assert parse_schedule("@every 30s").every == timedelta(seconds=30)
    assert parse_schedule("@every 15m").every == timedelta(minutes=15)
    assert parse_schedule("@every 2h").every == timedelta(hours=2)
    assert parse_schedule("@every 2h30m").every == timedelta(hours=2, minutes=30)
    assert parse_schedule("@every 1h15m30s").every == timedelta(
        hours=1, minutes=15, seconds=30
    )


def test_parse_cron() -> None:
    s = parse_schedule("0 9 * * 1")
    assert s.kind == "cron"
    assert s.cron == "0 9 * * 1"
    s = parse_schedule("@cron 0 4 * * 0")
    assert s.kind == "cron"
    assert s.cron == "0 4 * * 0"


def test_parse_rejects_bad_input() -> None:
    for bad in ("", "   ", "30m", "@every", "@every 0s", "@every abc", "@cron *"):
        with pytest.raises(ScheduleError):
            parse_schedule(bad)


@pytest.mark.asyncio
async def test_tick_enqueues_then_idempotent_then_reenqueues(db_session) -> None:
    async with _restore_amz_monitor():
        async with session_scope() as session:
            agent = (
                await session.execute(
                    select(Agent).where(Agent.id == "amz-monitor")
                )
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
    async with _restore_amz_monitor():
        async with session_scope() as session:
            agent = (
                await session.execute(
                    select(Agent).where(Agent.id == "amz-monitor")
                )
            ).scalar_one()
            agent.schedule_cron = "@every 1m"
            agent.last_scheduled_at = None
            agent.mode = "paused"

        enqueued = await tick_once(now=datetime.now(UTC))
        assert enqueued == 0


@pytest.mark.asyncio
async def test_tick_skips_bad_schedule(db_session) -> None:
    async with _restore_amz_monitor():
        async with session_scope() as session:
            agent = (
                await session.execute(
                    select(Agent).where(Agent.id == "amz-monitor")
                )
            ).scalar_one()
            agent.schedule_cron = "@every never"
            agent.last_scheduled_at = None

        enqueued = await tick_once(now=datetime.now(UTC))
        assert enqueued == 0
