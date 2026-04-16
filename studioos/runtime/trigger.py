"""Programmatic run trigger — used by webhook handlers."""
from __future__ import annotations

from typing import Any

from studioos.db import session_scope
from studioos.runtime.triggers import create_pending_run


async def trigger_run(
    *,
    agent_id: str,
    trigger_type: str = "api",
    trigger_ref: str = "",
    input_data: dict[str, Any] | None = None,
    priority: int = 30,
    workflow_override: str | None = None,
) -> str:
    """Create a pending run for an agent. Returns the run_id.

    If workflow_override is set, the runner will use that workflow
    instead of the agent's default template (e.g. "react_conversation"
    for Slack mentions).
    """
    if workflow_override and input_data is not None:
        input_data = {**input_data, "_workflow_override": workflow_override}
    elif workflow_override:
        input_data = {"_workflow_override": workflow_override}
    async with session_scope() as session:
        run = await create_pending_run(
            session,
            agent_id=agent_id,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref or None,
            priority=priority,
            input_snapshot=input_data,
        )
    return str(run.id)
