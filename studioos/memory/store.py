"""Memory store — write semantic memories, search via cosine similarity."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date as date_cls, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from studioos.logging import get_logger
from studioos.memory.embedder import get_embedder
from studioos.models import MemoryEpisodic, MemorySemantic

log = get_logger(__name__)


@dataclass
class MemorySearchResult:
    id: UUID
    content: str
    importance: float
    tags: list[str] | None
    distance: float
    created_at: datetime
    source_run_id: UUID | None


async def record_memory(
    session: AsyncSession,
    *,
    content: str,
    agent_id: str | None = None,
    studio_id: str | None = None,
    tags: list[str] | None = None,
    importance: float = 0.5,
    source_run_id: UUID | None = None,
) -> UUID:
    """Embed + persist a semantic memory. Caller controls commit."""
    embedder = get_embedder()
    embedding = await embedder.embed(content)

    row = MemorySemantic(
        id=uuid4(),
        agent_id=agent_id,
        studio_id=studio_id,
        content=content,
        embedding=embedding,
        tags=tags,
        importance=importance,
        source_run_id=source_run_id,
    )
    session.add(row)
    await session.flush()
    log.info(
        "memory.recorded",
        memory_id=str(row.id),
        agent_id=agent_id,
        studio_id=studio_id,
        importance=importance,
        tags=tags,
        content_preview=content[:80],
    )
    return row.id


async def search_memory(
    session: AsyncSession,
    *,
    query: str,
    agent_id: str | None = None,
    studio_id: str | None = None,
    tags: list[str] | None = None,
    limit: int = 5,
) -> list[MemorySearchResult]:
    """Semantic search using pgvector cosine distance."""
    embedder = get_embedder()
    qvec = await embedder.embed(query)

    distance_expr = MemorySemantic.embedding.cosine_distance(qvec).label("distance")

    stmt = select(MemorySemantic, distance_expr).order_by(distance_expr).limit(limit)
    if agent_id is not None:
        stmt = stmt.where(MemorySemantic.agent_id == agent_id)
    if studio_id is not None:
        stmt = stmt.where(MemorySemantic.studio_id == studio_id)
    if tags:
        stmt = stmt.where(MemorySemantic.tags.op("&&")(tags))

    rows = (await session.execute(stmt)).all()
    if not rows:
        return []

    # Update access timestamps (LRU bookkeeping)
    ids = [r[0].id for r in rows]
    await session.execute(
        update(MemorySemantic)
        .where(MemorySemantic.id.in_(ids))
        .values(accessed_at=datetime.now(UTC))
    )

    results = [
        MemorySearchResult(
            id=mem.id,
            content=mem.content,
            importance=float(mem.importance),
            tags=mem.tags,
            distance=float(distance),
            created_at=mem.created_at,
            source_run_id=mem.source_run_id,
        )
        for mem, distance in rows
    ]
    return results


async def record_episodic(
    session: AsyncSession,
    *,
    agent_id: str,
    journal_date: date_cls | None = None,
    content: str | None = None,
    summary: str | None = None,
    events_count: int | None = None,
) -> UUID:
    """Upsert today's episodic journal entry for the agent."""
    journal_date = journal_date or datetime.now(UTC).date()

    existing = (
        await session.execute(
            select(MemoryEpisodic).where(
                MemoryEpisodic.agent_id == agent_id,
                MemoryEpisodic.date == journal_date,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        row = MemoryEpisodic(
            id=uuid4(),
            agent_id=agent_id,
            date=journal_date,
            content=content,
            summary=summary,
            events_count=events_count or 0,
        )
        session.add(row)
        await session.flush()
        return row.id

    if content is not None:
        existing.content = content
    if summary is not None:
        existing.summary = summary
    if events_count is not None:
        existing.events_count = events_count
    return existing.id
