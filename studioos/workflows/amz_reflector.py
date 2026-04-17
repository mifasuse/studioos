"""amz_reflector workflow — Milestone 13: daily reflection / learning loop.

Once a day, scan the last 24 hours of agent_runs, events, verdicts,
tool_calls; build a structured digest; ask MiniMax to interpret it
(what worked, what failed, what to change tomorrow); persist the
reflection as a `memory_episodic` row; and send a morning summary
to Telegram.

This is the first agent that consumes its own organization's history
rather than external Amazon data.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import desc, func, select

from studioos.db import session_scope
from studioos.logging import get_logger
from studioos.models import (
    AgentRun,
    Event,
    MemoryEpisodic,
    MemoryProcedural,
    ToolCall,
)
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state
from studioos.workflows.outcome_checker import (
    OUTCOME_RULES,
    evaluate_discovery_outcome,
    evaluate_reprice_outcome,
    should_check_now,
    update_strategy_stats,
)

log = get_logger(__name__)


class ReflectorState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    # populated during run
    digest: dict[str, Any]
    previous_reflection: str | None
    current_playbook: str | None
    current_playbook_version: int
    reflection: str
    new_playbook: str | None
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str
    # M36: learning feedback loop
    strategy_stats: dict[str, Any]
    outcome_results: list[dict[str, Any]]
    learning_insight: str | None


SYSTEM_PROMPT = """You are the daily reflection agent for an autonomous
Amazon TR→US arbitrage studio. You receive a structured digest of the
last 24 hours of activity (which agents ran, what they decided, what
events fired, how much was spent on tools/LLM, what failed) plus —
when available — the previous day's reflection.

Your reflection has 5 sections, each ≤ 4 bullet points:

  Çalıştı  — what went well
  Dikkat   — anomalies, signals to watch, near-misses
  Düzelt   — concrete changes for tomorrow (cron cadence, threshold,
             prompt tweak, missing data)
  İlerleme — for each "Düzelt" from the previous reflection: did it
             actually get fixed? mark ✓ done / ✗ open / ~ partial
  Sayılar  — 1-2 metrics worth highlighting

Style: terse, factual, concrete. No hype, no filler. Reply in plain
Markdown — no code fences, no JSON, no preamble.
"""


PLAYBOOK_PROMPT = """You are the playbook curator for the AMZ studio.
You have just received today's reflection AND the current playbook
(if any). Your job is to update the playbook — a small set of stable
operating rules the agents should follow tomorrow.

