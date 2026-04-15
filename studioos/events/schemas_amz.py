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
    """amz.opportunity.confirmed — analyst endorses an opportunity."""

    asin: str = Field(min_length=10, max_length=10)
    marketplace: str = "US"
    # Anomaly fields are optional because confirmed can also come from a
    # discovery event (no previous_price, no delta) rather than an anomaly.
    previous_price: float | None = None
    current_price: float | None = None
    delta_pct: float | None = None
    direction: str | None = None
    source: str = "anomaly"  # "anomaly" | "discovery"
    verdict: str = "accept"
    confidence: float = Field(ge=0, le=1)
    rationale: str
    recommended_action: str | None = None


class OpportunityRejectedV1(BaseModel):
    """amz.opportunity.rejected — analyst dismisses an opportunity as noise."""

    asin: str = Field(min_length=10, max_length=10)
    marketplace: str = "US"
    delta_pct: float | None = None
    direction: str | None = None
    source: str = "anomaly"
    verdict: str = "reject"
    confidence: float = Field(ge=0, le=1)
    rationale: str


class OpportunityDiscoveredV1(BaseModel):
    """amz.opportunity.discovered — scout surfaces a new candidate."""

    asin: str = Field(min_length=10, max_length=10)
    marketplace: str = "US"
    title: str | None = None
    brand: str | None = None
    tr_price_try: float | None = None
    buybox_price_usd: float | None = None
    estimated_profit_usd: float | None = None
    profit_margin_pct: float | None = None
    roi_pct: float | None = None
    monthly_sold: int | None = None
    sales_rank: int | None = None
    review_count: int | None = None
    rating: float | None = None
    fba_offer_count: int | None = None


class RepriceRecommendedV1(BaseModel):
    """amz.reprice.recommended — pricer suggests a price change (notify-only)."""

    asin: str = Field(min_length=10, max_length=10)
    sku: str
    listing_id: int
    current_price: float
    proposed_price: float
    buy_box_price: float | None = None
    buybox_seller_name: str | None = None
    delta: float
    clamped_to_floor: bool = False
    strategy: str = "buy_box_win"
    strategy_rationale: str | None = None
    age_days: float | None = None


class QASmokeFailedV1(BaseModel):
    """amz.qa.smoke_failed — emitted by amz-qa when any infra healthcheck fails."""

    failed_services: list[str]
    details: list[dict] | None = None


class CrossListCandidateV1(BaseModel):
    """amz.crosslist.candidate — emitted by amz-crosslister for new eBay arbitrage finds."""

    asin: str = Field(min_length=10, max_length=10)
    title: str | None = None
    brand: str | None = None
    amazon_buybox_usd: float | None = None
    ebay_new_usd: float | None = None
    premium_pct: float | None = None
    monthly_sold: int | None = None
    fba_offer_count: int | None = None
    sales_rank: int | None = None


class AdCandidateV1(BaseModel):
    """amz.ad.candidate — emitted by amz-admanager for new PPC-eligible products."""

    asin: str = Field(min_length=10, max_length=10)
    title: str | None = None
    brand: str | None = None
    buybox_usd: float | None = None
    monthly_sold: int | None = None
    review_count: int | None = None
    rating: float | None = None
    fba_offer_count: int | None = None
    sales_rank: int | None = None


class TaskAssignedV1(BaseModel):
    """amz.task.assigned — CEO delegating a concrete task to a target agent."""

    target_agent: str
    title: str
    description: str
    priority: str = "normal"  # emergency | high | normal | low
    payload: dict | None = None
    deadline: str | None = None
    requested_by: str = "amz-ceo"


class DeployNotificationV1(BaseModel):
    """amz.deploy.notification — a service deploy completed; usually wakes amz-qa."""

    service: str
    commit: str | None = None
    success: bool = True
    message: str | None = None
    deployed_by: str | None = None


registry.register("amz.price.checked", 1, PriceCheckedV1)
registry.register("amz.price.anomaly_detected", 1, PriceAnomalyDetectedV1)
registry.register("amz.opportunity.confirmed", 1, OpportunityConfirmedV1)
registry.register("amz.opportunity.rejected", 1, OpportunityRejectedV1)
registry.register("amz.opportunity.discovered", 1, OpportunityDiscoveredV1)
registry.register("amz.reprice.recommended", 1, RepriceRecommendedV1)
registry.register("amz.qa.smoke_failed", 1, QASmokeFailedV1)
registry.register("amz.crosslist.candidate", 1, CrossListCandidateV1)
registry.register("amz.ad.candidate", 1, AdCandidateV1)
registry.register("amz.task.assigned", 1, TaskAssignedV1)
# Per-target task event types — let each agent subscribe to its own
# pattern without payload-side filtering. CEO routes to the right
# variant when delegating.
for _suffix in (
    "monitor",
    "scout",
    "analyst",
    "pricer",
    "repricer",
    "crosslister",
    "admanager",
    "qa",
    "dev",
):
    registry.register(f"amz.task.{_suffix}", 1, TaskAssignedV1)
registry.register("amz.deploy.notification", 1, DeployNotificationV1)
