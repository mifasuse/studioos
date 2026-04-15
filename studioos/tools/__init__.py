"""Tool registry + invocation layer.

Tools are named capabilities that workflows can call. Each tool has:
  - a unique name (e.g. "http.get_json", "memory.search")
  - a human description
  - a JSON-schema describing its input
  - an async handler: `async def(args: dict, ctx: ToolContext) -> dict`

Invocation is audited: every call inserts a row into `tool_calls` with
args, result, status, latency. Per-agent allow-listing is enforced via
`agents.config.tools = [...]` — omitted means "no tools".

MCP compatibility: this abstraction mirrors the MCP tool schema closely
enough that an MCP-backed tool is a drop-in subclass of the handler —
see `studioos/tools/mcp.py` (future).
"""
from __future__ import annotations

from .base import Tool, ToolContext, ToolError, ToolResult
from .invoker import invoke_tool
from .registry import get_tool, list_tools, register_tool
from .workflow_helper import context_from_state, invoke_from_state

__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "context_from_state",
    "get_tool",
    "invoke_from_state",
    "invoke_tool",
    "list_tools",
    "register_tool",
]
