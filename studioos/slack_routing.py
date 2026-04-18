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

# Channel ID → studio_id mapping, populated at startup
_CHANNEL_STUDIO_MAP: dict[str, str] = {}  # channel_id → studio_id

# Channel name patterns → studio_id (fallback when config is empty)
_CHANNEL_NAME_PATTERNS: dict[str, str] = {
    "amz": "amz",       # amz-hq, amz-ops, etc.
    "app": "app-studio", # app-hq, app-ops, etc.
}


_SINGLE_APP_BOT_UID: str | None = None  # set by init for single-app mode

# Known agent short names → full agent_id (populated at startup)
# NOTE: when short names collide (e.g. 'ceo' exists in both studios),
# last-write-wins. Use _KNOWN_AGENTS for authoritative membership tests.
_AGENT_SHORT_NAMES: dict[str, str] = {}

# Full set of known agent IDs (populated at startup) — used for membership
# tests so that channel-based routing can find amz-ceo even when 'ceo'
# short name is bound to app-studio-ceo in _AGENT_SHORT_NAMES.
_KNOWN_AGENTS: set[str] = set()


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
        "app-studio-game-designer",
        "amz-analyst-daily",
    ]
    for aid in _known_agents:
        _KNOWN_AGENTS.add(aid)
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
    # Auto-discover channel → studio mapping from Slack API
    # Uses config first, then falls back to channel name pattern matching
    studio_channels_cfg = _parse_map(
        getattr(settings, "slack_studio_channels", "")
    )
    if studio_channels_cfg:
        for studio_id, ch_id in studio_channels_cfg.items():
            _CHANNEL_STUDIO_MAP[ch_id] = studio_id
    elif bot_token:
        # Auto-discover: fetch channel list and match by name pattern
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://slack.com/api/conversations.list",
                    params={"types": "public_channel,private_channel", "limit": 100},
                    headers={"Authorization": f"Bearer {bot_token}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        for ch in data.get("channels") or []:
                            ch_name = ch.get("name", "").lower()
                            ch_id = ch.get("id", "")
                            for pattern, studio_id in _CHANNEL_NAME_PATTERNS.items():
                                if ch_name.startswith(pattern):
                                    _CHANNEL_STUDIO_MAP[ch_id] = studio_id
                                    log.info(
                                        "slack_routing.channel_mapped",
                                        channel=ch_name,
                                        channel_id=ch_id,
                                        studio_id=studio_id,
                                    )
                                    break
        except Exception as exc:
            log.warning("slack_routing.channel_discovery_error", error=str(exc))

    log.info(
        "slack_routing.ready",
        bot_count=len(_BOT_USER_MAP),
        single_app=bool(_SINGLE_APP_BOT_UID),
        channel_mappings=len(_CHANNEL_STUDIO_MAP),
    )


def _resolve_channel_studio(channel_id: str) -> str | None:
    """Resolve channel_id → studio_id on-the-fly via conversations.info.

    Called when a channel isn't in _CHANNEL_STUDIO_MAP yet (bot wasn't a
    member at startup, or conversations.list missed it). Result is cached.
    """
    bot_token = settings.slack_bot_token
    if not bot_token:
        return None
    try:
        import httpx as _httpx
        # Sync call — we're in a sync function context. Use a short timeout.
        with _httpx.Client(timeout=5.0) as client:
            resp = client.get(
                "https://slack.com/api/conversations.info",
                params={"channel": channel_id},
                headers={"Authorization": f"Bearer {bot_token}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    ch_name = (data.get("channel") or {}).get("name", "").lower()
                    for pattern, studio_id in _CHANNEL_NAME_PATTERNS.items():
                        if ch_name.startswith(pattern):
                            _CHANNEL_STUDIO_MAP[channel_id] = studio_id
                            log.info(
                                "slack_routing.channel_resolved",
                                channel=ch_name,
                                channel_id=channel_id,
                                studio_id=studio_id,
                            )
                            return studio_id
    except Exception as exc:
        log.warning("slack_routing.channel_resolve_error", error=str(exc)[:100])
    return None


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
        # Use _KNOWN_AGENTS (full set) not _AGENT_SHORT_NAMES.values()
        # because short-name map has last-write-wins collisions.
        all_agents = _KNOWN_AGENTS

        # First: try channel-based prefix (auto-discovered or configured)
        # If channel not in map yet, try to resolve it on-the-fly
        studio_id = _CHANNEL_STUDIO_MAP.get(channel)
        if studio_id is None and channel:
            studio_id = _resolve_channel_studio(channel)

        if studio_id:
            if studio_id == "amz":
                full_id = f"amz-{candidate}"
            elif studio_id == "app-studio":
                full_id = f"app-studio-{candidate}"
            else:
                full_id = f"{studio_id}-{candidate}"
            if full_id in all_agents:
                return full_id

        # Fallback: direct short name match (no channel context)
        if candidate in _AGENT_SHORT_NAMES:
            return _AGENT_SHORT_NAMES[candidate]

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
