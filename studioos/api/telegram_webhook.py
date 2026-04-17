"""Telegram Bot API webhook — routes messages to agent runs.

Similar to Slack inbound: first word = agent short name,
message routed to ReAct conversation workflow.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request

from studioos.config import settings
from studioos.logging import get_logger
from studioos.slack_routing import _AGENT_SHORT_NAMES

log = get_logger(__name__)

router = APIRouter()


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    body = await request.json()

    message = body.get("message") or {}
    text = message.get("text") or ""
    chat_id = str((message.get("chat") or {}).get("id", ""))
    user = (message.get("from") or {}).get("username") or str((message.get("from") or {}).get("id", ""))
    message_id = str(message.get("message_id", ""))

    if not text or not chat_id:
        return {"ok": True}

    # Ignore bot's own messages
    if (message.get("from") or {}).get("is_bot"):
        return {"ok": True}

    background_tasks.add_task(_process_telegram_message, text, chat_id, user, message_id)
    return {"ok": True}


async def _process_telegram_message(
    text: str, chat_id: str, user: str, message_id: str
) -> None:
    """Route a Telegram message to the correct agent."""
    # Strip /start or /help commands
    if text.startswith("/"):
        return

    # First word = agent short name
    parts = text.strip().split(None, 1)
    if not parts:
        return
    candidate = parts[0].lower().rstrip(",:;.!?")

    # Try to resolve agent — default to amz studio
    agent_id = _AGENT_SHORT_NAMES.get(candidate)
    if not agent_id:
        # Try with amz- prefix
        full_id = f"amz-{candidate}"
        if full_id in set(_AGENT_SHORT_NAMES.values()):
            agent_id = full_id

    if not agent_id:
        log.debug("telegram_webhook.no_agent", text=text[:80])
        return

    studio_id = "amz" if agent_id.startswith("amz-") else "app-studio"
    clean_text = text.strip()

    log.info(
        "telegram_webhook.message",
        agent_id=agent_id,
        user=user,
        text=clean_text[:80],
    )

    from studioos.runtime.trigger import trigger_run

    await trigger_run(
        agent_id=agent_id,
        trigger_type="telegram_message",
        trigger_ref=message_id,
        input_data={
            "event_type": "telegram.message.received",
            "payload": {
                "agent_id": agent_id,
                "studio_id": studio_id,
                "text": clean_text,
                "user": user,
                "chat_id": chat_id,
            },
        },
        workflow_override="react_conversation",
    )
