"""App Studio event schemas (M29)."""
from __future__ import annotations
from pydantic import BaseModel, Field
from studioos.events.registry import registry

class GrowthWeeklyReportV1(BaseModel):
    app_id: str
    period_days: int = 7
    mrr: float | None = None
    active_subs: int | None = None
    roi: float | None = None
    trial_starts: int | None = None
    churn_rate: float | None = None
    retention_d7: float | None = None
    anomalies: list[dict] = Field(default_factory=list)
    summary: str = ""

class GrowthAnomalyDetectedV1(BaseModel):
    app_id: str
    anomaly_type: str
    metric_name: str
    current_value: float | None = None
    previous_value: float | None = None
    delta_pct: float | None = None
    severity: str = "warning"

class DiscoveryCompletedV1(BaseModel):
    app_name: str
    competitors_count: int = 0
    mvp_features: list[str] = Field(default_factory=list)
    gtm_summary: str = ""

class ExperimentProposedV1(BaseModel):
    experiment_id: str
    app_id: str
    hypothesis: str
    variants: list[dict] = Field(default_factory=list)
    traffic_split: str = "50/50"
    duration_days: int = 14
    lane: str = "ceo"
    metrics: list[str] = Field(default_factory=list)

class ExperimentLaunchedV1(BaseModel):
    experiment_id: str
    app_id: str
    lane: str = "fast"
    launched_at: str = ""

class AppCeoWeeklyBriefV1(BaseModel):
    decisions: list[dict] = Field(default_factory=list)
    delegations: list[dict] = Field(default_factory=list)
    kpi_summary: dict = Field(default_factory=dict)

class PricingRecommendationV1(BaseModel):
    app_id: str
    current_price: str = ""
    recommended_price: str = ""
    rationale: str = ""
    ab_test_plan: dict = Field(default_factory=dict)

class AppTaskV1(BaseModel):
    target_agent: str
    title: str = ""
    description: str = ""
    priority: str = "normal"

# Register
registry.register("app.growth.weekly_report", 1, GrowthWeeklyReportV1)
registry.register("app.growth.anomaly_detected", 1, GrowthAnomalyDetectedV1)
registry.register("app.discovery.completed", 1, DiscoveryCompletedV1)
registry.register("app.experiment.proposed", 1, ExperimentProposedV1)
registry.register("app.experiment.launched", 1, ExperimentLaunchedV1)
registry.register("app.ceo.weekly_brief", 1, AppCeoWeeklyBriefV1)
registry.register("app.pricing.recommendation", 1, PricingRecommendationV1)
registry.register("app.task.growth_intel", 1, AppTaskV1)
registry.register("app.task.pricing", 1, AppTaskV1)
registry.register("app.task.growth_exec", 1, AppTaskV1)
