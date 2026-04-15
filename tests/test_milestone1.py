"""Milestone 1 vertical slice test.

Scenario:
  1. Enqueue a run for test-scout (manual trigger)
  2. Dispatcher picks it up, scout workflow runs, emits an opportunity event
  3. Outbox publisher matches subscription → enqueues analyst run
  4. Dispatcher picks up analyst run, acknowledges → emits second event
  5. Assert:
       - 2 runs with same correlation_id, both COMPLETED
       - 2 events (opportunity.detected + opportunity.acknowledged)
       - First event causation is null, second event causation links back

Note: scout_test emits only when mock value > 10. We monkeypatch `random` to
guarantee a detected opportunity for deterministic testing.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import select

from studioos.db import session_scope
from studioos.models import AgentRun, Event
from studioos.runtime.consumer import drain_once
from studioos.runtime.dispatcher import dispatch_once
from studioos.runtime.outbox import publish_batch
from studioos.runtime.triggers import create_pending_run


async def _drain_runtime(max_iters: int = 15) -> None:
    """Alternately dispatch + publish + consume until quiescent."""
    for _ in range(max_iters):
        ran = await dispatch_once()
        published = await publish_batch()
        consumed = await drain_once()
        if ran is None and published == 0 and consumed == 0:
            return
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_scout_to_analyst_chain(db_session) -> None:
    # Patch random to force an opportunity (value > 10)
    with patch("studioos.workflows.scout_test.random", return_value=0.8):
        async with session_scope() as session:
            run = await create_pending_run(
                session,
                agent_id="test-scout",
                trigger_type="manual",
                trigger_ref="pytest",
            )
            expected_correlation = run.correlation_id
            initial_run_id = run.id

        await _drain_runtime()

    # Assert the expected state
    async with session_scope() as session:
        runs = (
            (
                await session.execute(
                    select(AgentRun)
                    .where(AgentRun.correlation_id == expected_correlation)
                    .order_by(AgentRun.created_at)
                )
            )
            .scalars()
            .all()
        )

        events = (
            (
                await session.execute(
                    select(Event)
                    .where(Event.correlation_id == expected_correlation)
                    .order_by(Event.occurred_at)
                )
            )
            .scalars()
            .all()
        )

    assert len(runs) == 2, f"expected 2 runs, got {len(runs)}"
    assert {r.agent_id for r in runs} == {"test-scout", "test-analyst"}
    assert all(r.state == "completed" for r in runs), [
        (r.agent_id, r.state, r.error) for r in runs
    ]

    # run ordering: scout first, analyst second
    assert runs[0].agent_id == "test-scout"
    assert runs[0].id == initial_run_id
    assert runs[1].agent_id == "test-analyst"
    assert runs[1].parent_run_id == runs[0].id

    # 2 events: detected (from scout) + acknowledged (from analyst)
    assert len(events) == 2, [(e.event_type, str(e.payload)[:80]) for e in events]
    assert events[0].event_type == "test.opportunity.detected"
    assert events[1].event_type == "test.opportunity.acknowledged"

    # Causation chain: second event caused by first
    assert events[0].causation_id is None
    assert events[1].causation_id == events[0].id

    # Payload sanity
    assert events[0].payload["value"] > 10
    assert "opportunity_id" in events[0].payload
    assert (
        events[1].payload["opportunity_id"]
        == events[0].payload["opportunity_id"]
    )
    assert events[1].payload["verdict"] in ("accept", "reject")
