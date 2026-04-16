# M29: App Studio Growth Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the App Studio growth loop (4 agents) from OpenClaw to StudioOS — Growth Intelligence, Growth Execution, CEO, and Pricing — with Hub API tools, event schemas, and Slack per-agent routing.

**Architecture:** New `hub.py` tool module (3 tools for Hub API), new `schemas_app.py` (10 event types), 4 new workflow files following existing AMZ patterns (StateGraph + nodes), studio.yaml expansion from 3 to 7 agents.

**Tech Stack:** Python 3.12, LangGraph StateGraph, httpx, Pydantic schemas, pytest

---

### Task 1: Config + Hub API Tool Module

**Files:**
- Modify: `studioos/config.py`
- Create: `studioos/tools/hub.py`
- Test: `tests/test_hub_tools.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_hub_tools.py`:

```python
"""Hub API tool registration + response parsing."""
from __future__ import annotations

from studioos.tools.registry import get_tool


def test_hub_overview_registered() -> None:
    tool = get_tool("hub.api.overview")
    assert tool is not None
    schema = tool.input_schema
    assert "app_id" in schema["required"]


def test_hub_metrics_registered() -> None:
    tool = get_tool("hub.api.metrics")
    assert tool is not None
    schema = tool.input_schema
    assert "app_id" in schema["required"]
    assert "metric" in schema["required"]


def test_hub_campaigns_registered() -> None:
    tool = get_tool("hub.api.campaigns")
    assert tool is not None
    schema = tool.input_schema
    assert "action" in schema["required"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/ntopcugil/Documents/Projects/Amz/studioos && uv run pytest tests/test_hub_tools.py -v`
Expected: FAIL — tools not registered

- [ ] **Step 3: Add config fields**

In `studioos/config.py`, add after the `ebaycrosslister_password` line:

```python
    # Hub API (M29 — App Studio growth loop)
    hub_api_url: str = "https://hub.mifasuse.com/api"
    hub_api_key: str = ""
```

- [ ] **Step 4: Create `studioos/tools/hub.py`**

```python
"""Hub API tools — App Studio growth metrics + campaign management.

Hub is the internal analytics dashboard aggregating RevenueCat,
Firebase, Apple Search Ads, and AdMob data. Auth via X-API-Key header.
"""
from __future__ import annotations

from typing import Any

import httpx

from studioos.config import settings
from studioos.logging import get_logger

from .base import ToolContext, ToolError, ToolResult
from .registry import register_tool

log = get_logger(__name__)

_METRIC_ENDPOINTS = {
    "summary": "/metrics/summary",
    "conversion": "/metrics/conversion",
    "countries": "/metrics/countries",
    "cohort": "/metrics/cohort",
    "mrr_history": "/overview/mrr-history",
    "funnel": "/firebase/funnel",
    "retention": "/firebase/retention",
}


def _base() -> str:
    base = (settings.hub_api_url or "").rstrip("/")
    if not base:
        raise ToolError("STUDIOOS_HUB_API_URL is not configured")
    return base


def _headers() -> dict[str, str]:
    key = settings.hub_api_key
    if not key:
        raise ToolError("STUDIOOS_HUB_API_KEY is not configured")
    return {"X-API-Key": key}


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{_base()}{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
    except httpx.HTTPError as exc:
        raise ToolError(f"hub http error: {exc}") from exc
    if resp.status_code >= 400:
        raise ToolError(f"hub {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolError(f"hub non-json: {exc}") from exc


async def _put(path: str, body: dict[str, Any]) -> Any:
    url = f"{_base()}{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.put(url, headers=_headers(), json=body)
    except httpx.HTTPError as exc:
        raise ToolError(f"hub http error: {exc}") from exc
    if resp.status_code >= 400:
        raise ToolError(f"hub {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolError(f"hub non-json: {exc}") from exc


@register_tool(
    "hub.api.overview",
    description=(
        "Fetch single-app KPI snapshot from Hub: spend, installs, CPA, "
        "revenue, ROI, MRR, active_subscriptions, ARPU."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_id": {"type": "string"},
            "days": {"type": "integer"},
        },
        "required": ["app_id"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="app",
    cost_cents=0,
)
async def hub_api_overview(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    data = await _get(
        "/overview",
        {"app_id": args["app_id"], "days": int(args.get("days", 7))},
    )
    return ToolResult(data=data)


@register_tool(
    "hub.api.metrics",
    description=(
        "Fetch parametric metrics from Hub: summary, conversion, "
        "countries, cohort, mrr_history, funnel, or retention."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_id": {"type": "string"},
            "metric": {
                "type": "string",
                "enum": list(_METRIC_ENDPOINTS.keys()),
            },
            "days": {"type": "integer"},
        },
        "required": ["app_id", "metric"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="app",
    cost_cents=0,
)
async def hub_api_metrics(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    metric = args["metric"]
    path = _METRIC_ENDPOINTS.get(metric)
    if not path:
        raise ToolError(f"unknown metric: {metric}")
    params: dict[str, Any] = {"app_id": args["app_id"]}
    if metric not in ("mrr_history",):
        params["days"] = int(args.get("days", 30))
    data = await _get(path, params)
    return ToolResult(data=data)


@register_tool(
    "hub.api.campaigns",
    description=(
        "List or manage Apple Search Ads campaigns via Hub: "
        "list, pause, enable, set_budget."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "pause", "enable", "set_budget"],
            },
            "campaign_id": {"type": "integer"},
            "daily_budget": {"type": "number"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="app",
    cost_cents=1,
)
async def hub_api_campaigns(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    action = args["action"]
    if action == "list":
        data = await _get("/campaigns")
        return ToolResult(data=data)
    cid = args.get("campaign_id")
    if not cid:
        raise ToolError("campaign_id required for mutations")
    if action == "pause":
        data = await _put(f"/campaigns/{cid}/status", {"status": "PAUSED"})
    elif action == "enable":
        data = await _put(f"/campaigns/{cid}/status", {"status": "ENABLED"})
    elif action == "set_budget":
        budget = args.get("daily_budget")
        if budget is None:
            raise ToolError("daily_budget required for set_budget")
        data = await _put(f"/campaigns/{cid}/budget", {"daily_budget": float(budget)})
    else:
        raise ToolError(f"unknown action: {action}")
    return ToolResult(data=data)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_hub_tools.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add studioos/config.py studioos/tools/hub.py tests/test_hub_tools.py
git commit -m "feat(M29): Hub API tool module — overview, metrics, campaigns

3 tools wrapping hub.mifasuse.com API for App Studio growth loop.
X-API-Key auth via STUDIOOS_HUB_API_KEY env var.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Event Schemas

**Files:**
- Create: `studioos/events/schemas_app.py`
- Modify: `studioos/runtime/loop.py` (add import)
- Modify: `studioos/cli.py` (add import)
- Test: `tests/test_app_event_schemas.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_app_event_schemas.py`:

```python
"""App Studio event schema registration."""
from __future__ import annotations

