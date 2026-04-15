"""Pytest fixtures — async DB + seeded test studio."""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from studioos.db import dispose_all, get_engine, session_scope
from studioos.models import Base
from studioos.studios import seed_all

# Import workflows + event schemas so they register
from studioos import workflows  # noqa: F401
from studioos.events import schemas_test  # noqa: F401


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
    yield
