"""Built-in tool implementations — registered on import."""
from __future__ import annotations

from typing import Any

import httpx

from studioos.budget import current_budget
from studioos.db import session_scope
from studioos.kpi.store import get_current_state
from studioos.memory.store import search_memory

from .base import ToolContext, ToolError, ToolResult
from .registry import register_tool


@register_tool(
    "test.echo",
    description="Returns its input verbatim — used to exercise the tool pipeline.",
    input_schema={
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    },
    category="test",
    cost_cents=1,
)
async def test_echo(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return ToolResult(data={"echo": args["message"]})


@register_tool(
    "http.get_json",
    description="HTTP GET a URL and return the parsed JSON body. Network required.",
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "timeout_seconds": {"type": "number"},
            "headers": {"type": "object"},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="network",
    cost_cents=2,
)
async def http_get_json(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    url = args["url"]
    timeout = float(args.get("timeout_seconds", 10))
    headers = args.get("headers") or {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ToolError(f"http error: {exc}") from exc
    if resp.status_code >= 400:
        raise ToolError(f"http {resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise ToolError(f"non-json response: {exc}") from exc
    return ToolResult(
        data={
            "status_code": resp.status_code,
            "body": body,
        }
    )


@register_tool(
    "memory.search",
    description="Semantic search over the agent's memory.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    category="memory",
)
async def memory_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    query = args["query"]
    limit = int(args.get("limit", 5))
    async with session_scope() as session:
        results = await search_memory(
            session,
            query=query,
            agent_id=ctx.agent_id,
            studio_id=ctx.studio_id,
            limit=limit,
        )
    return ToolResult(
        data={
            "results": [
                {
                    "id": str(r.id),
                    "content": r.content,
                    "tags": r.tags or [],
                    "score": round(1 - r.distance, 4),
                }
                for r in results
            ]
        }
    )


@register_tool(
    "budget.check",
    description="Return current budget buckets (remaining cents) for the agent's scope.",
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    category="budget",
)
async def budget_check(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    async with session_scope() as session:
        views = await current_budget(
            session, agent_id=ctx.agent_id, studio_id=ctx.studio_id
        )
    return ToolResult(
        data={
            "budgets": [
                {
                    "scope": v.scope,
                    "period": v.period,
                    "limit_cents": v.limit_cents,
                    "spent_cents": v.spent_cents,
                    "remaining_cents": v.remaining_cents,
                    "over": v.over,
                }
                for v in views
            ]
        }
    )


@register_tool(
    "kpi.read",
    description="Read current KPI state for the agent's scope.",
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    category="kpi",
)
async def kpi_read(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    async with session_scope() as session:
        states = await get_current_state(
            session, studio_id=ctx.studio_id, agent_id=ctx.agent_id
        )
    return ToolResult(
        data={
            "kpis": [
                {
                    "name": s.name,
                    "target": float(s.target) if s.target is not None else None,
                    "current": float(s.current) if s.current is not None else None,
                    "direction": s.direction,
                    "reached": s.gap.reached if s.gap else None,
                }
                for s in states
            ]
        }
    )
