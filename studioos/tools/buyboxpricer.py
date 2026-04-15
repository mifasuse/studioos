"""BuyBoxPricer adapters — read-only DB tools.

Mirror the pattern from studioos.tools.amz: per-event-loop asyncpg
engine, narrow SELECT helpers, no writes. Phase 1 powers the
amz-pricer agent which only emits recommendations; actual repricing
calls (BuyBoxPricer Web API) come in a later milestone with an
approval gate.
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


# ---------------------------------------------------------------------------
# Write path — BuyBoxPricer HTTP API
# ---------------------------------------------------------------------------


_token_cache: dict[int, tuple[str, float]] = {}
_TOKEN_TTL_SECONDS = 60 * 60


async def _bbp_token(client: httpx.AsyncClient, *, force: bool = False) -> str:
    if not settings.buyboxpricer_username or not settings.buyboxpricer_password:
        raise ToolError(
            "STUDIOOS_BUYBOXPRICER_USERNAME/PASSWORD are not configured"
        )
    key = id(asyncio.get_event_loop())
    now = time.monotonic()
    cached = _token_cache.get(key)
    if not force and cached and (now - cached[1]) < _TOKEN_TTL_SECONDS:
        return cached[0]
    resp = await client.post(
        f"{settings.buyboxpricer_api_url.rstrip('/')}/auth/login",
        data={
            "username": settings.buyboxpricer_username,
            "password": settings.buyboxpricer_password,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise ToolError(
            f"buyboxpricer login failed: {resp.status_code} {resp.text[:200]}"
        )
    token = resp.json().get("access_token")
    if not token:
        raise ToolError("buyboxpricer login response missing access_token")
    _token_cache[key] = (token, now)
    return token


@register_tool(
    "buyboxpricer.api.run_single_repricing",
    description=(
        "Trigger BuyBoxPricer's own repricing engine for a single listing "
        "via POST /repricing/run-single/{id}. The engine consults the "
        "listing's repricing_profile and competitor data and may push a "
        "new price to Amazon. Authenticated; cost charged per call."
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
async def buyboxpricer_api_run_single(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    listing_id = int(args["listing_id"])
    base = settings.buyboxpricer_api_url.rstrip("/")
    url = f"{base}/repricing/run-single/{listing_id}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token = await _bbp_token(client)
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code == 401:
                token = await _bbp_token(client, force=True)
                resp = await client.post(
                    url, headers={"Authorization": f"Bearer {token}"}
                )
    except httpx.HTTPError as exc:
        raise ToolError(f"buyboxpricer http error: {exc}") from exc
    if resp.status_code >= 400:
        raise ToolError(
            f"buyboxpricer {resp.status_code}: {resp.text[:300]}"
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise ToolError(f"buyboxpricer non-json: {exc}") from exc
    return ToolResult(
        data={
            "listing_id": listing_id,
            "asin": body.get("asin"),
            "sku": body.get("sku"),
            "result": body.get("result"),
        }
    )
