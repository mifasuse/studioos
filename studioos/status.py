"""Aggregated system status — single shot of everything worth seeing.

Shared by `studioos status` (rich tables) and `GET /status` (JSON).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from studioos.budget import current_budget
from studioos.models import (
    Agent,
    AgentRun,
    Approval,
    Event,
    Studio,
    ToolCall,
)
from studioos.scheduler.parser import ScheduleError, parse_schedule


@dataclass
class AgentSummary:
    id: str
    studio_id: str
    mode: str
    schedule_cron: str | None
    last_scheduled_at: datetime | None
    next_due_seconds: int | None  # None = due now, negative = overdue
    tool_scope: list[str] = field(default_factory=list)


@dataclass
class RunSummary:
    id: str
    agent_id: str
    state: str
    trigger_type: str
    created_at: datetime
    ended_at: datetime | None
    summary: str | None
    error: str | None


@dataclass
class StatusSnapshot:
    as_of: datetime
    studios: list[dict[str, Any]]
    agents: list[AgentSummary]
    runs_by_state: dict[str, int]
    recent_runs: list[RunSummary]
    failures_last_hour: int
    event_type_counts_last_hour: dict[str, int]
    pending_approvals: int
    budgets: list[dict[str, Any]]
    tool_call_counts_last_hour: dict[str, int]
    tool_cost_cents_last_hour: int


async def build_snapshot(
    session: AsyncSession, *, limit_runs: int = 8
) -> StatusSnapshot:
    now = datetime.now(UTC)
    one_hour_ago = now - timedelta(hours=1)

    studio_rows = (await session.execute(select(Studio))).scalars().all()
    studios = [
        {
            "id": s.id,
            "display_name": s.display_name,
            "status": s.status,
            "mission": (s.mission or "")[:120],
        }
        for s in studio_rows
    ]

    agent_rows = (await session.execute(select(Agent))).scalars().all()
    agents: list[AgentSummary] = []
    for a in agent_rows:
        next_due_seconds: int | None = None
        if a.schedule_cron:
            try:
                cadence = parse_schedule(a.schedule_cron)
                if a.last_scheduled_at is None:
                    next_due_seconds = 0
                else:
                    delta = (a.last_scheduled_at + cadence) - now
                    next_due_seconds = int(delta.total_seconds())
            except ScheduleError:
                next_due_seconds = None
        agents.append(
            AgentSummary(
                id=a.id,
                studio_id=a.studio_id,
                mode=a.mode,
                schedule_cron=a.schedule_cron,
                last_scheduled_at=a.last_scheduled_at,
                next_due_seconds=next_due_seconds,
                tool_scope=list(a.tool_scope or []),
            )
        )

    # Run state histogram (all-time) + recent N runs
    state_counts = dict(
        (
            await session.execute(
                select(AgentRun.state, func.count())
                .group_by(AgentRun.state)
            )
        ).all()
    )
    recent_run_rows = (
        (
            await session.execute(
                select(AgentRun)
                .order_by(desc(AgentRun.created_at))
                .limit(limit_runs)
            )
        )
        .scalars()
        .all()
    )
    recent_runs = [
        RunSummary(
            id=str(r.id),
            agent_id=r.agent_id,
            state=r.state,
            trigger_type=r.trigger_type,
            created_at=r.created_at,
            ended_at=r.ended_at,
            summary=(r.output_snapshot or {}).get("summary") if r.output_snapshot else None,
            error=(r.error or {}).get("message") if r.error else None,
        )
        for r in recent_run_rows
    ]

    failures_last_hour = int(
        (
            await session.execute(
                select(func.count())
                .select_from(AgentRun)
                .where(
                    AgentRun.state.in_(
                        ("failed", "timed_out", "dead", "budget_exceeded")
                    )
                )
                .where(AgentRun.created_at >= one_hour_ago)
            )
        ).scalar_one()
    )

    # Event type counts in last hour
    event_rows = (
        await session.execute(
            select(Event.event_type, func.count())
            .where(Event.recorded_at >= one_hour_ago)
            .group_by(Event.event_type)
        )
    ).all()
    event_type_counts_last_hour = {t: int(c) for t, c in event_rows}

    pending_approvals = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Approval)
                .where(Approval.state == "pending")
            )
        ).scalar_one()
    )

    budget_views = await current_budget(session)
    budgets = [
        {
            "scope": v.scope,
            "period": v.period,
            "limit_cents": v.limit_cents,
            "spent_cents": v.spent_cents,
            "remaining_cents": v.remaining_cents,
            "over": v.over,
        }
        for v in budget_views
    ]

    tool_counter: Counter[str] = Counter()
    tool_cost_total = 0
    tool_rows = (
        (
            await session.execute(
                select(ToolCall.tool_name, ToolCall.cost_cents).where(
                    ToolCall.called_at >= one_hour_ago
                )
            )
        ).all()
    )
    for name, cents in tool_rows:
        tool_counter[name] += 1
        tool_cost_total += int(cents or 0)

    return StatusSnapshot(
        as_of=now,
        studios=studios,
        agents=agents,
        runs_by_state={k: int(v) for k, v in state_counts.items()},
        recent_runs=recent_runs,
        failures_last_hour=failures_last_hour,
        event_type_counts_last_hour=event_type_counts_last_hour,
        pending_approvals=pending_approvals,
        budgets=budgets,
        tool_call_counts_last_hour=dict(tool_counter),
        tool_cost_cents_last_hour=tool_cost_total,
    )