from studioos.events.registry import registry


def test_all_app_events_registered() -> None:
    expected = [
        "app.growth.weekly_report",
        "app.growth.anomaly_detected",
        "app.discovery.completed",
        "app.experiment.proposed",
        "app.experiment.launched",
        "app.ceo.weekly_brief",
        "app.pricing.recommendation",
        "app.task.growth_intel",
        "app.task.pricing",
        "app.task.growth_exec",
    ]
    # Force import to trigger registration
    import studioos.events.schemas_app  # noqa: F401

    for event_type in expected:
        schema = registry.get(event_type, 1)
        assert schema is not None, f"{event_type} not registered"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app_event_schemas.py -v`
Expected: FAIL — schemas_app does not exist

- [ ] **Step 3: Create `studioos/events/schemas_app.py`**

```python
"""App Studio event schemas (M29)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from studioos.events.registry import registry


class GrowthWeeklyReportV1(BaseModel):
    """app.growth.weekly_report — weekly metrics digest from Growth Intelligence."""

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
    """app.growth.anomaly_detected — metric crossed a threshold."""

    app_id: str
    anomaly_type: str  # critical | warning | alert
    metric_name: str
    current_value: float | None = None
    previous_value: float | None = None
    delta_pct: float | None = None
    severity: str = "warning"


class DiscoveryCompletedV1(BaseModel):
    """app.discovery.completed — Product Discovery doc finished."""

    app_name: str
    competitors_count: int = 0
    mvp_features: list[str] = Field(default_factory=list)
    gtm_summary: str = ""


class ExperimentProposedV1(BaseModel):
    """app.experiment.proposed — CEO Lane experiment awaiting approval."""

    experiment_id: str
    app_id: str
    hypothesis: str
    variants: list[dict] = Field(default_factory=list)
    traffic_split: str = "50/50"
    duration_days: int = 14
    lane: str = "ceo"  # fast | ceo
    metrics: list[str] = Field(default_factory=list)


class ExperimentLaunchedV1(BaseModel):
    """app.experiment.launched — experiment is live."""

    experiment_id: str
    app_id: str
    lane: str = "fast"
    launched_at: str = ""


class AppCeoWeeklyBriefV1(BaseModel):
    """app.ceo.weekly_brief — CEO's weekly strategic summary."""

    decisions: list[dict] = Field(default_factory=list)
    delegations: list[dict] = Field(default_factory=list)
    kpi_summary: dict = Field(default_factory=dict)


class PricingRecommendationV1(BaseModel):
    """app.pricing.recommendation — pricing agent's analysis."""

    app_id: str
    current_price: str = ""
    recommended_price: str = ""
    rationale: str = ""
    ab_test_plan: dict = Field(default_factory=dict)


class AppTaskV1(BaseModel):
    """Generic task delegation event."""

    target_agent: str
    title: str = ""
    description: str = ""
    priority: str = "normal"


# Register all
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
```

- [ ] **Step 4: Add import in runtime/loop.py and cli.py**

In `studioos/runtime/loop.py`, find the line:
```python
from studioos.events import schemas_amz, schemas_test  # noqa: F401
```
Change to:
```python
from studioos.events import schemas_amz, schemas_app, schemas_test  # noqa: F401
```

In `studioos/cli.py`, find the same line and make the same change.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_app_event_schemas.py -v`
Expected: 1 passed

- [ ] **Step 6: Commit**

```bash
git add studioos/events/schemas_app.py studioos/runtime/loop.py studioos/cli.py tests/test_app_event_schemas.py
git commit -m "feat(M29): App Studio event schemas — 10 event types

Growth weekly report, anomaly detected, discovery completed,
experiment proposed/launched, CEO brief, pricing recommendation,
and 3 task delegation events.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Growth Intelligence Workflow

**Files:**
- Create: `studioos/workflows/app_studio_growth_intel.py`
- Test: `tests/test_growth_intel.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_growth_intel.py`:

```python
"""Growth Intelligence — anomaly detection logic."""
from __future__ import annotations

from studioos.workflows.app_studio_growth_intel import detect_anomalies


def test_trial_zero_is_critical() -> None:
    overview = {"trial_starts": 0, "roi": 2.0, "mrr": 100}
    thresholds = {"min_roi": 1.0, "max_churn_rate": 15.0, "min_retention_d7": 20.0, "max_mrr_drop_pct": 20.0}
    anomalies = detect_anomalies("quit_smoking", overview, {}, thresholds)
    assert any(a["anomaly_type"] == "critical" and a["metric_name"] == "trial_starts" for a in anomalies)


