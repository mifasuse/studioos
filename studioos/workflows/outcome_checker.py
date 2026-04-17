"""Outcome checking rules — determines if an action succeeded.

Each action type has a rule: which tool to call, how long to wait,
and what counts as success.
"""
from __future__ import annotations
from datetime import datetime, timedelta, UTC
from typing import Any


OUTCOME_RULES: dict[str, dict[str, Any]] = {
    "amz.reprice.recommended": {
        "check_after_hours": 24,
        "description": "Check if Buy Box was recovered after reprice",
    },
    "amz.opportunity.discovered": {
        "check_after_hours": 168,  # 7 days
        "description": "Check if discovery led to confirmed opportunity",
    },
    "amz.crosslist.candidate": {
        "check_after_hours": 48,
        "description": "Check if cross-listed item sold on eBay",
    },
}


def is_outcome_checkable(event_type: str) -> bool:
    return event_type in OUTCOME_RULES


def should_check_now(event_type: str, event_time: datetime, now: datetime | None = None) -> bool:
    """Return True if enough time has passed to check the outcome."""
    now = now or datetime.now(UTC)
    rule = OUTCOME_RULES.get(event_type)
    if not rule:
        return False
    elapsed = now - event_time
    return elapsed >= timedelta(hours=rule["check_after_hours"])


def evaluate_reprice_outcome(
    asin: str,
    lost_buybox_asins: set[str],
) -> dict[str, Any]:
    """Check if a repriced ASIN recovered its Buy Box."""
    success = asin not in lost_buybox_asins
    return {
        "outcome": "success" if success else "failure",
        "detail": "Buy Box recovered" if success else "Still lost Buy Box",
    }


def evaluate_discovery_outcome(
    asin: str,
    confirmed_asins: set[str],
) -> dict[str, Any]:
    """Check if a discovered opportunity was confirmed by analyst."""
    confirmed = asin in confirmed_asins
    return {
        "outcome": "success" if confirmed else "pending",
        "detail": "Confirmed by analyst" if confirmed else "Not yet confirmed",
    }


def update_strategy_stats(
    current_stats: dict[str, Any],
    strategy: str,
    outcome: str,
) -> dict[str, Any]:
    """Update strategy success/failure counters."""
    stats = dict(current_stats)
    if strategy not in stats:
        stats[strategy] = {"total": 0, "success": 0, "failure": 0, "rate": 0.0}
    entry = dict(stats[strategy])
    entry["total"] = entry.get("total", 0) + 1
    if outcome == "success":
        entry["success"] = entry.get("success", 0) + 1
    elif outcome == "failure":
        entry["failure"] = entry.get("failure", 0) + 1
    total = entry["total"]
    entry["rate"] = round(entry["success"] / total, 3) if total > 0 else 0.0
    stats[strategy] = entry
    return stats
