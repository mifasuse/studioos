"""Tests for studioos.workflows.personas — M33 Task 1."""
from __future__ import annotations


def test_known_agent_has_persona() -> None:
    from studioos.workflows.personas import get_persona

    persona = get_persona("amz-pricer")
    assert "pricer" in persona.lower() or "fiyat" in persona.lower()
    assert len(persona) > 20


def test_unknown_agent_gets_default() -> None:
    from studioos.workflows.personas import get_persona

    persona = get_persona("nonexistent-agent")
    assert "StudioOS" in persona


def test_tool_description_list() -> None:
    from studioos.workflows.personas import format_tool_list

    tools = ["buyboxpricer.db.lost_buybox", "pricefinder.db.scout_candidates"]
    result = format_tool_list(tools)
    assert "buyboxpricer.db.lost_buybox" in result


def test_build_system_prompt_includes_tools() -> None:
    from studioos.workflows.personas import build_system_prompt

    prompt = build_system_prompt("amz-pricer", ["memory.search"])
    assert "memory.search" in prompt
    assert '"tool"' in prompt  # JSON format instruction