Rules:
- A playbook entry is a single concrete rule (ex: "Reject discoveries
  with sales_rank=null AND review_count<10 — too risky for B2B niche").
- Keep the playbook ≤ 12 rules total. If a new rule conflicts with an
  old one, replace.
- Drop rules that haven't paid off in 5 reflections.
- Prefer specific over generic; reference exact thresholds.

Reply in plain Markdown:

# AMZ Playbook v{next_version}
## Filtreler
- ...
## Kararlar
- ...
## Operasyon
- ...

No preamble.
"""


async def _build_digest(window_hours: int = 24) -> dict[str, Any]:
    """Aggregate the last N hours of platform activity."""
    since = datetime.now(UTC) - timedelta(hours=window_hours)
    async with session_scope() as session:
        # Run state histogram by agent
        run_rows = (
            await session.execute(
                select(AgentRun.agent_id, AgentRun.state, func.count())
                .where(AgentRun.created_at >= since)
                .group_by(AgentRun.agent_id, AgentRun.state)
            )
        ).all()

        # Event type histogram
        event_rows = (
            await session.execute(
                select(Event.event_type, func.count())
                .where(Event.recorded_at >= since)
                .group_by(Event.event_type)
            )
        ).all()

        # Tool usage + cost
        tool_rows = (
            await session.execute(
                select(
                    ToolCall.tool_name,
                    func.count(),
                    func.coalesce(func.sum(ToolCall.cost_cents), 0),
                )
                .where(ToolCall.called_at >= since)
                .group_by(ToolCall.tool_name)
            )
        ).all()

        # Failed runs (errors)
        fail_rows = (
            (
                await session.execute(
                    select(AgentRun.agent_id, AgentRun.error)
                    .where(AgentRun.created_at >= since)
                    .where(
                        AgentRun.state.in_(
                            ("failed", "timed_out", "dead", "budget_exceeded")
                        )
                    )
                    .order_by(desc(AgentRun.created_at))
                    .limit(10)
                )
            ).all()
        )

        # Recent confirmed/rejected opportunities
        verdict_rows = (
            (
                await session.execute(
                    select(Event)
                    .where(
                        Event.event_type.in_(
                            (
                                "amz.opportunity.confirmed",
                                "amz.opportunity.rejected",
                                "amz.opportunity.discovered",
                                "amz.reprice.recommended",
                                "amz.price.anomaly_detected",
                            )
                        )
                    )
                    .where(Event.recorded_at >= since)
                    .order_by(desc(Event.recorded_at))
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )

    runs_by_agent: dict[str, dict[str, int]] = {}
    for agent_id, state, count in run_rows:
        runs_by_agent.setdefault(agent_id, {})[state] = int(count)

    return {
        "window_hours": window_hours,
        "runs_by_agent": runs_by_agent,
        "events_by_type": {t: int(c) for t, c in event_rows},
        "tools": {
            name: {"calls": int(c), "cost_cents": int(cost)}
            for name, c, cost in tool_rows
        },
        "total_tool_cost_cents": sum(int(cost) for _, _, cost in tool_rows),
        "failures": [
            {
                "agent_id": aid,
                "type": (err or {}).get("type"),
                "message": (err or {}).get("message", "")[:200],
            }
            for aid, err in fail_rows
        ],
        "verdicts": [
            {
                "type": e.event_type,
                "asin": e.payload.get("asin"),
                "verdict": e.payload.get("verdict"),
                "confidence": e.payload.get("confidence"),
                "rationale": (e.payload.get("rationale") or "")[:160],
                "estimated_profit_usd": e.payload.get("estimated_profit_usd"),
                "roi_pct": e.payload.get("roi_pct"),
            }
            for e in verdict_rows
        ],
    }


_PLAYBOOK_ID = "amz_playbook"


async def _load_previous_reflection(agent_id: str) -> str | None:
    """Fetch yesterday's reflection summary for self-reading."""
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    async with session_scope() as session:
        row = (
            await session.execute(
                select(MemoryEpisodic)
                .where(MemoryEpisodic.agent_id == agent_id)
                .where(MemoryEpisodic.date >= yesterday - timedelta(days=2))
                .order_by(desc(MemoryEpisodic.date))
                .limit(1)
            )
        ).scalar_one_or_none()
    return row.summary if row else None


async def _load_active_playbook() -> tuple[str | None, int]:
    """Return (content, version) of the currently active playbook, or (None, 0)."""
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
        if row is None:
            return None, 0
        return row.content, int(row.version)


async def node_collect(state: ReflectorState) -> dict[str, Any]:
    goals = state.get("goals") or {}
    window = int(goals.get("window_hours", 24))
    digest = await _build_digest(window)
    prev_reflection = await _load_previous_reflection(state["agent_id"])
    playbook, version = await _load_active_playbook()
    return {
        "digest": digest,
        "previous_reflection": prev_reflection,
        "current_playbook": playbook,
        "current_playbook_version": version,
    }


async def node_reflect(state: ReflectorState) -> dict[str, Any]:
    digest = state.get("digest") or {}
    prev = state.get("previous_reflection")
    playbook = state.get("current_playbook")

    sections: list[str] = []
    if prev:
        sections.append(
            "## Önceki gün reflection (önceki Düzelt'leri buradan oku):\n"
            + prev[:3000]
        )
    if playbook:
        sections.append("## Mevcut playbook:\n" + playbook[:2000])
    sections.append(
        "## Bugünün 24 saat özeti:\n```json\n"
        + json.dumps(digest, ensure_ascii=False, indent=2)[:5000]
        + "\n```"
    )

    user = "\n\n".join(sections)

    result = await invoke_from_state(
        state,
        "llm.chat",
        {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "max_tokens": 2000,
            "temperature": 0.2,
        },
    )
    if result["status"] != "ok":
        log.warning(
            "amz_reflector.llm_failed",
            status=result["status"],
            error=result.get("error"),
        )
        return {"reflection": "_LLM çağrısı başarısız_"}
    content = (result["data"] or {}).get("content", "")
    return {"reflection": content.strip()}


async def node_update_playbook(state: ReflectorState) -> dict[str, Any]:
    """Ask the LLM to evolve the playbook from today's reflection."""
    reflection = state.get("reflection") or ""
    current = state.get("current_playbook") or "(empty)"
    next_version = int(state.get("current_playbook_version", 0)) + 1

    user = (
        f"## Bugünkü reflection\n{reflection[:3000]}\n\n"
        f"## Mevcut playbook\n{current[:3000]}\n\n"
        f"Yeni sürüm v{next_version} olacak."
    )
    result = await invoke_from_state(
        state,
        "llm.chat",
        {
            "messages": [
                {
                    "role": "system",
                    "content": PLAYBOOK_PROMPT.format(next_version=next_version),
                },
                {"role": "user", "content": user},
            ],
            "max_tokens": 1500,
            "temperature": 0.1,
        },
    )
    if result["status"] != "ok":
        log.warning(
            "amz_reflector.playbook_failed",
            error=result.get("error"),
        )
        return {"new_playbook": None}
    return {"new_playbook": (result["data"] or {}).get("content", "").strip()}


async def node_persist(state: ReflectorState) -> dict[str, Any]:
    digest = state.get("digest") or {}
    reflection = state.get("reflection") or ""
    new_playbook = state.get("new_playbook")
    next_version = int(state.get("current_playbook_version", 0)) + 1
    today = datetime.now(UTC).date()

    # Persist as an episodic memory row (one per day per agent).
    async with session_scope() as session:
        existing = (
            await session.execute(
                select(MemoryEpisodic).where(
                    MemoryEpisodic.agent_id == state["agent_id"],
                    MemoryEpisodic.date == today,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.content = json.dumps(digest, ensure_ascii=False)[:8000]
            existing.summary = reflection[:4000]
            existing.events_count = sum(digest.get("events_by_type", {}).values())
        else:
            session.add(
                MemoryEpisodic(
                    agent_id=state["agent_id"],
                    date=today,
                    content=json.dumps(digest, ensure_ascii=False)[:8000],
                    summary=reflection[:4000],
                    events_count=sum(
                        digest.get("events_by_type", {}).values()
                    ),
                )
            )

        # Persist the new playbook version (if the LLM returned one)
        # and deactivate any older active rows.
        if new_playbook:
            await session.execute(
                MemoryProcedural.__table__.update()
                .where(MemoryProcedural.id == _PLAYBOOK_ID)
                .where(MemoryProcedural.active.is_(True))
                .values(active=False)
            )
            session.add(
                MemoryProcedural(
                    id=_PLAYBOOK_ID,
                    studio_id=state.get("studio_id"),
                    version=next_version,
                    content=new_playbook[:8000],
                    author=state["agent_id"],
                    change_summary=f"Auto-evolved by reflector on {today.isoformat()}",
                    active=True,
                )
            )

    playbook_msg = ""
    if new_playbook:
        playbook_msg = (
            f"\n\n*📋 Playbook v{next_version} (auto-evolved):*\n"
            + new_playbook[:1500]
        )

    notify_text = (
        f"*🌅 StudioOS Daily Reflection — {today.isoformat()}*\n\n"
        f"{reflection[:2500]}"
        f"{playbook_msg}"
    )
    notify = await invoke_from_state(
        state,
        "telegram.notify",
        {
            "text": notify_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
    )
    notified = notify["status"] == "ok"

    state_accum = dict(state.get("state") or {})
    state_accum["reflections_total"] = (
        int(state_accum.get("reflections_total", 0)) + 1
    )
    state_accum["last_reflection_date"] = today.isoformat()

    return {
        "memories": [
            {
                "content": (
                    f"Daily reflection {today.isoformat()}: "
                    + reflection[:300]
                ),
                "tags": ["reflection", "daily", today.isoformat()],
                "importance": 0.7,
            }
        ],
        "kpi_updates": [
            {
                "name": "reflections_total",
                "value": state_accum["reflections_total"],
            }
        ],
        "state": state_accum,
        "summary": (
            f"Reflected on {sum(digest.get('events_by_type', {}).values())} "
            f"events, {sum(sum(v.values()) for v in digest.get('runs_by_agent', {}).values())} runs"
            + (" (notified)" if notified else "")
        ),
    }


LEARNING_INSIGHT_PROMPT = """Sen bir Amazon arbitraj stüdyosunun öğrenme analisti olarak görev yapıyorsun.
Sana bu haftanın strateji performans istatistikleri ve sonuç kontrol bulguları verilecek.
Bunlara dayanarak kısa, eyleme dönüştürülebilir bir öğrenme insight'ı üret.

Format: ≤ 5 madde, terse ve somut. Türkçe yaz.
"""


async def node_check_outcomes(state: ReflectorState) -> dict[str, Any]:
    """Check outcomes of past actions and update strategy stats."""
    since = datetime.now(UTC) - timedelta(days=7)
    checkable_types = list(OUTCOME_RULES.keys())

    async with session_scope() as session:
        event_rows = (
            await session.execute(
                select(Event)
                .where(Event.event_type.in_(checkable_types))
                .where(Event.recorded_at >= since)
                .order_by(desc(Event.recorded_at))
                .limit(100)
            )
        ).scalars().all()

    now = datetime.now(UTC)
    outcome_results: list[dict[str, Any]] = []
    strategy_stats: dict[str, Any] = dict(state.get("strategy_stats") or {})

    # Fetch current lost buybox ASINs once (if there are reprice events to check)
    reprice_events = [
        e for e in event_rows
        if e.event_type == "amz.reprice.recommended" and should_check_now(e.event_type, e.recorded_at, now)
    ]
    lost_buybox_asins: set[str] = set()
    if reprice_events:
        tool_result = await invoke_from_state(
            state,
            "buyboxpricer.db.lost_buybox",
            {"limit": 100},
        )
        if tool_result["status"] == "ok":
            items = (tool_result.get("data") or {}).get("items", [])
            lost_buybox_asins = {item["asin"] for item in items if item.get("asin")}

    # Fetch confirmed opportunity ASINs for discovery events
    discovery_events = [
        e for e in event_rows
        if e.event_type == "amz.opportunity.discovered" and should_check_now(e.event_type, e.recorded_at, now)
    ]
    confirmed_asins: set[str] = set()
    if discovery_events:
        async with session_scope() as session:
            confirmed_rows = (
                await session.execute(
                    select(Event.payload)
                    .where(Event.event_type == "amz.opportunity.confirmed")
                    .where(Event.recorded_at >= since)
                )
            ).all()
            confirmed_asins = {
                row[0].get("asin") for row in confirmed_rows if row[0].get("asin")
            }

    for event in event_rows:
        if not should_check_now(event.event_type, event.recorded_at, now):
            continue

        asin = event.payload.get("asin", "")
        if not asin:
            continue

        if event.event_type == "amz.reprice.recommended":
            result = evaluate_reprice_outcome(asin, lost_buybox_asins)
            strategy = event.payload.get("strategy", "reprice")
            strategy_stats = update_strategy_stats(strategy_stats, strategy, result["outcome"])
            outcome_results.append({
                "event_type": event.event_type,
                "asin": asin,
                **result,
            })

        elif event.event_type == "amz.opportunity.discovered":
            result = evaluate_discovery_outcome(asin, confirmed_asins)
            strategy = event.payload.get("strategy", "discovery")
            if result["outcome"] != "pending":
                strategy_stats = update_strategy_stats(strategy_stats, strategy, result["outcome"])
            outcome_results.append({
                "event_type": event.event_type,
                "asin": asin,
                **result,
            })

    log.info(
        "amz_reflector.check_outcomes",
        checked=len(outcome_results),
        strategies=list(strategy_stats.keys()),
    )

    return {
        "strategy_stats": strategy_stats,
        "outcome_results": outcome_results,
    }


async def node_learning_insight(state: ReflectorState) -> dict[str, Any]:
    """Generate a learning insight from strategy stats and outcome results."""
    strategy_stats = state.get("strategy_stats") or {}
    outcome_results = state.get("outcome_results") or []

    if not strategy_stats and not outcome_results:
        return {"learning_insight": None}

    stats_text = json.dumps(strategy_stats, ensure_ascii=False, indent=2)
    outcomes_text = json.dumps(outcome_results[:20], ensure_ascii=False, indent=2)

    user = (
        f"Bu hafta strateji performansı:\n```json\n{stats_text}\n```\n\n"
        f"Sonuç kontrol bulguları ({len(outcome_results)} adet):\n```json\n{outcomes_text}\n```\n\n"
        "Öğrenme insight'ı üret."
    )

    result = await invoke_from_state(
        state,
        "llm.chat",
        {
            "messages": [
                {"role": "system", "content": LEARNING_INSIGHT_PROMPT},
                {"role": "user", "content": user},
            ],
            "max_tokens": 800,
            "temperature": 0.2,
        },
    )

    if result["status"] != "ok":
        log.warning(
            "amz_reflector.learning_insight_failed",
            error=result.get("error"),
        )
        return {"learning_insight": None}

    insight = (result["data"] or {}).get("content", "").strip()

    # Persist insight as procedural memory
    if insight:
        today = datetime.now(UTC).date()
        async with session_scope() as session:
            session.add(
                MemoryProcedural(
                    id=f"amz_learning_insight_{today.isoformat()}",
                    studio_id=state.get("studio_id"),
                    version=1,
                    content=insight[:4000],
                    author=state["agent_id"],
                    change_summary=f"Learning insight auto-generated on {today.isoformat()}",
                    active=True,
                )
            )

    log.info("amz_reflector.learning_insight_written", chars=len(insight))

    return {"learning_insight": insight}


def build_graph() -> Any:
    graph = StateGraph(ReflectorState)
    graph.add_node("collect", node_collect)
    graph.add_node("reflect", node_reflect)
    graph.add_node("check_outcomes", node_check_outcomes)
    graph.add_node("learning_insight", node_learning_insight)
    graph.add_node("update_playbook", node_update_playbook)
    graph.add_node("persist", node_persist)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "reflect")
    graph.add_edge("reflect", "check_outcomes")
    graph.add_edge("check_outcomes", "learning_insight")
    graph.add_edge("learning_insight", "update_playbook")
    graph.add_edge("update_playbook", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_reflector", 1, compiled)
