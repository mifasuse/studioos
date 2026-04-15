"""AMZ tool adapters — wrappers over existing PriceFinder/BuyBoxPricer HTTP APIs.

PriceFinder runs on the same host behind traefik at
`https://pricefinder.mifasuse.com/api/v1`. It uses OAuth2 password-flow
tokens; we hold a single shared token per process and refresh on 401.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

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
