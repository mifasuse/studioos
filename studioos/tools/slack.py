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


def _parse_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for piece in (raw or "").split(","):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        k, _, v = piece.partition("=")
        k = k.strip()
        v = v.strip()
        if k and v:
            out[k] = v
    return out


def _resolve_slack_route(agent_id: str | None) -> tuple[str, str]:
    """Return (token, channel) for the calling agent.

    Priority:
      1. Per-agent token from STUDIOOS_SLACK_AGENT_TOKENS (legacy multi-token)
      2. Default bot token from STUDIOOS_SLACK_BOT_TOKEN (single-app mode)
    Channel:
      1. Per-agent channel from STUDIOOS_SLACK_AGENT_CHANNELS
      2. Studio-based default from STUDIOOS_SLACK_STUDIO_CHANNELS
      3. STUDIOOS_SLACK_DEFAULT_CHANNEL fallback
    """
    token_map = _parse_map(settings.slack_agent_tokens)
    channel_map = _parse_map(settings.slack_agent_channels)
    studio_channel_map = _parse_map(
        getattr(settings, "slack_studio_channels", "")
    )
    token = (
        (token_map.get(agent_id) if agent_id else None)
        or settings.slack_bot_token
    )
    # Channel: per-agent → per-studio → default
    channel = (channel_map.get(agent_id) if agent_id else None)
    if not channel and agent_id:
        if agent_id.startswith("amz-"):
            channel = studio_channel_map.get("amz")
        elif agent_id.startswith("app-studio-"):
            channel = studio_channel_map.get("app-studio")
    channel = channel or settings.slack_default_channel
    return token, channel


def _agent_display_name(agent_id: str | None) -> str | None:
    """Human-friendly display name for chat.postMessage username override."""
    if not agent_id:
        return None
    return agent_id.replace("-", " ").title()


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
    token, default_channel = _resolve_slack_route(ctx.agent_id)
    if not token:
        raise ToolError(
            "no slack token — set STUDIOOS_SLACK_BOT_TOKEN or "
            "STUDIOOS_SLACK_AGENT_TOKENS entry for this agent"
        )
    channel = args.get("channel") or default_channel
    if not channel:
        raise ToolError(
            "channel not provided and no default for agent "
            f"{ctx.agent_id!r} (STUDIOOS_SLACK_AGENT_CHANNELS / "
            "STUDIOOS_SLACK_DEFAULT_CHANNEL)"
        )

    body: dict[str, Any] = {
        "channel": channel,
        "text": args["text"][:40000],
        "mrkdwn": bool(args.get("mrkdwn", True)),
    }
    # Agent display name (single-app mode: all agents share one bot,
    # username override shows which agent is speaking)
    display_name = _agent_display_name(ctx.agent_id)
    if display_name:
        body["username"] = display_name
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
