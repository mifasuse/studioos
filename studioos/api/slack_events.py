"""Slack Events API webhook — routes @mentions to agent runs."""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Any
from uuid import UUID

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


async def _handle_approval_command(text: str, channel: str, ts: str) -> None:
    """Handle 'approve {id}' or 'deny {id}' commands from Slack."""
    parts = text.strip().split(None, 1)
    if len(parts) < 2:
        return
    action = parts[0].lower()
    raw_id = parts[1].strip()
    decision = "approved" if action == "approve" else "denied"
    try:
        approval_id = UUID(raw_id)
    except (ValueError, TypeError):
        log.warning("slack_events.bad_approval_id", raw=raw_id[:40])
        return
    try:
        from studioos.approvals import decide_approval
        from studioos.db import session_scope

        async with session_scope() as session:
            row = await decide_approval(
                session,
                approval_id=approval_id,
                decision=decision,
                decided_by="slack",
                note="via Slack command",
            )
        # Confirm in Slack
        token = settings.slack_bot_token
        if token:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={
                        "channel": channel,
                        "thread_ts": ts,
                        "text": f"✅ Approval `{raw_id[:8]}...` → **{decision}**",
                    },
                )
        log.info("slack_events.approval_decided", id=str(approval_id), decision=decision)
    except ValueError as exc:
        log.warning("slack_events.approval_error", error=str(exc)[:100])
    except Exception as exc:
        log.warning("slack_events.approval_error", error=str(exc)[:100])


async def _process_mention(event: dict[str, Any]) -> None:
    """Route a Slack message to the correct agent run.

    Handles both:
      - app_mention: "@StudioOS pricer ..." (explicit mention)
      - message: "pricer Buy Box durumu" (direct message, no mention needed)
    """
    event_type = event.get("type")
    if event_type not in ("app_mention", "message"):
        return
    # Skip message edits, file shares, etc.
    if event.get("subtype"):
        return

    text = event.get("text", "")
    channel = event.get("channel", "")

    # Handle approval commands: "approve {id}" or "deny {id}"
    clean = re.sub(r"<@[UW][A-Z0-9_]+>\s*", "", text).strip()
    if clean.lower().startswith(("approve ", "deny ")):
        await _handle_approval_command(clean, channel, event.get("ts", ""))
        return

    agent_id = resolve_agent_from_mention(text, channel=channel)
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
        workflow_override="react_conversation",
    )
