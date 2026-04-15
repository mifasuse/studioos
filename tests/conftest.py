"""Pytest fixtures — async DB + seeded test studio."""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from studioos.config import settings

# Force the in-process bus backend for the test suite. The Redis backend is
# smoke-verified via the M1/M2/M3 happy-path tests against a running Redis in
# prod — see verify_prod.sh — but DLQ + reclaim semantics need deterministic
# (non-idle-timer) behavior that only the inproc backend provides.
settings.bus_backend = "inproc"

from studioos.bus import reset_bus  # noqa: E402
from studioos.db import dispose_all, get_engine, session_scope  # noqa: E402
from studioos.models import Base  # noqa: E402
from studioos.studios import seed_all  # noqa: E402

# Import workflows + event schemas + tools so they register
from studioos import workflows  # noqa: F401, E402
from studioos.events import schemas_amz, schemas_test  # noqa: F401, E402
from studioos.tools import builtin as _builtin_tools  # noqa: F401, E402


@pytest_asyncio.fixture(scope="session")
async def db_setup() -> AsyncIterator[None]:
    """Drop + recreate all tables and seed studios at session start."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with session_scope() as session:
        await seed_all(session)

    yield

    await dispose_all()


@pytest_asyncio.fixture
async def db_session(db_setup: None) -> AsyncIterator[None]:
    """Clean transient tables between tests but keep seeded studio config."""
    async with session_scope() as session:
        for table in (
            "approvals",
            "budgets",
            "tool_calls",
            "kpi_snapshots",
            "kpi_targets",
            "memory_semantic",
            "memory_episodic",
            "events",
            "agent_runs",
        ):
            await session.execute(Base.metadata.tables[table].delete())
        # Reset agent_state rows
        from sqlalchemy import update

        from studioos.models import AgentState

        await session.execute(update(AgentState).values(state={}))
        # Re-run the seed loader so any fields tests mutated on agents
        # (schedule_cron, mode, tool_scope, last_scheduled_at) snap back
        # to the yaml-declared values before the next test.
        from studioos.models import Agent

        await session.execute(
            update(Agent).values(last_scheduled_at=None)
        )
        await seed_all(session)
    reset_bus()
    yield
