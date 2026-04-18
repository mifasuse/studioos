from studioos.tools.registry import get_tool


def test_nitter_search_registered() -> None:
    tool = get_tool("nitter.search")
    assert tool is not None
    assert "query" in tool.input_schema["required"]


def test_web_search_registered() -> None:
    tool = get_tool("web.search")
    assert tool is not None
    assert "query" in tool.input_schema["required"]
