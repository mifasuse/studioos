"""Event envelope — invariant structure for every event in StudioOS."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class EventSource(BaseModel):
    """Who produced this event."""

    type: Literal["agent", "system", "human", "external"]
    identifier: str  # agent_id, service name, user handle, webhook source
    run_id: UUID | None = None  # populated when source is an agent run


class EventEnvelope(BaseModel):
    """
    Invariant envelope wrapping every event.

    Payload is validated against a registered schema keyed by (event_type, event_version).
    """

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str  # e.g. "amz.opportunity.detected"
    event_version: int = 1
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    source: EventSource
    studio_id: str | None = None

    correlation_id: UUID = Field(default_factory=uuid4)
    causation_id: UUID | None = None
    idempotency_key: str | None = None

    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
