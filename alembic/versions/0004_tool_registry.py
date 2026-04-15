"""tool call audit table (M4)

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-15
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_calls",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column(
            "agent_id",
            sa.Text(),
            sa.ForeignKey("agents.id"),
            nullable=True,
        ),
        sa.Column(
            "run_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id"),
            nullable=True,
        ),
        sa.Column(
            "correlation_id",
            PG_UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("args", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "called_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "status IN ('ok','error','denied','invalid_args')",
            name="tool_calls_status_check",
        ),
    )
    op.create_index(
        "ix_tool_calls_called_at", "tool_calls", ["called_at"]
    )
    op.create_index(
        "ix_tool_calls_tool_name_called_at",
        "tool_calls",
        ["tool_name", "called_at"],
    )
    op.create_index(
        "ix_tool_calls_run_id", "tool_calls", ["run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_tool_calls_run_id", table_name="tool_calls")
    op.drop_index("ix_tool_calls_tool_name_called_at", table_name="tool_calls")
    op.drop_index("ix_tool_calls_called_at", table_name="tool_calls")
    op.drop_table("tool_calls")
