"""budgets + approvals + tool cost (M5)

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-15

Adds:
- budgets: per-scope period buckets (day/month) with atomic charge
- approvals: pending human decisions that gate a run
- tool_calls.cost_cents: cost attributed to a single tool invocation
- agent_runs.state now accepts 'awaiting_approval' and 'budget_exceeded'
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- budgets ---------------------------------------------------------
    op.create_table(
        "budgets",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "studio_id", sa.Text(), sa.ForeignKey("studios.id"), nullable=True
        ),
        sa.Column(
            "agent_id", sa.Text(), sa.ForeignKey("agents.id"), nullable=True
        ),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("period_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("period_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("limit_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "spent_cents",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "period IN ('day','month')", name="budgets_period_check"
        ),
        sa.CheckConstraint(
            "(studio_id IS NOT NULL) OR (agent_id IS NOT NULL)",
            name="budgets_scope_check",
        ),
        sa.CheckConstraint(
            "spent_cents >= 0 AND limit_cents >= 0",
            name="budgets_nonneg_check",
        ),
    )
    op.create_index(
        "ix_budgets_scope_period",
        "budgets",
        ["agent_id", "studio_id", "period", "period_start"],
        unique=True,
    )

    # --- approvals -------------------------------------------------------
    op.create_table(
        "approvals",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            sa.Text(),
            sa.ForeignKey("agents.id"),
            nullable=False,
        ),
        sa.Column(
            "studio_id", sa.Text(), sa.ForeignKey("studios.id"), nullable=True
        ),
        sa.Column(
            "correlation_id", PG_UUID(as_uuid=True), nullable=True
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column(
            "decided_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "expires_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "state IN ('pending','approved','denied','expired')",
            name="approvals_state_check",
        ),
    )
    op.create_index("ix_approvals_state", "approvals", ["state"])
    op.create_index("ix_approvals_run_id", "approvals", ["run_id"])

    # --- tool_calls.cost_cents ------------------------------------------
    op.add_column(
        "tool_calls",
        sa.Column(
            "cost_cents",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # --- agent_runs: widen state check ----------------------------------
    op.drop_constraint("agent_runs_state_check", "agent_runs", type_="check")
    op.create_check_constraint(
        "agent_runs_state_check",
        "agent_runs",
        "state IN ('pending','running','completed','failed','timed_out',"
        "'cancelled','dead','awaiting_approval','budget_exceeded')",
    )


def downgrade() -> None:
    op.drop_constraint("agent_runs_state_check", "agent_runs", type_="check")
    op.create_check_constraint(
        "agent_runs_state_check",
        "agent_runs",
        "state IN ('pending','running','completed','failed','timed_out','cancelled','dead')",
    )
    op.drop_column("tool_calls", "cost_cents")
    op.drop_index("ix_approvals_run_id", table_name="approvals")
    op.drop_index("ix_approvals_state", table_name="approvals")
    op.drop_table("approvals")
    op.drop_index("ix_budgets_scope_period", table_name="budgets")
    op.drop_table("budgets")
