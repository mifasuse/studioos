"""KPI target + snapshot persistence + gap calculation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from studioos.logging import get_logger
from studioos.models import KpiSnapshot, KpiTarget

log = get_logger(__name__)


@dataclass
class KpiGap:
    name: str
    target: Decimal
    current: Decimal
    direction: str
    delta: Decimal  # positive when on the wrong side
    reached: bool


@dataclass
class KpiState:
    name: str
    display_name: str | None
    target: Decimal | None
    current: Decimal | None
    direction: str
    unit: str | None
    last_recorded_at: datetime | None
    gap: KpiGap | None


async def upsert_target(
    session: AsyncSession,
    *,
    name: str,
    target_value: Decimal | float | int,
    direction: str = "higher_better",
    studio_id: str | None = None,
    agent_id: str | None = None,
    display_name: str | None = None,
    unit: str | None = None,
    description: str | None = None,
) -> int:
    """Create or replace a KPI target. Caller controls commit."""
    existing = (
        await session.execute(
            select(KpiTarget).where(
                KpiTarget.name == name,
                KpiTarget.studio_id == studio_id,
                KpiTarget.agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        row = KpiTarget(
            studio_id=studio_id,
            agent_id=agent_id,
            name=name,
            display_name=display_name,
            target_value=Decimal(str(target_value)),
            direction=direction,
            unit=unit,
            description=description,
        )
        session.add(row)
        await session.flush()
        return row.id
    existing.target_value = Decimal(str(target_value))
    existing.direction = direction
    existing.display_name = display_name or existing.display_name
    existing.unit = unit or existing.unit
    existing.description = description or existing.description
    return existing.id


async def record_snapshot(
    session: AsyncSession,
    *,
    name: str,
    value: Decimal | float | int,
    studio_id: str | None = None,
    agent_id: str | None = None,
    source_run_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Append a KPI snapshot row. Caller controls commit."""
    row = KpiSnapshot(
        studio_id=studio_id,
        agent_id=agent_id,
        name=name,
        value=Decimal(str(value)),
        source_run_id=source_run_id,
        snapshot_metadata=metadata or {},
    )
    session.add(row)
    await session.flush()
    log.info(
        "kpi.snapshot",
        name=name,
        value=str(row.value),
        agent_id=agent_id,
        studio_id=studio_id,
    )
    return row.id


async def get_target(
    session: AsyncSession,
    *,
    name: str,
    studio_id: str | None = None,
    agent_id: str | None = None,
) -> KpiTarget | None:
    return (
        await session.execute(
            select(KpiTarget).where(
                KpiTarget.name == name,
                KpiTarget.studio_id == studio_id,
                KpiTarget.agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()


async def latest_snapshot(
    session: AsyncSession,
    *,
    name: str,
    studio_id: str | None = None,
    agent_id: str | None = None,
) -> KpiSnapshot | None:
    stmt = (
        select(KpiSnapshot)
        .where(KpiSnapshot.name == name)
        .order_by(desc(KpiSnapshot.recorded_at))
        .limit(1)
    )
    if studio_id is not None:
        stmt = stmt.where(KpiSnapshot.studio_id == studio_id)
    if agent_id is not None:
        stmt = stmt.where(KpiSnapshot.agent_id == agent_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_current_state(
    session: AsyncSession,
    *,
    studio_id: str | None = None,
    agent_id: str | None = None,
) -> list[KpiState]:
    """Return current state for every KPI in the given scope."""
    targets_stmt = select(KpiTarget)
    if studio_id is not None:
        targets_stmt = targets_stmt.where(KpiTarget.studio_id == studio_id)
    if agent_id is not None:
        targets_stmt = targets_stmt.where(KpiTarget.agent_id == agent_id)
    targets = (await session.execute(targets_stmt)).scalars().all()

    out: list[KpiState] = []
    for t in targets:
        snap = await latest_snapshot(
            session,
            name=t.name,
            studio_id=t.studio_id,
            agent_id=t.agent_id,
        )
        current = snap.value if snap else None
        gap: KpiGap | None = None
        if current is not None:
            gap = _compute_gap(t, current)
        out.append(
            KpiState(
                name=t.name,
                display_name=t.display_name,
                target=t.target_value,
                current=current,
                direction=t.direction,
                unit=t.unit,
                last_recorded_at=snap.recorded_at if snap else None,
                gap=gap,
            )
        )
    return out


def _compute_gap(target: KpiTarget, current: Decimal) -> KpiGap:
    delta = target.target_value - current
    if target.direction == "higher_better":
        reached = current >= target.target_value
        positive_gap = max(delta, Decimal(0))
    elif target.direction == "lower_better":
        reached = current <= target.target_value
        positive_gap = max(-delta, Decimal(0))
    else:  # range — no direction, equality treated as reached
        reached = current == target.target_value
        positive_gap = abs(delta)
    return KpiGap(
        name=target.name,
        target=target.target_value,
        current=current,
        direction=target.direction,
        delta=positive_gap,
        reached=reached,
    )
