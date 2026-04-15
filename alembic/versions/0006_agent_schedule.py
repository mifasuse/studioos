"""agent scheduling (M7)

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-15

Adds `schedule_cron` (human-readable spec, starts with "@every Nm") and
`last_scheduled_at` to agents so the runtime scheduler loop can create
pending runs on a cadence without external cron.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("schedule_cron", sa.Text(), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column(
            "last_scheduled_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
    )
    op.create_index(
        "ix_agents_schedule",
        "agents",
        ["schedule_cron"],
        postgresql_where=sa.text("schedule_cron IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_agents_schedule", table_name="agents")
    op.drop_column("agents", "last_scheduled_at")
    op.drop_column("agents", "schedule_cron")
