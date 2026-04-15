"""Tool primitives: Tool, ToolContext, ToolResult, ToolError."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


class ToolError(Exception):
    """Raised by a tool handler to signal a clean, audited failure."""


@dataclass(frozen=True)
class ToolContext:
    """Per-call execution context handed to every tool handler."""

    agent_id: str | None
    run_id: UUID | None
    correlation_id: UUID | None
    studio_id: str | None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """What a tool returns. `data` is JSON-serializable."""

    data: dict[str, Any]


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult | dict[str, Any]]]


CostFn = Callable[[dict[str, Any], ToolResult | dict[str, Any]], int]


@dataclass(frozen=True)
class Tool:
    """A registered tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    requires_network: bool = False
    category: str = "general"
    # Static cost in integer cents per successful call. For variable cost
    # tools (LLM, paid APIs) set `cost_fn` to compute from args + result.
    cost_cents: int = 0
    cost_fn: CostFn | None = None
