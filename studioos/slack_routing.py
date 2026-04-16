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


_SINGLE_APP_BOT_UID: str | None = None  # set by init for single-app mode

# Known agent short names → full agent_id (populated at startup)
_AGENT_SHORT_NAMES: dict[str, str] = {}


async def init_bot_user_map() -> None:
    """Resolve bot user IDs.

    Single-app mode (STUDIOOS_SLACK_BOT_TOKEN set, no per-agent tokens):
      One bot for all agents. Mention routing uses the first word after
      the mention as the agent short name: "@StudioOS pricer ..." → amz-pricer.

    Multi-app mode (STUDIOOS_SLACK_AGENT_TOKENS set):
      Each agent has its own bot. Mention routing uses bot_user_id lookup.
    """
    global _SINGLE_APP_BOT_UID

    # Build short-name → agent_id map from all known agents
    # Covers both studios' agents
    _known_agents = [
        "amz-monitor", "amz-scout", "amz-analyst", "amz-pricer",
        "amz-repricer", "amz-crosslister", "amz-admanager", "amz-qa",
        "amz-dev", "amz-ceo", "amz-reflector", "amz-executor", "amz-pruner",
        "app-studio-pulse", "app-studio-reflector", "app-studio-pruner",
        "app-studio-growth-intel", "app-studio-growth-exec",
        "app-studio-ceo", "app-studio-pricing", "app-studio-dev",
        "app-studio-qa", "app-studio-marketing", "app-studio-hub-dev",
    ]
    for aid in _known_agents:
        # Short names: "pricer", "scout", "ceo", "growth-intel", etc.
        for prefix in ("amz-", "app-studio-"):
            if aid.startswith(prefix):
                short = aid[len(prefix):]
                _AGENT_SHORT_NAMES[short] = aid
                break

    # Single-app mode: resolve the one bot token
    bot_token = settings.slack_bot_token
    if bot_token:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {bot_token}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        _SINGLE_APP_BOT_UID = data["user_id"]
                        log.info("slack_routing.single_app", user_id=_SINGLE_APP_BOT_UID)
        except Exception as exc:
            log.warning("slack_routing.single_app_error", error=str(exc))

    # Multi-app mode: per-agent tokens
    token_map = _parse_map(settings.slack_agent_tokens)
    if token_map:
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
                except Exception:
                    pass
    log.info("slack_routing.ready", bot_count=len(_BOT_USER_MAP), single_app=bool(_SINGLE_APP_BOT_UID))


def resolve_agent_from_mention(text: str, channel: str = "") -> str | None:
    """Route a mention to an agent_id.

    Single-app mode: "@StudioOS pricer stok durumu" →
      strip bot mention, first word = agent short name,
      channel determines studio prefix (amz-hq → amz-, app-hq → app-studio-).

    Multi-app mode: "<@BOT_USER_ID>" → direct lookup.
    """
    # Multi-app mode: direct bot_user_id lookup
    for match in re.finditer(r"<@([UW][A-Z0-9_]+)>", text):
        uid = match.group(1)
        agent_id = _BOT_USER_MAP.get(uid)
        if agent_id:
            return agent_id

    # Single-app mode: extract agent name from text
    if _SINGLE_APP_BOT_UID:
        cleaned = re.sub(r"<@[UW][A-Z0-9_]+>\s*", "", text).strip()
        parts = cleaned.split()
        if not parts:
            return None
        candidate = parts[0].lower().rstrip(",:;.!?")

        # Direct match in short names
        if candidate in _AGENT_SHORT_NAMES:
            agent_id = _AGENT_SHORT_NAMES[candidate]
            # Verify studio matches channel
            studio_channels = _parse_map(
                getattr(settings, "slack_studio_channels", "")
            )
            for studio_id, ch_id in studio_channels.items():
                if ch_id == channel:
                    # Check agent belongs to this studio
                    if studio_id == "amz" and agent_id.startswith("amz-"):
                        return agent_id
                    if studio_id == "app-studio" and agent_id.startswith("app-studio-"):
                        return agent_id
            # No channel match — return anyway (best effort)
            return agent_id

        # Try with studio prefix from channel
        studio_channels = _parse_map(
            getattr(settings, "slack_studio_channels", "")
        )
        for studio_id, ch_id in studio_channels.items():
            if ch_id == channel:
                if studio_id == "amz":
                    full_id = f"amz-{candidate}"
                elif studio_id == "app-studio":
                    full_id = f"app-studio-{candidate}"
                else:
                    continue
                if full_id in [a for a in _AGENT_SHORT_NAMES.values()]:
                    return full_id

    return None


def clean_mention_text(text: str) -> str:
    """Remove <@UXXXXX> patterns from text, leaving the human-readable part."""
    return re.sub(r"<@[UW][A-Z0-9_]+>\s*", "", text).strip()


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
    for match in re.finditer(r"<@([UW][A-Z0-9_]+)>", text):
        uid = match.group(1)
        target = _BOT_USER_MAP.get(uid)
        if target and target != responding_agent_id:
            mentioned.append(target)
    return mentioned