def test_low_roi_is_warning() -> None:
    overview = {"trial_starts": 50, "roi": 0.5, "mrr": 100}
    thresholds = {"min_roi": 1.0, "max_churn_rate": 15.0, "min_retention_d7": 20.0, "max_mrr_drop_pct": 20.0}
    anomalies = detect_anomalies("quit_smoking", overview, {}, thresholds)
    assert any(a["anomaly_type"] == "warning" and a["metric_name"] == "roi" for a in anomalies)


def test_healthy_metrics_no_anomalies() -> None:
    overview = {"trial_starts": 100, "roi": 2.5, "mrr": 200}
    conversion = {"churn_rate": 5.0}
    thresholds = {"min_roi": 1.0, "max_churn_rate": 15.0, "min_retention_d7": 20.0, "max_mrr_drop_pct": 20.0}
    anomalies = detect_anomalies("quit_smoking", overview, conversion, thresholds)
    assert len(anomalies) == 0


def test_high_churn_is_warning() -> None:
    overview = {"trial_starts": 50, "roi": 2.0, "mrr": 100}
    conversion = {"churn_rate": 20.0}
    thresholds = {"min_roi": 1.0, "max_churn_rate": 15.0, "min_retention_d7": 20.0, "max_mrr_drop_pct": 20.0}
    anomalies = detect_anomalies("quit_smoking", overview, conversion, thresholds)
    assert any(a["metric_name"] == "churn_rate" for a in anomalies)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_growth_intel.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Create `studioos/workflows/app_studio_growth_intel.py`**

Full workflow with `detect_anomalies` pure function + LangGraph nodes. This is a large file — see the spec section 3a for node descriptions. The workflow should follow the exact same pattern as `amz_analyst.py`: StateGraph with typed state, async nodes calling `invoke_from_state`, events/memories/kpi_updates return dicts.

Key implementation details:
- `detect_anomalies(app_id, overview, conversion, thresholds)` is a pure function returning `list[dict]` — testable without mocks.
- `node_collect` loops over `goals.tracked_apps`, calls `hub.api.overview` + `hub.api.metrics` for each.
- `node_analyze` calls `detect_anomalies` and emits `app.growth.anomaly_detected` events.
- `node_report` calls `llm.chat` for a Turkish summary, posts to Slack + Telegram, emits `app.growth.weekly_report`.
- Schedule: `0 8 * * 1`.
- Template: `app_studio_growth_intel`.

