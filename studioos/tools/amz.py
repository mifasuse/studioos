"""AMZ tool adapters — wrappers over existing PriceFinder tool service.

Two integration paths coexist:

1. HTTP API (`pricefinder.lookup_asin`) — OAuth2-authenticated, hits
   `https://pricefinder.mifasuse.com/api/v1`. Use when you need
   business-logic output (profit, ROI, exchange rate) or write access
   in the future.

2. Direct read-only DB (`pricefinder.db.lookup_asins`) — batch-friendly,
   ~10ms per call, no auth round-trip. Read-only postgres role
   `studioos_ro` with SELECT on a narrow table set. Use for high-volume
   monitor loops and analyst scans.
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


class _PriceFinderClient:
    """Minimal async client with lazy OAuth2 token caching.

    One cached token per event loop so pytest-asyncio fixtures don't share
    a token bound to a dead loop. Token is re-fetched on 401.
    """

    _TOKEN_TTL_SECONDS = 60 * 60  # pricefinder default is 24h, we refresh hourly

    def __init__(self) -> None:
        self._tokens: dict[int, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    def _base(self) -> str:
        base = (settings.pricefinder_url or "").rstrip("/")
        if not base:
            raise ToolError("STUDIOOS_PRICEFINDER_URL is not configured")
        return base

    async def _fetch_token(self, client: httpx.AsyncClient) -> str:
        if not settings.pricefinder_username or not settings.pricefinder_password:
            raise ToolError(
                "STUDIOOS_PRICEFINDER_USERNAME/PASSWORD are not configured"
            )
        resp = await client.post(
            f"{self._base()}/auth/token",
            data={
                "username": settings.pricefinder_username,
                "password": settings.pricefinder_password,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise ToolError(
                f"pricefinder login failed: {resp.status_code} {resp.text[:200]}"
            )
        token = resp.json().get("access_token")
        if not token:
            raise ToolError("pricefinder login response missing access_token")
        return token

    async def _token(self, client: httpx.AsyncClient, *, force: bool = False) -> str:
        key = id(asyncio.get_event_loop())
        now = time.monotonic()
        async with self._lock:
            cached = self._tokens.get(key)
            if not force and cached and (now - cached[1]) < self._TOKEN_TTL_SECONDS:
                return cached[0]
            token = await self._fetch_token(client)
            self._tokens[key] = (token, now)
            return token

    async def search_by_asin(self, asin: str) -> dict[str, Any] | None:
        async with httpx.AsyncClient(
            timeout=settings.pricefinder_timeout_seconds
        ) as client:
            token = await self._token(client)
            url = f"{self._base()}/products/"
            params = {"search": asin, "page_size": 1}
            resp = await client.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code == 401:
                token = await self._token(client, force=True)
                resp = await client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code >= 400:
                raise ToolError(
                    f"pricefinder {resp.status_code}: {resp.text[:200]}"
                )
            try:
                body = resp.json()
            except ValueError as exc:
                raise ToolError(f"pricefinder non-json: {exc}") from exc

        items = body.get("items") or []
        if not items:
            return None
        # search is substring — return the exact-asin match if present
        for item in items:
            if (item.get("asin") or "").upper() == asin.upper():
                return item
        return items[0]


_client_singleton: _PriceFinderClient | None = None


def _client() -> _PriceFinderClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = _PriceFinderClient()
    return _client_singleton


def _pick_price(us_data: dict[str, Any] | None) -> float | None:
    """Pull the most authoritative US price out of us_market_data.

    Preference order (most authoritative first):
        buybox_price → lowest_price → new_3p_price → amazon_price
    """
    if not us_data:
        return None
    for key in (
        "buybox_price",
        "lowest_price",
        "new_3p_price",
        "amazon_price",
    ):
        val = us_data.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


# ---------------------------------------------------------------------------
# Direct read-only DB path — batch lookups for monitor/analyst loops
# ---------------------------------------------------------------------------

_pf_engines: dict[int, AsyncEngine] = {}


def _pf_engine() -> AsyncEngine:
    """Return a per-event-loop asyncpg engine for pricefinder RO."""
    if not settings.pricefinder_db_url:
        raise ToolError("STUDIOOS_PRICEFINDER_DB_URL is not configured")
    key = id(asyncio.get_event_loop())
    engine = _pf_engines.get(key)
    if engine is None:
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(
            settings.pricefinder_db_url,
            poolclass=NullPool,
            pool_pre_ping=True,
        )
        _pf_engines[key] = engine
    return engine


_BATCH_SQL = text(
    """
    SELECT
        p.id,
        p.asin,
        p.title,
        p.brand,
        p.tr_price,
        u.buybox_price,
        u.lowest_price,
        u.new_3p_price,
        u.amazon_price,
        u.fba_offer_count,
        u.new_offer_count,
        u.used_offer_count,
        u.sales_rank,
        u.monthly_sold,
        u.rating,
        u.review_count,
        u.fba_fee,
        u.referral_fee,
        u.buybox_is_fba,
        u.ebay_new_price,
        p.package_weight_g,
        u.last_update,
        -- Latest active opportunity row (scalar subquery — 1 per product)
        o.id                      AS opportunity_id,
        o.source_price            AS opp_source_price,
        o.target_price            AS opp_target_price,
        o.estimated_profit        AS opp_estimated_profit,
        o.profit_margin_percent   AS opp_profit_margin_percent,
        o.roi_percent             AS opp_roi_percent,
        o.monthly_sold            AS opp_monthly_sold,
        o.competition_level       AS opp_competition_level
    FROM products p
    LEFT JOIN us_market_data u ON u.product_id = p.id
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


