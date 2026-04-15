"""Event schema registry — keyed by (event_type, version)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError


class EventRegistry:
    """Central registry that validates event payloads against versioned schemas."""

    def __init__(self) -> None:
        self._schemas: dict[tuple[str, int], type[BaseModel]] = {}

    def register(
        self, event_type: str, version: int, schema: type[BaseModel]
    ) -> type[BaseModel]:
        key = (event_type, version)
        if key in self._schemas:
            raise ValueError(f"Event schema already registered: {event_type} v{version}")
        self._schemas[key] = schema
        return schema

    def get(self, event_type: str, version: int) -> type[BaseModel]:
        key = (event_type, version)
        if key not in self._schemas:
            raise KeyError(
                f"No schema for event {event_type} v{version}. "
                f"Known: {sorted(self._schemas.keys())}"
            )
        return self._schemas[key]

    def validate(
        self, event_type: str, version: int, payload: dict[str, Any]
    ) -> BaseModel:
        schema = self.get(event_type, version)
        try:
            return schema.model_validate(payload)
        except ValidationError as e:
            raise ValueError(
                f"Payload validation failed for {event_type} v{version}: {e}"
            ) from e

    def list_all(self) -> list[tuple[str, int]]:
        return sorted(self._schemas.keys())


registry = EventRegistry()
