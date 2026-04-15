"""Workflow definitions — LangGraph graphs.

Importing this module registers all workflows with the runtime workflow registry.
"""
from __future__ import annotations

from studioos.workflows import analyst_test, scout_test  # noqa: F401

__all__ = ["scout_test", "analyst_test"]
