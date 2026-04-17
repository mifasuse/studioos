"""Outcome checker pure functions — M36."""
from __future__ import annotations
from datetime import datetime, timedelta, UTC

from studioos.workflows.outcome_checker import (
    is_outcome_checkable,
    should_check_now,
    evaluate_reprice_outcome,
    evaluate_discovery_outcome,
    update_strategy_stats,
)


def test_reprice_is_checkable() -> None:
    assert is_outcome_checkable("amz.reprice.recommended") is True
    assert is_outcome_checkable("amz.price.checked") is False


def test_should_check_after_elapsed() -> None:
    event_time = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    now = datetime(2026, 4, 16, 11, 0, tzinfo=UTC)  # 25h later
    assert should_check_now("amz.reprice.recommended", event_time, now) is True


def test_should_not_check_too_early() -> None:
    event_time = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    now = datetime(2026, 4, 15, 20, 0, tzinfo=UTC)  # 10h later
    assert should_check_now("amz.reprice.recommended", event_time, now) is False


def test_reprice_success() -> None:
    result = evaluate_reprice_outcome("B00XYZ", lost_buybox_asins={"B00ABC", "B00DEF"})
    assert result["outcome"] == "success"


def test_reprice_failure() -> None:
    result = evaluate_reprice_outcome("B00XYZ", lost_buybox_asins={"B00XYZ", "B00ABC"})
    assert result["outcome"] == "failure"


def test_discovery_confirmed() -> None:
    result = evaluate_discovery_outcome("B00XYZ", confirmed_asins={"B00XYZ"})
    assert result["outcome"] == "success"


def test_discovery_pending() -> None:
    result = evaluate_discovery_outcome("B00XYZ", confirmed_asins=set())
    assert result["outcome"] == "pending"


def test_update_strategy_stats() -> None:
    stats = {}
    stats = update_strategy_stats(stats, "buy_box_win", "success")
    assert stats["buy_box_win"]["total"] == 1
    assert stats["buy_box_win"]["success"] == 1
    assert stats["buy_box_win"]["rate"] == 1.0
    stats = update_strategy_stats(stats, "buy_box_win", "failure")
    assert stats["buy_box_win"]["total"] == 2
    assert stats["buy_box_win"]["rate"] == 0.5
