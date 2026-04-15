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
        l.listing_status,
        l.created_at,
        EXTRACT(EPOCH FROM (NOW() - l.created_at)) / 86400.0 AS age_days,
        (
            SELECT count(*)
            FROM competitors c
            WHERE c.listing_id = l.id
        ) AS competitor_count
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


_INVENTORY_AGING_SQL = text(
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
        l.has_buybox,
        l.quantity,
        l.last_repriced_at,
        l.created_at,
        EXTRACT(EPOCH FROM (NOW() - l.created_at)) / 86400.0 AS age_days
    FROM listings l
    WHERE l.is_repricing_enabled = true
      AND l.listing_status IN ('Active', 'active')
      AND l.quantity > 0
      AND l.created_at IS NOT NULL
      AND EXTRACT(EPOCH FROM (NOW() - l.created_at)) / 86400.0 >= :min_age_days
    ORDER BY l.created_at ASC
    LIMIT :lim
    """
)


@register_tool(
    "buyboxpricer.db.aging_inventory",
    description=(
        "Return active repricing-enabled listings older than min_age_days. "
        "Used by the pricer's stok eritme strategy."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer"},
            "min_age_days": {"type": "integer"},
        },
        "additionalProperties": False,
    },
    requires_network=True,
    category="amz",
    cost_cents=0,
)
async def buyboxpricer_db_aging_inventory(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    eng = _engine()
    async with eng.connect() as conn:
        result = await conn.execute(
            _INVENTORY_AGING_SQL,
            {
                "lim": int(args.get("limit", 25)),
                "min_age_days": int(args.get("min_age_days", 90)),
            },
        )
        rows = [dict(r) for r in result.mappings()]

    def _f(v: Any) -> float | None:
        return float(v) if v is not None else None

    items = []
    for r in rows:
        items.append(
            {
                "listing_id": r["id"],
                "asin": r["asin"],
                "sku": r["sku"],
                "title": (r.get("title") or "")[:120],
                "acquisition_cost": _f(r.get("acquisition_cost")),
                "min_price": _f(r.get("min_price")),
                "max_price": _f(r.get("max_price")),
                "current_price": _f(r.get("current_price")),
                "buy_box_price": _f(r.get("buy_box_price")),
                "has_buybox": r.get("has_buybox"),
                "quantity": r.get("quantity"),
                "age_days": _f(r.get("age_days")),
                "last_repriced_at": (
                    r["last_repriced_at"].isoformat()
                    if r.get("last_repriced_at")
                    else None
                ),
            }
        )
    return ToolResult(data={"items": items, "count": len(items)})


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
                "age_days": _f(r.get("age_days")),
                "competitor_count": r.get("competitor_count"),
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


_rw_engines: dict[int, AsyncEngine] = {}


def _rw_engine() -> AsyncEngine:
    if not settings.buyboxpricer_db_rw_url:
        raise ToolError("STUDIOOS_BUYBOXPRICER_DB_RW_URL is not configured")
    key = id(asyncio.get_event_loop())
    eng = _rw_engines.get(key)
    if eng is None:
        from sqlalchemy.pool import NullPool

        eng = create_async_engine(
            settings.buyboxpricer_db_rw_url,
            poolclass=NullPool,
            pool_pre_ping=True,
        )
        _rw_engines[key] = eng
    return eng


_NULL_COST_LISTINGS_SQL = text(
    """
    SELECT id, asin, sku
    FROM listings
    WHERE is_repricing_enabled = true
      AND listing_status IN ('Active', 'active')
      AND acquisition_cost IS NULL
      AND quantity > 0
    LIMIT :lim
    """
)


_PF_COST_LOOKUP_SQL = text(
    """
    SELECT
        p.asin,
        p.tr_price,
        p.package_weight_g,
        o.source_price,
        o.shipping_cost AS opp_shipping,
        o.customs_cost AS opp_customs
    FROM products p
    LEFT JOIN LATERAL (
        SELECT *
        FROM arbitrage_opportunities ao
        WHERE ao.product_id = p.id
          AND ao.status = 'active'
        ORDER BY ao.found_at DESC NULLS LAST
        LIMIT 1
    ) o ON TRUE
    WHERE p.asin = ANY(:asins)
    """
)


_PF_GLOBAL_SETTINGS_SQL = text(
    """
    SELECT key, value FROM global_settings
    WHERE key IN ('exchange_rate', 'shipping_cost', 'customs_rate', 'shipping_rate_per_kg')
    """
)


_UPDATE_COST_SQL = text(
    """
    UPDATE listings
    SET acquisition_cost = :cost
    WHERE id = :listing_id
    """
)


@register_tool(
    "buyboxpricer.db.backfill_acquisition_cost",
    description=(
        "Backfill listings.acquisition_cost on BuyBoxPricer for any "
        "active repricing-enabled listing whose acquisition_cost is "
        "currently NULL, using PriceFinder's arbitrage_opportunities "
        "source_price (TR cost in USD) when available. Direct DB write "
        "via studioos_rw, column-scoped to acquisition_cost."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer"},
            "dry_run": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    requires_network=True,
    category="amz",
    cost_cents=0,
)
async def buyboxpricer_db_backfill_acquisition_cost(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    limit = int(args.get("limit", 200))
    dry_run = bool(args.get("dry_run", False))

    # 1. Read NULL-cost listings from BBP.
    bbp = _engine()
    async with bbp.connect() as conn:
        result = await conn.execute(_NULL_COST_LISTINGS_SQL, {"lim": limit})
        listings = [dict(r) for r in result.mappings()]
    if not listings:
        return ToolResult(
            data={"updated": 0, "skipped": 0, "reason": "no null-cost rows"}
        )

    asins = [l["asin"] for l in listings if l.get("asin")]
    if not asins:
        return ToolResult(data={"updated": 0, "skipped": len(listings)})

    # 2. Look up source_price (USD) in PriceFinder + global settings.
    from studioos.tools.amz import _pf_engine

    pf = _pf_engine()
    async with pf.connect() as conn:
        result = await conn.execute(_PF_COST_LOOKUP_SQL, {"asins": asins})
        cost_rows = {r["asin"]: dict(r) for r in result.mappings()}
        gs_result = await conn.execute(_PF_GLOBAL_SETTINGS_SQL)
        global_settings = {r["key"]: r["value"] for r in gs_result.mappings()}

    def _gs_float(key: str, default: float) -> float:
        try:
            return float(global_settings.get(key, default))
        except (TypeError, ValueError):
            return default

    exchange_rate = _gs_float("exchange_rate", 30.0)
    shipping_cost = _gs_float("shipping_cost", 6.0)
    customs_rate = _gs_float("customs_rate", 0.4)
    shipping_per_kg = _gs_float("shipping_rate_per_kg", 6.0)

    # 3. Compute and update.
    rw = _rw_engine()
    updates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for listing in listings:
        asin = listing["asin"]
        cost_data = cost_rows.get(asin)
        if not cost_data:
            skipped.append({"asin": asin, "reason": "no pricefinder row"})
            continue

        # Preferred: opportunity calculator's source_price (already USD
        # with shipping + customs baked in).
        cost: float | None = None
        source = cost_data.get("source_price")
        if source is not None:
            try:
                v = float(source)
                if v > 0:
                    cost = round(v, 2)
            except (TypeError, ValueError):
                pass

        # Fallback: derive from products.tr_price using global_settings.
        if cost is None:
            tr_price = cost_data.get("tr_price")
            if tr_price is None:
                skipped.append({"asin": asin, "reason": "no source_price + no tr_price"})
                continue
            try:
                tr_price_usd = float(tr_price) / exchange_rate
            except (TypeError, ValueError, ZeroDivisionError):
                skipped.append({"asin": asin, "reason": "bad tr_price"})
                continue

            weight_g = cost_data.get("package_weight_g") or 0
            try:
                shipping = max(
                    shipping_cost,
                    (float(weight_g) / 1000.0) * shipping_per_kg,
                )
            except (TypeError, ValueError):
                shipping = shipping_cost
            customs = tr_price_usd * customs_rate
            cost = round(tr_price_usd + shipping + customs, 2)

        if cost <= 0:
            skipped.append({"asin": asin, "reason": "non-positive cost"})
            continue
        updates.append(
            {
                "listing_id": listing["id"],
                "asin": asin,
                "sku": listing.get("sku"),
                "cost": cost,
            }
        )

    if not dry_run and updates:
        async with rw.begin() as conn:
            for u in updates:
                await conn.execute(
                    _UPDATE_COST_SQL,
                    {"cost": u["cost"], "listing_id": u["listing_id"]},
                )

    log.info(
        "buyboxpricer.backfill",
        candidates=len(listings),
        updated=0 if dry_run else len(updates),
        skipped=len(skipped),
        dry_run=dry_run,
    )

    return ToolResult(
        data={
            "candidates": len(listings),
            "updated": 0 if dry_run else len(updates),
            "would_update": len(updates),
            "skipped": len(skipped),
            "dry_run": dry_run,
            "sample_updates": updates[:5],
            "sample_skipped": skipped[:5],
        }
    )


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
