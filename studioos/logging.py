"""Structured logging with correlation-id propagation."""
from __future__ import annotations

import contextvars
import logging
from typing import Any
from uuid import UUID

import structlog
from structlog.types import EventDict

from studioos.config import settings

# Context variable for correlation tracking across async tasks
_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)
_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "run_id", default=None
)
_agent_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_id", default=None
)


def bind_correlation(correlation_id: UUID | str | None) -> None:
    _correlation_id.set(str(correlation_id) if correlation_id else None)


def bind_run(run_id: UUID | str | None) -> None:
    _run_id.set(str(run_id) if run_id else None)


def bind_agent(agent_id: str | None) -> None:
    _agent_id.set(agent_id)


def _inject_context(
    _logger: Any, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Attach context vars to every log record."""
    if (cid := _correlation_id.get()) is not None:
        event_dict.setdefault("correlation_id", cid)
    if (rid := _run_id.get()) is not None:
        event_dict.setdefault("run_id", rid)
    if (aid := _agent_id.get()) is not None:
        event_dict.setdefault("agent_id", aid)
    return event_dict


def configure_logging() -> None:
    """Configure structlog + stdlib logging once at startup."""
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        level=level,
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.env == "dev":
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "studioos") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
