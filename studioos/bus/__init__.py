"""Event bus — abstraction over Redis Streams with an in-process fallback.

Envelope shape (JSON):
    {
        "event_id": "<uuid>",
        "event_type": "domain.thing.happened",
        "event_version": 1,
        "correlation_id": "<uuid>",
        "causation_id": "<uuid|null>",
        "studio_id": "<str|null>",
        "source_type": "agent",
        "source_id": "scout_test",
        "source_run_id": "<uuid|null>",
        "payload": {...},
        "metadata": {...},
        "occurred_at": "ISO8601",
    }

Backends publish+consume this envelope. Delivery is at-least-once; consumers
must be idempotent (StudioOS runs rely on `idempotency_key` at the trigger
layer to dedupe downstream effects).
"""
from __future__ import annotations

from .base import BusBackend, DeliveredMessage, EventEnvelope
from .factory import get_bus, reset_bus

__all__ = [
    "BusBackend",
    "DeliveredMessage",
    "EventEnvelope",
    "get_bus",
    "reset_bus",
]
