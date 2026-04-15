"""Milestone 5 — budget enforcement + tool cost accounting + approval gate."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from studioos.approvals import create_approval, decide_approval, list_pending
from studioos.budget import charge, current_budget, ensure_budget, preflight_check
from studioos.db import session_scope
from studioos.models import AgentRun, Approval, Budget, ToolCall
from studioos.runtime.consumer import drain_once
from studioos.runtime.dispatcher import dispatch_once
from studioos.runtime.outbox import publish_batch
from studioos.runtime.triggers import create_pending_run
from studioos.tools import ToolContext, invoke_tool


async def _drain(max_iters: int = 15) -> None:
    for _ in range(max_iters):
        ran = await dispatch_once()
        published = await publish_batch()
        consumed = await drain_once()
        if ran is None and published == 0 and consumed == 0:
            return
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_tool_cost_charges_budget(db_session) -> None:
    async with session_scope() as session:
        await ensure_budget(
            session,
            limit_cents=1000,
            period="day",
            agent_id="test-scout",
        )

    ctx = ToolContext(
        agent_id="test-scout",
        run_id=None,
        correlation_id=None,
        studio_id="test",
    )
    result = await invoke_tool("test.echo", {"message": "charge-me"}, ctx)
    assert result["status"] == "ok"
    assert result["cost_cents"] == 1

    async with session_scope() as session:
        budget = (
            await session.execute(
                select(Budget).where(Budget.agent_id == "test-scout")
            )
        ).scalar_one()
    assert budget.spent_cents == 1

    async with session_scope() as session:
        call = (
            await session.execute(
                select(ToolCall).where(ToolCall.tool_name == "test.echo")
            )
        ).scalar_one()
    assert call.cost_cents == 1


@pytest.mark.asyncio
async def test_budget_exceeded_blocks_run(db_session) -> None:
    # Seed a 0-cent budget so the very first call fails preflight.
    async with session_scope() as session:
        await ensure_budget(
            session,
            limit_cents=0,
            period="day",
            agent_id="test-scout",
        )
        # Push spent over limit so preflight must fail regardless of charge_cents.
        await charge(
            session,
            agent_id="test-scout",
            studio_id=None,
            cents=1,
        )

    async with session_scope() as session:
        await create_pending_run(
            session,
            agent_id="test-scout",
            trigger_type="manual",
            trigger_ref="m5-budget",
        )
    await _drain()

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(AgentRun).where(
                        AgentRun.agent_id == "test-scout",
                        AgentRun.trigger_ref == "m5-budget",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows, "run not created"
    assert rows[0].state == "budget_exceeded"
    assert rows[0].error and rows[0].error["type"] == "BudgetExceeded"


@pytest.mark.asyncio
async def test_approval_gate_holds_and_releases(db_session) -> None:
    # Create a run and an approval against it; simulate the runner's decision
    # to park the run, then approve and confirm the run drops back to pending.
    async with session_scope() as session:
        run = await create_pending_run(
            session,
            agent_id="test-scout",
            trigger_type="manual",
            trigger_ref="m5-approval",
        )
        run.state = "awaiting_approval"
        approval = await create_approval(
            session,
            run_id=run.id,
            agent_id="test-scout",
            studio_id="test",
            correlation_id=run.correlation_id,
            reason="human-in-the-loop check",
            payload={"target": "publish", "amount_cents": 5000},
            expires_in_seconds=600,
        )
        approval_id = approval.id
        run_id = run.id

    async with session_scope() as session:
        pending = await list_pending(session)
    assert len(pending) == 1
    assert pending[0].reason == "human-in-the-loop check"

    async with session_scope() as session:
        settled = await decide_approval(
            session,
            approval_id=approval_id,
            decision="approved",
            decided_by="pytest",
            note="looks good",
        )
    assert settled.state == "approved"

    async with session_scope() as session:
        row = (
            await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        ).scalar_one()
    assert row.state == "pending"


@pytest.mark.asyncio
async def test_approval_denial_fails_run(db_session) -> None:
    async with session_scope() as session:
        run = await create_pending_run(
            session,
            agent_id="test-scout",
            trigger_type="manual",
            trigger_ref="m5-deny",
        )
        run.state = "awaiting_approval"
        approval = await create_approval(
            session,
            run_id=run.id,
            agent_id="test-scout",
            studio_id="test",
            correlation_id=run.correlation_id,
            reason="risky action",
            payload={},
        )
        approval_id = approval.id
        run_id = run.id

    async with session_scope() as session:
        await decide_approval(
            session,
            approval_id=approval_id,
            decision="denied",
            decided_by="pytest",
            note="nope",
        )

    async with session_scope() as session:
        row = (
            await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        ).scalar_one()
    assert row.state == "failed"
    assert row.error and row.error["type"] == "ApprovalDenied"