```python
"""app_studio_growth_intel workflow — weekly funnel report + anomaly detection.

Runs Monday 08:00 (1h before CEO). Pulls Hub API metrics for each
tracked app, detects anomalies deterministically, asks LLM for a
Turkish summary, and posts to Slack #intel + Telegram.
"""
from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class GrowthIntelState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    app_metrics: dict[str, Any]
    anomalies: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


def detect_anomalies(
    app_id: str,
    overview: dict[str, Any],
    conversion: dict[str, Any],
    thresholds: dict[str, Any],
) -> list[dict[str, Any]]:
    """Pure deterministic anomaly detection — no I/O."""
    anomalies: list[dict[str, Any]] = []
    trial = overview.get("trial_starts")
    if trial is not None and trial == 0:
        anomalies.append({
            "app_id": app_id,
            "anomaly_type": "critical",
            "metric_name": "trial_starts",
            "current_value": 0,
            "previous_value": None,
            "delta_pct": None,
            "severity": "critical",
        })
    roi = overview.get("roi")
    if roi is not None and roi < thresholds.get("min_roi", 1.0):
        anomalies.append({
            "app_id": app_id,
            "anomaly_type": "warning",
            "metric_name": "roi",
            "current_value": roi,
            "previous_value": None,
            "delta_pct": None,
            "severity": "warning",
        })
    churn = conversion.get("churn_rate")
    if churn is not None and churn > thresholds.get("max_churn_rate", 15.0):
        anomalies.append({
            "app_id": app_id,
            "anomaly_type": "warning",
            "metric_name": "churn_rate",
            "current_value": churn,
            "previous_value": None,
            "delta_pct": None,
            "severity": "warning",
        })
    ret_d7 = conversion.get("retention_d7")
    if ret_d7 is not None and ret_d7 < thresholds.get("min_retention_d7", 20.0):
        anomalies.append({
            "app_id": app_id,
            "anomaly_type": "warning",
            "metric_name": "retention_d7",
            "current_value": ret_d7,
            "previous_value": None,
            "delta_pct": None,
            "severity": "warning",
        })
    return anomalies


SYSTEM_PROMPT = """Sen bir mobil uygulama büyüme analistisin. Haftalık metrik
özetini Türkçe yaz. Anomali varsa vurgula. Kısa, somut, rakam odaklı ol.
Her app için: MRR, aktif abone, ROI, trial_starts, churn, retention_d7.
Max 300 kelime."""


async def node_collect(state: GrowthIntelState) -> dict[str, Any]:
    goals = state.get("goals") or {}
    tracked = goals.get("tracked_apps") or []
    all_metrics: dict[str, Any] = {}
    for app_id in tracked:
        overview = await invoke_from_state(
            state, "hub.api.overview", {"app_id": app_id, "days": 7}
        )
        conversion = await invoke_from_state(
            state, "hub.api.metrics", {"app_id": app_id, "metric": "conversion", "days": 7}
        )
        retention = await invoke_from_state(
            state, "hub.api.metrics", {"app_id": app_id, "metric": "retention", "days": 7}
        )
        all_metrics[app_id] = {
            "overview": (overview.get("data") or {}) if overview["status"] == "ok" else {},
            "conversion": (conversion.get("data") or {}) if conversion["status"] == "ok" else {},
            "retention": (retention.get("data") or {}) if retention["status"] == "ok" else {},
        }
    return {"app_metrics": all_metrics}


def node_analyze(state: GrowthIntelState) -> dict[str, Any]:
    goals = state.get("goals") or {}
    thresholds = goals.get("anomaly_thresholds") or {}
    app_metrics = state.get("app_metrics") or {}
    all_anomalies: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for app_id, metrics in app_metrics.items():
        anomalies = detect_anomalies(
            app_id, metrics.get("overview", {}), metrics.get("conversion", {}), thresholds
        )
        all_anomalies.extend(anomalies)
        for a in anomalies:
            events.append({
                "event_type": "app.growth.anomaly_detected",
                "event_version": 1,
                "payload": a,
                "idempotency_key": f"growth_intel:{state.get('run_id')}:{a['metric_name']}:{app_id}",
            })
    return {"anomalies": all_anomalies, "events": events}


async def node_report(state: GrowthIntelState) -> dict[str, Any]:
    app_metrics = state.get("app_metrics") or {}
    anomalies = state.get("anomalies") or []
    events = list(state.get("events") or [])
    memories: list[dict[str, Any]] = []
    kpi_updates: list[dict[str, Any]] = []

    user_msg = (
        f"App metrikleri:\n{json.dumps(app_metrics, ensure_ascii=False, default=str)[:3000]}\n\n"
        f"Anomaliler ({len(anomalies)}):\n{json.dumps(anomalies, ensure_ascii=False)[:1000]}\n\n"
        "Haftalık özet yaz."
    )
    llm = await invoke_from_state(state, "llm.chat", {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 1500,
        "temperature": 0.2,
    })
    brief = ((llm.get("data") or {}).get("content", "") if llm["status"] == "ok" else "LLM failed").strip()

    # Emit weekly report event
    for app_id, metrics in app_metrics.items():
        ov = metrics.get("overview", {})
        cv = metrics.get("conversion", {})
        events.append({
            "event_type": "app.growth.weekly_report",
            "event_version": 1,
            "payload": {
                "app_id": app_id,
                "period_days": 7,
                "mrr": ov.get("mrr"),
                "active_subs": ov.get("active_subscriptions"),
                "roi": ov.get("roi"),
                "trial_starts": cv.get("trial_starts"),
                "churn_rate": cv.get("churn_rate"),
                "retention_d7": cv.get("retention_d7"),
                "anomalies": [a for a in anomalies if a.get("app_id") == app_id],
                "summary": brief[:500],
            },
            "idempotency_key": f"growth_intel:{state.get('run_id')}:report:{app_id}",
        })

    # Notify
    text = f"*📊 App Studio — Haftalık Growth Raporu*\n\n{brief[:3500]}"
    await invoke_from_state(state, "telegram.notify", {
        "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True,
    })
    await invoke_from_state(state, "slack.notify", {"text": text, "mrkdwn": True})

    memories.append({
        "content": f"Weekly growth report: {brief[:300]}",
        "tags": ["app-studio", "growth", "weekly"],
        "importance": 0.7,
    })

    # KPI snapshots
    for app_id, metrics in app_metrics.items():
        ov = metrics.get("overview", {})
        if ov.get("mrr") is not None:
            kpi_updates.append({"name": f"app_{app_id}_mrr", "value": ov["mrr"]})
        if ov.get("active_subscriptions") is not None:
            kpi_updates.append({"name": f"app_{app_id}_active_subs", "value": ov["active_subscriptions"]})
        if ov.get("roi") is not None:
            kpi_updates.append({"name": f"app_{app_id}_roi", "value": ov["roi"]})

    return {
        "events": events,
        "memories": memories,
        "kpi_updates": kpi_updates,
        "summary": f"Growth report: {len(app_metrics)} apps, {len(anomalies)} anomalies",
    }


def build_graph() -> Any:
    graph = StateGraph(GrowthIntelState)
    graph.add_node("collect", node_collect)
    graph.add_node("analyze", node_analyze)
    graph.add_node("report", node_report)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "analyze")
    graph.add_edge("analyze", "report")
    graph.add_edge("report", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_growth_intel", 1, compiled)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_growth_intel.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add studioos/workflows/app_studio_growth_intel.py tests/test_growth_intel.py
git commit -m "feat(M29): Growth Intelligence workflow — weekly funnel + anomaly detection

Collects Hub API metrics per tracked app, detects anomalies
(trial=0 critical, ROI<1 warning, churn>15% warning), LLM summary,
Slack + Telegram notify.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Growth Execution + Pricing + CEO Workflows

**Files:**
- Create: `studioos/workflows/app_studio_growth_exec.py`
- Create: `studioos/workflows/app_studio_pricing.py`
- Create: `studioos/workflows/app_studio_ceo.py`
- Test: `tests/test_growth_exec.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_growth_exec.py`:

```python
"""Growth Execution — lane classification."""
from __future__ import annotations

from studioos.workflows.app_studio_growth_exec import classify_lane


def test_fast_lane_reversible_low_impact() -> None:
    exp = {"reversible": True, "days_to_implement": 0.5, "user_impact_pct": 10, "is_pricing": False, "is_paywall": False}
    assert classify_lane(exp) == "fast"


def test_ceo_lane_pricing_change() -> None:
    exp = {"reversible": True, "days_to_implement": 0.5, "user_impact_pct": 10, "is_pricing": True, "is_paywall": False}
    assert classify_lane(exp) == "ceo"


def test_ceo_lane_high_impact() -> None:
    exp = {"reversible": True, "days_to_implement": 0.5, "user_impact_pct": 30, "is_pricing": False, "is_paywall": False}
    assert classify_lane(exp) == "ceo"


