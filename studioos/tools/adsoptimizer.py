"""AdsOptimizer adapters — read-only DB access for the amz-admanager agent.

Phase 1 surfaces campaign + KPI data so the agent can reason about
which campaigns to flag for pause / spend-up. Real Amazon Ads API
mutations live in AdsOptimizer's own engine and are reachable via a
future approval-gated tool.
"""
from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from studioos.config import settings

from .base import ToolContext, ToolError, ToolResult
from .registry import register_tool

_engines: dict[int, AsyncEngine] = {}


def _engine() -> AsyncEngine:
    if not settings.adsoptimizer_db_url:
        raise ToolError("STUDIOOS_ADSOPTIMIZER_DB_URL is not configured")
    key = id(asyncio.get_event_loop())
    eng = _engines.get(key)
    if eng is None:
        from sqlalchemy.pool import NullPool

        eng = create_async_engine(
            settings.adsoptimizer_db_url,
            poolclass=NullPool,
            pool_pre_ping=True,
        )
        _engines[key] = eng
    return eng


_CAMPAIGN_SQL = text(
    """
    SELECT
        c.id,
        c.name,
        c.campaign_type,
        c.targeting_type,
        c.state,
        c.daily_budget,
        c.bidding_strategy,
        c.target_acos,
        c.start_date,
        c.end_date
    FROM campaigns c
    WHERE (:state IS NULL OR c.state = :state)
    ORDER BY c.daily_budget DESC NULLS LAST
    LIMIT :lim
    """
)


@register_tool(
    "adsoptimizer.db.list_campaigns",
    description=(
        "Return AdsOptimizer campaigns ordered by daily_budget desc. "
        "Filter by state ('enabled'|'paused'|'archived') optional. "
        "Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer"},
            "state": {"type": "string"},
        },
        "additionalProperties": False,
    },
    requires_network=True,
    category="amz",
    cost_cents=0,
)
async def adsoptimizer_db_list_campaigns(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    params = {
        "lim": int(args.get("limit", 25)),
        "state": args.get("state"),
    }
    eng = _engine()
    async with eng.connect() as conn:
        result = await conn.execute(_CAMPAIGN_SQL, params)
        rows = [dict(r) for r in result.mappings()]

    def _f(v: Any) -> float | None:
        return float(v) if v is not None else None

    items = [
        {
            "campaign_id": r["id"],
            "name": (r.get("name") or "")[:120],
            "type": r.get("campaign_type"),
            "targeting": r.get("targeting_type"),
            "state": r.get("state"),
            "daily_budget": _f(r.get("daily_budget")),
            "bidding_strategy": r.get("bidding_strategy"),
            "target_acos": _f(r.get("target_acos")),
            "start_date": (
                r["start_date"].isoformat() if r.get("start_date") else None
            ),
            "end_date": (
                r["end_date"].isoformat() if r.get("end_date") else None
            ),
        }
        for r in rows
    ]
    return ToolResult(data={"items": items, "count": len(items)})
