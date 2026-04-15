"""Minimal MCP (Model Context Protocol) HTTP transport client.

Implements just enough of the MCP spec to discover and call tools
served by a remote MCP HTTP server: a JSON-RPC 2.0 client that
speaks `tools/list` and `tools/call` over POST. No notifications,
no resources, no prompts — those land in later milestones if
needed.

How to register a server (in env):

    STUDIOOS_MCP_HTTP_SERVERS=playwright=https://playwright.example.com/mcp,github=https://gh.example.com/mcp

Each name= prefix becomes the StudioOS tool prefix; tools surface as
`<name>.<remote_tool_name>` in the local registry. On agent startup
the runtime calls `register_mcp_http_servers()`, which contacts each
configured server, lists its tools, and registers a proxy handler
for every one.

This is deliberately a thin MVP. The next iteration adds the stdio
transport (subprocess spawn + line-delimited JSON-RPC) and proper
session management with `initialize` / `initialized` lifecycle.
"""
from __future__ import annotations

import asyncio
import itertools
from typing import Any

import httpx

from studioos.config import settings
from studioos.logging import get_logger

from .base import Tool, ToolContext, ToolError, ToolResult
from .registry import _REGISTRY

log = get_logger(__name__)


_id_counter = itertools.count(1)


async def _jsonrpc(
    base_url: str, method: str, params: dict[str, Any] | None = None
) -> Any:
    """Send a single JSON-RPC 2.0 request and return result. Raises ToolError."""
    payload = {
        "jsonrpc": "2.0",
        "id": next(_id_counter),
        "method": method,
        "params": params or {},
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                base_url.rstrip("/"),
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        raise ToolError(f"mcp http transport error: {exc}") from exc

    if resp.status_code >= 400:
        raise ToolError(f"mcp {resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise ToolError(f"mcp non-json: {exc}") from exc
    if "error" in body:
        err = body["error"] or {}
        raise ToolError(
            f"mcp jsonrpc error {err.get('code')}: {err.get('message')}"
        )
    return body.get("result")


async def list_tools(base_url: str) -> list[dict[str, Any]]:
    """Call MCP `tools/list` and return the raw tool definitions."""
    result = await _jsonrpc(base_url, "tools/list")
    return (result or {}).get("tools") or []


async def call_tool(
    base_url: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Call MCP `tools/call` for one remote tool."""
    result = await _jsonrpc(
        base_url, "tools/call", {"name": name, "arguments": arguments}
    )
    if result is None:
        return {}
    return result


def _make_proxy_handler(base_url: str, remote_name: str):
    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = await call_tool(base_url, remote_name, args)
        # MCP tool results are content blocks; flatten text chunks for the
        # caller and pass through structured data.
        text_parts: list[str] = []
        for block in result.get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                text_parts.append(block["text"])
        return ToolResult(
            data={
                "text": "\n".join(text_parts),
                "blocks": result.get("content") or [],
                "is_error": bool(result.get("isError")),
                "structured": result.get("structuredContent"),
            }
        )

    return handler


def _parse_servers(spec: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for entry in (spec or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            log.warning("mcp.bad_server_spec", entry=entry)
            continue
        name, url = entry.split("=", 1)
        out.append((name.strip(), url.strip()))
    return out


async def register_mcp_http_servers() -> int:
    """Discover + register every server listed in settings.mcp_http_servers.

    Returns the number of remote tools registered (0 on failure or if no
    servers are configured).
    """
    spec = (settings.mcp_http_servers or "").strip()
    if not spec:
        return 0

    registered = 0
    for prefix, base_url in _parse_servers(spec):
        try:
            tools = await list_tools(base_url)
        except ToolError as exc:
            log.warning(
                "mcp.discover_failed",
                prefix=prefix,
                base_url=base_url,
                error=str(exc),
            )
            continue
        for t in tools:
            remote_name = t.get("name")
            if not remote_name:
                continue
            local_name = f"{prefix}.{remote_name}"
            input_schema = t.get("inputSchema") or {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            }
            tool = Tool(
                name=local_name,
                description=(
                    f"[mcp:{prefix}] {t.get('description', '')}"
                )[:500],
                input_schema=input_schema,
                handler=_make_proxy_handler(base_url, remote_name),
                requires_network=True,
                category=f"mcp:{prefix}",
                cost_cents=0,
                cost_fn=None,
            )
            _REGISTRY[local_name] = tool
            registered += 1
            log.info(
                "mcp.registered_remote_tool",
                prefix=prefix,
                remote=remote_name,
                local=local_name,
            )
    return registered


def register_mcp_http_servers_sync() -> int:
    """Convenience for sync startup paths (CLI, lifespan)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return 0  # caller is async, should call the async version
    except RuntimeError:
        pass
    return asyncio.run(register_mcp_http_servers())
