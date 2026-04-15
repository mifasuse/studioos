"""core schema — studios, agents, runs, events, subscriptions

Revision ID: 0001
Revises:
Create Date: 2026-04-15

Milestone 1 scope:
- studios
- agent_templates
- agents
- agent_state
- agent_runs
- events
- subscriptions

Not in this migration (later milestones):
- memory_* tables
- budget_usage, budget_limits
- approvals
- dead_events
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- studios ----------------------------------------------------------
    op.create_table(
        "studios",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("mission", sa.Text()),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="active",
        ),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("retired_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(
            "status IN ('active','paused','retired')",
            name="studios_status_check",
        ),
    )

    # --- agent_templates --------------------------------------------------
    op.create_table(
        "agent_templates",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("workflow_ref", sa.Text(), nullable=False),
        sa.Column("input_schema", postgresql.JSONB()),
        sa.Column("output_schema", postgresql.JSONB()),
        sa.Column("required_tools", postgresql.ARRAY(sa.Text())),
        sa.Column(
            "default_config",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("deprecated_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", "version"),
    )

    # --- agents -----------------------------------------------------------
    op.create_table(
        "agents",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "studio_id",
            sa.Text(),
            sa.ForeignKey("studios.id"),
            nullable=False,
        ),
        sa.Column("template_id", sa.Text(), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.Text()),
        sa.Column("slack_handle", sa.Text()),
        sa.Column(
            "mode",
            sa.Text(),
            nullable=False,
            server_default="normal",
        ),
        sa.Column("heartbeat_config", postgresql.JSONB()),
        sa.Column("goals", postgresql.JSONB()),
        sa.Column("tool_scope", postgresql.ARRAY(sa.Text())),
        sa.Column("budget_tier", sa.Text()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("retired_at", sa.TIMESTAMP(timezone=True)),
        sa.ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["agent_templates.id", "agent_templates.version"],
            name="fk_agents_template",
        ),
        sa.CheckConstraint(
            "mode IN ('normal','degraded','paused','emergency')",
            name="agents_mode_check",
        ),
    )
    op.create_index("idx_agents_studio", "agents", ["studio_id"])

    # --- agent_state ------------------------------------------------------
    op.create_table(
        "agent_state",
        sa.Column(
            "agent_id",
            sa.Text(),
            sa.ForeignKey("agents.id"),
            primary_key=True,
        ),
        sa.Column("state", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "state_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("last_run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # --- agent_runs -------------------------------------------------------
    op.create_table(
        "agent_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "agent_id",
            sa.Text(),
            sa.ForeignKey("agents.id"),
            nullable=False,
        ),
        sa.Column(
            "studio_id",
            sa.Text(),
            sa.ForeignKey("studios.id"),
            nullable=False,
        ),
        sa.Column(
            "correlation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("trigger_type", sa.Text(), nullable=False),
        sa.Column("trigger_ref", sa.Text()),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default="50",
        ),
        sa.Column("workflow_version", sa.Text()),
        sa.Column("input_snapshot", postgresql.JSONB()),
        sa.Column("output_snapshot", postgresql.JSONB()),
        sa.Column("workflow_state", postgresql.JSONB()),
        sa.Column("error", postgresql.JSONB()),
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "parent_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "tokens_used",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "cost_usd",
            sa.Numeric(10, 4),
            nullable=False,
            server_default="0",
        ),
        sa.CheckConstraint(
            "state IN ('pending','running','completed','failed','timed_out','cancelled','dead')",
            name="agent_runs_state_check",
        ),
    )
    op.create_index(
        "idx_runs_agent_time",
        "agent_runs",
        ["agent_id", sa.text("created_at DESC")],
    )
    op.create_index("idx_runs_correlation", "agent_runs", ["correlation_id"])
    op.create_index(
        "idx_runs_state_pending",
        "agent_runs",
        ["state", "priority", "created_at"],
        postgresql_where=sa.text("state IN ('pending','running')"),
    )

    # --- events -----------------------------------------------------------
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("studio_id", sa.Text(), sa.ForeignKey("studios.id")),
        sa.Column(
            "correlation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text()),
        sa.Column(
            "source_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id"),
        ),
        sa.Column("idempotency_key", sa.Text()),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "occurred_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "publish_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_events_idempotency"),
    )
    op.create_index(
        "idx_events_unpublished",
        "events",
        ["recorded_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_index("idx_events_correlation", "events", ["correlation_id"])
    op.create_index(
        "idx_events_type_time",
        "events",
        ["event_type", sa.text("occurred_at DESC")],
    )

    # --- subscriptions ----------------------------------------------------
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subscriber_type", sa.Text(), nullable=False),
        sa.Column("subscriber_id", sa.Text(), nullable=False),
        sa.Column("event_pattern", sa.Text(), nullable=False),
        sa.Column("filter", postgresql.JSONB()),
        sa.Column("action", sa.Text(), nullable=False, server_default="wake_agent"),
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default="50",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "idx_subscriptions_pattern",
        "subscriptions",
        ["event_pattern"],
    )


def downgrade() -> None:
    op.drop_index("idx_subscriptions_pattern", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index("idx_events_type_time", table_name="events")
    op.drop_index("idx_events_correlation", table_name="events")
    op.drop_index("idx_events_unpublished", table_name="events")
    op.drop_table("events")
    op.drop_index("idx_runs_state_pending", table_name="agent_runs")
    op.drop_index("idx_runs_correlation", table_name="agent_runs")
    op.drop_index("idx_runs_agent_time", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_table("agent_state")
    op.drop_index("idx_agents_studio", table_name="agents")
    op.drop_table("agents")
    op.drop_table("agent_templates")
    op.drop_table("studios")
