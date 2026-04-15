"""Escalation matrix — OpenClaw ORCHESTRATION.md 79–87."""
from __future__ import annotations

from studioos.approvals.escalation import classify, to_approval_row


def test_normal_task_no_gating() -> None:
    e = classify("normal_task")
    assert e.to_agent is True
    assert not e.to_ceo
    assert not e.to_human
    assert e.is_gated is False


def test_strategy_change_needs_ceo() -> None:
    e = classify("strategy_change")
    assert e.to_ceo and not e.to_human


def test_large_budget_needs_human_only() -> None:
    e = classify("large_budget")
    assert e.to_human and not e.to_ceo


def test_prod_down_hits_both() -> None:
    e = classify("prod_down_incident")
    assert e.to_ceo and e.to_human
    assert e.priority == "emergency"


def test_destructive_human_only() -> None:
    e = classify("destructive_operation")
    assert e.to_human and not e.to_ceo
    assert e.priority == "emergency"


def test_aggressive_roi_ceo_only() -> None:
    e = classify("aggressive_roi_100_plus")
    assert e.to_ceo and not e.to_human


def test_unknown_falls_through_to_ceo() -> None:
    e = classify("totally_made_up")
    assert e.kind == "unknown"
    assert e.to_ceo


def test_to_approval_row_shape() -> None:
    esc = classify("prod_down_incident")
    row = to_approval_row(
        esc,
        reason="pricefinder health down",
        payload={"service": "pricefinder"},
    )
    assert "prod_down_incident:emergency" in row["reason"]
    assert row["payload"]["service"] == "pricefinder"
    assert row["payload"]["escalation"]["to_human"] is True
    assert row["payload"]["escalation"]["to_ceo"] is True
