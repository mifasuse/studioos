"""Web search tool — searches via DuckDuckGo HTML interface."""
from __future__ import annotations

import re
from typing import Any

import httpx

from .base import ToolContext, ToolError, ToolResult
from .registry import register_tool

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


@register_tool(
    "web.search",
    description="Search the web via DuckDuckGo HTML for market research.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["query"],
    },
    requires_network=True,
    category="research",
    cost_cents=0,
)
async def web_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    query: str = args["query"]
    limit: int = int(args.get("limit", 10))

    url = f"https://html.duckduckgo.com/html/?q={query}"
    try:
        async with httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise ToolError(f"web.search http error: {exc}") from exc

    if resp.status_code >= 400:
        raise ToolError(f"web.search {resp.status_code}: {resp.text[:200]}")

    html = resp.text

    # Extract result titles + URLs from <a class="result__a" href="...">title</a>
    title_url_pairs = re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        html,
        re.DOTALL,
    )

    # Extract snippets from <a class="result__snippet" ...>snippet</a>
    snippets = re.findall(
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        html,
        re.DOTALL,
    )

    results: list[dict[str, str]] = []
    for i, (url_raw, title_raw) in enumerate(title_url_pairs[:limit]):
        title = re.sub(r"<[^>]+>", "", title_raw).strip()
        snippet_raw = snippets[i] if i < len(snippets) else ""
        snippet = re.sub(r"<[^>]+>", "", snippet_raw).strip()
        results.append({"title": title, "url": url_raw, "snippet": snippet})

    return ToolResult(data={"results": results, "count": len(results)})
