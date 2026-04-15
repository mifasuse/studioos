"""AMZ-specific tools — wrappers around existing Amazon tool services.

These are thin, schema-validated, cost-tracked adapters over HTTP APIs
(PriceFinder today; BuyBoxPricer, AdsOptimizer, EbayCrossLister later).

Each tool assumes the service is reachable on the studioos-net docker
network. Base URLs come from settings (STUDIOOS_* env vars).
"""
from __future__ import annotations

from typing import Any

import httpx

from studioos.config import settings

from .base import ToolContext, ToolError, ToolResult
from .registry import register_tool


@register_tool(
    "pricefinder.lookup_asin",
    description=(
        "Look up current price + buy-box + offer count for an ASIN via the "
        "PriceFinder service. Network-backed; charged to budget per call."
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
    base = (settings.pricefinder_url or "").rstrip("/")
    if not base:
        raise ToolError("STUDIOOS_PRICEFINDER_URL is not configured")
    asin = args["asin"]
    marketplace = args.get("marketplace", "US")
    url = f"{base}/lookup"
    try:
        async with httpx.AsyncClient(
            timeout=settings.pricefinder_timeout_seconds
        ) as client:
            resp = await client.get(
                url, params={"asin": asin, "marketplace": marketplace}
            )
    except httpx.HTTPError as exc:
        raise ToolError(f"pricefinder http error: {exc}") from exc
    if resp.status_code >= 400:
        raise ToolError(
            f"pricefinder {resp.status_code}: {resp.text[:200]}"
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise ToolError(f"pricefinder non-json: {exc}") from exc

    # Contract: { asin, price, currency, ... }. We pass through but normalize
    # the fields the monitor workflow relies on.
    price = body.get("price")
    if price is None:
        raise ToolError(f"pricefinder missing 'price' for {asin}")
    return ToolResult(
        data={
            "asin": asin,
            "marketplace": marketplace,
            "price": float(price),
            "currency": body.get("currency", "USD"),
            "raw": body,
        }
    )
