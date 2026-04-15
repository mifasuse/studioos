"""Telegram notification tool.

Thin wrapper over Telegram's Bot API sendMessage endpoint. Bot token
and a default chat_id come from settings; callers may override the
chat_id per call.
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
    "telegram.notify",
    description=(
        "Send a Markdown-formatted Telegram message via the Bot API. "
        "Uses STUDIOOS_TELEGRAM_BOT_TOKEN and STUDIOOS_TELEGRAM_DEFAULT_CHAT_ID "
        "unless the caller overrides chat_id."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "chat_id": {"type": "string"},
            "parse_mode": {"type": "string"},  # "Markdown" | "HTML" | ""
            "disable_web_page_preview": {"type": "boolean"},
        },
        "required": ["text"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="notify",
    cost_cents=0,
)
async def telegram_notify(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    token = settings.telegram_bot_token
    if not token:
        raise ToolError("STUDIOOS_TELEGRAM_BOT_TOKEN is not configured")
    chat_id = args.get("chat_id") or settings.telegram_default_chat_id
    if not chat_id:
        raise ToolError(
            "chat_id not provided and STUDIOOS_TELEGRAM_DEFAULT_CHAT_ID is empty"
        )

    body: dict[str, Any] = {
        "chat_id": chat_id,
        "text": args["text"][:4096],  # Telegram per-message cap
    }
    parse_mode = args.get("parse_mode", "Markdown")
    if parse_mode:
        body["parse_mode"] = parse_mode
    if args.get("disable_web_page_preview") is not None:
        body["disable_web_page_preview"] = bool(
            args["disable_web_page_preview"]
        )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=body)
    except httpx.HTTPError as exc:
        raise ToolError(f"telegram http error: {exc}") from exc

    if resp.status_code >= 400:
        raise ToolError(f"telegram {resp.status_code}: {resp.text[:300]}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise ToolError(f"telegram non-json: {exc}") from exc

    if not payload.get("ok"):
        raise ToolError(
            f"telegram api error: {payload.get('description', '?')}"
        )

    return ToolResult(
        data={
            "ok": True,
            "chat_id": chat_id,
            "message_id": (payload.get("result") or {}).get("message_id"),
        }
    )
