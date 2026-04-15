"""memory tables + KPI tables (M2)

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-15

Adds:
- pgvector extension
- memory_semantic (vector embeddings)
- memory_episodic (daily journals)
- memory_procedural (versioned playbooks)
- kpi_targets (per-agent or per-studio targets)
- kpi_snapshots (time-series of current values)
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 1536  # OpenAI text-embedding-3-small / fake embedder


def upgrade() -> None:
    # pgvector extension (image is pgvector/pgvector:pg16)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- memory_semantic --------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE memory_semantic (
            id              UUID PRIMARY KEY,
            agent_id        TEXT REFERENCES agents(id),
            studio_id       TEXT REFERENCES studios(id),
            content         TEXT NOT NULL,
            embedding       vector({EMBEDDING_DIM}),
            tags            TEXT[],
            importance      REAL NOT NULL DEFAULT 0.5,
            source_run_id   UUID REFERENCES agent_runs(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            accessed_at     TIMESTAMPTZ,
            decay_after     TIMESTAMPTZ
        )
        """
    )
    op.create_index(
        "idx_memory_semantic_agent",
        "memory_semantic",
        ["agent_id"],
    )
    op.create_index(
        "idx_memory_semantic_studio",
        "memory_semantic",
        ["studio_id"],
    )
    # ivfflat needs data; create a simpler hnsw or skip until populated.
    # Use a regular index for now; vector index will be added in later milestone.

    # --- memory_episodic --------------------------------------------------
    op.create_table(
        "memory_episodic",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_id",
            sa.Text(),
            sa.ForeignKey("agents.id"),
            nullable=False,
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("events_count", sa.Integer(), server_default="0"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("agent_id", "date", name="uq_episodic_agent_date"),
    )

    # --- memory_procedural ------------------------------------------------
    op.create_table(
        "memory_procedural",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("studio_id", sa.Text(), sa.ForeignKey("studios.id")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("change_summary", sa.Text()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.PrimaryKeyConstraint("id", "version"),
    )

    # --- kpi_targets ------------------------------------------------------
    op.create_table(
        "kpi_targets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("studio_id", sa.Text(), sa.ForeignKey("studios.id")),
        sa.Column("agent_id", sa.Text(), sa.ForeignKey("agents.id")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text()),
        sa.Column("target_value", sa.Numeric(20, 6), nullable=False),
        sa.Column(
            "direction",
            sa.Text(),
            nullable=False,
            server_default="higher_better",
        ),
        sa.Column("unit", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "direction IN ('higher_better','lower_better','range')",
            name="kpi_targets_direction_check",
        ),
        sa.UniqueConstraint(
            "studio_id", "agent_id", "name", name="uq_kpi_target_scope"
        ),
    )

    # --- kpi_snapshots ----------------------------------------------------
    op.create_table(
        "kpi_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("studio_id", sa.Text(), sa.ForeignKey("studios.id")),
        sa.Column("agent_id", sa.Text(), sa.ForeignKey("agents.id")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(20, 6), nullable=False),
        sa.Column(
            "source_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id"),
        ),
        sa.Column(
            "snapshot_metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "idx_kpi_snapshots_lookup",
        "kpi_snapshots",
        [
            "studio_id",
            "agent_id",
            "name",
            sa.text("recorded_at DESC"),
        ],
    )


def downgrade() -> None:
    op.drop_index("idx_kpi_snapshots_lookup", table_name="kpi_snapshots")
    op.drop_table("kpi_snapshots")
    op.drop_table("kpi_targets")
    op.drop_table("memory_procedural")
    op.drop_table("memory_episodic")
    op.drop_index("idx_memory_semantic_studio", table_name="memory_semantic")
    op.drop_index("idx_memory_semantic_agent", table_name="memory_semantic")
    op.execute("DROP TABLE memory_semantic")
    op.execute("DROP EXTENSION IF EXISTS vector")
