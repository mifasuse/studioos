"""KPI layer — targets, snapshots, gap analysis."""
from __future__ import annotations

from studioos.kpi.store import (
    KpiGap,
    KpiState,
    get_current_state,
    get_target,
    record_snapshot,
    upsert_target,
)

__all__ = [
    "KpiGap",
    "KpiState",
    "get_current_state",
    "get_target",
    "record_snapshot",
    "upsert_target",
]
