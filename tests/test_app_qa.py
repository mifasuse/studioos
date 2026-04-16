"""Tests for App Studio QA agent — M30 Task 2."""
from studioos.workflows.app_studio_qa import check_app_health


def test_healthy_app_passes() -> None:
    flags = check_app_health("quit_smoking", {"roi": 2.0, "mrr": 100}, 5.0, {"failure_rate_threshold": 20.0})
    assert len(flags) == 0


def test_negative_roi_fails() -> None:
    flags = check_app_health("quit_smoking", {"roi": -0.5, "mrr": 100}, 5.0, {"failure_rate_threshold": 20.0})
    assert any(f["check"] == "negative_roi" for f in flags)


def test_high_failure_rate_fails() -> None:
    flags = check_app_health("quit_smoking", {"roi": 2.0, "mrr": 100}, 25.0, {"failure_rate_threshold": 20.0})
    assert any(f["check"] == "high_failure_rate" for f in flags)


def test_zero_mrr_fails() -> None:
    flags = check_app_health("quit_smoking", {"roi": 2.0, "mrr": 0}, 5.0, {"failure_rate_threshold": 20.0})
    assert any(f["check"] == "zero_mrr" for f in flags)


def test_workflow_imports() -> None:
    from studioos.workflows.app_studio_qa import build_graph
    assert build_graph() is not None