def test_ceo_lane_paywall_change() -> None:
    exp = {"reversible": True, "days_to_implement": 0.5, "user_impact_pct": 5, "is_pricing": False, "is_paywall": True}
    assert classify_lane(exp) == "ceo"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_growth_exec.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Create all three workflow files**

Create `studioos/workflows/app_studio_growth_exec.py`:

```python
"""app_studio_growth_exec — experiment design and launch.

Event-triggered by app.growth.weekly_report. Proposes experiments
classified as Fast Lane (auto-launch) or CEO Lane (approval-gated).
"""
from __future__ import annotations

import json
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class GrowthExecState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    report: dict[str, Any]
    experiments: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    summary: str


def classify_lane(exp: dict[str, Any]) -> str:
    """Fast Lane vs CEO Lane classification."""
    if exp.get("is_pricing") or exp.get("is_paywall"):
        return "ceo"
    if exp.get("user_impact_pct", 0) > 20:
        return "ceo"
    if exp.get("days_to_implement", 0) > 1:
        return "ceo"
    if not exp.get("reversible", True):
        return "ceo"
    return "fast"


SYSTEM_PROMPT = """Sen bir growth experiment tasarımcısısın. Haftalık metrik
raporuna dayanarak 1-3 experiment öner. Her experiment için:
- hypothesis (1 cümle)
- variants (kontrol + test)
- traffic_split
- duration_days
- metrics (başarı kriteri)
- is_pricing (bool), is_paywall (bool), reversible (bool),
  days_to_implement (float), user_impact_pct (int)

JSON array olarak yanıtla. Markdown fence kullanma."""


async def node_intake(state: GrowthExecState) -> dict[str, Any]:
    event = state.get("input") or {}
    payload = event.get("payload") or {}
    return {"report": payload}


async def node_propose(state: GrowthExecState) -> dict[str, Any]:
    report = state.get("report") or {}
    llm = await invoke_from_state(state, "llm.chat", {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(report, ensure_ascii=False, default=str)[:3000]},
        ],
        "max_tokens": 1500,
        "temperature": 0.3,
        "response_format": "json_object",
    })
    experiments: list[dict[str, Any]] = []
    if llm["status"] == "ok":
        data = (llm.get("data") or {}).get("parsed_json")
        if isinstance(data, list):
            experiments = data[:3]
        elif isinstance(data, dict) and "experiments" in data:
            experiments = data["experiments"][:3]
    for exp in experiments:
        exp["lane"] = classify_lane(exp)
        exp["experiment_id"] = str(uuid4())[:8]
        exp["app_id"] = report.get("app_id", "unknown")
    return {"experiments": experiments}


async def node_gate(state: GrowthExecState) -> dict[str, Any]:
    experiments = state.get("experiments") or []
    events: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    fast_count = 0
    ceo_count = 0
    for exp in experiments:
        if exp.get("lane") == "fast":
            events.append({
                "event_type": "app.experiment.launched",
                "event_version": 1,
                "payload": {
                    "experiment_id": exp.get("experiment_id", "?"),
                    "app_id": exp.get("app_id", "?"),
                    "lane": "fast",
                    "launched_at": "",
                },
                "idempotency_key": f"growth_exec:{state.get('run_id')}:fast:{exp.get('experiment_id')}",
            })
            fast_count += 1
        else:
            events.append({
                "event_type": "app.experiment.proposed",
                "event_version": 1,
                "payload": exp,
                "idempotency_key": f"growth_exec:{state.get('run_id')}:ceo:{exp.get('experiment_id')}",
            })
            approvals.append({
                "reason": f"CEO Lane experiment: {exp.get('hypothesis', '?')[:100]}",
                "payload": exp,
                "expires_in_seconds": 60 * 60 * 24 * 7,
            })
            ceo_count += 1
    if experiments:
        text = (
            f"*🧪 Growth Execution — {len(experiments)} experiment*\n"
            f"Fast Lane: {fast_count} | CEO Lane: {ceo_count}"
        )
        await invoke_from_state(state, "slack.notify", {"text": text, "mrkdwn": True})
        await invoke_from_state(state, "telegram.notify", {
            "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True,
        })
        memories.append({
            "content": f"Proposed {len(experiments)} experiments: {fast_count} fast, {ceo_count} CEO lane",
            "tags": ["app-studio", "growth-exec", "experiments"],
            "importance": 0.6,
        })
    return {
        "events": events,
        "approvals": approvals,
        "memories": memories,
        "summary": f"{len(experiments)} experiments ({fast_count} fast, {ceo_count} CEO)",
    }


def build_graph() -> Any:
    graph = StateGraph(GrowthExecState)
    graph.add_node("intake", node_intake)
    graph.add_node("propose", node_propose)
    graph.add_node("gate", node_gate)
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "propose")
    graph.add_edge("propose", "gate")
    graph.add_edge("gate", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_growth_exec", 1, compiled)
```

Create `studioos/workflows/app_studio_pricing.py`:

