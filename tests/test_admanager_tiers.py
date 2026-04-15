"""ADMANAGER budget-tier classification — OpenClaw ADMANAGER.md 27–30."""
from __future__ import annotations

from studioos.workflows.amz_admanager import (
    BUDGET_TIERS,
    classify_budget_tier,
)


def test_high_tier_needs_both_volume_and_rating() -> None:
    assert classify_budget_tier(250, 4.5) == "high"
    assert classify_budget_tier(500, 4.1) == "high"


def test_high_rejects_low_rating() -> None:
    # High volume but rating dropped below 4.0 — drops to low (spec
    # only grants "high" when *both* volume and rating clear the bar).
    assert classify_budget_tier(500, 3.9) == "low"


def test_medium_tier_mid_volume() -> None:
    assert classify_budget_tier(100, 4.0) == "medium"
    assert classify_budget_tier(200, 3.6) == "medium"


def test_none_tier_low_volume() -> None:
    assert classify_budget_tier(30, 4.5) == "none"
    assert classify_budget_tier(49, 5.0) == "none"


def test_low_tier_fallthrough() -> None:
    # Between 50 and 200 but rating too low for medium
    assert classify_budget_tier(100, 3.4) == "low"


def test_all_tiers_have_budget_config() -> None:
    for t in ("high", "medium", "low", "none"):
        assert t in BUDGET_TIERS
    assert BUDGET_TIERS["none"]["daily_budget_usd"] == 0.0
    assert BUDGET_TIERS["high"]["daily_budget_usd"] > BUDGET_TIERS["medium"]["daily_budget_usd"]
