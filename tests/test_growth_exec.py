from studioos.workflows.app_studio_growth_exec import classify_lane


def test_fast_lane_reversible_low_impact() -> None:
    assert classify_lane({"reversible": True, "days_to_implement": 0.5, "user_impact_pct": 10, "is_pricing": False, "is_paywall": False}) == "fast"


def test_ceo_lane_pricing_change() -> None:
    assert classify_lane({"reversible": True, "days_to_implement": 0.5, "user_impact_pct": 10, "is_pricing": True, "is_paywall": False}) == "ceo"


def test_ceo_lane_high_impact() -> None:
    assert classify_lane({"reversible": True, "days_to_implement": 0.5, "user_impact_pct": 30, "is_pricing": False, "is_paywall": False}) == "ceo"


def test_ceo_lane_paywall_change() -> None:
    assert classify_lane({"reversible": True, "days_to_implement": 0.5, "user_impact_pct": 5, "is_pricing": False, "is_paywall": True}) == "ceo"
