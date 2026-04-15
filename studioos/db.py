"""Async SQLAlchemy engine + session factory.

Engine and session factory are lazy-initialised per running event loop. This
avoids the "Future attached to a different loop" trap when pytest-asyncio
spawns its own loops, while still keeping a single shared engine in normal
runtime.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from studioos.config import settings

# Per-loop engine cache: avoids cross-loop connection sharing in tests.
_engines: dict[int, AsyncEngine] = {}
_factories: dict[int, async_sessionmaker[AsyncSession]] = {}


def _loop_id() -> int:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return 0
    return id(loop)


def get_engine() -> AsyncEngine:
    key = _loop_id()
    if key not in _engines:
        _engines[key] = create_async_engine(
            settings.database_url,
            echo=False,
            poolclass=NullPool,
            pool_pre_ping=True,
        )
    return _engines[key]


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    key = _loop_id()
    if key not in _factories:
        _factories[key] = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _factories[key]


# Backwards-compat aliases for callers expecting module-level names
def engine() -> AsyncEngine:  # type: ignore[no-redef]
    return get_engine()


def SessionLocal() -> AsyncSession:  # type: ignore[no-redef]
    return get_session_factory()()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context manager that commits on success, rolls back on error."""
    session = get_session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_all() -> None:
    """Dispose every cached engine — used by test teardown."""
    for engine_obj in list(_engines.values()):
        await engine_obj.dispose()
    _engines.clear()
    _factories.clear()
