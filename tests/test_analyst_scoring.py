"""Deterministic ANALYST scoring — profit formula, risk, decision matrix.

Mirrors OpenClaw ANALYST.md spec (lines 11–45) so regressions to the
core math are caught before they land in production routing.
"""
from __future__ import annotations

from studioos.workflows.amz_analyst_scoring import (
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
    # price 1 + demand 1 + fx 3 + cat 2 + quality 1 = 8
    assert r["total"] == 8


def test_risk_high_when_thin_and_crowded() -> None:
    r = compute_risk(
        {
            "fba_offer_count": 18,
            "monthly_sold": 5,
            "rating": 3.0,
            "review_count": 4,
        }
    )
    # price 5 + demand 5 + fx 3 + cat 2 + quality 5 = 20
    assert r["total"] == 20


def test_decision_matrix() -> None:
    assert decide(8, 50.0, 100) == "GUCLU_AL"
    assert decide(12, 35.0, 40) == "AL"
    assert decide(14, 22.0, 20) == "IZLE"
    assert decide(20, 10.0, 5) == "GEC"
    # Boundary: risk 15 disqualifies from AL
    assert decide(15, 50.0, 100) == "GEC"
