"""Tool invoker — validates, allow-list-checks, executes, audits.

Public entrypoint: `invoke_tool(name, args, ctx)`. Always returns a dict
(never raises); failures are recorded with a non-ok status. The caller
decides whether to surface the error to the workflow or swallow it.
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select

from studioos.budget import charge as charge_budget
from studioos.db import session_scope
from studioos.logging import get_logger
from studioos.models import Agent, ToolCall

from .base import ToolContext, ToolError, ToolResult
from .registry import get_tool
from .validate import SchemaError, validate

log = get_logger(__name__)


async def _allowed_tools_for_agent(agent_id: str | None) -> set[str] | None:
    """Return the allow-list for `agent_id`, or None if no enforcement.

    Reads `agents.tool_scope` (ARRAY[text]). A NULL column means "no
    explicit scope configured" and we fall back to deny-by-default with
    an empty set. A known unknown `agent_id` returns None so callers
    (direct invocations from tests/CLI) aren't blocked.
    """
    if agent_id is None:
        return None
    async with session_scope() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.id == agent_id))
        ).scalar_one_or_none()
        if agent is None:
            return None
        if agent.tool_scope is None:
            return set()
        return set(agent.tool_scope)


async def _record(
    *,
    tool_name: str,
    ctx: ToolContext,
    args: dict[str, Any],
    result: dict[str, Any] | None,
    error: str | None,
    status: str,
    duration_ms: int,
    cost_cents: int = 0,
) -> None:
    async with session_scope() as session:
        session.add(
            ToolCall(
                tool_name=tool_name,
                agent_id=ctx.agent_id,
                run_id=ctx.run_id,
                correlation_id=ctx.correlation_id,
                args=args,
                result=result,
                error=error,
                status=status,
                duration_ms=duration_ms,
                cost_cents=cost_cents,
            )
        )
        if cost_cents > 0 and status == "ok":
            await charge_budget(
                session,
                agent_id=ctx.agent_id,
                studio_id=ctx.studio_id,
                cents=cost_cents,
            )


async def invoke_tool(
    name: str,
    args: dict[str, Any],
    ctx: ToolContext,
    *,
    enforce_allow_list: bool = True,
) -> dict[str, Any]:
    """Call a tool and audit the outcome. Returns a result envelope.

    Envelope shape:
        {"status": "ok", "data": {...}}                   on success
        {"status": "error", "error": "<msg>"}             on handler failure
        {"status": "denied", "error": "<msg>"}            on allow-list deny
        {"status": "invalid_args", "error": "<msg>"}      on schema violation
    """
    tool = get_tool(name)
    if tool is None:
        msg = f"unknown tool: {name}"
        await _record(
            tool_name=name, ctx=ctx, args=args, result=None,
            error=msg, status="error", duration_ms=0,
        )
        return {"status": "error", "error": msg}

    if enforce_allow_list and ctx.agent_id is not None:
        allowed = await _allowed_tools_for_agent(ctx.agent_id)
        if allowed is not None and name not in allowed:
            msg = f"agent {ctx.agent_id} not allowed to call {name}"
            log.warning("tools.denied", tool=name, agent=ctx.agent_id)
            await _record(
                tool_name=name, ctx=ctx, args=args, result=None,
                error=msg, status="denied", duration_ms=0,
            )
            return {"status": "denied", "error": msg}

    try:
        validate(args, tool.input_schema)
    except SchemaError as exc:
        msg = str(exc)
        log.warning("tools.invalid_args", tool=name, error=msg)
        await _record(
            tool_name=name, ctx=ctx, args=args, result=None,
            error=msg, status="invalid_args", duration_ms=0,
        )
        return {"status": "invalid_args", "error": msg}

    start = time.monotonic()
    try:
        raw = await tool.handler(args, ctx)
    except ToolError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        log.info("tools.error", tool=name, error=str(exc), duration_ms=duration_ms)
        await _record(
            tool_name=name, ctx=ctx, args=args, result=None,
            error=str(exc), status="error", duration_ms=duration_ms,
        )
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        log.exception("tools.handler_crashed", tool=name)
        await _record(
            tool_name=name, ctx=ctx, args=args, result=None,
            error=f"{type(exc).__name__}: {exc}",
            status="error",
            duration_ms=duration_ms,
        )
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    duration_ms = int((time.monotonic() - start) * 1000)
    data = raw.data if isinstance(raw, ToolResult) else (raw or {})
    if not isinstance(data, dict):
        msg = f"tool {name} returned {type(data).__name__}, expected dict"
        await _record(
            tool_name=name, ctx=ctx, args=args, result=None,
            error=msg, status="error", duration_ms=duration_ms,
        )
        return {"status": "error", "error": msg}

    cost = tool.cost_cents
    if tool.cost_fn is not None:
        try:
            cost = int(tool.cost_fn(args, data))
        except Exception:
            log.exception("tools.cost_fn_failed", tool=name)
            cost = tool.cost_cents

    log.info("tools.ok", tool=name, duration_ms=duration_ms, cost_cents=cost)
    await _record(
        tool_name=name, ctx=ctx, args=args, result=data,
        error=None, status="ok", duration_ms=duration_ms,
        cost_cents=cost,
    )
    return {"status": "ok", "data": data, "cost_cents": cost}
