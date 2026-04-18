"""Hub analytics API tool adapters (M29 — App Studio growth loop).

Three tools:
  hub.api.overview   — app overview metrics
  hub.api.metrics    — parametric metrics (summary, conversion, countries, …)
  hub.api.campaigns  — campaign management (list / pause / enable / set_budget)

Auth: X-API-Key header from settings.hub_api_key
Base URL: settings.hub_api_url
"""
from __future__ import annotations

from typing import Any

import httpx

from studioos.config import settings

from .base import ToolContext, ToolError, ToolResult
from .registry import register_tool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_METRIC_PATHS: dict[str, str] = {
    "summary": "/metrics/summary",
    "conversion": "/metrics/conversion",
    "countries": "/metrics/countries",
    "cohort": "/metrics/cohort",
    "mrr_history": "/overview/mrr-history",
    "funnel": "/firebase/funnel",
    "retention": "/firebase/retention",
}

# metrics that do NOT accept a `days` param
_METRIC_NO_DAYS: frozenset[str] = frozenset({"mrr_history"})


def _base() -> str:
    return (settings.hub_api_url or "https://hub.mifasuse.com/api").rstrip("/")


def _headers() -> dict[str, str]:
    key = settings.hub_api_key
    if not key:
        raise ToolError("STUDIOOS_HUB_API_KEY is not configured")
    return {"X-API-Key": key}


async def _get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"{_base()}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params, headers=_headers())
    if resp.status_code >= 400:
        raise ToolError(f"hub API {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolError(f"hub API non-JSON response: {exc}") from exc


async def _put(path: str, body: dict[str, Any]) -> dict[str, Any]:
    url = f"{_base()}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(url, json=body, headers=_headers())
    if resp.status_code >= 400:
        raise ToolError(f"hub API {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolError(f"hub API non-JSON response: {exc}") from exc


# ---------------------------------------------------------------------------
# hub.api.overview
# ---------------------------------------------------------------------------

@register_tool(
    "hub.api.overview",
    description=(
        "Fetch the overview dashboard metrics for an app from the Hub analytics "
        "API. Returns install counts, revenue, MRR, churn, and other KPIs for "
        "the requested look-back window."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_id": {
                "type": "string",
                "description": "The app identifier in Hub.",
            },
            "days": {
                "type": "integer",
                "description": "Look-back window in days (default 7).",
                "default": 7,
            },
        },
        "required": ["app_id"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="app",
    cost_cents=0,
)
async def hub_api_overview(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    app_id: str = args["app_id"]
    days: int = int(args.get("days", 7))
    data = await _get("/overview", {"app_id": app_id, "days": days})
    return ToolResult(data=data)


# ---------------------------------------------------------------------------
# hub.api.metrics
# ---------------------------------------------------------------------------

@register_tool(
    "hub.api.metrics",
    description=(
        "Fetch a specific analytics metric from Hub for an app. "
        "Available metrics: summary, conversion, countries, cohort, "
        "mrr_history, funnel, retention."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_id": {
                "type": "string",
                "description": "The app identifier in Hub.",
            },
            "metric": {
                "type": "string",
                "enum": list(_METRIC_PATHS.keys()),
                "description": "Which metric to retrieve.",
            },
            "days": {
                "type": "integer",
                "description": "Look-back window in days (default 30). Ignored for mrr_history.",
                "default": 30,
            },
        },
        "required": ["app_id", "metric"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="app",
    cost_cents=0,
)
async def hub_api_metrics(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    app_id: str = args["app_id"]
    metric: str = args["metric"]
    days: int = int(args.get("days", 30))

    if metric not in _METRIC_PATHS:
        raise ToolError(
            f"Unknown metric '{metric}'. Valid: {', '.join(_METRIC_PATHS)}"
        )

    path = _METRIC_PATHS[metric]
    params: dict[str, Any] = {"app_id": app_id}
    if metric not in _METRIC_NO_DAYS:
        params["days"] = days

    data = await _get(path, params)
    return ToolResult(data=data)


# ---------------------------------------------------------------------------
# hub.api.campaigns
# ---------------------------------------------------------------------------

@register_tool(
    "hub.api.campaigns",
    description=(
        "Manage ad campaigns via Hub. "
        "action=list returns all campaigns; "
        "action=pause pauses a campaign; "
        "action=enable enables a campaign; "
        "action=set_budget updates the daily budget."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "pause", "enable", "set_budget"],
                "description": "Campaign operation to perform.",
            },
            "campaign_id": {
                "type": "integer",
                "description": "Campaign ID (required for pause, enable, set_budget).",
            },
            "daily_budget": {
                "type": "number",
                "description": "New daily budget amount (required for set_budget).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="app",
    cost_cents=1,
)
async def hub_api_campaigns(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    action: str = args["action"]

    if action == "list":
        data = await _get("/campaigns", {})
        return ToolResult(data=data if isinstance(data, dict) else {"items": data})

    # Mutations require campaign_id
    campaign_id = args.get("campaign_id")
    if campaign_id is None:
        raise ToolError(f"campaign_id is required for action='{action}'")
    campaign_id = int(campaign_id)

    if action in ("pause", "enable"):
        status = "PAUSED" if action == "pause" else "ENABLED"
        data = await _put(f"/campaigns/{campaign_id}/status", {"status": status})
        return ToolResult(data=data if isinstance(data, dict) else {"result": data})

    if action == "set_budget":
        daily_budget = args.get("daily_budget")
        if daily_budget is None:
            raise ToolError("daily_budget is required for action='set_budget'")
        data = await _put(
            f"/campaigns/{campaign_id}/budget",
            {"daily_budget": float(daily_budget)},
        )
        return ToolResult(data=data if isinstance(data, dict) else {"result": data})

    raise ToolError(f"Unknown action '{action}'")


# ---------------------------------------------------------------------------
# hub.api.conversion — convenience alias for hub.api.metrics(metric=conversion)
# ---------------------------------------------------------------------------

@register_tool(
    "hub.api.conversion",
    description=(
        "Fetch conversion funnel metrics for an app from Hub. "
        "Shortcut for hub.api.metrics with metric=conversion."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_id": {
                "type": "string",
                "description": "The app identifier in Hub.",
            },
            "days": {
                "type": "integer",
                "description": "Look-back window in days (default 30).",
                "default": 30,
            },
        },
        "required": ["app_id"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="app",
    cost_cents=0,
)
async def hub_api_conversion(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    app_id: str = args["app_id"]
    days: int = int(args.get("days", 30))
    data = await _get("/metrics/conversion", {"app_id": app_id, "days": days})
    return ToolResult(data=data)


# ---------------------------------------------------------------------------
# hub.api.countries — convenience alias for hub.api.metrics(metric=countries)
# ---------------------------------------------------------------------------

@register_tool(
    "hub.api.countries",
    description=(
        "Fetch country-level metrics for an app from Hub. "
        "Shortcut for hub.api.metrics with metric=countries."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_id": {
                "type": "string",
                "description": "The app identifier in Hub.",
            },
            "days": {
                "type": "integer",
                "description": "Look-back window in days (default 30).",
                "default": 30,
            },
        },
        "required": ["app_id"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="app",
    cost_cents=0,
)
async def hub_api_countries(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    app_id: str = args["app_id"]
    days: int = int(args.get("days", 30))
    data = await _get("/metrics/countries", {"app_id": app_id, "days": days})
    return ToolResult(data=data)
