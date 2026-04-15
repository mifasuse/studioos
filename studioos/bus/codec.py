"""JSON codec for event envelopes on the bus."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from .base import EventEnvelope


def _default(o: Any) -> Any:
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not json-serializable: {type(o).__name__}")


def encode(envelope: EventEnvelope) -> str:
    return json.dumps(
        {
            "event_id": str(envelope.event_id),
            "event_type": envelope.event_type,
            "event_version": envelope.event_version,
            "correlation_id": str(envelope.correlation_id),
            "causation_id": str(envelope.causation_id)
            if envelope.causation_id
            else None,
            "studio_id": envelope.studio_id,
            "source_type": envelope.source_type,
            "source_id": envelope.source_id,
            "source_run_id": str(envelope.source_run_id)
            if envelope.source_run_id
            else None,
            "payload": envelope.payload,
            "metadata": envelope.metadata,
            "occurred_at": envelope.occurred_at.isoformat()
            if envelope.occurred_at
            else None,
        },
        default=_default,
        ensure_ascii=False,
    )


def decode(raw: str) -> EventEnvelope:
    data = json.loads(raw)
    return EventEnvelope(
        event_id=UUID(data["event_id"]),
        event_type=data["event_type"],
        event_version=int(data["event_version"]),
        correlation_id=UUID(data["correlation_id"]),
        causation_id=UUID(data["causation_id"]) if data.get("causation_id") else None,
        studio_id=data.get("studio_id"),
        source_type=data["source_type"],
        source_id=data.get("source_id"),
        source_run_id=UUID(data["source_run_id"])
        if data.get("source_run_id")
        else None,
        payload=data.get("payload") or {},
        metadata=data.get("metadata") or {},
        occurred_at=datetime.fromisoformat(data["occurred_at"])
        if data.get("occurred_at")
        else None,
    )
