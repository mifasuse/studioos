"""Repricer live mode — no approval gate, direct execution."""
from __future__ import annotations
from typing import Any
from studioos.workflows.amz_repricer import node_decide


def _state(**over: Any) -> dict:
    base: dict[str, Any] = {
        "agent_id": "amz-repricer",
        "studio_id": "amz",
        "run_id": "test-run-1",
        "goals": {"dry_run": False},
        "recommendation": {
            "asin": "B00TEST0001",
            "sku": "SKU-1",
            "listing_id": 101,
            "current_price": 50.0,
            "proposed_price": 45.0,
            "buy_box_price": 44.0,
            "delta": 5.0,
            "clamped_to_floor": False,
        },
        "already_granted": False,
        "state": {},
    }
    base.update(over)
    return base


def test_decide_skips_approval_when_not_dry_run() -> None:
    result = node_decide(_state())
    approvals = result.get("approvals") or []
    assert len(approvals) == 0


def test_decide_still_creates_approval_in_dry_run() -> None:
    result = node_decide(_state(goals={"dry_run": True}))
    approvals = result.get("approvals") or []
    assert len(approvals) == 1
    assert "DRY-RUN" in approvals[0]["reason"]
