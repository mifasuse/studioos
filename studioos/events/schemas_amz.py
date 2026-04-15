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


registry.register("amz.price.checked", 1, PriceCheckedV1)
registry.register("amz.price.anomaly_detected", 1, PriceAnomalyDetectedV1)
