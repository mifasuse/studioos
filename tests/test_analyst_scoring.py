"""Deterministic ANALYST scoring — profit formula, risk, decision matrix.

Mirrors OpenClaw ANALYST.md spec (lines 11–45) so regressions to the
core math are caught before they land in production routing.
"""
from __future__ import annotations

from studioos.workflows.amz_analyst_scoring import (
    _category_risk_from_product,
    _fx_risk_from_rate,
    compute_profit,
    compute_risk,
    decide,
)


SETTINGS = {
    "exchange_rate": 30.0,
    "shipping_cost": 6.0,
    "customs_rate": 0.4,
    "shipping_rate_per_kg": 6.0,
}


def test_profit_formula_matches_analyst_md() -> None:
    # TR 300 TL = $10; customs 40% = $4; shipping $6; FBA $3; referral $2
    # cost = 10 + 4 + 6 + 3 + 2 = $25, buybox $50 → net $25, ROI 100%
    p = compute_profit(
        {
            "tr_price": 300.0,
            "buybox_price": 50.0,
            "fba_fee": 3.0,
            "referral_fee": 2.0,
            "package_weight_g": 0,
        },
        SETTINGS,
    )
    assert p["tr_price_usd"] == 10.0
    assert p["customs_usd"] == 4.0
    assert p["shipping_usd"] == 6.0
    assert p["total_cost_usd"] == 25.0
    assert p["net_profit_usd"] == 25.0
    assert p["roi_pct"] == 100.0
    assert p["margin_pct"] == 50.0


def test_profit_respects_weight_for_shipping() -> None:
    # 3 kg × $6 = $18 shipping (overrides flat $6)
    p = compute_profit(
        {
            "tr_price": 300.0,
            "buybox_price": 60.0,
            "fba_fee": 0,
            "referral_fee": 0,
            "package_weight_g": 3000,
        },
        SETTINGS,
    )
    assert p["shipping_usd"] == 18.0


def test_profit_null_inputs_no_crash() -> None:
    p = compute_profit({"tr_price": None, "buybox_price": None}, SETTINGS)
    assert p["net_profit_usd"] is None
    assert p["roi_pct"] is None


def test_risk_low_when_healthy() -> None:
    r = compute_risk(
        {
            "fba_offer_count": 2,
            "monthly_sold": 200,
            "rating": 4.6,
            "review_count": 500,
        }
    )
    # price 1 + demand 1 + fx 3(default) + cat 1(no category) + quality 1 = 7
    assert r["price"] == 1
    assert r["demand"] == 1
    assert r["fx"] == 3  # no exchange_rate → default 3
    assert r["quality"] == 1


def test_risk_high_when_thin_and_crowded() -> None:
    r = compute_risk(
        {
            "fba_offer_count": 18,
            "monthly_sold": 5,
            "rating": 3.0,
            "review_count": 4,
        }
    )
    # price 5 + demand 5 + fx 3 + cat 1 + quality 5 = 19
    assert r["price"] == 5
    assert r["demand"] == 5
    assert r["quality"] == 5


# ---------------------------------------------------------------------------
# FX risk scoring (rate-based)
# ---------------------------------------------------------------------------

def test_fx_risk_from_rate_thresholds() -> None:
    assert _fx_risk_from_rate(None) == 3      # no data → neutral
    assert _fx_risk_from_rate(0) == 3          # invalid → neutral
    assert _fx_risk_from_rate(28.0) == 2       # < 30 → stable
    assert _fx_risk_from_rate(30.0) == 3       # 30-33 → moderate
    assert _fx_risk_from_rate(32.9) == 3
    assert _fx_risk_from_rate(33.0) == 4       # 33-36 → elevated
    assert _fx_risk_from_rate(35.9) == 4
    assert _fx_risk_from_rate(36.0) == 5       # >= 36 → high
    assert _fx_risk_from_rate(40.0) == 5


def test_risk_with_exchange_rate() -> None:
    product = {
        "fba_offer_count": 2,
        "monthly_sold": 200,
        "rating": 4.6,
        "review_count": 500,
    }
    # Low rate → fx_risk=2
    r = compute_risk(product, exchange_rate=28.0)
    assert r["fx"] == 2
    # High rate → fx_risk=5
    r = compute_risk(product, exchange_rate=38.0)
    assert r["fx"] == 5


# ---------------------------------------------------------------------------
# Category risk scoring
# ---------------------------------------------------------------------------

def test_category_risk_gated_keywords() -> None:
    assert _category_risk_from_product({"category": "Health & Beauty"}) == 4
    assert _category_risk_from_product({"category": "Grocery & Gourmet Food"}) == 4
    assert _category_risk_from_product({"product_group": "Dietary Supplement"}) == 4


def test_category_risk_normal() -> None:
    assert _category_risk_from_product({"category": "Electronics"}) == 1
    assert _category_risk_from_product({"category": "Tools & Home Improvement"}) == 1


def test_category_risk_gated_flag() -> None:
    assert _category_risk_from_product({"is_gated": True, "category": "Electronics"}) == 4


def test_category_risk_unknown() -> None:
    assert _category_risk_from_product({}) == 2       # no category → slight risk
    assert _category_risk_from_product({"category": ""}) == 2


def test_risk_with_gated_category() -> None:
    r = compute_risk(
        {
            "fba_offer_count": 2,
            "monthly_sold": 200,
            "rating": 4.6,
            "review_count": 500,
            "category": "Health & Beauty",
        },
        exchange_rate=30.0,
    )
    assert r["category"] == 4
    assert r["fx"] == 3


def test_decision_matrix() -> None:
    assert decide(8, 50.0, 100) == "GUCLU_AL"
    assert decide(12, 35.0, 40) == "AL"
    assert decide(14, 22.0, 20) == "IZLE"
    assert decide(20, 10.0, 5) == "GEC"
    # Boundary: risk >= 15 disqualifies from AL
    assert decide(15, 50.0, 100) == "GEC"