```python
"""app_studio_pricing — country-based pricing analysis + A/B test planning.

Event-triggered by app.task.pricing from CEO. Pulls Hub country data,
asks LLM for WTP analysis, recommends price + A/B test plan.
Approval-gated — CEO must approve before experiment launches.
"""
from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class PricingState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    pricing_data: dict[str, Any]
    recommendation: dict[str, Any]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    summary: str


SYSTEM_PROMPT = """Sen bir mobil uygulama fiyatlama stratejistisin. Ülke bazlı
ARPU, conversion ve cohort verilerine bakarak:
1. Mevcut fiyatın uygunluğunu değerlendir
2. Önerilen fiyat noktası (gerekçeli)
3. A/B test planı (variants, split, duration, success metric)

JSON olarak yanıtla: {recommended_price, rationale, ab_test_plan}"""


async def node_collect(state: PricingState) -> dict[str, Any]:
    event = state.get("input") or {}
    payload = event.get("payload") or {}
    app_id = payload.get("description", "quit_smoking").split()[-1] if payload.get("description") else "quit_smoking"
    countries = await invoke_from_state(
        state, "hub.api.metrics", {"app_id": app_id, "metric": "countries", "days": 30}
    )
    conversion = await invoke_from_state(
        state, "hub.api.metrics", {"app_id": app_id, "metric": "conversion", "days": 30}
    )
    mrr = await invoke_from_state(
        state, "hub.api.metrics", {"app_id": app_id, "metric": "mrr_history"}
    )
    return {"pricing_data": {
        "app_id": app_id,
        "countries": (countries.get("data") or {}) if countries["status"] == "ok" else {},
        "conversion": (conversion.get("data") or {}) if conversion["status"] == "ok" else {},
        "mrr_history": (mrr.get("data") or {}) if mrr["status"] == "ok" else {},
    }}


async def node_analyze(state: PricingState) -> dict[str, Any]:
    data = state.get("pricing_data") or {}
    llm = await invoke_from_state(state, "llm.chat", {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False, default=str)[:4000]},
        ],
        "max_tokens": 1500,
        "temperature": 0.2,
        "response_format": "json_object",
    })
    rec: dict[str, Any] = {}
    if llm["status"] == "ok":
        parsed = (llm.get("data") or {}).get("parsed_json")
        if isinstance(parsed, dict):
            rec = parsed
    rec["app_id"] = data.get("app_id", "?")
    return {"recommendation": rec}


async def node_recommend(state: PricingState) -> dict[str, Any]:
    rec = state.get("recommendation") or {}
    app_id = rec.get("app_id", "?")
    events: list[dict[str, Any]] = [{
        "event_type": "app.pricing.recommendation",
        "event_version": 1,
        "payload": {
            "app_id": app_id,
            "current_price": rec.get("current_price", ""),
            "recommended_price": rec.get("recommended_price", ""),
            "rationale": rec.get("rationale", ""),
            "ab_test_plan": rec.get("ab_test_plan", {}),
        },
        "idempotency_key": f"pricing:{state.get('run_id')}:{app_id}",
    }]
    approvals: list[dict[str, Any]] = [{
        "reason": f"Pricing recommendation for {app_id}: {rec.get('recommended_price', '?')}",
        "payload": rec,
        "expires_in_seconds": 60 * 60 * 24 * 7,
    }]
    text = (
        f"*💰 Pricing — {app_id}*\n"
        f"Öneri: {rec.get('recommended_price', '?')}\n"
        f"Gerekçe: {rec.get('rationale', '—')[:200]}"
    )
    await invoke_from_state(state, "slack.notify", {"text": text, "mrkdwn": True})
    await invoke_from_state(state, "telegram.notify", {
        "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True,
    })
    return {
        "events": events,
        "approvals": approvals,
        "memories": [{
            "content": f"Pricing recommendation for {app_id}: {rec.get('recommended_price')} — {rec.get('rationale', '')[:200]}",
            "tags": ["app-studio", "pricing", app_id],
            "importance": 0.7,
        }],
        "summary": f"Pricing rec for {app_id}: {rec.get('recommended_price', '?')}",
    }


def build_graph() -> Any:
    graph = StateGraph(PricingState)
    graph.add_node("collect", node_collect)
    graph.add_node("analyze", node_analyze)
    graph.add_node("recommend", node_recommend)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "analyze")
    graph.add_edge("analyze", "recommend")
    graph.add_edge("recommend", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_pricing", 1, compiled)
```

Create `studioos/workflows/app_studio_ceo.py`:

