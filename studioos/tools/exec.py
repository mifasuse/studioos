"""Narrow read-only exec tools for the amz-dev agent.

Three deliberately-narrow shell wrappers:
  exec.git_status            — `git -C <repo> status --porcelain`
  exec.git_log               — `git -C <repo> log -n N --oneline`
  exec.docker_compose_ps     — `docker compose -f <compose_file> ps --format json`

No arbitrary command execution. Each tool has a fixed argv shape and
validates the repo path against a configured allow-list
(STUDIOOS_DEV_REPO_ALLOWLIST). The amz-dev agent uses these to
report on the engineering state of the supporting projects without
touching the broader exec tool surface OpenClaw exposes.

Future write tools (git pull, docker compose up, alembic upgrade)
land later behind explicit approval gates.
"""
from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any

from studioos.config import settings
from studioos.logging import get_logger

from .base import ToolContext, ToolError, ToolResult
from .registry import register_tool

log = get_logger(__name__)


def _allowlisted_repos() -> list[str]:
    spec = (settings.dev_repo_allowlist or "").strip()
    if not spec:
        return []
    return [s.strip() for s in spec.split(",") if s.strip()]


def _check_repo(path: str) -> str:
    """Resolve + validate a repo path against the allow-list."""
    allowed = _allowlisted_repos()
    if not allowed:
        raise ToolError(
            "STUDIOOS_DEV_REPO_ALLOWLIST is empty — no repos approved for dev tools"
        )
    resolved = str(Path(path).resolve())
    for a in allowed:
        a_resolved = str(Path(a).resolve())
        if resolved == a_resolved or resolved.startswith(a_resolved + "/"):
            return resolved
    raise ToolError(
        f"repo path {resolved!r} is not in the allow-list ({allowed})"
    )


async def _run(argv: list[str], cwd: str | None = None, timeout: float = 15.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise ToolError(f"{argv[0]} timed out after {timeout}s")
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


@register_tool(
    "exec.git_status",
    description=(
        "Run `git status --porcelain` in an allow-listed repo. "
        "Returns the porcelain lines as a list. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {"repo": {"type": "string"}},
        "required": ["repo"],
        "additionalProperties": False,
    },
    requires_network=False,
    category="exec",
    cost_cents=0,
)
async def exec_git_status(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    repo = _check_repo(args["repo"])
    code, out, err = await _run(["git", "-C", repo, "status", "--porcelain"])
    if code != 0:
        raise ToolError(f"git status failed: {err[:200]}")
    lines = [line for line in out.splitlines() if line.strip()]
    return ToolResult(
        data={
            "repo": repo,
            "clean": len(lines) == 0,
            "changes": lines[:200],
            "change_count": len(lines),
        }
    )


@register_tool(
    "exec.git_log",
    description=(
        "Run `git log -n LIMIT --oneline` in an allow-listed repo. "
        "Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["repo"],
        "additionalProperties": False,
    },
    requires_network=False,
    category="exec",
    cost_cents=0,
)
async def exec_git_log(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    repo = _check_repo(args["repo"])
    limit = max(1, min(int(args.get("limit", 10)), 100))
    code, out, err = await _run(
        ["git", "-C", repo, "log", f"-n{limit}", "--oneline"]
    )
    if code != 0:
        raise ToolError(f"git log failed: {err[:200]}")
    return ToolResult(
        data={
            "repo": repo,
            "commits": [line for line in out.splitlines() if line.strip()],
        }
    )


@register_tool(
    "exec.docker_compose_ps",
    description=(
        "Run `docker compose -f COMPOSE_FILE ps --format json` and "
        "return parsed entries. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {"compose_file": {"type": "string"}},
        "required": ["compose_file"],
        "additionalProperties": False,
    },
    requires_network=False,
    category="exec",
    cost_cents=0,
)
async def exec_docker_compose_ps(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    compose = args["compose_file"]
    # Allow-list check via the parent dir
    parent = str(Path(compose).resolve().parent)
    _check_repo(parent)
    code, out, err = await _run(
        ["docker", "compose", "-f", compose, "ps", "--format", "json"],
        timeout=20.0,
    )
    if code != 0:
        raise ToolError(f"docker compose ps failed: {err[:200]}")
    import json as _json

    entries: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(_json.loads(line))
        except ValueError:
            continue
    return ToolResult(
        data={
            "compose_file": compose,
            "services": [
                {
                    "name": e.get("Service") or e.get("Name"),
                    "image": e.get("Image"),
                    "state": e.get("State"),
                    "status": e.get("Status"),
                }
                for e in entries
            ],
        }
    )
