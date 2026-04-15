"""BuyBoxPricer adapters — read-only DB tools.

Mirror the pattern from studioos.tools.amz: per-event-loop asyncpg
engine, narrow SELECT helpers, no writes. Phase 1 powers the
amz-pricer agent which only emits recommendations; actual repricing
calls (BuyBoxPricer Web API) come in a later milestone with an
approval gate.
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
    if not settings.buyboxpricer_db_url:
        raise ToolError("STUDIOOS_BUYBOXPRICER_DB_URL is not configured")
    key = id(asyncio.get_event_loop())
    eng = _engines.get(key)
    if eng is None:
        from sqlalchemy.pool import NullPool

        eng = create_async_engine(
            settings.buyboxpricer_db_url,
            poolclass=NullPool,
            pool_pre_ping=True,
        )
        _engines[key] = eng
    return eng


_LOST_BUYBOX_SQL = text(
    """
    SELECT
        l.id,
        l.asin,
        l.sku,
        l.title,
        l.acquisition_cost,
        l.min_price,
        l.max_price,
        l.current_price,
        l.buy_box_price,
        l.competitive_price,
        l.fees_estimate,
        l.has_buybox,
        l.buybox_seller_name,
        l.quantity,
        l.fulfillment_channel,
        l.is_repricing_enabled,
        l.last_repriced_at,
        l.last_synced_at,
        l.has_pricing_violation,
        l.listing_status
    FROM listings l
    WHERE l.is_repricing_enabled = true
      AND l.listing_status IN ('Active', 'active')
      AND l.has_buybox = false
      AND l.buy_box_price IS NOT NULL
      AND l.current_price IS NOT NULL
      AND l.buy_box_price < l.current_price
      AND l.quantity > 0
    ORDER BY (l.current_price - l.buy_box_price) DESC
    LIMIT :lim
    """
)


@register_tool(
    "buyboxpricer.db.lost_buybox",
    description=(
        "Return active BuyBoxPricer listings that have lost the buy box "
        "and are sitting above the competitor price — sorted by gap "
        "descending. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {"limit": {"type": "integer"}},
        "additionalProperties": False,
    },
    requires_network=True,
    category="amz",
    cost_cents=0,
)
async def buyboxpricer_db_lost_buybox(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    limit = int(args.get("limit", 25))
    eng = _engine()
    async with eng.connect() as conn:
        result = await conn.execute(_LOST_BUYBOX_SQL, {"lim": limit})
        rows = [dict(r) for r in result.mappings()]

    def _f(v: Any) -> float | None:
        return float(v) if v is not None else None

    items: list[dict[str, Any]] = []
    for r in rows:
        current = _f(r.get("current_price"))
        buybox = _f(r.get("buy_box_price"))
        gap = (current - buybox) if (current and buybox) else None
        gap_pct = (
            round((gap / current) * 100, 2)
            if (gap is not None and current and current > 0)
            else None
        )
        items.append(
            {
                "listing_id": r["id"],
                "asin": r["asin"],
                "sku": r["sku"],
                "title": (r.get("title") or "")[:120],
                "acquisition_cost": _f(r.get("acquisition_cost")),
                "min_price": _f(r.get("min_price")),
                "max_price": _f(r.get("max_price")),
                "current_price": current,
                "buy_box_price": buybox,
                "competitive_price": _f(r.get("competitive_price")),
                "fees_estimate": _f(r.get("fees_estimate")),
                "buybox_seller_name": r.get("buybox_seller_name"),
                "quantity": r.get("quantity"),
                "fulfillment_channel": r.get("fulfillment_channel"),
                "gap": gap,
                "gap_pct": gap_pct,
                "last_repriced_at": (
                    r["last_repriced_at"].isoformat()
                    if r.get("last_repriced_at")
                    else None
                ),
                "last_synced_at": (
                    r["last_synced_at"].isoformat()
                    if r.get("last_synced_at")
                    else None
                ),
            }
        )

    return ToolResult(
        data={
            "items": items,
            "count": len(items),
        }
    )
