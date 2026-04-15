"""Outbox publisher — reads unpublished events, pushes to the bus.

Postgres remains the source of truth (atomic state+event commit). The publisher
drains the outbox and XADDs each envelope to the configured bus backend. Once
acknowledged by the bus, we mark `published_at`.

Consumer-side fan-out + subscription matching now lives in
`runtime.consumer`, not here.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from studioos.bus import EventEnvelope, get_bus
from studioos.db import session_scope
from studioos.logging import bind_correlation, bind_run, get_logger
from studioos.models import Event

log = get_logger(__name__)


def _to_envelope(event: Event) -> EventEnvelope:
    return EventEnvelope(
        event_id=event.id,
        event_type=event.event_type,
        event_version=event.event_version,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        studio_id=event.studio_id,
        source_type=event.source_type,
        source_id=event.source_id,
        source_run_id=event.source_run_id,
        payload=event.payload or {},
        metadata=event.event_metadata or {},
        occurred_at=event.occurred_at,
    )


async def _publish_one(session: AsyncSession, event_id: UUID) -> bool:
    event = (
        await session.execute(select(Event).where(Event.id == event_id))
    ).scalar_one()

    bind_correlation(event.correlation_id)
    bind_run(event.source_run_id)

    envelope = _to_envelope(event)
    bus = get_bus()
    try:
        bus_id = await bus.publish(envelope)
    except Exception:
        event.publish_attempts = (event.publish_attempts or 0) + 1
        log.exception(
            "outbox.publish_failed",
            event_id=str(event_id),
            event_type=event.event_type,
        )
        return False

    event.published_at = datetime.now(UTC)
    event.publish_attempts = (event.publish_attempts or 0) + 1
    log.info(
        "outbox.published",
        event_id=str(event_id),
        event_type=event.event_type,
        bus_id=bus_id,
    )
    return True


async def publish_batch() -> int:
    """Publish all unpublished events in one pass. Returns published count."""
    async with session_scope() as session:
        stmt = (
            select(Event.id)
            .where(Event.published_at.is_(None))
            .where(Event.dead_letter_at.is_(None))
            .order_by(Event.recorded_at.asc())
            .limit(50)
            .with_for_update(skip_locked=True)
        )
        rows = (await session.execute(stmt)).scalars().all()
        if not rows:
            return 0
        ok = 0
        for event_id in rows:
            try:
                if await _publish_one(session, event_id):
                    ok += 1
            except Exception:
                log.exception("outbox.batch_error", event_id=str(event_id))
        return ok


async def outbox_loop(stop_event: asyncio.Event, poll_seconds: float) -> None:
    log.info("outbox.started", poll_seconds=poll_seconds)
    while not stop_event.is_set():
        try:
            processed = await publish_batch()
            if processed == 0:
                await asyncio.sleep(poll_seconds)
        except Exception:
            log.exception("outbox.loop_error")
            await asyncio.sleep(poll_seconds)
    log.info("outbox.stopped")
