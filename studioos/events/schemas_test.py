"""Test event schemas — Milestone 1 vertical slice."""
from __future__ import annotations

from pydantic import BaseModel, Field

from studioos.events.registry import registry


class OpportunityDetectedV1(BaseModel):
    """test.opportunity.detected — emitted by scout_test workflow."""

    opportunity_id: str
    value: float = Field(ge=0)
    label: str = Field(min_length=1)
    source: str = "mock"


class OpportunityAcknowledgedV1(BaseModel):
    """test.opportunity.acknowledged — emitted by analyst_test workflow."""

    opportunity_id: str
    verdict: str
    notes: str | None = None


registry.register("test.opportunity.detected", 1, OpportunityDetectedV1)
registry.register("test.opportunity.acknowledged", 1, OpportunityAcknowledgedV1)
