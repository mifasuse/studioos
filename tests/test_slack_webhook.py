"""Slack webhook endpoint + cascade protection tests."""
from __future__ import annotations

import pytest
from studioos.slack_routing import check_cascade, detect_mentions_in_response, _BOT_USER_MAP, _THREAD_MENTION_COUNTS


def test_cascade_allows_first_mention() -> None:
    _THREAD_MENTION_COUNTS.clear()
    assert check_cascade("t1", "amz-pricer") is True


def test_cascade_blocks_after_max() -> None:
    _THREAD_MENTION_COUNTS.clear()
    for _ in range(3):
        check_cascade("t2", "amz-pricer")
    assert check_cascade("t2", "amz-pricer") is False


def test_cascade_blocks_self_mention() -> None:
    _THREAD_MENTION_COUNTS.clear()
    assert check_cascade("t3", "amz-pricer", responding_agent_id="amz-pricer") is False


def test_detect_mentions_in_response() -> None:
    _BOT_USER_MAP["U111"] = "amz-analyst"
    _BOT_USER_MAP["U222"] = "amz-pricer"
    mentions = detect_mentions_in_response("<@U111> check this", "amz-pricer")
    assert mentions == ["amz-analyst"]
    # Self-mention filtered
    mentions = detect_mentions_in_response("<@U222> self", "amz-pricer")
    assert mentions == []
    _BOT_USER_MAP.clear()


def test_verify_signature_skips_when_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from studioos.config import settings
    from studioos.api.slack_events import verify_slack_signature
    monkeypatch.setattr(settings, "slack_signing_secret", "")
    assert verify_slack_signature(b"body", "9999999999", "v0=fake") is True


def test_studio_for_agent() -> None:
    from studioos.api.slack_events import _studio_for_agent
    assert _studio_for_agent("amz-pricer") == "amz"
    assert _studio_for_agent("app-studio-ceo") == "app-studio"
    assert _studio_for_agent("unknown") == ""
