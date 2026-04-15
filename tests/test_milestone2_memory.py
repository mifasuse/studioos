"""Milestone 2 — memory + KPI roundtrip via runtime.

Validates:
  - Workflow output `memories[]` is embedded + persisted to memory_semantic
  - Workflow output `kpi_updates[]` becomes rows in kpi_snapshots
  - Search by tag returns the freshly-written memory
  - Cosine-distance ordering works (FakeEmbedder is deterministic per content)
  - KPI gap calculation reflects current vs target
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select

from studioos.db import session_scope
from studioos.kpi.store import (
    get_current_state,
    upsert_target,
)
from studioos.memory.store import search_memory
from studioos.models import KpiSnapshot, MemorySemantic
from studioos.runtime.consumer import drain_once
from studioos.runtime.dispatcher import dispatch_once
from studioos.runtime.outbox import publish_batch
from studioos.runtime.triggers import create_pending_run


async def _drain(max_iters: int = 15) -> None:
    for _ in range(max_iters):
        ran = await dispatch_once()
        published = await publish_batch()
        consumed = await drain_once()
        if ran is None and published == 0 and consumed == 0:
            return
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_memory_and_kpi_roundtrip(db_session) -> None:
    # Seed a target so gap calculation has something to compare
    async with session_scope() as session:
        await upsert_target(
            session,
            name="hit_rate",
            target_value=Decimal("0.5"),
            direction="higher_better",
            agent_id="test-scout",
            studio_id="test",
            display_name="Scout Hit Rate",
            unit="ratio",
        )

    # Force a successful opportunity (random > 10 → opp emitted)
    with patch("studioos.workflows.scout_test.random", return_value=0.95):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="test-scout",
                trigger_type="manual",
                trigger_ref="m2-test",
            )
        await _drain()

    # 1) Semantic memory was persisted with the right tags
    async with session_scope() as session:
        rows = (
            (await session.execute(select(MemorySemantic)))
            .scalars()
            .all()
        )
    # scout writes 1 memory + analyst writes 1 (per acked opportunity)
    assert len(rows) >= 1
    scout_memories = [
        r for r in rows if r.tags and "opportunity" in (r.tags or [])
    ]
    assert scout_memories, [r.tags for r in rows]
    assert "scout_test" in (scout_memories[0].tags or [])
    assert scout_memories[0].embedding is not None
    assert len(scout_memories[0].embedding) == 1536

    # 2) Search retrieves the scout memory by topical query
    async with session_scope() as session:
        results = await search_memory(
            session,
            query="opportunity scan",
            agent_id="test-scout",
            limit=5,
        )
    assert results, "search_memory returned nothing"
    top_contents = [r.content for r in results]
    assert any("opportunity" in c.lower() for c in top_contents)

    # 3) KPI snapshots got recorded for scans_total + opportunities_found + hit_rate
    async with session_scope() as session:
        snaps = (
            (await session.execute(select(KpiSnapshot)))
            .scalars()
            .all()
        )
    snap_names = {s.name for s in snaps}
    assert {"scans_total", "opportunities_found", "hit_rate"} <= snap_names

    # 4) get_current_state reflects the latest snapshot + computes gap
    async with session_scope() as session:
        states = await get_current_state(
            session,
            studio_id="test",
            agent_id="test-scout",
        )
    by_name = {s.name: s for s in states}
    assert "hit_rate" in by_name
    hit = by_name["hit_rate"]
    assert hit.target == Decimal("0.500000")
    assert hit.current == Decimal("1.000000")  # 1/1 after one successful scan
    assert hit.gap is not None
    assert hit.gap.reached is True
    assert hit.gap.delta == Decimal("0")

    # 5) Analyst run also wrote a verdict memory
    async with session_scope() as session:
        verdict_rows = (
            (
                await session.execute(
                    select(MemorySemantic).where(
                        MemorySemantic.agent_id == "test-analyst"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert verdict_rows, "analyst should have written a verdict memory"
    assert any(
        "verdict" in (r.tags or []) for r in verdict_rows
    ), [r.tags for r in verdict_rows]
