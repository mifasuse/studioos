"""Workflow definitions — LangGraph graphs.

Importing this module registers all workflows with the runtime workflow registry.
"""
from __future__ import annotations

from studioos.workflows import (  # noqa: F401
    amz_analyst,
    amz_executor,
    amz_monitor,
    amz_pricer,
    amz_scout,
    analyst_test,
    scout_test,
)

__all__ = [
    "scout_test",
    "analyst_test",
    "amz_monitor",
    "amz_analyst",
    "amz_executor",
    "amz_scout",
    "amz_pricer",
]
