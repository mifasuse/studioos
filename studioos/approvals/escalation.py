"""Inter-agent escalation matrix (OpenClaw ORCHESTRATION.md 79–87).

Workflows import ``classify(kind)`` to decide whether a situation
warrants:

  - only a peer agent message (``to_agent`` = True)
  - a CEO-level approval / ping (``to_ceo`` = True)
  - a Nuri (human) ping (``to_human`` = True)

Keeping this logic in one place means new situation types land here
first and every agent sees the update, instead of drifting silently
across individual workflows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EscalationKind = Literal[
    "normal_task",
    "strategy_change",
    "large_budget",          # > $50/day
    "new_market_supplier",
    "prod_down_incident",
    "destructive_operation",  # DROP TABLE, rm -rf, force push
    "aggressive_roi_100_plus",
    "unknown",
]


@dataclass(frozen=True)
class Escalation:
    kind: EscalationKind
    to_agent: bool
    to_ceo: bool
    to_human: bool
    priority: str  # "emergency" | "high" | "normal" | "low"
    description: str

    @property
    def is_gated(self) -> bool:
        return self.to_ceo or self.to_human


_MATRIX: dict[EscalationKind, Escalation] = {
    "normal_task": Escalation(
        kind="normal_task",
        to_agent=True,
        to_ceo=False,
        to_human=False,
        priority="normal",
        description="Agent-to-agent handoff; no gating.",
    ),
    "strategy_change": Escalation(
        kind="strategy_change",
        to_agent=False,
        to_ceo=True,
        to_human=False,
        priority="high",
        description="Operating-rule change; CEO must approve.",
    ),
    "large_budget": Escalation(
        kind="large_budget",
        to_agent=False,
        to_ceo=False,
        to_human=True,
        priority="high",
        description="Daily spend above $50 — Nuri sign-off required.",
    ),
    "new_market_supplier": Escalation(
        kind="new_market_supplier",
        to_agent=False,
        to_ceo=False,
        to_human=True,
        priority="high",
        description="New marketplace or supplier — Nuri only.",
    ),
    "prod_down_incident": Escalation(
        kind="prod_down_incident",
        to_agent=False,
        to_ceo=True,
        to_human=True,
        priority="emergency",
        description="Prod-down — both CEO and Nuri.",
    ),
    "destructive_operation": Escalation(
        kind="destructive_operation",
        to_agent=False,
        to_ceo=False,
        to_human=True,
        priority="emergency",
        description="DROP TABLE / rm -rf / force push — Nuri only.",
    ),
    "aggressive_roi_100_plus": Escalation(
        kind="aggressive_roi_100_plus",
        to_agent=False,
        to_ceo=True,
        to_human=False,
        priority="high",
        description="ROI%100+ aggressive opportunity — CEO review.",
    ),
    "unknown": Escalation(
        kind="unknown",
        to_agent=False,
        to_ceo=True,
        to_human=False,
        priority="normal",
        description="Unrecognised situation — defer to CEO.",
    ),
}


def classify(kind: str) -> Escalation:
    """Return the escalation policy for a situation.

    Unknown kinds fall through to the "unknown" bucket (CEO-only).
    Never raises.
    """
    return _MATRIX.get(kind, _MATRIX["unknown"])  # type: ignore[return-value]


def to_approval_row(
    esc: Escalation,
    *,
    reason: str,
    payload: dict,
    expires_in_seconds: int = 60 * 60 * 24,
) -> dict:
    """Shape an Escalation + context into the ``approvals`` workflow output."""
    return {
        "reason": f"[{esc.kind}:{esc.priority}] {reason}",
        "payload": {
            **payload,
            "escalation": {
                "kind": esc.kind,
                "to_agent": esc.to_agent,
                "to_ceo": esc.to_ceo,
                "to_human": esc.to_human,
                "priority": esc.priority,
            },
        },
        "expires_in_seconds": expires_in_seconds,
    }
