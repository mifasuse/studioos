"""Budget CRUD + atomic charge + enforcement helpers.

Scope resolution: we look at both the agent-scoped budget and the studio-scoped
budget. If either is over, the operation is blocked. Charging happens against
BOTH scopes atomically.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from studioos.logging import get_logger
from studioos.models import Agent, Budget

log = get_logger(__name__)

Period = Literal["day", "month"]


@dataclass(frozen=True)
class BudgetView:
    scope: str  # "agent:<id>" or "studio:<id>"
    period: str
    limit_cents: int
    spent_cents: int
    period_start: datetime
    period_end: datetime

    @property
    def remaining_cents(self) -> int:
        return max(0, self.limit_cents - self.spent_cents)

    @property
    def over(self) -> bool:
        return self.spent_cents >= self.limit_cents


def _period_window(period: Period, now: datetime) -> tuple[datetime, datetime]:
    now = now.astimezone(UTC)
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # Move to the 1st of next month
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    else:
        raise ValueError(f"unknown period: {period}")
    return start, end


async def ensure_budget(
    session: AsyncSession,
    *,
    limit_cents: int,
    period: Period = "day",
    agent_id: str | None = None,
    studio_id: str | None = None,
) -> Budget:
    """Create (or update limit for) the current-period budget bucket."""
    if agent_id is None and studio_id is None:
        raise ValueError("budget must have agent_id or studio_id")
    start, end = _period_window(period, datetime.now(UTC))

    stmt = select(Budget).where(
        Budget.period == period,
        Budget.period_start == start,
    )
    if agent_id is not None:
        stmt = stmt.where(Budget.agent_id == agent_id)
    else:
        stmt = stmt.where(Budget.agent_id.is_(None))
    if studio_id is not None:
        stmt = stmt.where(Budget.studio_id == studio_id)
    else:
        stmt = stmt.where(Budget.studio_id.is_(None))

    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        if existing.limit_cents != limit_cents:
            existing.limit_cents = limit_cents
        return existing

    row = Budget(
        agent_id=agent_id,
        studio_id=studio_id,
        period=period,
        period_start=start,
        period_end=end,
        limit_cents=limit_cents,
        spent_cents=0,
    )
    session.add(row)
    await session.flush()
    log.info(
        "budget.ensured",
        agent_id=agent_id,
        studio_id=studio_id,
        period=period,
        limit_cents=limit_cents,
    )
    return row


async def get_or_create_period(
    session: AsyncSession,
    *,
    agent_id: str | None,
    studio_id: str | None,
    period: Period,
) -> Budget | None:
    """Return the active-period budget row for this scope, or None if none configured."""
    start, _end = _period_window(period, datetime.now(UTC))
    stmt = select(Budget).where(
        Budget.period == period,
        Budget.period_start == start,
    )
    if agent_id is not None:
        stmt = stmt.where(Budget.agent_id == agent_id)
    else:
        stmt = stmt.where(Budget.agent_id.is_(None))
    if studio_id is not None:
        stmt = stmt.where(Budget.studio_id == studio_id)
    else:
        stmt = stmt.where(Budget.studio_id.is_(None))
    return (await session.execute(stmt)).scalar_one_or_none()


async def _applicable_budgets(
    session: AsyncSession,
    *,
    agent_id: str | None,
    studio_id: str | None,
) -> list[Budget]:
    """Return current-period budgets matching either scope."""
    out: list[Budget] = []
    for period in ("day", "month"):
        for scope_agent, scope_studio in (
            (agent_id, None),
            (None, studio_id),
        ):
            if scope_agent is None and scope_studio is None:
                continue
            row = await get_or_create_period(
                session,
                agent_id=scope_agent,
                studio_id=scope_studio,
                period=period,  # type: ignore[arg-type]
            )
            if row is not None:
                out.append(row)
    return out


async def preflight_check(
    session: AsyncSession,
    *,
    agent_id: str | None,
    studio_id: str | None,
    charge_cents: int = 0,
) -> tuple[bool, str | None]:
    """Return (ok, reason). ok=False means the run must not proceed."""
    rows = await _applicable_budgets(
        session, agent_id=agent_id, studio_id=studio_id
    )
    for row in rows:
        projected = row.spent_cents + charge_cents
        if projected > row.limit_cents:
            scope = (
                f"agent:{row.agent_id}" if row.agent_id else f"studio:{row.studio_id}"
            )
            return (
                False,
                f"{scope} over budget: "
                f"{projected}/{row.limit_cents} cents ({row.period})",
            )
    return True, None


async def charge(
    session: AsyncSession,
    *,
    agent_id: str | None,
    studio_id: str | None,
    cents: int,
) -> None:
    """Increment spent_cents across every applicable bucket. Non-blocking.

    Atomicity: each update statement is a single SQL UPDATE; we don't
    gate on the limit at charge time (we're accounting after the fact).
    Pre-run `preflight_check` is the gate that refuses over-budget work.
    """
    if cents <= 0:
        return
    rows = await _applicable_budgets(
        session, agent_id=agent_id, studio_id=studio_id
    )
    for row in rows:
        await session.execute(
            update(Budget)
            .where(Budget.id == row.id)
            .values(spent_cents=Budget.spent_cents + cents)
        )
    log.info(
        "budget.charged",
        agent_id=agent_id,
        studio_id=studio_id,
        cents=cents,
        applied_buckets=len(rows),
    )


async def is_over_budget(
    session: AsyncSession,
    *,
    agent_id: str | None,
    studio_id: str | None,
) -> bool:
    ok, _ = await preflight_check(
        session, agent_id=agent_id, studio_id=studio_id
    )
    return not ok


async def current_budget(
    session: AsyncSession,
    *,
    agent_id: str | None = None,
    studio_id: str | None = None,
) -> list[BudgetView]:
    rows = await _applicable_budgets(
        session, agent_id=agent_id, studio_id=studio_id
    )
    out: list[BudgetView] = []
    for row in rows:
        scope = f"agent:{row.agent_id}" if row.agent_id else f"studio:{row.studio_id}"
        out.append(
            BudgetView(
                scope=scope,
                period=row.period,
                limit_cents=row.limit_cents,
                spent_cents=row.spent_cents,
                period_start=row.period_start,
                period_end=row.period_end,
            )
        )
    return out


# Ensures agents table is referenced so the FK import isn't dead weight
# (makes mypy happy + doubles as a sanity pull for the type checker).
_ = Agent  # noqa: F841
