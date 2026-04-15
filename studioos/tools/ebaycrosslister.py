"""EbayCrossLister adapters — read-only DB access for the amz-crosslister agent.

Phase 1: surface inventory items that are listable on eBay (Amazon
inventory present, not yet listed on eBay, FBA fulfillable). Real
eBay listing creation lives in the EbayCrossLister service and is
gated behind a future approval-driven write tool.
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
    if not settings.ebaycrosslister_db_url:
        raise ToolError("STUDIOOS_EBAYCROSSLISTER_DB_URL is not configured")
    key = id(asyncio.get_event_loop())
    eng = _engines.get(key)
    if eng is None:
        from sqlalchemy.pool import NullPool

        eng = create_async_engine(
            settings.ebaycrosslister_db_url,
            poolclass=NullPool,
            pool_pre_ping=True,
        )
        _engines[key] = eng
    return eng


_LISTABLE_SQL = text(
    """
    SELECT
        a.id,
        a.asin,
        a.sku,
        a.title,
        a.amazon_price,
        a.fulfillable_quantity,
        a.fulfillment_channel,
        a.condition,
        a.is_listed_on_ebay,
        a.is_stranded,
        a.listing_status,
        a.last_synced_at
    FROM amazon_inventory_items a
    WHERE a.is_listed_on_ebay = false
      AND a.is_stranded = false
      AND a.fulfillable_quantity > 0
      AND a.amazon_price IS NOT NULL
    ORDER BY a.fulfillable_quantity DESC, a.amazon_price DESC
    LIMIT :lim
    """
)


@register_tool(
    "ebaycrosslister.db.listable_items",
    description=(
        "Return Amazon inventory items that are not yet listed on eBay "
        "but have FBA stock and a known Amazon price. Read-only."
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
async def ebaycrosslister_db_listable_items(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    limit = int(args.get("limit", 30))
    eng = _engine()
    async with eng.connect() as conn:
        result = await conn.execute(_LISTABLE_SQL, {"lim": limit})
        rows = [dict(r) for r in result.mappings()]

    def _f(v: Any) -> float | None:
        return float(v) if v is not None else None

    items = [
        {
            "inventory_id": r["id"],
            "asin": r["asin"],
            "sku": r["sku"],
            "title": (r.get("title") or "")[:120],
            "amazon_price": _f(r.get("amazon_price")),
            "fulfillable_quantity": r.get("fulfillable_quantity"),
            "fulfillment_channel": r.get("fulfillment_channel"),
            "condition": r.get("condition"),
            "listing_status": r.get("listing_status"),
            "last_synced_at": (
                r["last_synced_at"].isoformat()
                if r.get("last_synced_at")
                else None
            ),
        }
        for r in rows
    ]
    return ToolResult(data={"items": items, "count": len(items)})
