"""Per-agent Slack token + channel routing — OpenClaw SLACK_POSTING.md."""
from __future__ import annotations

import pytest

from studioos.config import settings
from studioos.tools.slack import _parse_map, _resolve_slack_route


def test_parse_map_handles_spaces_and_empty() -> None:
    m = _parse_map("amz-ceo=xoxb-1, amz-scout=xoxb-2 ,  , bad")
    assert m == {"amz-ceo": "xoxb-1", "amz-scout": "xoxb-2"}


def test_parse_map_none_input() -> None:
    assert _parse_map("") == {}


@pytest.fixture
def slack_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-default")
    monkeypatch.setattr(settings, "slack_default_channel", "#amz-hq")
    monkeypatch.setattr(
        settings,
        "slack_agent_tokens",
        "amz-ceo=xoxb-ceo,amz-scout=xoxb-scout",
    )
    monkeypatch.setattr(
        settings,
        "slack_agent_channels",
        "amz-scout=#amz-opportunities,amz-pricer=#amz-pricing",
    )


def test_resolve_uses_per_agent_token(slack_env: None) -> None:
    token, channel = _resolve_slack_route("amz-ceo")
    assert token == "xoxb-ceo"
    assert channel == "#amz-hq"  # no per-agent channel → default


def test_resolve_uses_per_agent_channel(slack_env: None) -> None:
    token, channel = _resolve_slack_route("amz-pricer")
    # amz-pricer has no per-agent token → falls back to default
    assert token == "xoxb-default"
    assert channel == "#amz-pricing"


def test_resolve_falls_back_to_defaults(slack_env: None) -> None:
    token, channel = _resolve_slack_route("amz-unknown")
    assert token == "xoxb-default"
    assert channel == "#amz-hq"


def test_resolve_no_agent(slack_env: None) -> None:
    token, channel = _resolve_slack_route(None)
    assert token == "xoxb-default"
    assert channel == "#amz-hq"


from studioos.slack_routing import resolve_agent_from_mention, clean_mention_text, _BOT_USER_MAP


def test_resolve_agent_from_mention() -> None:
    _BOT_USER_MAP["U0ABC123"] = "amz-pricer"
    _BOT_USER_MAP["U0DEF456"] = "amz-analyst"
    assert resolve_agent_from_mention("<@U0ABC123> check this") == "amz-pricer"
    assert resolve_agent_from_mention("no mention here") is None
    assert resolve_agent_from_mention("<@U9999999> unknown") is None
    # Cleanup
    _BOT_USER_MAP.clear()


def test_clean_mention_text() -> None:
    assert clean_mention_text("<@U0ABC123> check pricing") == "check pricing"
    assert clean_mention_text("<@U0ABC123>   multiple  <@U0DEF456> mentions") == "multiple  mentions"


def test_slack_event_schemas_registered() -> None:
    import studioos.events.schemas_slack  # noqa: F401
    from studioos.events.registry import registry
    assert registry.get("slack.mention.received", 1) is not None
    assert registry.get("slack.mention.responded", 1) is not None
