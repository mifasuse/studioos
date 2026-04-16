"""Slack bot-user-id → agent-id mapping.

At startup, resolves each bot token in STUDIOOS_SLACK_AGENT_TOKENS
via Slack's auth.test API to get the bot's user_id. This map is
used to route incoming @mentions to the correct agent.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from studioos.config import settings
from studioos.logging import get_logger
from studioos.tools.slack import _parse_map

log = get_logger(__name__)

# Populated by init_bot_user_map()
_BOT_USER_MAP: dict[str, str] = {}  # slack_user_id → agent_id
_AGENT_BOT_MAP: dict[str, str] = {}  # agent_id → slack_user_id


async def init_bot_user_map() -> None:
    """Call auth.test for each agent bot token to learn its user_id."""
    token_map = _parse_map(settings.slack_agent_tokens)
    if not token_map:
        log.warning("slack_routing.no_tokens", msg="STUDIOOS_SLACK_AGENT_TOKENS empty")
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        for agent_id, token in token_map.items():
            try:
                resp = await client.post(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        uid = data["user_id"]
                        _BOT_USER_MAP[uid] = agent_id
                        _AGENT_BOT_MAP[agent_id] = uid
                        log.info("slack_routing.mapped", agent_id=agent_id, user_id=uid)
                    else:
                        log.warning("slack_routing.auth_failed", agent_id=agent_id, error=data.get("error"))
            except Exception as exc:
                log.warning("slack_routing.error", agent_id=agent_id, error=str(exc))
    log.info("slack_routing.ready", bot_count=len(_BOT_USER_MAP))


def resolve_agent_from_mention(text: str) -> str | None:
    """Extract the first <@UXXXXX> mention that maps to a known agent."""
    for match in re.finditer(r"<@(U[A-Z0-9]+)>", text):
        uid = match.group(1)
        agent_id = _BOT_USER_MAP.get(uid)
        if agent_id:
            return agent_id
    return None


def clean_mention_text(text: str) -> str:
    """Remove <@UXXXXX> patterns from text, leaving the human-readable part."""
    return re.sub(r"<@U[A-Z0-9]+>\s*", "", text).strip()


def get_bot_user_id(agent_id: str) -> str | None:
    """Get the Slack user_id for an agent (for self-mention detection)."""
    return _AGENT_BOT_MAP.get(agent_id)


# Cascade protection
_THREAD_MENTION_COUNTS: dict[str, int] = {}  # "thread_ts:agent_id" → count
MAX_MENTIONS_PER_THREAD = 3
MAX_THREAD_DEPTH = 10


def check_cascade(thread_ts: str, agent_id: str, responding_agent_id: str | None = None) -> bool:
    """Return True if this mention should be processed (not cascade-blocked)."""
    # Self-mention: agent can't trigger itself
    if responding_agent_id and responding_agent_id == agent_id:
        return False
    # Per-agent-per-thread limit
    key = f"{thread_ts}:{agent_id}"
    count = _THREAD_MENTION_COUNTS.get(key, 0)
    if count >= MAX_MENTIONS_PER_THREAD:
        return False
    _THREAD_MENTION_COUNTS[key] = count + 1
    return True


def reset_cascade_counts() -> None:
    """Clear cascade counters (called periodically or on thread close)."""
    _THREAD_MENTION_COUNTS.clear()


def detect_mentions_in_response(text: str, responding_agent_id: str) -> list[str]:
    """Find @mentions in an agent's response text. Returns list of agent_ids to trigger."""
    mentioned: list[str] = []
    for match in re.finditer(r"<@(U[A-Z0-9]+)>", text):
        uid = match.group(1)
        target = _BOT_USER_MAP.get(uid)
        if target and target != responding_agent_id:
            mentioned.append(target)
    return mentioned
