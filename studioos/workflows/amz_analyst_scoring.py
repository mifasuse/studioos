"""Deterministic analyst scoring per OpenClaw ANALYST.md spec.

Three pure functions, no I/O:

- compute_profit: BuyBox − (TR_USD × 1.40 + $6 + FBA fee + Referral fee)
- compute_risk: 5 dimensions (price / demand / fx / category / quality),
  each scored 1–5; sum is total risk (5 best, 25 worst).
- decide: maps (total_risk, roi_pct, monthly_sold) → verdict string:
    GÜÇLÜ AL | AL | İZLE | GEÇ

The workflow uses these as the primary signal. The LLM is only consulted
for edge cases where fields are missing or for the rationale/action
free-text.
"""
from __future__ import annotations

from typing import Any, TypedDict


class Profit(TypedDict):
    buybox_price: float | None
    tr_price_usd: float | None
    customs_usd: float | None
    shipping_usd: float | None
    fba_fee: float | None
    referral_fee: float | None
    total_cost_usd: float | None
    net_profit_usd: float | None
    roi_pct: float | None
    margin_pct: float | None


def _num(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def compute_profit(product: dict[str, Any], settings: dict[str, float]) -> Profit:
    """Deterministic per-unit profit.

    Formula (ANALYST.md line 18):
        net = BuyBox − (TR_USD × (1 + customs_rate) + shipping + FBA + referral)

    Shipping respects package weight when present: max($6, kg × $6).
    """
    buybox = _num(product.get("buybox_price"))
    tr_try = _num(product.get("tr_price"))
    fba = _num(product.get("fba_fee")) or 0.0
    referral = _num(product.get("referral_fee")) or 0.0
    exchange_rate = settings.get("exchange_rate", 30.0) or 30.0
    customs_rate = settings.get("customs_rate", 0.4)
    ship_flat = settings.get("shipping_cost", 6.0)
    ship_per_kg = settings.get("shipping_rate_per_kg", 6.0)

    tr_usd: float | None = None
    if tr_try is not None and exchange_rate > 0:
        tr_usd = tr_try / exchange_rate

    weight_g = _num(product.get("package_weight_g")) or 0.0
    shipping = max(ship_flat, (weight_g / 1000.0) * ship_per_kg)
    customs = (tr_usd or 0.0) * customs_rate

    total_cost: float | None = None
    net: float | None = None
    roi: float | None = None
    margin: float | None = None
    if tr_usd is not None and buybox is not None:
        total_cost = tr_usd + customs + shipping + fba + referral
        net = buybox - total_cost
        if total_cost > 0:
            roi = (net / total_cost) * 100.0
        if buybox > 0:
            margin = (net / buybox) * 100.0

    return Profit(
        buybox_price=buybox,
        tr_price_usd=round(tr_usd, 2) if tr_usd is not None else None,
        customs_usd=round(customs, 2) if tr_usd is not None else None,
        shipping_usd=round(shipping, 2),
        fba_fee=fba,
        referral_fee=referral,
        total_cost_usd=round(total_cost, 2) if total_cost is not None else None,
        net_profit_usd=round(net, 2) if net is not None else None,
        roi_pct=round(roi, 1) if roi is not None else None,
        margin_pct=round(margin, 1) if margin is not None else None,
    )


class Risk(TypedDict):
    price: int
    demand: int
    fx: int
    category: int
    quality: int
    total: int


def _fx_risk_from_rate(exchange_rate: float | None) -> int:
    """Estimate FX risk from TRY/USD exchange rate level.

    Higher exchange rate = weaker TRY = more volatile historically.
    Thresholds calibrated for 2024-2026 TRY/USD range (~28-38):
      - rate < 30  → 2 (relatively stable zone)
      - rate 30-33 → 3 (moderate)
      - rate 33-36 → 4 (elevated volatility)
      - rate >= 36 → 5 (high risk, TRY weakening fast)

    If no rate is available, default to 3 (neutral).
    """
    if exchange_rate is None or exchange_rate <= 0:
        return 3
    if exchange_rate < 30:
        return 2
    if exchange_rate < 33:
        return 3
    if exchange_rate < 36:
        return 4
    return 5


def _category_risk_from_product(product: dict[str, Any]) -> int:
    """Estimate category risk from product signals.

    Uses category/product_group if available + gated category indicators.
    """
    cat = (product.get("category") or product.get("product_group") or "").lower()
    # Known restricted/gated categories on Amazon US
    gated_keywords = {
        "grocery", "food", "topical", "beauty", "health",
        "personal care", "dietary", "supplement", "pesticide",
        "alcohol", "tobacco", "weapon", "hazmat",
    }
    if any(kw in cat for kw in gated_keywords):
        return 4
    is_gated = product.get("is_gated") or product.get("gated")
    if is_gated:
        return 4
    if not cat or cat == "—":
        return 2  # unknown — slight risk
    return 1


def compute_risk(
    product: dict[str, Any],
    exchange_rate: float | None = None,
) -> Risk:
    """5-dimension risk (each 1–5, lower is better).

    ANALYST.md lines 32–39:
      - price:    fba_offer_count tiers (> 10 riskli)
      - demand:   monthly_sold tiers (< 20 riskli)
      - fx:       TRY/USD rate-based (higher rate = higher risk)
      - category: gated/restricted category detection
      - quality:  rating < 3.5 or review_count < 10
    """
    fba_offers = _num(product.get("fba_offer_count")) or 0
    if fba_offers >= 15:
        price_risk = 5
    elif fba_offers >= 10:
        price_risk = 4
    elif fba_offers >= 7:
        price_risk = 3
    elif fba_offers >= 3:
        price_risk = 2
    else:
        price_risk = 1

    monthly = _num(product.get("monthly_sold"))
    if monthly is None:
        demand_risk = 3  # unknown — neutral
    elif monthly < 10:
        demand_risk = 5
    elif monthly < 20:
        demand_risk = 4
    elif monthly < 50:
        demand_risk = 3
    elif monthly < 100:
        demand_risk = 2
    else:
        demand_risk = 1

    fx_risk = _fx_risk_from_rate(exchange_rate)

    category_risk = _category_risk_from_product(product)

    rating = _num(product.get("rating"))
    reviews = _num(product.get("review_count")) or 0
    if (rating is not None and rating < 3.5) or reviews < 10:
        quality_risk = 5
    elif (rating is not None and rating < 4.0) or reviews < 30:
        quality_risk = 3
    else:
        quality_risk = 1

    total = price_risk + demand_risk + fx_risk + category_risk + quality_risk
    return Risk(
        price=price_risk,
        demand=demand_risk,
        fx=fx_risk,
        category=category_risk,
        quality=quality_risk,
        total=total,
    )


def decide(risk_total: int, roi_pct: float | None, monthly_sold: float | None) -> str:
    """Decision matrix from ANALYST.md lines 42–45."""
    roi = roi_pct or 0.0
    sold = monthly_sold or 0.0
    if risk_total < 10 and roi > 40.0 and sold > 50:
        return "GUCLU_AL"
    if risk_total < 15 and roi > 30.0 and sold > 30:
        return "AL"
    if risk_total < 15 and roi > 20.0:
        return "IZLE"
    return "GEC"


VERDICT_TO_ANALYST = {
    "GUCLU_AL": "accept",
    "AL": "accept",
    "IZLE": "uncertain",
    "GEC": "reject",
}


def verdict_confidence(verdict: str, risk_total: int, roi_pct: float | None) -> float:
    """Rough confidence mapping for the downstream approval gate."""
    roi = roi_pct or 0.0
    if verdict == "GUCLU_AL":
        return min(1.0, 0.8 + max(0.0, roi - 40.0) / 200.0)
    if verdict == "AL":
        return 0.65
    if verdict == "IZLE":
        return 0.45
    return max(0.2, 1.0 - risk_total / 25.0)
