"""Stream consumer — reads envelopes from the bus and fans out to subscribers.

One consumer group per subscription row. On each envelope we:
  1. Match against the subscription's event_pattern (glob).
  2. If match + action=wake_agent: enqueue a pending run (idempotent at the
     idempotency_key layer).
  3. XACK the message.

If processing raises, we leave the message pending; the next loop iteration's
reclaim pass will pick it up. After `max_delivery_attempts`, the message is
moved to the DLQ stream and the corresponding event gets `dead_letter_at`.
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import socket
from datetime import UTC, datetime

from sqlalchemy import select

from studioos.bus import BusBackend, DeliveredMessage, get_bus
from studioos.config import settings
from studioos.db import session_scope
from studioos.logging import bind_correlation, bind_run, get_logger
from studioos.models import Event, Subscription
from studioos.runtime.triggers import create_pending_run

log = get_logger(__name__)


def _consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def _group_name(sub: Subscription) -> str:
    return f"sub:{sub.id}:{sub.subscriber_id}"


async def _load_subscriptions() -> list[Subscription]:
    async with session_scope() as session:
        rows = (await session.execute(select(Subscription))).scalars().all()
        # detach for use outside session
        session.expunge_all()
        return list(rows)


async def _handle_message(sub: Subscription, msg: DeliveredMessage) -> bool:
    """Process one delivered message for a subscription. Returns True on ack."""
    envelope = msg.envelope

    if not fnmatch.fnmatch(envelope.event_type, sub.event_pattern):
        # Not for us — ack and move on.
        return True

    if sub.action != "wake_agent":
        log.warning(
            "consumer.unknown_action",
            action=sub.action,
            subscriber=sub.subscriber_id,
        )
        return True

    bind_correlation(envelope.correlation_id)
    bind_run(envelope.source_run_id)

    async with session_scope() as session:
        await create_pending_run(
            session,
            agent_id=sub.subscriber_id,
            trigger_type="event",
            trigger_ref=str(envelope.event_id),
            correlation_id=envelope.correlation_id,
            parent_run_id=envelope.source_run_id,
            priority=sub.priority,
            input_snapshot={
                "event_id": str(envelope.event_id),
                "event_type": envelope.event_type,
                "event_version": envelope.event_version,
                "payload": envelope.payload,
                "metadata": envelope.metadata,
            },
        )

    log.info(
        "consumer.delivered",
        event_type=envelope.event_type,
        subscriber=sub.subscriber_id,
        bus_id=msg.bus_id,
        delivery=msg.delivery_count,
    )
    return True


async def _mark_dead_letter(event_id: str, reason: str) -> None:
    async with session_scope() as session:
        event = (
            await session.execute(
                select(Event).where(Event.id == event_id)
            )
        ).scalar_one_or_none()
        if event is None:
            return
        event.dead_letter_at = datetime.now(UTC)
        event.delivery_attempts = (event.delivery_attempts or 0) + 1
    log.error("consumer.dead_letter", event_id=event_id, reason=reason)


async def _increment_delivery(event_id: str) -> None:
    async with session_scope() as session:
        event = (
            await session.execute(
                select(Event).where(Event.id == event_id)
            )
        ).scalar_one_or_none()
        if event is None:
            return
        event.delivery_attempts = (event.delivery_attempts or 0) + 1


async def _process_batch(
    bus: BusBackend, sub: Subscription, consumer: str
) -> int:
    group = _group_name(sub)
    await bus.ensure_group(group)

    # Reclaim stuck messages from dead consumers first.
    reclaimed = await bus.reclaim(
        group,
        consumer,
        min_idle_ms=settings.bus_claim_idle_ms,
        count=10,
    )
    new_msgs = await bus.consume(
        group,
        consumer,
        count=10,
        block_ms=settings.bus_read_block_ms,
    )
    all_msgs = list(reclaimed) + list(new_msgs)
    if not all_msgs:
        return 0

    for msg in all_msgs:
        try:
            if msg.delivery_count > settings.bus_max_delivery_attempts:
                await bus.dead_letter(
                    group, msg, reason="max_delivery_attempts_exceeded"
                )
                await _mark_dead_letter(
                    str(msg.envelope.event_id),
                    reason=f"group={group} attempts={msg.delivery_count}",
                )
                continue

            ack = await _handle_message(sub, msg)
            if ack:
                await bus.ack(group, msg.bus_id)
        except Exception:
            log.exception(
                "consumer.handler_error",
                bus_id=msg.bus_id,
                subscriber=sub.subscriber_id,
                delivery=msg.delivery_count,
            )
            await _increment_delivery(str(msg.envelope.event_id))
            # leave pending; next reclaim cycle retries
    return len(all_msgs)


async def drain_once() -> int:
    """Single pass across all subscriptions — used by tests."""
    bus = get_bus()
    subs = await _load_subscriptions()
    consumer = _consumer_name()
    total = 0
    for sub in subs:
        total += await _process_batch(bus, sub, consumer)
    return total


async def consumer_loop(stop_event: asyncio.Event) -> None:
    bus = get_bus()
    consumer = _consumer_name()
    log.info("consumer.started", consumer=consumer, backend=settings.bus_backend)
    refresh_every = 10.0
    last_refresh = 0.0
    subs: list[Subscription] = []
    import time

    while not stop_event.is_set():
        try:
            now = time.monotonic()
            if now - last_refresh > refresh_every:
                subs = await _load_subscriptions()
                for sub in subs:
                    await bus.ensure_group(_group_name(sub))
                last_refresh = now

            worked = False
            for sub in subs:
                if stop_event.is_set():
                    break
                processed = await _process_batch(bus, sub, consumer)
                if processed:
                    worked = True
            if not worked:
                await asyncio.sleep(0.1)
        except Exception:
            log.exception("consumer.loop_error")
            await asyncio.sleep(1.0)
    log.info("consumer.stopped")
