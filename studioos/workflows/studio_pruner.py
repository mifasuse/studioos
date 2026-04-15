"""studio_pruner — weekly memory garbage collection (plan Q9 closure).

Walks memory_semantic and deletes rows that are both:
  - importance < 0.3
  - older than min_age_days (default 30)
  - never accessed in the last min_unused_days (default 14)

Reflective by design: the goal isn't to maximize delete count, it's to
shrink the memory surface to the rows that actually paid off in
recent retrieval. Per-studio scoped.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import and_, delete, func, or_, select

from studioos.db import session_scope
from studioos.logging import get_logger
from studioos.models import MemorySemantic
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class PrunerState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    snapshot: dict[str, Any]
    pruned_count: int
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


async def node_collect(state: PrunerState) -> dict[str, Any]:
    studio_id = state.get("studio_id")
    async with session_scope() as session:
        total = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(MemorySemantic)
                    .where(
                        MemorySemantic.studio_id == studio_id
                        if studio_id
                        else MemorySemantic.studio_id.isnot(None)
                    )
                )
            ).scalar_one()
        )
    return {
        "snapshot": {
            "studio_id": studio_id,
            "total_memories_before": total,
        }
    }


async def node_prune(state: PrunerState) -> dict[str, Any]:
    goals = state.get("goals") or {}
    studio_id = state.get("studio_id")
    min_age_days = int(goals.get("min_age_days", 30))
    min_unused_days = int(goals.get("min_unused_days", 14))
    importance_threshold = float(goals.get("importance_threshold", 0.3))
    dry_run = bool(goals.get("dry_run", False))

    now = datetime.now(UTC)
    age_cutoff = now - timedelta(days=min_age_days)
    unused_cutoff = now - timedelta(days=min_unused_days)

    async with session_scope() as session:
        condition = and_(
            MemorySemantic.importance < importance_threshold,
            MemorySemantic.created_at < age_cutoff,
            or_(
                MemorySemantic.accessed_at.is_(None),
                MemorySemantic.accessed_at < unused_cutoff,
            ),
        )
        if studio_id:
            condition = and_(condition, MemorySemantic.studio_id == studio_id)

        # Count first so we can report cleanly.
        count = int(
            (
                await session.execute(
                    select(func.count()).select_from(MemorySemantic).where(condition)
                )
            ).scalar_one()
        )

        if not dry_run and count > 0:
            await session.execute(delete(MemorySemantic).where(condition))

    return {"pruned_count": count}


async def node_report(state: PrunerState) -> dict[str, Any]:
    snap = state.get("snapshot") or {}
    pruned = state.get("pruned_count") or 0
    studio_id = state.get("studio_id") or "(global)"
    before = snap.get("total_memories_before", 0)
    after = max(0, before - pruned)

    text = (
        f"*🧹 Memory Pruner — {studio_id}*\n"
        f"Total before: {before}\n"
        f"Pruned: {pruned}\n"
        f"After: {after}"
    )
    notify = await invoke_from_state(
        state,
        "telegram.notify",
        {
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
    )
    notified = notify["status"] == "ok"

    state_accum = dict(state.get("state") or {})
    state_accum["pruner_runs_total"] = (
        int(state_accum.get("pruner_runs_total", 0)) + 1
    )
    state_accum["pruner_pruned_total"] = (
        int(state_accum.get("pruner_pruned_total", 0)) + pruned
    )

    return {
        "memories": [
            {
                "content": (
                    f"Pruner: removed {pruned} memories from {studio_id}; "
                    f"surface {before} → {after}"
                ),
                "tags": ["pruner", "weekly", studio_id],
                "importance": 0.4,
            }
        ],
        "kpi_updates": [
            {"name": "memory_pruned_last", "value": pruned},
            {"name": "memory_total_after", "value": after},
        ],
        "state": state_accum,
        "summary": (
            f"Pruned {pruned} memories ({before} → {after})"
            + (" (notified)" if notified else "")
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(PrunerState)
    graph.add_node("collect", node_collect)
    graph.add_node("prune", node_prune)
    graph.add_node("report", node_report)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "prune")
    graph.add_edge("prune", "report")
    graph.add_edge("report", END)
    return graph.compile()


compiled = build_graph()

register_workflow("studio_pruner", 1, compiled)
