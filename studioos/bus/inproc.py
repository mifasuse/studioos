"""In-process bus backend — used for tests and single-node dev.

Per-event-loop singleton: each asyncio loop gets its own stream + groups so
pytest-asyncio fixtures (which spin fresh loops) stay isolated without needing
an external Redis.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .base import BusBackend, DeliveredMessage, EventEnvelope


@dataclass
class _Entry:
    bus_id: str
    envelope: EventEnvelope
    delivery_count: int = 0


@dataclass
class _Group:
    pending: dict[str, _Entry] = field(default_factory=dict)
    cursor: int = 0


@dataclass
class _Stream:
    seq: int = 0
    entries: list[_Entry] = field(default_factory=list)
    groups: dict[str, _Group] = field(default_factory=dict)


class InProcBus:
    """Simple asyncio-only bus; no external dependencies."""

    def __init__(self, stream: str, dlq_stream: str) -> None:
        self._stream = _Stream()
        self._dlq = _Stream()
        self._stream_name = stream
        self._dlq_name = dlq_stream
        self._cond = asyncio.Condition()

    async def publish(self, envelope: EventEnvelope) -> str:
        async with self._cond:
            self._stream.seq += 1
            bus_id = f"{self._stream.seq}-0"
            entry = _Entry(bus_id=bus_id, envelope=envelope)
            self._stream.entries.append(entry)
            self._cond.notify_all()
            return bus_id

    async def ensure_group(self, group: str) -> None:
        async with self._cond:
            self._stream.groups.setdefault(group, _Group())

    async def consume(
        self,
        group: str,
        consumer: str,
        *,
        count: int = 10,
        block_ms: int = 1000,
    ) -> list[DeliveredMessage]:
        async with self._cond:
            grp = self._stream.groups.setdefault(group, _Group())
            # Wait for new entries if cursor is caught up.
            if grp.cursor >= len(self._stream.entries):
                try:
                    await asyncio.wait_for(
                        self._cond.wait_for(
                            lambda: grp.cursor < len(self._stream.entries)
                        ),
                        timeout=max(block_ms, 1) / 1000.0,
                    )
                except asyncio.TimeoutError:
                    return []
            out: list[DeliveredMessage] = []
            while grp.cursor < len(self._stream.entries) and len(out) < count:
                entry = self._stream.entries[grp.cursor]
                grp.cursor += 1
                entry.delivery_count += 1
                grp.pending[entry.bus_id] = entry
                out.append(
                    DeliveredMessage(
                        bus_id=entry.bus_id,
                        envelope=entry.envelope,
                        delivery_count=entry.delivery_count,
                    )
                )
            return out

    async def reclaim(
        self,
        group: str,
        consumer: str,
        *,
        min_idle_ms: int,
        count: int = 10,
    ) -> list[DeliveredMessage]:
        # InProc has no idle timing — always redeliver pending so retry + DLQ
        # behavior can be exercised deterministically in tests.
        async with self._cond:
            grp = self._stream.groups.setdefault(group, _Group())
            out: list[DeliveredMessage] = []
            for entry in list(grp.pending.values())[:count]:
                entry.delivery_count += 1
                out.append(
                    DeliveredMessage(
                        bus_id=entry.bus_id,
                        envelope=entry.envelope,
                        delivery_count=entry.delivery_count,
                    )
                )
            return out

    async def ack(self, group: str, bus_id: str) -> None:
        async with self._cond:
            grp = self._stream.groups.get(group)
            if grp is not None:
                grp.pending.pop(bus_id, None)

    async def dead_letter(
        self, group: str, message: DeliveredMessage, reason: str
    ) -> None:
        async with self._cond:
            self._dlq.seq += 1
            dlq_id = f"{self._dlq.seq}-0"
            self._dlq.entries.append(
                _Entry(bus_id=dlq_id, envelope=message.envelope)
            )
            grp = self._stream.groups.get(group)
            if grp is not None:
                grp.pending.pop(message.bus_id, None)
        # Stash reason in metadata for debugging from tests
        self._last_dlq_reason: dict[str, Any] = {
            "bus_id": message.bus_id,
            "reason": reason,
        }

    async def close(self) -> None:
        return None

    # Test helpers -------------------------------------------------------
    def dlq_size(self) -> int:
        return len(self._dlq.entries)

    def stream_size(self) -> int:
        return len(self._stream.entries)


_inproc_singletons: dict[int, InProcBus] = {}


def get_inproc_bus(stream: str, dlq_stream: str) -> InProcBus:
    loop = asyncio.get_event_loop()
    key = id(loop)
    bus = _inproc_singletons.get(key)
    if bus is None:
        bus = InProcBus(stream=stream, dlq_stream=dlq_stream)
        _inproc_singletons[key] = bus
    return bus


def reset_inproc_buses() -> None:
    _inproc_singletons.clear()