def _pick_db_price(row: dict[str, Any]) -> tuple[float | None, str | None]:
    for key in ("buybox_price", "lowest_price", "new_3p_price", "amazon_price"):
        val = row.get(key)
        if val is not None:
            try:
                return float(val), key.replace("_price", "")
            except (TypeError, ValueError):
                continue
    return None, None


@register_tool(
    "pricefinder.db.lookup_asins",
    description=(
        "Batch-read current US prices for a list of ASINs straight from "
        "the PriceFinder read-only replica. Single round-trip, no auth, "
        "use for monitor/analyst scans."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "asins": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "required": ["asins"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="amz",
    cost_cents=0,
)
async def pricefinder_db_lookup_asins(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    asins = [a.strip().upper() for a in args["asins"] if a and a.strip()]
    if not asins:
        return ToolResult(data={"items": [], "found": 0, "missing": []})
    engine = _pf_engine()
    async with engine.connect() as conn:
        result = await conn.execute(_BATCH_SQL, {"asins": asins})
        rows = [dict(r) for r in result.mappings()]

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        price, source = _pick_db_price(row)
        if price is None:
            continue
        asin = row["asin"]
        seen.add(asin)
        def _f(key: str) -> float | None:
            v = row.get(key)
            return float(v) if v is not None else None

        items.append(
            {
                "asin": asin,
                "product_id": row["id"],
                "title": (row.get("title") or "")[:120],
                "brand": row.get("brand"),
                "price": price,
                "currency": "USD",
                "price_source": source,
                "fba_offer_count": row.get("fba_offer_count"),
                "new_offer_count": row.get("new_offer_count"),
                "used_offer_count": row.get("used_offer_count"),
                "sales_rank": row.get("sales_rank"),
                "monthly_sold": row.get("monthly_sold"),
                "rating": _f("rating"),
                "review_count": row.get("review_count"),
                "buybox_price": _f("buybox_price"),
                "fba_fee": _f("fba_fee"),
                "referral_fee": _f("referral_fee"),
                "buybox_is_fba": row.get("buybox_is_fba"),
                "ebay_new_price": _f("ebay_new_price"),
                "package_weight_g": row.get("package_weight_g"),
                "last_update": (
                    row["last_update"].isoformat()
                    if row.get("last_update")
                    else None
                ),
                # Arbitrage signals (from the latest active opportunity row,
                # or null if PriceFinder hasn't flagged this ASIN as an
                # opportunity yet)
                "opportunity_id": row.get("opportunity_id"),
                "opp_source_price_try": _f("opp_source_price"),
                "opp_target_price_usd": _f("opp_target_price"),
                "estimated_profit_usd": _f("opp_estimated_profit"),
                "profit_margin_pct": _f("opp_profit_margin_percent"),
                "roi_pct": _f("opp_roi_percent"),
                "opp_monthly_sold": row.get("opp_monthly_sold"),
                "competition_level": row.get("opp_competition_level"),
                "tr_price": (
                    float(row["tr_price"]) if row.get("tr_price") else None
                ),
            }
        )
    missing = [a for a in asins if a not in seen]
    return ToolResult(
        data={
            "items": items,
            "found": len(items),
            "missing": missing,
        }
    )


_PF_GLOBAL_SETTINGS_SQL = text(
    """
    SELECT key, value FROM global_settings
    WHERE key IN ('exchange_rate', 'shipping_cost', 'customs_rate', 'shipping_rate_per_kg')
    """
)


@register_tool(
    "pricefinder.db.global_settings",
    description=(
        "Fetch PriceFinder global_settings (exchange_rate, shipping_cost, "
        "customs_rate, shipping_rate_per_kg) as a single dict. Used by "
        "analyst/pricer for deterministic profit math."
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    requires_network=True,
    category="amz",
    cost_cents=0,
)
async def pricefinder_db_global_settings(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    engine = _pf_engine()
    async with engine.connect() as conn:
        result = await conn.execute(_PF_GLOBAL_SETTINGS_SQL)
        rows = {r["key"]: r["value"] for r in result.mappings()}

    def _f(key: str, default: float) -> float:
        try:
            return float(rows.get(key, default))
        except (TypeError, ValueError):
            return default

    return ToolResult(
        data={
            "exchange_rate": _f("exchange_rate", 30.0),
            "shipping_cost": _f("shipping_cost", 6.0),
            "customs_rate": _f("customs_rate", 0.4),
            "shipping_rate_per_kg": _f("shipping_rate_per_kg", 6.0),
        }
    )


_TOP_OPPS_SQL = text(
    """
    SELECT
        o.id AS opportunity_id,
        p.asin,
        p.title,
        p.brand,
        o.source_price,
        o.target_price,
        o.estimated_profit,
        o.profit_margin_percent,
        o.roi_percent,
        o.monthly_sold,
        o.competition_level,
        o.found_at
    FROM arbitrage_opportunities o
    JOIN products p ON p.id = o.product_id
    WHERE o.status = 'active'
      AND p.is_active = true
      AND p.asin IS NOT NULL
      AND o.estimated_profit >= :min_profit
      AND COALESCE(o.profit_margin_percent, 0) >= :min_margin
    ORDER BY o.estimated_profit DESC NULLS LAST
    LIMIT :lim
    """
)


@register_tool(
    "pricefinder.db.top_opportunities",
    description=(
        "Return the top-N active arbitrage opportunities from PriceFinder's "
        "replica, filtered by minimum estimated profit and margin. "
        "Used by the monitor/analyst to build a dynamic watchlist."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer"},
            "min_profit_dollars": {"type": "number"},
            "min_margin_pct": {"type": "number"},
        },
        "additionalProperties": False,
    },
    requires_network=True,
    category="amz",
    cost_cents=0,
)
async def pricefinder_db_top_opportunities(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    limit = int(args.get("limit", 20))
    min_profit = float(args.get("min_profit_dollars", 10.0))
    min_margin = float(args.get("min_margin_pct", 30.0))
    engine = _pf_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            _TOP_OPPS_SQL,
            {"lim": limit, "min_profit": min_profit, "min_margin": min_margin},
        )
        rows = [dict(r) for r in result.mappings()]

    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "opportunity_id": row["opportunity_id"],
                "asin": row["asin"],
                "title": (row.get("title") or "")[:120],
                "brand": row.get("brand"),
                "source_price": float(row["source_price"])
                if row.get("source_price") is not None
                else None,
                "target_price": float(row["target_price"])
                if row.get("target_price") is not None
                else None,
                "estimated_profit": float(row["estimated_profit"])
                if row.get("estimated_profit") is not None
                else None,
                "profit_margin_percent": float(row["profit_margin_percent"])
                if row.get("profit_margin_percent") is not None
                else None,
                "roi_percent": float(row["roi_percent"])
                if row.get("roi_percent") is not None
                else None,
                "monthly_sold": row.get("monthly_sold"),
                "competition_level": row.get("competition_level"),
                "found_at": row["found_at"].isoformat()
                if row.get("found_at")
                else None,
            }
        )
    return ToolResult(
        data={
            "items": items,
            "count": len(items),
            "filters": {
                "limit": limit,
                "min_profit_dollars": min_profit,
                "min_margin_pct": min_margin,
            },
        }
    )


_SCOUT_SQL = text(
    """
    SELECT
        p.asin,
        p.title,
        p.brand,
        p.tr_price,
        p.tr_source,
        p.package_weight_g,
        u.buybox_price,
        u.lowest_price,
        u.fba_lowest_price,
        u.sales_rank,
        u.monthly_sold,
        u.review_count,
        u.rating,
        u.fba_offer_count,
        u.new_offer_count,
        u.ebay_new_price,
        o.id   AS opportunity_id,
        o.estimated_profit,
        o.profit_margin_percent,
        o.roi_percent
    FROM products p
    JOIN us_market_data u ON u.product_id = p.id
    LEFT JOIN LATERAL (
        SELECT *
        FROM arbitrage_opportunities ao
        WHERE ao.product_id = p.id
          AND ao.status = 'active'
        ORDER BY ao.found_at DESC NULLS LAST
        LIMIT 1
    ) o ON TRUE
    WHERE p.in_stock = true
      AND p.is_active = true
      AND u.buybox_price IS NOT NULL
      AND COALESCE(u.monthly_sold, 0) >= :min_monthly_sold
      AND COALESCE(u.sales_rank, 999999999) <= :max_sales_rank
      AND COALESCE(u.rating, 0) >= :min_rating
      AND COALESCE(u.review_count, 0) >= :min_review_count
      AND COALESCE(o.roi_percent, -1) >= :min_roi_pct
      AND COALESCE(o.roi_percent, 0) <= :max_roi_pct
      AND COALESCE(o.estimated_profit, 0) >= :min_profit_dollars
      AND COALESCE(p.tr_price, 0) >= :min_tr_price
      AND NOT EXISTS (
        SELECT 1 FROM brand_blacklist bl
        WHERE LOWER(bl.brand_name) = LOWER(p.brand)
      )
    ORDER BY o.estimated_profit DESC NULLS LAST,
             o.profit_margin_percent DESC NULLS LAST
    LIMIT :lim
    """
)


@register_tool(
    "pricefinder.db.scout_candidates",
    description=(
        "Run the OpenClaw amz-scout filter against the PriceFinder "
        "replica and return the top-N candidates: ROI floor, sales "
        "rank ceiling, monthly_sold floor, rating + review minimums, "
        "in-stock, valid TR price."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer"},
            "min_roi_pct": {"type": "number"},
            "max_roi_pct": {"type": "number"},
            "max_sales_rank": {"type": "integer"},
            "min_monthly_sold": {"type": "integer"},
            "min_rating": {"type": "number"},
            "min_review_count": {"type": "integer"},
            "min_profit_dollars": {"type": "number"},
            "min_tr_price": {"type": "number"},
        },
        "additionalProperties": False,
    },
    requires_network=True,
    category="amz",
    cost_cents=0,
)
async def pricefinder_db_scout_candidates(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    params = {
        "lim": int(args.get("limit", 20)),
        "min_roi_pct": float(args.get("min_roi_pct", 20.0)),
        "max_roi_pct": float(args.get("max_roi_pct", 1000.0)),
        "max_sales_rank": int(args.get("max_sales_rank", 100_000)),
        "min_monthly_sold": int(args.get("min_monthly_sold", 30)),
        "min_rating": float(args.get("min_rating", 3.5)),
        "min_review_count": int(args.get("min_review_count", 10)),
        "min_profit_dollars": float(args.get("min_profit_dollars", 10.0)),
        "min_tr_price": float(args.get("min_tr_price", 5.0)),
    }
    engine = _pf_engine()
    async with engine.connect() as conn:
        result = await conn.execute(_SCOUT_SQL, params)
        rows = [dict(r) for r in result.mappings()]

    def _f(v: Any) -> float | None:
        return float(v) if v is not None else None

    items = [
        {
            "asin": r["asin"],
            "title": (r.get("title") or "")[:200],
            "brand": r.get("brand"),
            "tr_price": _f(r.get("tr_price")),
            "tr_source": r.get("tr_source"),
            "package_weight_g": r.get("package_weight_g"),
            "buybox_price": _f(r.get("buybox_price")),
            "fba_lowest_price": _f(r.get("fba_lowest_price")),
            "sales_rank": r.get("sales_rank"),
            "monthly_sold": r.get("monthly_sold"),
            "review_count": r.get("review_count"),
            "rating": _f(r.get("rating")),
            "fba_offer_count": r.get("fba_offer_count"),
            "new_offer_count": r.get("new_offer_count"),
            "ebay_new_price": _f(r.get("ebay_new_price")),
            "opportunity_id": r.get("opportunity_id"),
            "estimated_profit": _f(r.get("estimated_profit")),
            "profit_margin_percent": _f(r.get("profit_margin_percent")),
            "roi_percent": _f(r.get("roi_percent")),
        }
        for r in rows
    ]
    return ToolResult(
        data={
            "items": items,
            "count": len(items),
            "filters": params,
        }
    )


_CROSSLIST_SQL = text(
    """
    SELECT
        p.asin,
        p.title,
        p.brand,
        u.buybox_price,
        u.ebay_new_price,
        u.monthly_sold,
        u.fba_offer_count,
        u.sales_rank,
        u.last_update
    FROM products p
    JOIN us_market_data u ON u.product_id = p.id
    WHERE p.is_active = true
      AND u.buybox_price IS NOT NULL
      AND u.ebay_new_price IS NOT NULL
      AND u.ebay_new_price > u.buybox_price * :min_premium
      AND COALESCE(u.monthly_sold, 0) >= :min_monthly_sold
      AND COALESCE(u.fba_offer_count, 0) >= 1
    ORDER BY (u.ebay_new_price - u.buybox_price) DESC
    LIMIT :lim
    """
)


@register_tool(
    "pricefinder.db.crosslist_candidates",
    description=(
        "Find listings where eBay's new-price is meaningfully above the "
        "Amazon buy-box (eBay arbitrage candidates). Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer"},
            "min_premium": {"type": "number"},
            "min_monthly_sold": {"type": "integer"},
        },
        "additionalProperties": False,
    },
    requires_network=True,
    category="amz",
    cost_cents=0,
)
async def pricefinder_db_crosslist_candidates(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    params = {
        "lim": int(args.get("limit", 15)),
        "min_premium": float(args.get("min_premium", 1.15)),
        "min_monthly_sold": int(args.get("min_monthly_sold", 30)),
    }
    engine = _pf_engine()
    async with engine.connect() as conn:
        result = await conn.execute(_CROSSLIST_SQL, params)
        rows = [dict(r) for r in result.mappings()]

    items = []
    for r in rows:
        bb = float(r["buybox_price"])
        eb = float(r["ebay_new_price"])
        items.append(
            {
                "asin": r["asin"],
                "title": (r.get("title") or "")[:120],
                "brand": r.get("brand"),
                "amazon_buybox_usd": bb,
                "ebay_new_usd": eb,
                "premium_pct": round(((eb - bb) / bb) * 100, 2),
                "monthly_sold": r.get("monthly_sold"),
                "fba_offer_count": r.get("fba_offer_count"),
                "sales_rank": r.get("sales_rank"),
            }
        )
    return ToolResult(data={"items": items, "count": len(items)})


_AD_SQL = text(
    """
    SELECT
        p.asin,
        p.title,
        p.brand,
        u.buybox_price,
        u.monthly_sold,
        u.review_count,
        u.rating,
        u.fba_offer_count,
        u.sales_rank
    FROM products p
    JOIN us_market_data u ON u.product_id = p.id
    WHERE p.is_active = true
      AND u.buybox_price IS NOT NULL
      AND COALESCE(u.monthly_sold, 0) >= :min_monthly_sold
      AND COALESCE(u.review_count, 0) >= :min_reviews
      AND COALESCE(u.rating, 0) >= :min_rating
      AND COALESCE(u.fba_offer_count, 999) <= :max_competitors
    ORDER BY u.monthly_sold DESC NULLS LAST
    LIMIT :lim
    """
)


@register_tool(
    "pricefinder.db.ad_candidates",
    description=(
        "Find listings worth running PPC ads on: high monthly_sold, "
        "decent review count + rating, manageable competition."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer"},
            "min_monthly_sold": {"type": "integer"},
            "min_reviews": {"type": "integer"},
            "min_rating": {"type": "number"},
            "max_competitors": {"type": "integer"},
        },
        "additionalProperties": False,
    },
    requires_network=True,
    category="amz",
    cost_cents=0,
)
async def pricefinder_db_ad_candidates(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    params = {
        "lim": int(args.get("limit", 15)),
        "min_monthly_sold": int(args.get("min_monthly_sold", 50)),
        "min_reviews": int(args.get("min_reviews", 50)),
        "min_rating": float(args.get("min_rating", 4.0)),
        "max_competitors": int(args.get("max_competitors", 15)),
    }
    engine = _pf_engine()
    async with engine.connect() as conn:
        result = await conn.execute(_AD_SQL, params)
        rows = [dict(r) for r in result.mappings()]

    items = [
        {
            "asin": r["asin"],
            "title": (r.get("title") or "")[:120],
            "brand": r.get("brand"),
            "buybox_usd": float(r["buybox_price"]) if r.get("buybox_price") else None,
            "monthly_sold": r.get("monthly_sold"),
            "review_count": r.get("review_count"),
            "rating": float(r["rating"]) if r.get("rating") else None,
            "fba_offer_count": r.get("fba_offer_count"),
            "sales_rank": r.get("sales_rank"),
        }
        for r in rows
    ]
    return ToolResult(data={"items": items, "count": len(items)})


@register_tool(
    "pricefinder.lookup_asin",
    description=(
        "Look up current US buy-box (or lowest) price for an ASIN via the "
        "PriceFinder service. OAuth2-authenticated; cost charged per call."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "asin": {"type": "string"},
            "marketplace": {"type": "string"},
        },
        "required": ["asin"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="amz",
    cost_cents=1,
)
async def pricefinder_lookup_asin(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    asin = args["asin"].strip().upper()
    marketplace = args.get("marketplace", "US")
    if marketplace != "US":
        raise ToolError(
            "pricefinder.lookup_asin currently only supports marketplace=US"
        )

    item = await _client().search_by_asin(asin)
    if item is None:
        raise ToolError(f"no pricefinder product for asin {asin}")

    us = item.get("us_market_data") or {}
    price = _pick_price(us)
    if price is None:
        raise ToolError(
            f"pricefinder has no US price for {asin} "
            f"(keys={list(us.keys())[:5]})"
        )

    return ToolResult(
        data={
            "asin": asin,
            "marketplace": marketplace,
            "price": price,
            "currency": "USD",
            "price_source": (
                "buybox" if us.get("buybox_price") is not None else "lowest"
            ),
            "product_id": item.get("id"),
            "title": (item.get("title") or "")[:120],
            "brand": item.get("brand"),
            "us_profit": item.get("us_profit"),
            "us_roi": item.get("us_roi"),
            "us_margin": item.get("us_margin"),
            "last_update": us.get("last_update"),
        }
    )
