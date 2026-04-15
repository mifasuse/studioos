"""Milestone 4 — tool registry + invoker + audit + allow-list + schema."""
from __future__ import annotations

import asyncio
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import desc, select

from studioos.db import session_scope
from studioos.models import Agent, ToolCall
from studioos.runtime.consumer import drain_once
from studioos.runtime.dispatcher import dispatch_once
from studioos.runtime.outbox import publish_batch
from studioos.runtime.triggers import create_pending_run
from studioos.tools import (
    ToolContext,
    get_tool,
    invoke_tool,
    list_tools,
)


async def _drain(max_iters: int = 15) -> None:
    for _ in range(max_iters):
        ran = await dispatch_once()
        published = await publish_batch()
        consumed = await drain_once()
        if ran is None and published == 0 and consumed == 0:
            return
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_registry_has_builtins(db_session) -> None:
    names = {t.name for t in list_tools()}
    assert {"test.echo", "http.get_json", "memory.search", "kpi.read"} <= names
    echo = get_tool("test.echo")
    assert echo is not None
    assert "message" in echo.input_schema["properties"]


@pytest.mark.asyncio
async def test_invoke_ok_writes_audit_row(db_session) -> None:
    ctx = ToolContext(
        agent_id="test-scout",
        run_id=None,
        correlation_id=uuid4(),
        studio_id="test",
    )
    result = await invoke_tool("test.echo", {"message": "hi"}, ctx)
    assert result["status"] == "ok"
    assert result["data"] == {"echo": "hi"}

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(ToolCall)
                    .where(ToolCall.tool_name == "test.echo")
                    .order_by(desc(ToolCall.called_at))
                )
            )
            .scalars()
            .all()
        )
    assert rows, "audit row not written"
    assert rows[0].status == "ok"
    assert rows[0].result == {"echo": "hi"}
    assert rows[0].agent_id == "test-scout"
    assert rows[0].error is None


@pytest.mark.asyncio
async def test_invoke_invalid_args(db_session) -> None:
    ctx = ToolContext(
        agent_id="test-scout",
        run_id=None,
        correlation_id=None,
        studio_id="test",
    )
    result = await invoke_tool("test.echo", {}, ctx)
    assert result["status"] == "invalid_args"
    assert "required" in result["error"]

    async with session_scope() as session:
        row = (
            await session.execute(
                select(ToolCall)
                .where(ToolCall.tool_name == "test.echo")
                .order_by(desc(ToolCall.called_at))
                .limit(1)
            )
        ).scalar_one()
    assert row.status == "invalid_args"


@pytest.mark.asyncio
async def test_allow_list_denies_unlisted_tool(db_session) -> None:
    # test-scout is seeded with tool_scope = [test.echo, memory.search].
    # kpi.read is NOT in that list → must be denied.
    ctx = ToolContext(
        agent_id="test-scout",
        run_id=None,
        correlation_id=None,
        studio_id="test",
    )
    result = await invoke_tool("kpi.read", {}, ctx)
    assert result["status"] == "denied"

    async with session_scope() as session:
        row = (
            await session.execute(
                select(ToolCall)
                .where(ToolCall.tool_name == "kpi.read")
                .where(ToolCall.status == "denied")
            )
        ).scalar_one()
    assert row.agent_id == "test-scout"


@pytest.mark.asyncio
async def test_scout_run_uses_tool_end_to_end(db_session) -> None:
    with patch("studioos.workflows.scout_test.random", return_value=0.85):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="test-scout",
                trigger_type="manual",
                trigger_ref="m4-tools",
            )
        await _drain()

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(ToolCall)
                    .where(ToolCall.tool_name == "test.echo")
                    .where(ToolCall.agent_id == "test-scout")
                )
            )
            .scalars()
            .all()
        )
    assert rows, "scout run should have produced a tool_call audit row"
    assert rows[0].status == "ok"
    assert "scout-scan:" in rows[0].args["message"]
    assert rows[0].result and "echo" in rows[0].result
