"""Convenience: call tools from inside a workflow node using its state dict."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from .base import ToolContext
from .invoker import invoke_tool


def context_from_state(state: dict[str, Any]) -> ToolContext:
    def _uuid(val: Any) -> UUID | None:
        if val is None:
            return None
        if isinstance(val, UUID):
            return val
        return UUID(str(val))

    extra: dict[str, Any] = {}
    goals = state.get("goals") or {}
    if goals.get("llm_provider"):
        extra["llm_provider"] = goals["llm_provider"]

    return ToolContext(
        agent_id=state.get("agent_id"),
        run_id=_uuid(state.get("run_id")),
        correlation_id=_uuid(state.get("correlation_id")),
        studio_id=state.get("studio_id"),
        extra=extra,
    )


async def invoke_from_state(
    state: dict[str, Any],
    name: str,
    args: dict[str, Any],
    *,
    enforce_allow_list: bool = True,
) -> dict[str, Any]:
    ctx = context_from_state(state)
    return await invoke_tool(
        name, args, ctx, enforce_allow_list=enforce_allow_list
    )
