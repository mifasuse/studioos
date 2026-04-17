"""Dev exec tools — M35."""
from __future__ import annotations

from studioos.tools.registry import get_tool


def test_git_commit_registered() -> None:
    tool = get_tool("exec.git_commit")
    assert tool is not None
    assert "repo" in tool.input_schema["required"]
    assert "message" in tool.input_schema["required"]
    assert "files" in tool.input_schema["required"]


def test_git_push_registered() -> None:
    tool = get_tool("exec.git_push")
    assert tool is not None
    assert "repo" in tool.input_schema["required"]


def test_gh_workflow_dispatch_registered() -> None:
    tool = get_tool("exec.gh_workflow_dispatch")
    assert tool is not None
    assert "repo_name" in tool.input_schema["required"]


def test_codemagic_trigger_registered() -> None:
    tool = get_tool("exec.codemagic_trigger")
    assert tool is not None
    assert "app_id" in tool.input_schema["required"]


def test_blocklist_rejects_env_file() -> None:
    from studioos.tools.exec import _is_file_blocked
    assert _is_file_blocked(".env") is True
    assert _is_file_blocked("src/credentials.json") is True
    assert _is_file_blocked("deploy.key") is True
    assert _is_file_blocked("main.py") is False
    assert _is_file_blocked("README.md") is False