```python
"""app_studio_ceo — weekly strategic decision agent for App Studio.

Runs Monday 09:00. Reads Hub metrics + last week's growth report +
KPI state. LLM produces weekly brief + max 2 decisions + task
delegations. Posts to Slack #hq + Telegram.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import desc, select

from studioos.db import session_scope
from studioos.kpi.store import get_current_state, upsert_target
from studioos.logging import get_logger
from studioos.models import Event
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class AppCeoState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    digest: dict[str, Any]
    brief: str
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


APP_CEO_KPI_TARGETS = [
    ("app_mrr", 500.0, "higher_better", "$", "MRR hedefi"),
    ("app_roi", 2.0, "higher_better", "x", "ROI hedefi"),
    ("app_churn_rate", 10.0, "lower_better", "%", "Churn oranı hedefi"),
    ("app_active_subs", 200.0, "higher_better", "", "Aktif abone hedefi"),
]


async def node_seed_kpi(state: AppCeoState) -> dict[str, Any]:
    state_accum = dict(state.get("state") or {})
    if state_accum.get("kpi_targets_seeded"):
        return {}
    async with session_scope() as session:
        for name, value, direction, unit, desc_text in APP_CEO_KPI_TARGETS:
            await upsert_target(
                session, name=name, target_value=value,
                direction=direction, studio_id="app-studio",
                unit=unit, description=desc_text,
            )
    state_accum["kpi_targets_seeded"] = True
    return {"state": state_accum}


SYSTEM_PROMPT = """Sen App Studio CEO'susun. Haftalık growth raporu ve KPI'lara
bakarak:

## Bu hafta ne oldu
3-5 bullet, somut sayılarla.

## MRR'ı en çok etkileyecek 3 şey

## Kararlar (max 2)
1. Pricing değişikliği (varsa)
2. Acquisition değişikliği (varsa)

## Delegasyonlar
```json
{"tasks": [{"target_agent": "app-studio-pricing|app-studio-growth-intel|app-studio-growth-exec", "title": "...", "description": "...", "priority": "high|normal"}]}
```

Türkçe yaz. Terse, factual."""


_VALID_TARGETS = {
    "app-studio-growth-intel",
    "app-studio-growth-exec",
    "app-studio-pricing",
}


async def node_collect(state: AppCeoState) -> dict[str, Any]:
    goals = state.get("goals") or {}
    tracked = goals.get("tracked_apps") or ["quit_smoking", "sms_forward"]
    app_overviews: dict[str, Any] = {}
    for app_id in tracked:
        r = await invoke_from_state(state, "hub.api.overview", {"app_id": app_id, "days": 7})
        if r["status"] == "ok":
            app_overviews[app_id] = r.get("data") or {}
    since = datetime.now(UTC) - timedelta(days=7)
    async with session_scope() as session:
        reports = (
            (await session.execute(
                select(Event)
                .where(Event.studio_id == "app-studio")
                .where(Event.event_type == "app.growth.weekly_report")
                .where(Event.recorded_at >= since)
                .order_by(desc(Event.recorded_at)).limit(5)
            )).scalars().all()
        )
        kpi_views = await get_current_state(session, studio_id="app-studio")
    return {"digest": {
        "apps": app_overviews,
        "weekly_reports": [{"app_id": e.payload.get("app_id"), "summary": e.payload.get("summary", "")[:300]} for e in reports],
        "kpis": [{"name": s.name, "current": float(s.current) if s.current is not None else None, "target": float(s.target) if s.target is not None else None} for s in kpi_views],
    }}


async def node_brief(state: AppCeoState) -> dict[str, Any]:
    digest = state.get("digest") or {}
    llm = await invoke_from_state(state, "llm.chat", {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(digest, ensure_ascii=False, default=str)[:5000]},
        ],
        "max_tokens": 2000,
        "temperature": 0.2,
    })
    if llm["status"] != "ok":
        return {"brief": "_LLM çağrısı başarısız_"}
    return {"brief": ((llm.get("data") or {}).get("content", "")).strip()}


async def node_publish(state: AppCeoState) -> dict[str, Any]:
    brief = state.get("brief") or ""
    today = datetime.now(UTC).date().isoformat()
    # Extract delegations
    match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", brief, re.MULTILINE)
    delegations: list[dict[str, Any]] = []
    if match:
        try:
            data = json.loads(match.group(1))
            for t in (data.get("tasks") or [])[:5]:
                if isinstance(t, dict) and t.get("target_agent") in _VALID_TARGETS:
                    delegations.append(t)
        except ValueError:
            pass
    human_brief = re.sub(r"```json[\s\S]*?```", "", brief, flags=re.MULTILINE).strip()
    if delegations:
        lines = ["\n*Delegasyonlar:*"]
        for t in delegations:
            lines.append(f"• `{t['target_agent']}` _{t.get('priority', 'normal')}_ — {t.get('title', '?')}")
        human_brief += "\n" + "\n".join(lines)
    text = f"*🧭 App Studio CEO — Haftalık Brief — {today}*\n\n{human_brief[:3500]}"
    await invoke_from_state(state, "slack.notify", {"text": text, "mrkdwn": True})
    await invoke_from_state(state, "telegram.notify", {
        "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True,
    })
    events: list[dict[str, Any]] = [{
        "event_type": "app.ceo.weekly_brief",
        "event_version": 1,
        "payload": {"decisions": [], "delegations": delegations, "kpi_summary": {}},
        "idempotency_key": f"app_ceo:{state.get('run_id')}:brief",
    }]
    for t in delegations:
        suffix = t["target_agent"].replace("app-studio-", "")
        events.append({
            "event_type": f"app.task.{suffix}",
            "event_version": 1,
            "payload": t,
            "idempotency_key": f"app_ceo:{state.get('run_id')}:{t['target_agent']}",
        })
    state_accum = dict(state.get("state") or {})
    state_accum["briefs_total"] = int(state_accum.get("briefs_total", 0)) + 1
    return {
        "events": events,
        "memories": [{
            "content": f"App CEO weekly brief {today}: {brief[:300]}",
            "tags": ["app-studio", "ceo", "weekly", today],
            "importance": 0.8,
        }],
        "kpi_updates": [{"name": "app_ceo_briefs_total", "value": state_accum["briefs_total"]}],
        "state": state_accum,
        "summary": f"Weekly brief + {len(delegations)} delegations",
    }


def build_graph() -> Any:
    graph = StateGraph(AppCeoState)
    graph.add_node("seed_kpi", node_seed_kpi)
    graph.add_node("collect", node_collect)
    graph.add_node("brief", node_brief)
    graph.add_node("publish", node_publish)
    graph.add_edge(START, "seed_kpi")
    graph.add_edge("seed_kpi", "collect")
    graph.add_edge("collect", "brief")
    graph.add_edge("brief", "publish")
    graph.add_edge("publish", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_ceo", 1, compiled)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_growth_exec.py tests/test_growth_intel.py -v`
Expected: 8 passed

- [ ] **Step 5: Smoke-import all workflows**

