"""Workflow registry — maps (template_id, version) to an invokable workflow.

Milestone 1 uses a plain Python protocol rather than requiring LangGraph for every
workflow. The LangGraph workflows from `studioos.workflows.*` implement the same
`ainvoke(input)` contract so the runner does not care about the engine.
"""
from __future__ import annotations

from typing import Any, Protocol


class Workflow(Protocol):
    """Minimal callable contract used by the runner."""

    async def ainvoke(self, input: dict[str, Any]) -> dict[str, Any]: ...


_REGISTRY: dict[tuple[str, int], Workflow] = {}


def register_workflow(
    template_id: str, version: int, workflow: Workflow
) -> Workflow:
    key = (template_id, version)
    if key in _REGISTRY:
        raise ValueError(f"Workflow already registered: {template_id} v{version}")
    _REGISTRY[key] = workflow
    return workflow


def resolve_workflow(template_id: str, version: int) -> Workflow:
    key = (template_id, version)
    if key not in _REGISTRY:
        raise KeyError(
            f"No workflow registered for {template_id} v{version}. "
            f"Known: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[key]


def list_workflows() -> list[tuple[str, int]]:
    return sorted(_REGISTRY.keys())
