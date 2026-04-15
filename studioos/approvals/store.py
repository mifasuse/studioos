"""Approval store + state transitions.

A run can reach `awaiting_approval`: its associated `approvals` rows must be
settled (approved or denied) before the run is re-enqueued. Denial fails the
run; approval drops it back to `pending` so the dispatcher picks it up again.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from studioos.logging import get_logger
from studioos.models import AgentRun, Approval

log = get_logger(__name__)


async def create_approval(
    session: AsyncSession,
    *,
    run_id: UUID,
    agent_id: str,
    studio_id: str | None,
    correlation_id: UUID | None,
    reason: str,
    payload: dict[str, Any] | None = None,
    expires_in_seconds: int | None = None,
) -> Approval:
    expires_at = None
    if expires_in_seconds is not None:
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in_seconds)
    row = Approval(
        run_id=run_id,
        agent_id=agent_id,
        studio_id=studio_id,
        correlation_id=correlation_id,
        reason=reason,
        payload=payload or {},
        state="pending",
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    log.info(
        "approval.created",
        approval_id=str(row.id),
        run_id=str(run_id),
        agent_id=agent_id,
        reason=reason,
    )
    return row


async def list_pending(
    session: AsyncSession,
    *,
    agent_id: str | None = None,
    studio_id: str | None = None,
    limit: int = 50,
) -> list[Approval]:
    stmt = (
        select(Approval)
        .where(Approval.state == "pending")
        .order_by(Approval.created_at.asc())
        .limit(limit)
    )
    if agent_id:
        stmt = stmt.where(Approval.agent_id == agent_id)
    if studio_id:
        stmt = stmt.where(Approval.studio_id == studio_id)
    return list((await session.execute(stmt)).scalars().all())


async def pending_for_run(
    session: AsyncSession, run_id: UUID
) -> list[Approval]:
    stmt = (
        select(Approval)
        .where(Approval.run_id == run_id)
        .where(Approval.state == "pending")
    )
    return list((await session.execute(stmt)).scalars().all())


async def decide_approval(
    session: AsyncSession,
    approval_id: UUID,
    *,
    decision: str,
    decided_by: str,
    note: str | None = None,
) -> Approval:
    """Settle an approval. Side-effects the parent run as appropriate."""
    if decision not in ("approved", "denied"):
        raise ValueError("decision must be 'approved' or 'denied'")
    row = (
        await session.execute(select(Approval).where(Approval.id == approval_id))
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(f"approval {approval_id} not found")
    if row.state != "pending":
        raise ValueError(f"approval already settled: {row.state}")

    row.state = decision
    row.decided_by = decided_by
    row.decided_at = datetime.now(UTC)
    row.decision_note = note
    await session.flush()

    # Side-effect the run.
    run = (
        await session.execute(select(AgentRun).where(AgentRun.id == row.run_id))
    ).scalar_one()

    if decision == "approved":
        # Are any sibling approvals still pending?
        others = await pending_for_run(session, run.id)
        if not others:
            run.state = "pending"
            run.error = None
            log.info(
                "approval.cleared_run",
                run_id=str(run.id),
                approval_id=str(row.id),
            )
    else:  # denied
        run.state = "failed"
        run.ended_at = datetime.now(UTC)
        run.error = {
            "type": "ApprovalDenied",
            "message": note or "denied by human",
            "approval_id": str(row.id),
            "decided_by": decided_by,
        }
        log.info(
            "approval.denied_run",
            run_id=str(run.id),
            approval_id=str(row.id),
        )

    return row


async def expire_stale(session: AsyncSession) -> int:
    """Mark pending approvals past expires_at as expired + fail their runs."""
    now = datetime.now(UTC)
    stmt = (
        select(Approval)
        .where(Approval.state == "pending")
        .where(Approval.expires_at.is_not(None))
        .where(Approval.expires_at <= now)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        row.state = "expired"
        row.decided_at = now
        run = (
            await session.execute(
                select(AgentRun).where(AgentRun.id == row.run_id)
            )
        ).scalar_one()
        if run.state == "awaiting_approval":
            run.state = "failed"
            run.ended_at = now
            run.error = {
                "type": "ApprovalExpired",
                "message": "approval expired",
                "approval_id": str(row.id),
            }
    if rows:
        log.info("approval.expired_batch", count=len(rows))
    return len(rows)


async def bulk_update_run_state(
    session: AsyncSession,
    run_id: UUID,
    *,
    state: str,
) -> None:
    """Helper used by the runner when emitting an awaiting_approval hold."""
    await session.execute(
        update(AgentRun).where(AgentRun.id == run_id).values(state=state)
    )
