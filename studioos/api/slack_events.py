"""Slack Events API webhook — routes @mentions to agent runs."""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request, Response

from studioos.config import settings
from studioos.logging import get_logger
from studioos.slack_routing import resolve_agent_from_mention, clean_mention_text

log = get_logger(__name__)

router = APIRouter()

# Agent_id → studio_id mapping (derived from studios at startup or hardcoded)
_AGENT_STUDIO: dict[str, str] = {}


def _studio_for_agent(agent_id: str) -> str:
    """Derive studio_id from agent_id prefix."""
    if agent_id.startswith("amz-"):
        return "amz"
    if agent_id.startswith("app-studio-"):
        return "app-studio"
    return ""


def verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """HMAC-SHA256 verification of Slack request."""
    secret = settings.slack_signing_secret
    if not secret:
        return True  # Skip verification in dev (no secret configured)
    if abs(time.time() - float(timestamp)) > 300:
        return False  # Replay attack protection
    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    my_sig = "v0=" + hmac.new(
        secret.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(my_sig, signature)


@router.post("/slack/events", response_model=None)
async def slack_events(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any] | Response:
    raw_body = await request.body()
    body = await request.json()

    # Verify signature
    ts = request.headers.get("X-Slack-Request-Timestamp", "0")
    sig = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(raw_body, ts, sig):
        return Response(status_code=401, content="invalid signature")

    # URL verification challenge
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge", "")}

    # Event callback
    if body.get("type") == "event_callback":
        event = body.get("event") or {}
        # Ignore bot messages (prevent loops)
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return {"ok": True}
        background_tasks.add_task(_process_mention, event)

    return {"ok": True}


async def _process_mention(event: dict[str, Any]) -> None:
    """Route an app_mention event to the correct agent run."""
    if event.get("type") != "app_mention":
        return

    text = event.get("text", "")
    agent_id = resolve_agent_from_mention(text)
    if not agent_id:
        log.debug("slack_events.no_agent", text=text[:100])
        return

    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    message_ts = event.get("ts", "")
    user = event.get("user", "")
    studio_id = _studio_for_agent(agent_id)
    clean_text = clean_mention_text(text)

    log.info(
        "slack_events.mention",
        agent_id=agent_id,
        user=user,
        channel=channel,
        text=clean_text[:80],
    )

    # Create a run directly via the runtime
    from studioos.runtime.trigger import trigger_run

    await trigger_run(
        agent_id=agent_id,
        trigger_type="slack_mention",
        trigger_ref=message_ts,
        input_data={
            "event_type": "slack.mention.received",
            "payload": {
                "agent_id": agent_id,
                "studio_id": studio_id,
                "text": clean_text,
                "user": user,
                "channel": channel,
                "thread_ts": thread_ts,
                "message_ts": message_ts,
            },
        },
    )
