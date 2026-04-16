"""Slack inbound event schemas (M32)."""
from __future__ import annotations
from pydantic import BaseModel
from studioos.events.registry import registry


class SlackMentionReceivedV1(BaseModel):
    """slack.mention.received — an agent was @mentioned in Slack."""
    agent_id: str
    studio_id: str = ""
    text: str = ""
    user: str = ""
    channel: str = ""
    thread_ts: str = ""
    message_ts: str = ""


class SlackMentionRespondedV1(BaseModel):
    """slack.mention.responded — agent posted a reply in the thread."""
    agent_id: str
    text: str = ""
    channel: str = ""
    thread_ts: str = ""
    response_ts: str = ""


registry.register("slack.mention.received", 1, SlackMentionReceivedV1)
registry.register("slack.mention.responded", 1, SlackMentionRespondedV1)
