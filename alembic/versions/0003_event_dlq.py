"""events dead-letter tracking (M3)

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-15

Adds:
- events.dead_letter_at — set when bus consumer gives up after max attempts
- events.delivery_attempts — bus-side delivery counter (separate from publish_attempts)
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("dead_letter_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "events",
        sa.Column(
            "delivery_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index(
        "ix_events_dead_letter",
        "events",
        ["dead_letter_at"],
        postgresql_where=sa.text("dead_letter_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_events_dead_letter", table_name="events")
    op.drop_column("events", "delivery_attempts")
    op.drop_column("events", "dead_letter_at")
