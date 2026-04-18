"""Nitter search tool — searches Twitter/X via internal Nitter instance."""
from __future__ import annotations

import re
from typing import Any

import httpx

from .base import ToolContext, ToolError, ToolResult
from .registry import register_tool


@register_tool(
    "nitter.search",
    description="Search Twitter/X via internal Nitter instance for market signals.",
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
async def nitter_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    query: str = args["query"]
    limit: int = int(args.get("limit", 10))

    url = f"http://nitter:8080/search?q={query}&f=tweets"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise ToolError(f"nitter http error: {exc}") from exc

    if resp.status_code >= 400:
        raise ToolError(f"nitter {resp.status_code}: {resp.text[:200]}")

    # Extract tweet texts from <div class="tweet-content"> elements
    tweets = re.findall(
        r'<div[^>]*class="tweet-content"[^>]*>(.*?)</div>',
        resp.text,
        re.DOTALL,
    )

    # Strip inner HTML tags for plain text
    clean_tweets: list[str] = []
    for raw in tweets[:limit]:
        text = re.sub(r"<[^>]+>", "", raw).strip()
        if text:
            clean_tweets.append(text)

    return ToolResult(data={"tweets": clean_tweets, "count": len(clean_tweets)})
