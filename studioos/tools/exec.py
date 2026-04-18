"""Narrow exec tools for the amz-dev agent.

Read-only shell wrappers:
  exec.git_status            — `git -C <repo> status --porcelain`
  exec.git_log               — `git -C <repo> log -n N --oneline`
  exec.docker_compose_ps     — `docker compose -f <compose_file> ps --format json`

Write tools (M35 — approval-gated at the workflow level):
  exec.git_commit            — `git add <files> && git commit -m <msg>`
  exec.git_push              — `git push origin <branch>`
  exec.gh_workflow_dispatch  — `gh workflow run <workflow> -R <repo_name> --ref <branch>`
  exec.codemagic_trigger     — HTTP POST to Codemagic builds API

No arbitrary command execution. Each tool has a fixed argv shape and
validates the repo path against a configured allow-list
(STUDIOOS_DEV_REPO_ALLOWLIST). The amz-dev agent uses these to
report on and manage the engineering state of the supporting projects.
"""
from __future__ import annotations

import asyncio
import json as _json
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


# ---------------------------------------------------------------------------
# Commit blocklist (M35)
# ---------------------------------------------------------------------------

_COMMIT_BLOCKLIST = {".env", ".env.example", ".env.local", ".env.production"}
_COMMIT_BLOCKLIST_PATTERNS = ["credentials", "secret", "token", ".pem", ".key"]


def _is_file_blocked(filepath: str) -> bool:
    """Return True if the file must not be committed."""
    name = Path(filepath).name
    if name in _COMMIT_BLOCKLIST:
        return True
    name_lower = name.lower()
    return any(pat in name_lower for pat in _COMMIT_BLOCKLIST_PATTERNS)


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
    "exec.read_file",
    description=(
        "Read a text file from an allow-listed repo. "
        "Returns first 10KB of content. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Allow-listed repo path"},
            "path": {"type": "string", "description": "Relative path within repo"},
        },
        "required": ["repo", "path"],
        "additionalProperties": False,
    },
    requires_network=False,
    category="exec",
    cost_cents=0,
)
async def exec_read_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    repo = _check_repo(args["repo"])
    rel_path = args["path"].lstrip("/")
    full_path = Path(repo) / rel_path
    # Prevent path traversal
    resolved = full_path.resolve()
    if not str(resolved).startswith(repo):
        raise ToolError(f"path {rel_path!r} escapes repo")
    if not resolved.exists():
        return ToolResult(data={"exists": False, "path": rel_path})
    if not resolved.is_file():
        raise ToolError(f"{rel_path} is not a file")
    # Read first 10KB only
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")[:10240]
    except Exception as exc:
        raise ToolError(f"read failed: {exc}")
    return ToolResult(
        data={
            "exists": True,
            "path": rel_path,
            "content": content,
            "size_bytes": resolved.stat().st_size,
        }
    )


