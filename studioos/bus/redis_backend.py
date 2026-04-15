"""Redis Streams bus backend."""
from __future__ import annotations

import asyncio
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from studioos.logging import get_logger

from .base import BusBackend, DeliveredMessage, EventEnvelope
from .codec import decode, encode

log = get_logger(__name__)


class RedisBus:
    """Redis Streams implementation of BusBackend.

    Per-event-loop Redis client cache — asyncpg-style: each asyncio loop gets
    its own connection to avoid cross-loop binding issues under pytest.
    """

    def __init__(
        self,
        *,
        redis_url: str,
        stream: str,
        dlq_stream: str,
    ) -> None:
        self._redis_url = redis_url
        self._stream = stream
        self._dlq = dlq_stream
        self._clients: dict[int, Redis] = {}
        self._ensured: dict[tuple[int, str], bool] = {}

    def _client(self) -> Redis:
        loop = asyncio.get_event_loop()
        key = id(loop)
        client = self._clients.get(key)
        if client is None:
            client = Redis.from_url(self._redis_url, decode_responses=True)
            self._clients[key] = client
        return client

    async def publish(self, envelope: EventEnvelope) -> str:
        body = encode(envelope)
        bus_id = await self._client().xadd(
            self._stream,
            {"data": body, "event_type": envelope.event_type},
        )
        return bus_id  # type: ignore[return-value]

    async def ensure_group(self, group: str) -> None:
        loop_key = id(asyncio.get_event_loop())
        if self._ensured.get((loop_key, group)):
            return
        try:
            await self._client().xgroup_create(
                name=self._stream, groupname=group, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._ensured[(loop_key, group)] = True

    def _to_message(self, raw: tuple[str, dict[str, Any]]) -> DeliveredMessage | None:
        bus_id, fields = raw
        data = fields.get("data")
        if data is None:
            return None
        try:
            envelope = decode(data)
        except Exception:
            log.exception("bus.decode_failed", bus_id=bus_id)
            return None
        return DeliveredMessage(
            bus_id=bus_id, envelope=envelope, delivery_count=0
        )

    async def consume(
        self,
        group: str,
        consumer: str,
        *,
        count: int = 10,
        block_ms: int = 1000,
    ) -> list[DeliveredMessage]:
        await self.ensure_group(group)
        resp = await self._client().xreadgroup(
            groupname=group,
            consumername=consumer,
            streams={self._stream: ">"},
            count=count,
            block=block_ms,
        )
        if not resp:
            return []
        out: list[DeliveredMessage] = []
        for _stream_name, items in resp:
            for raw in items:
                msg = self._to_message(raw)
                if msg is not None:
                    out.append(msg)
        # Populate delivery_count from XPENDING for each bus_id.
        if out:
            ids = [m.bus_id for m in out]
            pending = await self._client().xpending_range(
                name=self._stream,
                groupname=group,
                min=ids[0],
                max=ids[-1],
                count=len(ids),
                consumername=consumer,
            )
            counts = {p["message_id"]: int(p["times_delivered"]) for p in pending}
            out = [
                DeliveredMessage(
                    bus_id=m.bus_id,
                    envelope=m.envelope,
                    delivery_count=counts.get(m.bus_id, 1),
                )
                for m in out
            ]
        return out

    async def reclaim(
        self,
        group: str,
        consumer: str,
        *,
        min_idle_ms: int,
        count: int = 10,
    ) -> list[DeliveredMessage]:
        await self.ensure_group(group)
        try:
            next_id, claimed, _deleted = await self._client().xautoclaim(
                name=self._stream,
                groupname=group,
                consumername=consumer,
                min_idle_time=min_idle_ms,
                start_id="0-0",
                count=count,
            )
        except AttributeError:
            # Older redis-py fallback
            return []
        if not claimed:
            return []
        out: list[DeliveredMessage] = []
        for raw in claimed:
            msg = self._to_message(raw)
            if msg is not None:
                out.append(msg)
        if out:
            ids = [m.bus_id for m in out]
            pending = await self._client().xpending_range(
                name=self._stream,
                groupname=group,
                min=ids[0],
                max=ids[-1],
                count=len(ids),
                consumername=consumer,
            )
            counts = {p["message_id"]: int(p["times_delivered"]) for p in pending}
            out = [
                DeliveredMessage(
                    bus_id=m.bus_id,
                    envelope=m.envelope,
                    delivery_count=counts.get(m.bus_id, 1),
                )
                for m in out
            ]
        return out

    async def ack(self, group: str, bus_id: str) -> None:
        await self._client().xack(self._stream, group, bus_id)

    async def dead_letter(
        self, group: str, message: DeliveredMessage, reason: str
    ) -> None:
        await self._client().xadd(
            self._dlq,
            {
                "data": encode(message.envelope),
                "event_type": message.envelope.event_type,
                "reason": reason,
                "source_group": group,
                "original_bus_id": message.bus_id,
            },
        )
        await self._client().xack(self._stream, group, message.bus_id)

    async def close(self) -> None:
        for client in self._clients.values():
            try:
                await client.aclose()
            except Exception:
                pass
        self._clients.clear()
        self._ensured.clear()