Run: `uv run python -c "from studioos.workflows import app_studio_growth_intel, app_studio_growth_exec, app_studio_pricing, app_studio_ceo; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add studioos/workflows/app_studio_growth_exec.py studioos/workflows/app_studio_pricing.py studioos/workflows/app_studio_ceo.py tests/test_growth_exec.py
git commit -m "feat(M29): Growth Exec + Pricing + CEO workflows

Growth Execution: experiment proposal with Fast/CEO lane gating.
Pricing: country-based WTP analysis + A/B test plan (approval-gated).
CEO: weekly brief + KPI targets + task delegation.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: studio.yaml + Slack Env + Deploy

**Files:**
- Modify: `studioos/studios/app_studio/studio.yaml`

- [ ] **Step 1: Update studio.yaml**

Replace the entire file with the expanded version — keeping existing 3 agents (pulse, reflector, pruner) and adding 4 new agents + templates + subscriptions:

Add these 4 new templates to the `templates:` section:
```yaml
  - id: app_studio_growth_intel
    version: 1
    display_name: "Growth Intelligence"
    description: "Weekly funnel report + anomaly detection + product discovery"
    workflow_ref: app_studio_growth_intel
  - id: app_studio_growth_exec
    version: 1
    display_name: "Growth Execution"
    description: "Experiment design and launch — Fast Lane vs CEO Lane"
    workflow_ref: app_studio_growth_exec
  - id: app_studio_ceo
    version: 1
    display_name: "App Studio CEO"
    description: "Weekly strategic decisions, KPI tracking, task delegation"
    workflow_ref: app_studio_ceo
  - id: app_studio_pricing
    version: 1
    display_name: "Pricing"
    description: "Country-based pricing analysis and A/B test planning"
    workflow_ref: app_studio_pricing
```

Add these 4 new agents to the `agents:` section:
```yaml
  - id: app-studio-growth-intel
    template_id: app_studio_growth_intel
    template_version: 1
    display_name: "Growth Intelligence"
    mode: normal
    heartbeat_config: {}
    schedule_cron: "0 8 * * 1"
    goals:
      tracked_apps: [quit_smoking, sms_forward]
      anomaly_thresholds:
        min_roi: 1.0
        max_churn_rate: 15.0
        min_retention_d7: 20.0
        max_mrr_drop_pct: 20.0
    tool_scope:
      - hub.api.overview
      - hub.api.metrics
      - llm.chat
      - slack.notify
      - telegram.notify
      - memory.search
  - id: app-studio-growth-exec
    template_id: app_studio_growth_exec
    template_version: 1
    display_name: "Growth Execution"
    mode: normal
    heartbeat_config: {}
    goals: {}
    tool_scope:
      - llm.chat
      - slack.notify
      - telegram.notify
      - memory.search
  - id: app-studio-ceo
    template_id: app_studio_ceo
    template_version: 1
    display_name: "App Studio CEO"
    mode: normal
    heartbeat_config: {}
    schedule_cron: "0 9 * * 1"
    goals:
      tracked_apps: [quit_smoking, sms_forward]
    tool_scope:
      - hub.api.overview
      - hub.api.metrics
      - llm.chat
      - slack.notify
      - telegram.notify
      - memory.search
      - kpi.read
  - id: app-studio-pricing
    template_id: app_studio_pricing
    template_version: 1
    display_name: "Pricing"
    mode: normal
    heartbeat_config: {}
    goals: {}
    tool_scope:
      - hub.api.overview
      - hub.api.metrics
      - llm.chat
      - slack.notify
      - telegram.notify
      - memory.search
```

Add subscriptions:
```yaml
subscriptions:
  - subscriber: app-studio-growth-exec
    event_pattern: "app.growth.weekly_report"
    priority: 30
  - subscriber: app-studio-growth-exec
    event_pattern: "app.task.growth_exec"
    priority: 20
  - subscriber: app-studio-pricing
    event_pattern: "app.task.pricing"
    priority: 20
  - subscriber: app-studio-growth-intel
    event_pattern: "app.task.growth_intel"
    priority: 20
```

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest tests/test_hub_tools.py tests/test_app_event_schemas.py tests/test_growth_intel.py tests/test_growth_exec.py tests/test_analyst_scoring.py tests/test_pricer_gates.py -q`
Expected: all pass

- [ ] **Step 3: Commit**

```bash
git add studioos/studios/app_studio/studio.yaml
git commit -m "feat(M29): App Studio — 4 growth loop agents in studio.yaml

Growth Intelligence (Mon 08:00), CEO (Mon 09:00), Growth Execution
(event-triggered), Pricing (event-triggered). Subscriptions wired.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 4: Add env vars on prod**

SSH to server and append Hub API + Slack tokens:

```bash
ssh -i deployer_new_key deployer@168.119.15.239 'cd /home/deployer/studioos && cat >> .env << "EOF"
STUDIOOS_HUB_API_KEY=orzKx9ShTha9PHbPGm6ieuMjCfbZVu33TLvPn1Oz3Y4
STUDIOOS_HUB_API_URL=https://hub.mifasuse.com/api
EOF
'
```

Append to existing STUDIOOS_SLACK_AGENT_TOKENS (add these entries):
```
app-studio-ceo=xoxb-REDACTED
app-studio-growth-intel=xoxb-REDACTED
app-studio-growth-exec=xoxb-REDACTED
app-studio-pricing=xoxb-REDACTED
```

Append to existing STUDIOOS_SLACK_AGENT_CHANNELS:
```
app-studio-growth-intel=C0AN9PGJELE
app-studio-growth-exec=C0ANFD5F32Q
app-studio-pricing=C0ANFD4APK6
```

- [ ] **Step 5: Push and deploy**

```bash
git push origin main
```

- [ ] **Step 6: Verify on prod**

After deploy (~2 min):
```bash
ssh -i deployer_new_key deployer@168.119.15.239 \
  "cd /home/deployer/studioos && docker compose exec -T studioos uv run python -c \"
from studioos.tools import hub
from studioos.tools.registry import get_tool
print('hub.api.overview:', get_tool('hub.api.overview') is not None)
print('hub.api.metrics:', get_tool('hub.api.metrics') is not None)
print('hub.api.campaigns:', get_tool('hub.api.campaigns') is not None)
\""
```

Expected: all True.
