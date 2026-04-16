"""Hub API tool registration + response parsing."""
from __future__ import annotations

from studioos.tools.registry import get_tool


def test_hub_overview_registered() -> None:
    tool = get_tool("hub.api.overview")
    assert tool is not None
    schema = tool.input_schema
    assert "app_id" in schema["required"]


def test_hub_metrics_registered() -> None:
    tool = get_tool("hub.api.metrics")
    assert tool is not None
    schema = tool.input_schema
    assert "app_id" in schema["required"]
    assert "metric" in schema["required"]


def test_hub_campaigns_registered() -> None:
    tool = get_tool("hub.api.campaigns")
    assert tool is not None
    schema = tool.input_schema
    assert "action" in schema["required"]
