"""MCP stdio transport — subprocess + JSON-RPC over stdin/stdout.

Spawns a child process and speaks line-delimited JSON-RPC 2.0 over
its stdin/stdout. This matches the stdio half of the official MCP
spec used by tools like `npx @modelcontextprotocol/server-filesystem`
and similar local CLI MCP servers.

Configuration via STUDIOOS_MCP_STDIO_SERVERS, comma-separated, each
entry of the form `prefix=command arg1 arg2`. Example:

    STUDIOOS_MCP_STDIO_SERVERS=fs=npx -y @modelcontextprotocol/server-filesystem /tmp,git=mcp-git

On startup the runtime spawns each server, runs `initialize`, then
`tools/list`, and registers every remote tool as `<prefix>.<name>`
in the local registry. The subprocess is kept alive for the lifetime
of the StudioOS process and shared by all subsequent calls.

This is the bare minimum to be useful — we don't yet handle
notifications, prompts, resources, or progress updates. Adding them
later does not require changes to the registered Tool wrappers.
"""
from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any

from studioos.config import settings
from studioos.logging import get_logger

from .base import Tool, ToolContext, ToolError, ToolResult
from .registry import _REGISTRY

log = get_logger(__name__)


class StdioMcpClient:
    """Minimal async JSON-RPC 2.0 client over a subprocess's stdin/stdout."""

    def __init__(self, command: list[str]) -> None:
        self._command = command
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0

    async def start(self) -> None:
        if self._proc is not None:
            return
        log.info("mcp_stdio.spawning", command=self._command)
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Send the MCP initialize handshake.
        await self._call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "studioos",
                    "version": "0.1.0",
                },
            },
        )
        # Notify server we're ready (no response expected).
        await self._send_notification("notifications/initialized", {})

    async def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._proc.kill()
        finally:
            self._proc = None

    async def _send_notification(
        self, method: str, params: dict[str, Any]
    ) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise ToolError("mcp stdio process not running")
        envelope = {"jsonrpc": "2.0", "method": method, "params": params}
        line = (json.dumps(envelope) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        async with self._lock:
            if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
                raise ToolError("mcp stdio process not running")
            self._next_id += 1
            req_id = self._next_id
            envelope = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }
            self._proc.stdin.write(
                (json.dumps(envelope) + "\n").encode("utf-8")
            )
            await self._proc.stdin.drain()

            # Read until we see a matching response. Servers may emit
            # log/notification lines we should skip (server→client only).
            while True:
                raw = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=30.0
                )
                if not raw:
                    raise ToolError("mcp stdio EOF before response")
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except ValueError:
                    log.warning(
                        "mcp_stdio.junk_line", raw=raw[:200]
                    )
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") != req_id:
                    # notification or unrelated response
                    continue
                if "error" in msg:
                    err = msg["error"] or {}
                    raise ToolError(
                        f"mcp stdio jsonrpc error "
                        f"{err.get('code')}: {err.get('message')}"
                    )
                return msg.get("result")

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._call("tools/list", {})
        return (result or {}).get("tools") or []

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self._call(
            "tools/call", {"name": name, "arguments": arguments}
        )
        return result or {}


_clients: dict[str, StdioMcpClient] = {}


def _make_proxy_handler(client: StdioMcpClient, remote_name: str):
    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = await client.call_tool(remote_name, args)
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


def _parse_servers(spec: str) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for entry in (spec or "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        prefix, command = entry.split("=", 1)
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            log.warning(
                "mcp_stdio.bad_command",
                entry=entry,
                error=str(exc),
            )
            continue
        if not argv:
            continue
        out.append((prefix.strip(), argv))
    return out


async def register_mcp_stdio_servers() -> int:
    """Spawn + register every server in settings.mcp_stdio_servers."""
    spec = (settings.mcp_stdio_servers or "").strip()
    if not spec:
        return 0
    registered = 0
    for prefix, argv in _parse_servers(spec):
        client = StdioMcpClient(argv)
        try:
            await client.start()
            tools = await client.list_tools()
        except ToolError as exc:
            log.warning(
                "mcp_stdio.discover_failed", prefix=prefix, error=str(exc)
            )
            await client.stop()
            continue
        except Exception as exc:
            log.exception(
                "mcp_stdio.spawn_failed", prefix=prefix, error=str(exc)
            )
            await client.stop()
            continue
        _clients[prefix] = client
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
                    f"[mcp-stdio:{prefix}] {t.get('description', '')}"
                )[:500],
                input_schema=input_schema,
                handler=_make_proxy_handler(client, remote_name),
                requires_network=False,
                category=f"mcp-stdio:{prefix}",
                cost_cents=0,
                cost_fn=None,
            )
            _REGISTRY[local_name] = tool
            registered += 1
            log.info(
                "mcp_stdio.registered_remote_tool",
                prefix=prefix,
                remote=remote_name,
                local=local_name,
            )
    return registered


async def shutdown_mcp_stdio_servers() -> None:
    for client in list(_clients.values()):
        await client.stop()
    _clients.clear()
