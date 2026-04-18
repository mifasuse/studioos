"""App Studio QA — check_app_health pure function tests."""
from __future__ import annotations

from studioos.workflows.app_studio_qa import check_app_health


def test_healthy_app_no_flags() -> None:
    flags = check_app_health(
        "quit_smoking",
        overview={"roi": 2.5, "mrr": 150.0},
        failure_rate_pct=5.0,
        thresholds={"failure_rate_threshold": 20.0},
    )
    assert flags == []


def test_negative_roi_flagged() -> None:
    flags = check_app_health(
        "quit_smoking",
        overview={"roi": -0.5, "mrr": 100.0},
        failure_rate_pct=0.0,
        thresholds={},
    )
    assert len(flags) == 1
    assert flags[0]["check"] == "negative_roi"


def test_high_failure_rate_flagged() -> None:
    flags = check_app_health(
        "sms_forward",
        overview={"roi": 1.0, "mrr": 50.0},
        failure_rate_pct=25.0,
        thresholds={"failure_rate_threshold": 20.0},
    )
    assert len(flags) == 1
    assert flags[0]["check"] == "high_failure_rate"
    assert flags[0]["value"] == 25.0


def test_zero_mrr_no_previous_is_ok() -> None:
    """MRR=0 on a free/pre-launch app is NOT a failure."""
    flags = check_app_health(
        "moodmate",
        overview={"roi": 0, "mrr": 0},
        failure_rate_pct=0.0,
        thresholds={},
    )
    assert flags == []


def test_zero_mrr_with_previous_mrr_is_flagged() -> None:
    """MRR dropped from >0 to 0 → subscription/payment issue."""
    flags = check_app_health(
        "quit_smoking",
        overview={"roi": 0, "mrr": 0, "prev_mrr": 200.0},
        failure_rate_pct=0.0,
        thresholds={},
    )
    assert len(flags) == 1
    assert flags[0]["check"] == "mrr_dropped_to_zero"


def test_zero_mrr_alternative_field_name() -> None:
    """Also handles mrr_previous field name."""
    flags = check_app_health(
        "quit_smoking",
        overview={"mrr": 0, "mrr_previous": 100.0},
        failure_rate_pct=0.0,
        thresholds={},
    )
    assert len(flags) == 1
    assert flags[0]["check"] == "mrr_dropped_to_zero"


def test_empty_overview_flags_api_unreachable() -> None:
    """Empty overview dict = Hub API is down."""
    flags = check_app_health(
        "sms_forward",
        overview={},
        failure_rate_pct=0.0,
        thresholds={},
    )
    assert len(flags) == 1
    assert flags[0]["check"] == "hub_api_unreachable"


def test_multiple_failures_combined() -> None:
    flags = check_app_health(
        "quit_smoking",
        overview={"roi": -1.0, "mrr": 0, "prev_mrr": 50.0},
        failure_rate_pct=30.0,
        thresholds={"failure_rate_threshold": 20.0},
    )
    checks = {f["check"] for f in flags}
    assert "negative_roi" in checks
    assert "high_failure_rate" in checks
    assert "mrr_dropped_to_zero" in checks
    assert len(flags) == 3
