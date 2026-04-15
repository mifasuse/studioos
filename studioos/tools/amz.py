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
        u.sales_rank,
        u.last_update
    FROM products p
    LEFT JOIN us_market_data u ON u.product_id = p.id
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
                "sales_rank": row.get("sales_rank"),
                "last_update": (
                    row["last_update"].isoformat()
                    if row.get("last_update")
                    else None
                ),
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
