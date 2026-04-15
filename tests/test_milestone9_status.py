"""Milestone 9 — aggregated status snapshot.

Thin smoke test: after running a scout opportunity + a tool call, the
status snapshot reports the right counts and surfaces the run.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from studioos.db import session_scope
from studioos.runtime.consumer import drain_once
from studioos.runtime.dispatcher import dispatch_once
from studioos.runtime.outbox import publish_batch
from studioos.runtime.triggers import create_pending_run
from studioos.status import build_snapshot


async def _drain(max_iters: int = 15) -> None:
    for _ in range(max_iters):
        ran = await dispatch_once()
        published = await publish_batch()
        consumed = await drain_once()
        if ran is None and published == 0 and consumed == 0:
            return
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_status_snapshot_reports_recent_activity(db_session) -> None:
    with patch("studioos.workflows.scout_test.random", return_value=0.8):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="test-scout",
                trigger_type="manual",
                trigger_ref="m9-status",
            )
        await _drain()

    async with session_scope() as session:
        snap = await build_snapshot(session)

    # Agents include test-scout and amz-monitor.
    agent_ids = {a.id for a in snap.agents}
    assert "test-scout" in agent_ids
    assert "amz-monitor" in agent_ids

    # At least 2 completed runs: scout + analyst that woke up from event.
    assert snap.runs_by_state.get("completed", 0) >= 2

    # Recent runs list ordered newest first and contains the scout run.
    assert any(r.agent_id == "test-scout" for r in snap.recent_runs)

    # Tool calls fired during the run (test.echo costs 1¢).
    assert snap.tool_call_counts_last_hour.get("test.echo", 0) >= 1
    assert snap.tool_cost_cents_last_hour >= 1

    # Events from the chain show up in the last-hour bucket.
    assert "test.opportunity.detected" in snap.event_type_counts_last_hour
    assert "test.opportunity.acknowledged" in snap.event_type_counts_last_hour
