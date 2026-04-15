"""Outbox publisher — reads unpublished events, dispatches to subscribers.

Milestone 1: in-process dispatch. No Redis. Subscription resolution happens
directly in Python; when a subscription matches, a new pending run is
enqueued for the subscriber agent.

M3 will replace the inner push with Redis Streams XADD.
"""
from __future__ import annotations

import asyncio
import fnmatch
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from studioos.db import session_scope
from studioos.logging import bind_correlation, bind_run, get_logger
from studioos.models import Event, Subscription
from studioos.runtime.triggers import create_pending_run

log = get_logger(__name__)


async def _match_subscriptions(
    session: AsyncSession, event: Event
) -> list[Subscription]:
    """Return subscriptions whose pattern matches the event type."""
    all_subs = (await session.execute(select(Subscription))).scalars().all()
    matched = [s for s in all_subs if fnmatch.fnmatch(event.event_type, s.event_pattern)]
    # Highest priority first
    matched.sort(key=lambda s: s.priority)
    return matched


async def process_event(session: AsyncSession, event_id: UUID) -> int:
    """Deliver one event to all matching subscribers. Returns fanout count."""
    event = (
        await session.execute(select(Event).where(Event.id == event_id))
    ).scalar_one()

    bind_correlation(event.correlation_id)
    bind_run(event.source_run_id)

    subs = await _match_subscriptions(session, event)
    if not subs:
        log.debug("outbox.no_subscribers", event_type=event.event_type)
    for sub in subs:
        if sub.action == "wake_agent":
            await create_pending_run(
                session,
                agent_id=sub.subscriber_id,
                trigger_type="event",
                trigger_ref=str(event.id),
                correlation_id=event.correlation_id,
                parent_run_id=event.source_run_id,
                priority=sub.priority,
                input_snapshot={
                    "event_id": str(event.id),
                    "event_type": event.event_type,
                    "event_version": event.event_version,
                    "payload": event.payload,
                    "metadata": event.event_metadata,
                },
            )
            log.info(
                "outbox.delivered",
                event_type=event.event_type,
                subscriber=sub.subscriber_id,
            )
        else:
            log.warning("outbox.unknown_action", action=sub.action)

    event.published_at = datetime.now(UTC)
    event.publish_attempts = (event.publish_attempts or 0) + 1
    return len(subs)


async def publish_batch() -> int:
    """Publish all unpublished events in one pass."""
    async with session_scope() as session:
        stmt = (
            select(Event.id)
            .where(Event.published_at.is_(None))
            .order_by(Event.recorded_at.asc())
            .limit(50)
            .with_for_update(skip_locked=True)
        )
        rows = (await session.execute(stmt)).scalars().all()
        if not rows:
            return 0
        for event_id in rows:
            try:
                await process_event(session, event_id)
            except Exception:
                log.exception("outbox.publish_failed", event_id=str(event_id))
        return len(rows)


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
