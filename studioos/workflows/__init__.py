"""Workflow definitions — LangGraph graphs.

Importing this module registers all workflows with the runtime workflow registry.
"""
from __future__ import annotations

from studioos.workflows import (  # noqa: F401
    amz_admanager,
    amz_analyst,
    amz_analyst_daily,
    amz_ceo,
    amz_crosslister,
    amz_dev,
    amz_executor,
    amz_monitor,
    amz_pricer,
    amz_qa,
    amz_reflector,
    amz_repricer,
    amz_scout,
    analyst_test,
    app_studio_ceo,
    app_studio_dev,
    app_studio_growth_exec,
    app_studio_growth_intel,
    app_studio_hub_dev,
    app_studio_marketing,
    app_studio_pricing,
    app_studio_pulse,
    app_studio_qa,
    app_studio_reflector,
    react_conversation,
    scout_test,
    studio_pruner,
)

__all__ = [
    "scout_test",
    "analyst_test",
    "amz_monitor",
    "amz_analyst",
    "amz_executor",
    "amz_scout",
    "amz_pricer",
    "amz_reflector",
    "amz_repricer",
    "amz_ceo",
    "amz_qa",
    "amz_crosslister",
    "amz_admanager",
    "amz_dev",
    "app_studio_pulse",
    "app_studio_reflector",
]