@register_tool(
    "exec.list_dir",
    description=(
        "List files/directories in a path within an allow-listed repo. "
        "Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "path": {"type": "string", "default": "."},
        },
        "required": ["repo"],
        "additionalProperties": False,
    },
    requires_network=False,
    category="exec",
    cost_cents=0,
)
async def exec_list_dir(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    repo = _check_repo(args["repo"])
    rel_path = args.get("path", ".").lstrip("/") or "."
    full_path = (Path(repo) / rel_path).resolve()
    if not str(full_path).startswith(repo):
        raise ToolError(f"path {rel_path!r} escapes repo")
    if not full_path.exists() or not full_path.is_dir():
        return ToolResult(data={"exists": False, "path": rel_path})
    entries = []
    for item in sorted(full_path.iterdir())[:100]:
        entries.append({
            "name": item.name,
            "type": "dir" if item.is_dir() else "file",
        })
    return ToolResult(data={"exists": True, "path": rel_path, "entries": entries})


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


# ---------------------------------------------------------------------------
# M35 — write tools
# ---------------------------------------------------------------------------


@register_tool(
    "exec.git_commit",
    description=(
        "Stage specific files and create a git commit in an allow-listed repo. "
        "Blocked files (.env, credentials, secrets, keys) are rejected. "
        "Returns the new commit SHA and staged file list."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Absolute path to the git repo"},
            "message": {"type": "string", "description": "Commit message"},
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of file paths (relative to repo root) to stage",
                "minItems": 1,
            },
        },
        "required": ["repo", "message", "files"],
        "additionalProperties": False,
    },
    requires_network=False,
    category="exec",
    cost_cents=0,
)
async def exec_git_commit(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    repo = _check_repo(args["repo"])
    message: str = args["message"]
    files: list[str] = args["files"]

    blocked = [f for f in files if _is_file_blocked(f)]
    if blocked:
        raise ToolError(
            f"Commit blocked — the following files are on the blocklist: {blocked}"
        )
    if not files:
        raise ToolError("files list must not be empty")

    # git add
    add_code, _, add_err = await _run(["git", "-C", repo, "add", "--"] + files)
    if add_code != 0:
        raise ToolError(f"git add failed: {add_err[:300]}")

    # git commit
    commit_code, commit_out, commit_err = await _run(
        ["git", "-C", repo, "commit", "-m", message]
    )
    if commit_code != 0:
        raise ToolError(f"git commit failed: {commit_err[:300]}")

    # retrieve the new SHA
    sha_code, sha_out, _ = await _run(
        ["git", "-C", repo, "rev-parse", "--short", "HEAD"]
    )
    commit_sha = sha_out.strip() if sha_code == 0 else "unknown"

    return ToolResult(
        data={
            "commit_sha": commit_sha,
            "files_staged": files,
            "message": message,
        }
    )


@register_tool(
    "exec.git_push",
    description=(
        "Push the current branch (or a named branch) to origin in an allow-listed repo. "
        "Force-push is not permitted."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Absolute path to the git repo"},
            "branch": {"type": "string", "default": "main", "description": "Branch to push"},
        },
        "required": ["repo"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="exec",
    cost_cents=0,
)
async def exec_git_push(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    repo = _check_repo(args["repo"])
    branch: str = args.get("branch", "main") or "main"

    if "--force" in branch or "-f" in branch.split():
        raise ToolError("Force-push is not permitted via this tool")

    code, out, err = await _run(
        ["git", "-C", repo, "push", "origin", branch],
        timeout=60.0,
    )
    if code != 0:
        raise ToolError(f"git push failed: {err[:300]}")

    return ToolResult(
        data={
            "pushed": True,
            "branch": branch,
            "output": (out + err).strip()[:500],
        }
    )


@register_tool(
    "exec.gh_workflow_dispatch",
    description=(
        "Trigger a GitHub Actions workflow via `gh workflow run`. "
        "Requires the `gh` CLI to be authenticated."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "repo_name": {
                "type": "string",
                "description": "GitHub repo in 'owner/repo' format",
            },
            "workflow": {
                "type": "string",
                "default": "deploy.yml",
                "description": "Workflow filename or ID",
            },
            "branch": {
                "type": "string",
                "default": "main",
                "description": "Branch to run the workflow on",
            },
        },
        "required": ["repo_name"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="exec",
    cost_cents=0,
)
async def exec_gh_workflow_dispatch(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    repo_name: str = args["repo_name"]
    workflow: str = args.get("workflow", "deploy.yml") or "deploy.yml"
    branch: str = args.get("branch", "main") or "main"

    code, out, err = await _run(
        ["gh", "workflow", "run", workflow, "-R", repo_name, "--ref", branch],
        timeout=30.0,
    )
    if code != 0:
        raise ToolError(f"gh workflow run failed: {err[:300]}")

    return ToolResult(
        data={
            "triggered": True,
            "workflow": workflow,
            "repo_name": repo_name,
            "branch": branch,
        }
    )


@register_tool(
    "exec.codemagic_trigger",
    description=(
        "Trigger a Codemagic build via the REST API. "
        "Requires STUDIOOS_CODEMAGIC_TOKEN to be set."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_id": {
                "type": "string",
                "description": "Codemagic application ID",
            },
            "branch": {
                "type": "string",
                "default": "main",
                "description": "Branch to build",
            },
        },
        "required": ["app_id"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="exec",
    cost_cents=0,
)
async def exec_codemagic_trigger(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    import urllib.request

    app_id: str = args["app_id"]
    branch: str = args.get("branch", "main") or "main"

    token = settings.codemagic_token
    if not token:
        raise ToolError(
            "STUDIOOS_CODEMAGIC_TOKEN is not configured — cannot trigger build"
        )

    payload = _json.dumps(
        {"appId": app_id, "workflowId": "default", "branch": branch}
    ).encode()

    req = urllib.request.Request(
        "https://api.codemagic.io/builds",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-auth-token": token,
        },
        method="POST",
    )

    loop = asyncio.get_event_loop()
    try:
        response_body = await loop.run_in_executor(
            None,
            lambda: urllib.request.urlopen(req, timeout=30).read(),
        )
    except urllib.request.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise ToolError(f"Codemagic API error {exc.code}: {body}") from exc

    data = _json.loads(response_body)
    build_id = (
        data.get("buildId")
        or data.get("build", {}).get("_id")
        or data.get("_id")
        or "unknown"
    )
    status = data.get("status", "queued")

    return ToolResult(
        data={
            "build_id": build_id,
            "status": status,
        }
    )
