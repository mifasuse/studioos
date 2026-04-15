"""Global tool registry — process-local singleton."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from studioos.logging import get_logger

from .base import Handler, Tool

log = get_logger(__name__)

_REGISTRY: dict[str, Tool] = {}


def register_tool(
    name: str,
    *,
    description: str,
    input_schema: dict[str, Any],
    requires_network: bool = False,
    category: str = "general",
) -> Callable[[Handler], Handler]:
    """Decorator to register a tool handler.

    Usage:
        @register_tool(
            "http.get_json",
            description="...",
            input_schema={...},
        )
        async def http_get_json(args, ctx):
            ...
    """

    def decorator(handler: Handler) -> Handler:
        if name in _REGISTRY:
            log.warning("tools.duplicate_registration", name=name)
        _REGISTRY[name] = Tool(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            requires_network=requires_network,
            category=category,
        )
        log.debug("tools.registered", name=name, category=category)
        return handler

    return decorator


def get_tool(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def list_tools() -> list[Tool]:
    return sorted(_REGISTRY.values(), key=lambda t: t.name)


def clear_registry() -> None:
    """Test helper."""
    _REGISTRY.clear()
