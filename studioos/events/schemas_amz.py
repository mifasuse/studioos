"""AMZ Studio event schemas (M6)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from studioos.events.registry import registry


class PriceCheckedV1(BaseModel):
    """amz.price.checked — emitted once per scanned ASIN."""

    asin: str = Field(min_length=10, max_length=10)
    marketplace: str = "US"
    price: float = Field(ge=0)
    currency: str = "USD"
    source: str = "pricefinder"


class PriceAnomalyDetectedV1(BaseModel):
    """amz.price.anomaly_detected — emitted when a price delta crosses a threshold."""

    asin: str = Field(min_length=10, max_length=10)
    marketplace: str = "US"
    previous_price: float
    current_price: float
    delta_pct: float
    threshold_pct: float
    direction: str  # "up" | "down"


class OpportunityConfirmedV1(BaseModel):
    """amz.opportunity.confirmed — analyst endorses an anomaly as actionable."""

    asin: str = Field(min_length=10, max_length=10)
    marketplace: str = "US"
    previous_price: float
    current_price: float
    delta_pct: float
    direction: str
    verdict: str = "accept"
    confidence: float = Field(ge=0, le=1)
    rationale: str
    recommended_action: str | None = None


class OpportunityRejectedV1(BaseModel):
    """amz.opportunity.rejected — analyst dismisses an anomaly as noise."""

    asin: str = Field(min_length=10, max_length=10)
    marketplace: str = "US"
    delta_pct: float
    direction: str
    verdict: str = "reject"
    confidence: float = Field(ge=0, le=1)
    rationale: str


registry.register("amz.price.checked", 1, PriceCheckedV1)
registry.register("amz.price.anomaly_detected", 1, PriceAnomalyDetectedV1)
registry.register("amz.opportunity.confirmed", 1, OpportunityConfirmedV1)
registry.register("amz.opportunity.rejected", 1, OpportunityRejectedV1)
