"""Tests for studioos.workflows.react_conversation — M33 Task 2."""
from __future__ import annotations

from studioos.workflows.react_conversation import parse_llm_response, MAX_ITERATIONS


def test_parse_tool_call() -> None:
    text = '{"tool": "buyboxpricer.db.lost_buybox", "args": {"limit": 5}}'
    result = parse_llm_response(text)
    assert result["type"] == "tool_call"
    assert result["tool"] == "buyboxpricer.db.lost_buybox"
    assert result["args"] == {"limit": 5}


def test_parse_plain_response() -> None:
    text = "Şu an 5 listing Buy Box kaybetmiş durumda."
    result = parse_llm_response(text)
    assert result["type"] == "response"
    assert result["text"] == text


def test_parse_json_in_markdown_fence() -> None:
    text = '```json\n{"tool": "hub.api.overview", "args": {"app_id": "quit_smoking"}}\n```'
    result = parse_llm_response(text)
    assert result["type"] == "tool_call"
    assert result["tool"] == "hub.api.overview"


def test_parse_mixed_text_and_json() -> None:
    text = 'Buy Box durumunu kontrol ediyorum...\n\n{"tool": "buyboxpricer.db.lost_buybox", "args": {}}'
    result = parse_llm_response(text)
    assert result["type"] == "tool_call"
    assert result["tool"] == "buyboxpricer.db.lost_buybox"


def test_parse_tool_call_tag_format() -> None:
    text = '[TOOL_CALL]\n{"tool": "buyboxpricer.db.lost_buybox", "args": {}}\n[/TOOL_CALL]'
    result = parse_llm_response(text)
    assert result["type"] == "tool_call"
    assert result["tool"] == "buyboxpricer.db.lost_buybox"


def test_parse_tool_call_with_surrounding_text() -> None:
    text = 'Scout taraması başlatılıyor...\n\n{"tool": "pricefinder.db.scout_candidates", "args": {"limit": 3}}\n\nSonuçlar gelecek.'
    result = parse_llm_response(text)
    assert result["type"] == "tool_call"
    assert result["tool"] == "pricefinder.db.scout_candidates"


def test_parse_invalid_json_is_response() -> None:
    text = '{"broken json'
    result = parse_llm_response(text)
    assert result["type"] == "response"


def test_max_iterations_constant() -> None:
    assert MAX_ITERATIONS == 5


def test_workflow_compiles() -> None:
    from studioos.workflows.react_conversation import build_graph
    graph = build_graph()
    assert graph is not None
