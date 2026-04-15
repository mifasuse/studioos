"""Slack notification tool — chat.postMessage via Web API.

OpenClaw uses Slack socket mode for inbound; we only need outbound
posts. A bot token + channel is enough for chat.postMessage. Token +
default channel come from settings; callers may override the channel
per call. The tool name `slack.notify` mirrors `telegram.notify`
so a workflow can send to either with one line each.
"""
from __future__ import annotations

from typing import Any

import httpx

from studioos.config import settings
from studioos.logging import get_logger

from .base import ToolContext, ToolError, ToolResult
from .registry import register_tool

log = get_logger(__name__)


@register_tool(
    "slack.notify",
    description=(
        "Post a Slack message via chat.postMessage using the bot token "
        "in STUDIOOS_SLACK_BOT_TOKEN. Default channel from "
        "STUDIOOS_SLACK_DEFAULT_CHANNEL unless overridden per call."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "channel": {"type": "string"},
            "thread_ts": {"type": "string"},
            "mrkdwn": {"type": "boolean"},
            "unfurl_links": {"type": "boolean"},
        },
        "required": ["text"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="notify",
    cost_cents=0,
)
async def slack_notify(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    token = settings.slack_bot_token
    if not token:
        raise ToolError("STUDIOOS_SLACK_BOT_TOKEN is not configured")
    channel = args.get("channel") or settings.slack_default_channel
    if not channel:
        raise ToolError(
            "channel not provided and STUDIOOS_SLACK_DEFAULT_CHANNEL is empty"
        )

    body: dict[str, Any] = {
        "channel": channel,
        "text": args["text"][:40000],
        "mrkdwn": bool(args.get("mrkdwn", True)),
    }
    if args.get("thread_ts"):
        body["thread_ts"] = args["thread_ts"]
    if args.get("unfurl_links") is not None:
        body["unfurl_links"] = bool(args["unfurl_links"])

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=body,
            )
    except httpx.HTTPError as exc:
        raise ToolError(f"slack http error: {exc}") from exc

    if resp.status_code >= 400:
        raise ToolError(f"slack {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    if not payload.get("ok"):
        raise ToolError(
            f"slack api error: {payload.get('error', '?')}"
            f" warning={payload.get('warning', '')}"
        )
    return ToolResult(
        data={
            "ok": True,
            "channel": payload.get("channel"),
            "ts": payload.get("ts"),
        }
    )
