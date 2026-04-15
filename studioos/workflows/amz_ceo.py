"""amz_ceo workflow — weekly strategic decision agent.

Mirrors the OpenClaw amz-ceo role at a coarse grain: once a week,
read the last 7 days of confirmed/rejected verdicts, pricer
recommendations, scout discoveries, runs/failures, and active KPI
state. Hand the digest + active playbook to MiniMax with a tight
"top 3 things that moved ROI" prompt and post the result to Slack
(amz channel) plus Telegram.

This is a META agent — it doesn't execute, it interprets and
directs. The output is a written brief, not a run trigger.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import desc, func, select

from studioos.db import session_scope
from studioos.kpi.store import get_current_state
from studioos.logging import get_logger
from studioos.models import (
    AgentRun,
    Event,
    MemoryProcedural,
    ToolCall,
)
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)

_PLAYBOOK_ID = "amz_playbook"


class CEOState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    digest: dict[str, Any]
    playbook: str | None
    brief: str
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


SYSTEM_PROMPT = """You are the CEO of an autonomous Amazon TR→US
arbitrage studio. You run once a week. Your job is to (a) summarize
what happened, (b) decide what the next 7 days should focus on, and
(c) delegate concrete tasks to specific agents.

You receive:
  - 7-day digest of agent runs, verdicts, scout discoveries,
    pricer recommendations, tool spend, failures
  - Current KPI snapshot
  - Active playbook (operating rules)

Available agents you can delegate to:
  amz-monitor, amz-scout, amz-analyst, amz-pricer, amz-repricer,
  amz-crosslister, amz-admanager, amz-qa, amz-dev

Your reply is in TWO parts.

PART 1 — Brief (Turkish, plain Markdown), 4 sections:

  ## Bu hafta ne oldu
  3-5 bullet, somut sayılarla.

  ## Bu haftanın 3 ROI etkisi
  ROI/profit'i en çok etkileyen 3 şey.

  ## Önümüzdeki hafta ne yapacağız
  3 somut karar.

  ## Risk ve eşikler
  1-2 dikkat noktası.

PART 2 — Delegations as a fenced JSON code block at the very end:

```json
{
  "tasks": [
    {
      "target_agent": "amz-pricer",
      "title": "Aging stock review",
      "description": "Check inventory > 60 days, recommend price drops",
      "priority": "high"
    }
  ]
}
```

Each task: target_agent (must be one of the available list),
title (≤ 60 chars), description (≤ 240 chars), priority
("emergency" | "high" | "normal" | "low"). Maximum 5 tasks. Skip
the JSON block entirely if no delegations needed.

