"""EbayCrossLister adapters — read-only DB access for the amz-crosslister agent.

Phase 1: surface inventory items that are listable on eBay (Amazon
inventory present, not yet listed on eBay, FBA fulfillable). Real
eBay listing creation lives in the EbayCrossLister service and is
gated behind a future approval-driven write tool.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from studioos.config import settings
from studioos.logging import get_logger

from .base import ToolContext, ToolError, ToolResult
from .registry import register_tool

log = get_logger(__name__)

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


# ---------------------------------------------------------------------------
# Write path — EbayCrossLister HTTP API
# ---------------------------------------------------------------------------


_token_cache: dict[int, tuple[str, float]] = {}
_TOKEN_TTL_SECONDS = 60 * 60


async def _ebay_token(client: httpx.AsyncClient, *, force: bool = False) -> str:
    if not settings.ebaycrosslister_username or not settings.ebaycrosslister_password:
        raise ToolError(
            "STUDIOOS_EBAYCROSSLISTER_USERNAME/PASSWORD are not configured"
        )
    key = id(asyncio.get_event_loop())
    now = time.monotonic()
    cached = _token_cache.get(key)
    if not force and cached and (now - cached[1]) < _TOKEN_TTL_SECONDS:
        return cached[0]
    base = settings.ebaycrosslister_api_url.rstrip("/")
    resp = await client.post(
        f"{base}/auth/login",
        data={
            "username": settings.ebaycrosslister_username,
            "password": settings.ebaycrosslister_password,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise ToolError(
            f"ebaycrosslister login failed: {resp.status_code} {resp.text[:200]}"
        )
    token = resp.json().get("access_token")
    if not token:
        raise ToolError("ebaycrosslister login response missing access_token")
    _token_cache[key] = (token, now)
    return token


@register_tool(
    "ebaycrosslister.api.publish_listing",
    description=(
        "Publish a draft eBay listing via POST /listings/{id}/publish. "
        "Caller must provide an existing draft listing_id. "
        "Authenticated; cost charged per call."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "listing_id": {"type": "integer"},
        },
        "required": ["listing_id"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="amz",
    cost_cents=2,
)
async def ebaycrosslister_api_publish_listing(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    listing_id = int(args["listing_id"])
    base = settings.ebaycrosslister_api_url.rstrip("/")
    url = f"{base}/listings/{listing_id}/publish"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token = await _ebay_token(client)
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code == 401:
                token = await _ebay_token(client, force=True)
                resp = await client.post(
                    url, headers={"Authorization": f"Bearer {token}"}
                )
    except httpx.HTTPError as exc:
        raise ToolError(f"ebaycrosslister http error: {exc}") from exc
    if resp.status_code >= 400:
        raise ToolError(
            f"ebaycrosslister {resp.status_code}: {resp.text[:300]}"
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise ToolError(f"ebaycrosslister non-json: {exc}") from exc
    return ToolResult(
        data={
            "listing_id": listing_id,
            "result": body,
        }
    )
