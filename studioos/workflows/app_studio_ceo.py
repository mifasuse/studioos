"""app_studio_ceo — weekly strategic decisions for App Studio (M29).

Schedule: 0 9 * * 1  (every Monday at 09:00)

Workflow: START → seed_kpi → collect → brief → publish → END

seed_kpi:  First-run-only — seed 4 KPI targets.
collect:   Hub API overview per tracked app + last week's growth report
           from events table + KPI state.
brief:     LLM with Turkish prompt, max 2 decisions, task delegations JSON.
publish:   Slack + Telegram, emit app.ceo.weekly_brief + app.task.* events.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import desc, select

from studioos.db import session_scope
from studioos.kpi.store import get_current_state, upsert_target
from studioos.logging import get_logger
from studioos.models import Event
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


SYSTEM_PROMPT = (
    "Sen App Studio'nun CEO'susun. Haftada bir çalışıyorsun. "
    "Görevin: (a) bu hafta ne olduğunu özetle, "
    "(b) önümüzdeki 7 gün için en fazla 2 karar ver, "
    "(c) somut görevleri ajanlara delege et (JSON).\n\n"
    "Kullanılabilir ajanlar (delegation target): "
    "app-studio-growth-intel, app-studio-growth-exec, app-studio-pricing.\n\n"
    "KPI HEDEFLERİ:\n"
    "  - app_mrr ≥ 500 USD\n"
    "  - app_roi ≥ 2.0x\n"
    "  - app_churn_rate ≤ %10\n"
    "  - app_active_subs ≥ 200\n\n"
    "Yanıtın İKİ bölümden oluşmalı:\n\n"
    "BÖLÜM 1 — Brief (Türkçe, düz Markdown):\n\n"
    "  ## Bu hafta ne oldu\n"
    "  3-5 madde, somut sayılarla.\n\n"
    "  ## Önümüzdeki hafta kararlar (max 2)\n"
    "  Her karar için kısa gerekçe.\n\n"
    "  ## KPI durumu\n"
    "  Hedeften sapmaları listele.\n\n"
    "BÖLÜM 2 — Delegasyonlar (JSON kod bloğu, en sonda):\n\n"
    "```json\n"
    "{\n"
    "  \"tasks\": [\n"
    "    {\n"
    "      \"target_agent\": \"app-studio-growth-exec\",\n"
    "      \"title\": \"Görev başlığı (max 60 karakter)\",\n"
    "      \"description\": \"Açıklama (max 240 karakter)\",\n"
    "      \"priority\": \"high\"\n"
    "    }\n"
    "  ]\n"
    "}\n"
    "```\n\n"
    "Öneri yoksa JSON bloğunu tamamen atla. "
    "Her görev: target_agent (listedeki biri), title (≤60 karakter), "
    "description (≤240 karakter), priority (emergency|high|normal|low). "
    "Stil: kısa, somut, kanıt odaklı."
)

_VALID_TARGETS = {
    "app-studio-growth-intel",
    "app-studio-growth-exec",
    "app-studio-pricing",
    "app-studio-dev",
    "app-studio-qa",
    "app-studio-marketing",
    "app-studio-hub-dev",
}

APP_STUDIO_KPI_TARGETS = [
    ("app_mrr", 500.0, "higher_better", "USD", "App Studio aylık yinelenen gelir"),
    ("app_roi", 2.0, "higher_better", "x", "App Studio yatırım getirisi"),
    ("app_churn_rate", 10.0, "lower_better", "%", "App Studio abonelik kayıp oranı"),
    ("app_active_subs", 200.0, "higher_better", "subs", "App Studio aktif abone sayısı"),
]


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

class AppCEOState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    recent_memories: list[dict[str, Any]]
    kpis: list[dict[str, Any]]
    # populated during run
    app_overviews: dict[str, Any]
    last_growth_report: dict[str, Any] | None
    kpi_state: list[dict[str, Any]]
    brief: str
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def node_seed_kpi(state: AppCEOState) -> dict[str, Any]:
    """Idempotently seed 4 App Studio KPI targets (first-run-only)."""
    state_accum = dict(state.get("state") or {})
    if state_accum.get("kpi_targets_seeded"):
        return {}
    studio_id = state.get("studio_id") or "app"
    async with session_scope() as session:
        for name, value, direction, unit, desc in APP_STUDIO_KPI_TARGETS:
            await upsert_target(
                session,
                name=name,
                target_value=value,
                direction=direction,
                studio_id=studio_id,
                unit=unit,
                description=desc,
            )
    state_accum["kpi_targets_seeded"] = True
    return {"state": state_accum}


async def node_collect(state: AppCEOState) -> dict[str, Any]:
    """Fetch Hub API overview per tracked app, last growth report, and KPI state."""
    goals = state.get("goals") or {}
    tracked_apps: list[str] = goals.get("tracked_apps") or []
    studio_id = state.get("studio_id") or "app"
    since = datetime.now(UTC) - timedelta(days=7)

    # Hub API overview for each app
    app_overviews: dict[str, Any] = {}
    for app_id in tracked_apps:
        result = await invoke_from_state(
            state, "hub.api.overview", {"app_id": app_id, "days": 7}
        )
        app_overviews[app_id] = (
            (result.get("data") or {}) if result.get("status") == "ok" else {}
        )

    # Last week's growth report from events table
    last_growth_report: dict[str, Any] | None = None
    try:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Event)
                    .where(Event.studio_id == studio_id)
                    .where(Event.event_type == "app.growth.weekly_report")
                    .where(Event.recorded_at >= since)
                    .order_by(desc(Event.recorded_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is not None:
                last_growth_report = row.payload
    except Exception as exc:  # noqa: BLE001
        log.warning("app_studio_ceo.collect_events_failed", error=str(exc))

    # KPI state
    kpi_state: list[dict[str, Any]] = []
    try:
        async with session_scope() as session:
            kpi_views = await get_current_state(session, studio_id=studio_id)
            kpi_state = [
                {
                    "name": s.name,
                    "current": float(s.current) if s.current is not None else None,
                    "target": float(s.target) if s.target is not None else None,
                    "direction": s.direction,
                    "reached": s.gap.reached if s.gap else None,
                }
                for s in kpi_views
            ]
    except Exception as exc:  # noqa: BLE001
        log.warning("app_studio_ceo.collect_kpi_failed", error=str(exc))

    return {
        "app_overviews": app_overviews,
        "last_growth_report": last_growth_report,
        "kpi_state": kpi_state,
    }


async def node_brief(state: AppCEOState) -> dict[str, Any]:
    """Generate weekly CEO brief via LLM (Turkish)."""
    app_overviews = state.get("app_overviews") or {}
    last_growth_report = state.get("last_growth_report")
    kpi_state = state.get("kpi_state") or []

    sections = [
        "## Uygulama Metrikleri\n```json\n"
        + json.dumps(app_overviews, ensure_ascii=False, indent=2, default=str)[:3000]
        + "\n```",
        "## Son Büyüme Raporu\n```json\n"
        + json.dumps(last_growth_report or {}, ensure_ascii=False, indent=2, default=str)[:2000]
        + "\n```",
        "## KPI Durumu\n```json\n"
        + json.dumps(kpi_state, ensure_ascii=False, indent=2)[:1000]
        + "\n```",
    ]
    user_message = "\n\n".join(sections)

    llm_result = await invoke_from_state(
        state,
        "llm.chat",
        {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": 2000,
            "temperature": 0.2,
        },
    )

    if llm_result.get("status") != "ok":
        log.warning("app_studio_ceo.llm_failed", error=llm_result.get("error"))
        return {"brief": "_LLM çağrısı başarısız_"}

    return {"brief": (llm_result.get("data") or {}).get("content", "").strip()}


def _extract_tasks(brief: str) -> list[dict[str, Any]]:
    """Pull the trailing ```json {tasks: [...]} ``` block from the brief."""
    match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", brief, re.MULTILINE)
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


async def node_publish(state: AppCEOState) -> dict[str, Any]:
    """Publish brief to Slack + Telegram, emit events."""
    brief = state.get("brief") or ""
    run_id = state.get("run_id") or str(uuid.uuid4())
    today = datetime.now(UTC).date().isoformat()
    delegations = _extract_tasks(brief)

    # Strip JSON fence for human-facing text
    human_brief = re.sub(r"```json[\s\S]*?```", "", brief, flags=re.MULTILINE).strip()

    if delegations:
        lines = ["", "*Delegasyonlar:*"]
        for t in delegations:
            lines.append(
                f"• `{t['target_agent']}` _{t['priority']}_ — {t['title']}"
            )
        human_brief = human_brief + "\n" + "\n".join(lines)

    text_slack = f"*App Studio CEO — Haftalık Brief — {today}*\n\n{human_brief[:38000]}"
    text_tg = f"*App Studio CEO — Haftalık Brief — {today}*\n\n{human_brief[:3500]}"

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

    # CEO weekly brief event
    events_out: list[dict[str, Any]] = [
        {
            "event_type": "app.ceo.weekly_brief",
            "event_version": 1,
            "payload": {
                "decisions": [],  # filled by LLM extraction if needed
                "delegations": [
                    {
                        "target_agent": t["target_agent"],
                        "title": t["title"],
                        "priority": t["priority"],
                    }
                    for t in delegations
                ],
                "kpi_summary": {
                    k["name"]: k.get("current") for k in (state.get("kpi_state") or [])
                },
            },
            "idempotency_key": f"app_ceo:{run_id}:brief:{today}",
        }
    ]

    # Task delegation events
    for t in delegations:
        suffix = t["target_agent"].replace("app-studio-", "", 1)
        events_out.append(
            {
                "event_type": f"app.task.{suffix.replace('-', '_')}",
                "event_version": 1,
                "payload": {
                    "target_agent": t["target_agent"],
                    "title": t["title"],
                    "description": t["description"],
                    "priority": t["priority"],
                    "payload": t.get("payload") or {},
                    "requested_by": "app-studio-ceo",
                },
                "idempotency_key": (
                    f"app_ceo:{run_id}:{t['target_agent']}:{t['title'][:32]}"
                ),
            }
        )

    return {
        "events": events_out,
        "memories": [
            {
                "content": (
                    f"App Studio CEO brief {today}: "
                    f"{brief[:300]} "
                    f"(delegated {len(delegations)} tasks)"
                ),
                "tags": ["app-studio", "ceo", "weekly", today],
                "importance": 0.8,
            }
        ],
        "kpi_updates": [
            {"name": "app_ceo_briefs_total", "value": state_accum["briefs_total"]},
            {"name": "app_ceo_delegations_last", "value": len(delegations)},
        ],
        "state": state_accum,
        "summary": (
            f"App CEO weekly brief + {len(delegations)} delegations "
            f"(slack={slack_res.get('status')}, tg={tg_res.get('status')})"
        ),
    }


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_graph() -> Any:
    graph = StateGraph(AppCEOState)
    graph.add_node("seed_kpi", node_seed_kpi)
    graph.add_node("collect", node_collect)
    graph.add_node("brief", node_brief)
    graph.add_node("publish", node_publish)

    graph.add_edge(START, "seed_kpi")
    graph.add_edge("seed_kpi", "collect")
    graph.add_edge("collect", "brief")
    graph.add_edge("brief", "publish")
    graph.add_edge("publish", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_ceo", 1, compiled)
