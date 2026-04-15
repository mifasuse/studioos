"""Bus backend protocol + envelope dataclass."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class EventEnvelope:
    """Serializable event envelope carried over the bus."""

    event_id: UUID
    event_type: str
    event_version: int
    correlation_id: UUID
    causation_id: UUID | None
    studio_id: str | None
    source_type: str
    source_id: str | None
    source_run_id: UUID | None
    payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class DeliveredMessage:
    """A message pulled from the bus, pending ack."""

    bus_id: str
    envelope: EventEnvelope
    delivery_count: int


class BusBackend(Protocol):
    """Minimal bus contract: publish, consume, ack, dead-letter, reclaim."""

    async def publish(self, envelope: EventEnvelope) -> str:
        """Append envelope to the primary stream. Returns the bus id."""

    async def ensure_group(self, group: str) -> None:
        """Idempotently create a consumer group on the primary stream."""

    async def consume(
        self,
        group: str,
        consumer: str,
        *,
        count: int = 10,
        block_ms: int = 1000,
    ) -> list[DeliveredMessage]:
        """Block-read up to `count` new messages for `group`."""

    async def reclaim(
        self,
        group: str,
        consumer: str,
        *,
        min_idle_ms: int,
        count: int = 10,
    ) -> list[DeliveredMessage]:
        """Claim idle pending messages from crashed consumers."""

    async def ack(self, group: str, bus_id: str) -> None:
        """Mark message as processed; removes from pending list."""

    async def dead_letter(
        self, group: str, message: DeliveredMessage, reason: str
    ) -> None:
        """Move message to DLQ stream and ack the original."""

    async def close(self) -> None:
        """Release any held connections."""
