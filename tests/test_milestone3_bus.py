"""Milestone 3 — event bus (Redis Streams / inproc) + DLQ.

Exercised on the inproc backend so it runs without external Redis. The Redis
backend is wire-compatible and is verified in prod via the M1/M2 e2e suite
once the container is up.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest

from studioos.bus import EventEnvelope, get_bus, reset_bus
from studioos.config import settings
from studioos.db import session_scope
from studioos.models import Event
from studioos.runtime.consumer import drain_once
from studioos.runtime.dispatcher import dispatch_once
from studioos.runtime.outbox import publish_batch
from studioos.runtime.triggers import create_pending_run
from sqlalchemy import select


async def _drain_runtime(max_iters: int = 20) -> None:
    for _ in range(max_iters):
        ran = await dispatch_once()
        published = await publish_batch()
        consumed = await drain_once()
        if ran is None and published == 0 and consumed == 0:
            return
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_bus_publish_consume_ack(db_session) -> None:
    """Happy path: scout run → event published to bus → consumer wakes analyst."""
    with patch("studioos.workflows.scout_test.random", return_value=0.85):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="test-scout",
                trigger_type="manual",
                trigger_ref="m3-happy",
            )
        await _drain_runtime()

    # Event row was published (marked) and not dead-lettered.
    async with session_scope() as session:
        events = (
            (await session.execute(select(Event))).scalars().all()
        )
    assert events, "no events recorded"
    published = [e for e in events if e.published_at is not None]
    assert published, "at least one event should be published"
    assert all(e.dead_letter_at is None for e in events)

    # Verify analyst run was enqueued by the consumer fan-out.
    from studioos.models import AgentRun

    async with session_scope() as session:
        runs = (
            (
                await session.execute(
                    select(AgentRun).where(AgentRun.agent_id == "test-analyst")
                )
            )
            .scalars()
            .all()
        )
    assert runs, "consumer should have created an analyst run from the bus event"
    assert any(r.trigger_type == "event" for r in runs)


@pytest.mark.asyncio
async def test_bus_dlq_on_poison_message(db_session) -> None:
    """Poison path: handler always raises → after N attempts, DLQ + dead_letter_at."""
    reset_bus()
    bus = get_bus()

    # Publish a raw envelope directly (bypassing outbox so we control event_id).
    event_id = uuid4()
    correlation_id = uuid4()

    # Insert an Event row so dead-letter bookkeeping has a target.
    async with session_scope() as session:
        session.add(
            Event(
                id=event_id,
                event_type="test.opportunity.detected",
                event_version=1,
                studio_id="test",
                correlation_id=correlation_id,
                source_type="test",
                source_id="m3-dlq",
                payload={"opportunity_id": "poison", "value": 99},
                occurred_at=datetime.now(UTC),
                published_at=datetime.now(UTC),
            )
        )

    envelope = EventEnvelope(
        event_id=event_id,
        event_type="test.opportunity.detected",
        event_version=1,
        correlation_id=correlation_id,
        causation_id=None,
        studio_id="test",
        source_type="test",
        source_id="m3-dlq",
        source_run_id=None,
        payload={"opportunity_id": "poison", "value": 99},
        metadata={},
        occurred_at=datetime.now(UTC),
    )
    await bus.publish(envelope)

    # Force the handler to always raise.
    attempts = {"n": 0}

    async def _boom(*args, **kwargs):
        attempts["n"] += 1
        raise RuntimeError("boom")

    with patch("studioos.runtime.consumer._handle_message", side_effect=_boom):
        max_attempts = settings.bus_max_delivery_attempts
        for _ in range(max_attempts + 2):
            await drain_once()
            await asyncio.sleep(0)

    assert attempts["n"] >= 1, "handler should have been called at least once"

    # Event marked as dead-lettered.
    async with session_scope() as session:
        row = (
            await session.execute(select(Event).where(Event.id == event_id))
        ).scalar_one()
    assert row.dead_letter_at is not None
    assert row.delivery_attempts >= 1

    # DLQ stream has the message (inproc helper).
    from studioos.bus.inproc import InProcBus

    assert isinstance(bus, InProcBus)
    assert bus.dlq_size() >= 1
