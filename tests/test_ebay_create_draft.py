"""ebaycrosslister.api.create_draft tool — registration smoke test."""
from __future__ import annotations
from studioos.tools.registry import get_tool


def test_create_draft_tool_registered() -> None:
    tool = get_tool("ebaycrosslister.api.create_draft")
    assert tool is not None
    assert tool.name == "ebaycrosslister.api.create_draft"
    schema = tool.input_schema
    assert "title" in schema["properties"]
    assert "price" in schema["properties"]
    assert "quantity" in schema["properties"]
    assert "title" in schema["required"]
    assert "price" in schema["required"]
