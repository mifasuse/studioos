"""Studio configuration loader — reads YAML, seeds DB."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from studioos.logging import get_logger
from studioos.models import (
    Agent,
    AgentState,
    AgentTemplate,
    Studio,
    Subscription,
)

log = get_logger(__name__)

STUDIOS_ROOT = Path(__file__).parent


def list_studio_configs() -> list[Path]:
    return sorted(STUDIOS_ROOT.glob("*/studio.yaml"))


def load_studio_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def seed_studio(session: AsyncSession, config: dict[str, Any]) -> None:
    """Upsert studio + templates + agents + subscriptions from a config dict."""
    studio_id = config["id"]

    # Studio row
    existing = (
        await session.execute(select(Studio).where(Studio.id == studio_id))
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            Studio(
                id=studio_id,
                display_name=config["display_name"],
                mission=config.get("mission"),
                status=config.get("status", "active"),
            )
        )
        await session.flush()
        log.info("seed.studio_created", studio_id=studio_id)
    else:
        existing.display_name = config["display_name"]
        existing.mission = config.get("mission")
        existing.status = config.get("status", "active")

    # Templates
    for tmpl in config.get("templates", []):
        existing_tmpl = (
            await session.execute(
                select(AgentTemplate).where(
                    AgentTemplate.id == tmpl["id"],
                    AgentTemplate.version == tmpl["version"],
                )
            )
        ).scalar_one_or_none()
        if existing_tmpl is None:
            session.add(
                AgentTemplate(
                    id=tmpl["id"],
                    version=tmpl["version"],
                    display_name=tmpl["display_name"],
                    description=tmpl.get("description"),
                    workflow_ref=tmpl["workflow_ref"],
                    required_tools=tmpl.get("required_tools"),
                )
            )
            log.info(
                "seed.template_created",
                template=tmpl["id"],
                version=tmpl["version"],
            )

    await session.flush()

    # Agents
    for agent_cfg in config.get("agents", []):
        existing_agent = (
            await session.execute(select(Agent).where(Agent.id == agent_cfg["id"]))
        ).scalar_one_or_none()
        if existing_agent is None:
            session.add(
                Agent(
                    id=agent_cfg["id"],
                    studio_id=studio_id,
                    template_id=agent_cfg["template_id"],
                    template_version=agent_cfg["template_version"],
                    display_name=agent_cfg.get("display_name"),
                    slack_handle=agent_cfg.get("slack_handle"),
                    mode=agent_cfg.get("mode", "normal"),
                    heartbeat_config=agent_cfg.get("heartbeat_config"),
                    goals=agent_cfg.get("goals"),
                    tool_scope=agent_cfg.get("tool_scope"),
                    budget_tier=agent_cfg.get("budget_tier"),
                    schedule_cron=agent_cfg.get("schedule_cron"),
                )
            )
            await session.flush()
            session.add(AgentState(agent_id=agent_cfg["id"], state={}))
            log.info("seed.agent_created", agent_id=agent_cfg["id"])
        else:
            existing_agent.mode = agent_cfg.get("mode", existing_agent.mode)
            existing_agent.goals = agent_cfg.get("goals", existing_agent.goals)
            if "tool_scope" in agent_cfg:
                existing_agent.tool_scope = agent_cfg.get("tool_scope")
            if "schedule_cron" in agent_cfg:
                existing_agent.schedule_cron = agent_cfg.get("schedule_cron")

    await session.flush()

    # Subscriptions — sync from YAML (add new, delete removed)
    yaml_subs = {
        (sub["subscriber"], sub["event_pattern"])
        for sub in config.get("subscriptions", [])
    }
    # Get all agent IDs in this studio to scope deletion
    studio_agent_ids = {a["id"] for a in config.get("agents", [])}

    for sub in config.get("subscriptions", []):
        existing_sub = (
            await session.execute(
                select(Subscription).where(
                    Subscription.subscriber_id == sub["subscriber"],
                    Subscription.event_pattern == sub["event_pattern"],
                )
            )
        ).scalar_one_or_none()
        if existing_sub is None:
            session.add(
                Subscription(
                    subscriber_type="agent",
                    subscriber_id=sub["subscriber"],
                    event_pattern=sub["event_pattern"],
                    filter=sub.get("filter"),
                    action=sub.get("action", "wake_agent"),
                    priority=sub.get("priority", 50),
                )
            )
            log.info(
                "seed.subscription_created",
                subscriber=sub["subscriber"],
                pattern=sub["event_pattern"],
            )

    # Delete subscriptions that are in DB but not in YAML (for this studio's agents)
    if studio_agent_ids:
        db_subs = (
            await session.execute(
                select(Subscription).where(
                    Subscription.subscriber_id.in_(studio_agent_ids)
                )
            )
        ).scalars().all()
        for db_sub in db_subs:
            key = (db_sub.subscriber_id, db_sub.event_pattern)
            if key not in yaml_subs:
                await session.delete(db_sub)
                log.info(
                    "seed.subscription_deleted",
                    subscriber=db_sub.subscriber_id,
                    pattern=db_sub.event_pattern,
                )


async def seed_all(session: AsyncSession) -> int:
    count = 0
    for path in list_studio_configs():
        config = load_studio_config(path)
        await seed_studio(session, config)
        count += 1
    return count