Style: terse, factual, kanıt odaklı.
"""


async def _build_weekly_digest() -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=7)
    async with session_scope() as session:
        run_rows = (
            await session.execute(
                select(AgentRun.agent_id, AgentRun.state, func.count())
                .where(AgentRun.studio_id == "amz")
                .where(AgentRun.created_at >= since)
                .group_by(AgentRun.agent_id, AgentRun.state)
            )
        ).all()
        verdict_rows = (
            (
                await session.execute(
                    select(Event)
                    .where(Event.studio_id == "amz")
                    .where(Event.recorded_at >= since)
                    .where(
                        Event.event_type.in_(
                            (
                                "amz.opportunity.confirmed",
                                "amz.opportunity.rejected",
                                "amz.opportunity.discovered",
                                "amz.reprice.recommended",
                            )
                        )
                    )
                    .order_by(desc(Event.recorded_at))
                    .limit(40)
                )
            )
            .scalars()
            .all()
        )
        tool_rows = (
            await session.execute(
                select(
                    ToolCall.tool_name,
                    func.count(),
                    func.coalesce(func.sum(ToolCall.cost_cents), 0),
                )
                .join(AgentRun, AgentRun.id == ToolCall.run_id)
                .where(AgentRun.studio_id == "amz")
                .where(ToolCall.called_at >= since)
                .group_by(ToolCall.tool_name)
            )
        ).all()
        kpi_views = await get_current_state(session, studio_id="amz")

    runs_by_agent: dict[str, dict[str, int]] = {}
    for agent_id, state, count in run_rows:
        runs_by_agent.setdefault(agent_id, {})[state] = int(count)

    confirmed = [
        v for v in verdict_rows if v.event_type == "amz.opportunity.confirmed"
    ]
    rejected = [
        v for v in verdict_rows if v.event_type == "amz.opportunity.rejected"
    ]
    discovered = [
        v for v in verdict_rows if v.event_type == "amz.opportunity.discovered"
    ]
    reprices = [
        v for v in verdict_rows if v.event_type == "amz.reprice.recommended"
    ]

    return {
        "window_days": 7,
        "runs_by_agent": runs_by_agent,
        "tools": {
            name: {"calls": int(c), "cost_cents": int(cost)}
            for name, c, cost in tool_rows
        },
        "total_tool_cost_cents": sum(int(cost) for _, _, cost in tool_rows),
        "counts": {
            "confirmed": len(confirmed),
            "rejected": len(rejected),
            "discovered": len(discovered),
            "reprice_recommended": len(reprices),
        },
        "top_confirmed": [
            {
                "asin": e.payload.get("asin"),
                "confidence": e.payload.get("confidence"),
                "rationale": (e.payload.get("rationale") or "")[:160],
                "recommended_action": e.payload.get("recommended_action"),
            }
            for e in confirmed[:5]
        ],
        "top_discovered": [
            {
                "asin": e.payload.get("asin"),
                "title": (e.payload.get("title") or "")[:80],
                "estimated_profit_usd": e.payload.get("estimated_profit_usd"),
                "roi_pct": e.payload.get("roi_pct"),
                "monthly_sold": e.payload.get("monthly_sold"),
            }
            for e in discovered[:8]
        ],
        "top_reprices": [
            {
                "asin": e.payload.get("asin"),
                "current_price": e.payload.get("current_price"),
                "proposed_price": e.payload.get("proposed_price"),
                "delta": e.payload.get("delta"),
            }
            for e in reprices[:5]
        ],
        "kpis": [
            {
                "name": s.name,
                "current": float(s.current) if s.current is not None else None,
                "target": float(s.target) if s.target is not None else None,
                "direction": s.direction,
                "reached": s.gap.reached if s.gap else None,
            }
            for s in kpi_views
        ],
    }


async def _load_playbook() -> str | None:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(MemoryProcedural)
                .where(MemoryProcedural.id == _PLAYBOOK_ID)
                .where(MemoryProcedural.active.is_(True))
                .order_by(desc(MemoryProcedural.version))
                .limit(1)
            )
        ).scalar_one_or_none()
    return row.content if row else None


async def node_collect(state: CEOState) -> dict[str, Any]:
    digest = await _build_weekly_digest()
    playbook = await _load_playbook()
    return {"digest": digest, "playbook": playbook}


async def node_brief(state: CEOState) -> dict[str, Any]:
    digest = state.get("digest") or {}
    playbook = state.get("playbook")
    sections = [
        "## Aktif playbook\n" + (playbook or "(yok)"),
        "## Son 7 gün özeti\n```json\n"
        + json.dumps(digest, ensure_ascii=False, indent=2, default=str)[:6000]
        + "\n```",
    ]
    user = "\n\n".join(sections)
    result = await invoke_from_state(
        state,
        "llm.chat",
        {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "max_tokens": 2500,
            "temperature": 0.2,
        },
    )
    if result["status"] != "ok":
        log.warning("amz_ceo.llm_failed", error=result.get("error"))
        return {"brief": "_LLM çağrısı başarısız_"}
    return {"brief": (result["data"] or {}).get("content", "").strip()}


_VALID_TARGETS = {
    "amz-monitor",
    "amz-scout",
    "amz-analyst",
    "amz-pricer",
    "amz-repricer",
    "amz-crosslister",
    "amz-admanager",
    "amz-qa",
    "amz-dev",
}


def _extract_tasks(brief: str) -> list[dict[str, Any]]:
    """Pull the trailing ```json {tasks: [...]} ``` block out of the brief."""
    import re

    match = re.search(
        r"```json\s*(\{[\s\S]*?\})\s*```", brief, re.MULTILINE
    )
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return []
    raw_tasks = data.get("tasks") or []
    out: list[dict[str, Any]] = []
    for t in raw_tasks[:5]:
        if not isinstance(t, dict):
            continue
        target = t.get("target_agent")
        if target not in _VALID_TARGETS:
            continue
        out.append(
            {
                "target_agent": target,
                "title": str(t.get("title", ""))[:60] or "untitled",
                "description": str(t.get("description", ""))[:240],
                "priority": (t.get("priority") or "normal").lower(),
                "payload": t.get("payload") or {},
            }
        )
    return out


async def node_publish(state: CEOState) -> dict[str, Any]:
    brief = state.get("brief") or ""
    today = datetime.now(UTC).date().isoformat()
    delegations = _extract_tasks(brief)
    # Strip the json fence from the human-facing brief so Slack/Telegram
    # don't show the raw JSON twice.
    import re

    human_brief = re.sub(
        r"```json[\s\S]*?```", "", brief, flags=re.MULTILINE
    ).strip()

    if delegations:
        lines = ["", "*Delegasyonlar:*"]
        for t in delegations:
            lines.append(
                f"• `{t['target_agent']}` _{t['priority']}_ — {t['title']}"
            )
        human_brief = human_brief + "\n" + "\n".join(lines)

    text_slack = f"*🧭 AMZ CEO — Haftalık Brief — {today}*\n\n{human_brief[:38000]}"
    text_tg = f"*🧭 AMZ CEO — Haftalık Brief — {today}*\n\n{human_brief[:3500]}"

    slack_res = await invoke_from_state(
        state,
        "slack.notify",
        {"text": text_slack, "mrkdwn": True, "unfurl_links": False},
    )
    tg_res = await invoke_from_state(
        state,
        "telegram.notify",
        {
            "text": text_tg,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
    )

    state_accum = dict(state.get("state") or {})
    state_accum["briefs_total"] = int(state_accum.get("briefs_total", 0)) + 1
    state_accum["last_delegations"] = len(delegations)

    events_out: list[dict[str, Any]] = []
    for t in delegations:
        suffix = t["target_agent"].replace("amz-", "", 1)
        events_out.append(
            {
                "event_type": f"amz.task.{suffix}",
                "event_version": 1,
                "payload": {
                    "target_agent": t["target_agent"],
                    "title": t["title"],
                    "description": t["description"],
                    "priority": t["priority"],
                    "payload": t.get("payload") or {},
                    "requested_by": "amz-ceo",
                },
                "idempotency_key": (
                    f"amz_ceo:{state['run_id']}:{t['target_agent']}:{t['title'][:32]}"
                ),
            }
        )

    return {
        "events": events_out,
        "memories": [
            {
                "content": (
                    f"Weekly CEO brief {today}: {brief[:300]} "
                    f"(delegated {len(delegations)} tasks)"
                ),
                "tags": ["amz", "ceo", "weekly", today],
                "importance": 0.8,
            }
        ],
        "kpi_updates": [
            {"name": "ceo_briefs_total", "value": state_accum["briefs_total"]},
            {"name": "ceo_delegations_last", "value": len(delegations)},
        ],
        "state": state_accum,
        "summary": (
            f"Weekly brief + {len(delegations)} delegations published "
            f"(slack={slack_res['status']}, telegram={tg_res['status']})"
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(CEOState)
    graph.add_node("collect", node_collect)
    graph.add_node("brief", node_brief)
    graph.add_node("publish", node_publish)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "brief")
    graph.add_edge("brief", "publish")
    graph.add_edge("publish", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_ceo", 1, compiled)
