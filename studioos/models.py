"""SQLAlchemy models — mirrors migration 0001."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# studios
# ---------------------------------------------------------------------------
class Studio(Base):
    __tablename__ = "studios"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','paused','retired')",
            name="studios_status_check",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    mission: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="active"
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    studio_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    retired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


# ---------------------------------------------------------------------------
# agent_templates
# ---------------------------------------------------------------------------
class AgentTemplate(Base):
    __tablename__ = "agent_templates"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    workflow_ref: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    required_tools: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    default_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    deprecated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------
class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["agent_templates.id", "agent_templates.version"],
            name="fk_agents_template",
        ),
        CheckConstraint(
            "mode IN ('normal','degraded','paused','emergency')",
            name="agents_mode_check",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    studio_id: Mapped[str] = mapped_column(
        Text, ForeignKey("studios.id"), nullable=False
    )
    template_id: Mapped[str] = mapped_column(Text, nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    slack_handle: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="normal"
    )
    heartbeat_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    goals: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tool_scope: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    budget_tier: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    retired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


# ---------------------------------------------------------------------------
# agent_state
# ---------------------------------------------------------------------------
class AgentState(Base):
    __tablename__ = "agent_state"

    agent_id: Mapped[str] = mapped_column(
        Text, ForeignKey("agents.id"), primary_key=True
    )
    state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    state_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    last_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    last_run_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )


# ---------------------------------------------------------------------------
# agent_runs
# ---------------------------------------------------------------------------
class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','running','completed','failed','timed_out','cancelled','dead')",
            name="agent_runs_state_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    agent_id: Mapped[str] = mapped_column(
        Text, ForeignKey("agents.id"), nullable=False
    )
    studio_id: Mapped[str] = mapped_column(
        Text, ForeignKey("studios.id"), nullable=False
    )
    correlation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_ref: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending"
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="50")
    workflow_version: Mapped[str | None] = mapped_column(Text)
    input_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    workflow_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    parent_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_runs.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    tokens_used: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default="0"
    )


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------
class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_events_idempotency"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False)
    studio_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("studios.id")
    )
    correlation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    causation_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str | None] = mapped_column(Text)
    source_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_runs.id")
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    publish_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )


# ---------------------------------------------------------------------------
# subscriptions
# ---------------------------------------------------------------------------
class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    subscriber_type: Mapped[str] = mapped_column(Text, nullable=False)
    subscriber_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    filter: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    action: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="wake_agent"
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="50"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
