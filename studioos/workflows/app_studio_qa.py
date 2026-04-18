"""app_studio_qa — health check agent for App Studio apps.

Workflow: START → collect → check → verdict → END

- collect: For each goals.tracked_apps, call hub.api.overview; query DB for
  app-studio failed runs in last 6h to compute failure_rate_pct.
- check: Call check_app_health per app; accumulate flags.
- verdict: 0 flags → PASS (app.qa.passed). 1+ flags → FAIL (app.qa.failed)
  + @dev mention. Notifies Slack #build + Telegram.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select

from studioos.db import session_scope
from studioos.logging import get_logger
from studioos.models import AgentRun
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pure health-check logic
# ---------------------------------------------------------------------------

def check_app_health(
    app_id: str,
    overview: dict,
    failure_rate_pct: float,
    thresholds: dict,
) -> list[dict]:
    """Deterministic health checks — returns list of failed checks."""
    failed = []
    # Revenue WoW drop > threshold
    roi = overview.get("roi")
    if roi is not None and roi < 0:
        failed.append({"check": "negative_roi", "value": roi, "threshold": 0})
    # Failure rate too high
    max_fail = thresholds.get("failure_rate_threshold", 20.0)
    if failure_rate_pct > max_fail:
        failed.append({"check": "high_failure_rate", "value": failure_rate_pct, "threshold": max_fail})
    # MRR sudden drop: only flag if app previously had MRR > 0 and now
    # dropped to zero (indicates a payment/subscription issue). MRR=0
    # on its own is normal for free/pre-launch apps — not a QA failure.
    mrr = overview.get("mrr")
    prev_mrr = overview.get("prev_mrr") or overview.get("mrr_previous")
    if mrr is not None and mrr == 0 and prev_mrr and float(prev_mrr) > 0:
        failed.append({"check": "mrr_dropped_to_zero", "value": 0, "threshold": prev_mrr})
    # Hub API unreachable (overview came back empty = API down)
    if not overview:
        failed.append({"check": "hub_api_unreachable", "value": None, "threshold": "reachable"})
    return failed


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class QAState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    # populated during run
    app_overviews: dict[str, dict]        # app_id -> overview dict
    app_failure_rates: dict[str, float]   # app_id -> failure_rate_pct
    app_flags: dict[str, list[dict]]      # app_id -> failed checks
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def node_collect(state: QAState) -> dict[str, Any]:
    """Fetch hub overviews and compute failure rates from DB."""
    goals = state.get("goals") or {}
    tracked_apps: list[str] = goals.get("tracked_apps") or []
    studio_id = state.get("studio_id") or "app-studio"
    thresholds: dict = (state.get("input") or {}).get("thresholds") or {}

    since = datetime.now(UTC) - timedelta(hours=6)

    app_overviews: dict[str, dict] = {}
    app_failure_rates: dict[str, float] = {}

    for app_id in tracked_apps:
        # Fetch overview from Hub API
        result = await invoke_from_state(
            state,
            "hub.api.overview",
            {"app_id": app_id},
        )
        if result.get("status") == "ok":
            app_overviews[app_id] = result.get("data") or {}
        else:
            log.warning(
                "app_studio_qa.overview_failed",
                app_id=app_id,
                error=result.get("error"),
            )
            app_overviews[app_id] = {}

        # Compute failure rate from DB (app-studio runs last 6h)
        try:
            async with session_scope() as session:
                total_runs = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(AgentRun)
                            .where(AgentRun.studio_id == studio_id)
                            .where(AgentRun.created_at >= since)
                        )
                    ).scalar_one()
                )
                failed_runs = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(AgentRun)
                            .where(AgentRun.studio_id == studio_id)
                            .where(AgentRun.created_at >= since)
                            .where(
                                AgentRun.state.in_(
                                    ("failed", "timed_out", "dead", "budget_exceeded")
                                )
                            )
                        )
                    ).scalar_one()
                )
            failure_rate_pct = (failed_runs / total_runs * 100.0) if total_runs > 0 else 0.0
        except Exception:
            log.warning("app_studio_qa.db_query_failed", app_id=app_id)
            failure_rate_pct = 0.0

        app_failure_rates[app_id] = failure_rate_pct

    return {
        "app_overviews": app_overviews,
        "app_failure_rates": app_failure_rates,
    }


async def node_check(state: QAState) -> dict[str, Any]:
    """Run check_app_health for each tracked app."""
    goals = state.get("goals") or {}
    tracked_apps: list[str] = goals.get("tracked_apps") or []
    thresholds: dict = (state.get("input") or {}).get("thresholds") or {}
    app_overviews = state.get("app_overviews") or {}
    app_failure_rates = state.get("app_failure_rates") or {}

    app_flags: dict[str, list[dict]] = {}
    for app_id in tracked_apps:
        overview = app_overviews.get(app_id) or {}
        failure_rate_pct = app_failure_rates.get(app_id) or 0.0
        flags = check_app_health(app_id, overview, failure_rate_pct, thresholds)
        app_flags[app_id] = flags
        if flags:
            log.info(
                "app_studio_qa.app_failed",
                app_id=app_id,
                flags=[f["check"] for f in flags],
            )

    return {"app_flags": app_flags}


async def node_verdict(state: QAState) -> dict[str, Any]:
    """Emit event + notify based on aggregate health flags."""
    goals = state.get("goals") or {}
    tracked_apps: list[str] = goals.get("tracked_apps") or []
    app_flags = state.get("app_flags") or {}
    run_id = state.get("run_id") or "unknown"

    all_flags: list[dict] = []
    failed_apps: list[str] = []
    for app_id in tracked_apps:
        flags = app_flags.get(app_id) or []
        all_flags.extend(flags)
        if flags:
            failed_apps.append(app_id)

    total_apps = len(tracked_apps)
    passed_apps = total_apps - len(failed_apps)
    overall = "PASS" if not all_flags else "FAIL"
    icon = "✅" if overall == "PASS" else "🚨"

    # Build notification text
    lines = [
        f"{icon} *App Studio QA — {overall}*",
        f"Apps: {passed_apps}/{total_apps} healthy",
    ]
    if failed_apps:
        lines.append("")
        for app_id in failed_apps:
            flags = app_flags.get(app_id) or []
            checks_str = ", ".join(f["check"] for f in flags)
            lines.append(f"  ✗ *{app_id}*: {checks_str}")
        lines.append("")
        lines.append("@dev fix gerekli")

    text = "\n".join(lines)

    # Only notify on FAIL — PASS is silent (no spam every 6h)
    if overall == "FAIL":
        await invoke_from_state(
            state,
            "slack.notify",
            {"text": text, "mrkdwn": True, "unfurl_links": False},
        )
        await invoke_from_state(
            state,
            "telegram.notify",
            {"text": text, "parse_mode": "Markdown", "disable_web_page_preview": True},
        )

    # Emit event
    events: list[dict[str, Any]] = []
    if overall == "PASS":
        events.append(
            {
                "event_type": "app.qa.passed",
                "event_version": 1,
                "payload": {
                    "app_id": ",".join(tracked_apps),
                    "checks_passed": total_apps,
                    "checks_total": total_apps,
                    "summary": f"All {total_apps} app(s) healthy",
                },
                "idempotency_key": f"app_studio_qa:{run_id}:passed",
            }
        )
    else:
        failed_checks_names = [f["check"] for f in all_flags]
        events.append(
            {
                "event_type": "app.qa.failed",
                "event_version": 1,
                "payload": {
                    "app_id": ",".join(failed_apps),
                    "checks_passed": passed_apps,
                    "checks_total": total_apps,
                    "failed_checks": failed_checks_names,
                    "summary": f"{len(failed_apps)} app(s) failed QA: {', '.join(failed_apps)}",
                },
                "idempotency_key": f"app_studio_qa:{run_id}:failed",
            }
        )

    state_accum = dict(state.get("state") or {})
    state_accum["qa_runs_total"] = int(state_accum.get("qa_runs_total", 0)) + 1
    if failed_apps:
        state_accum["last_failed_apps"] = failed_apps

    return {
        "events": events,
        "memories": [
            {
                "content": (
                    f"App Studio QA {overall}: {passed_apps}/{total_apps} apps healthy"
                    + (f" — failed: {', '.join(failed_apps)}" if failed_apps else "")
                ),
                "tags": ["app-studio", "qa", overall.lower()],
                "importance": 0.8 if failed_apps else 0.4,
            }
        ],
        "kpi_updates": [
            {"name": "app_qa_pass", "value": 1 if not failed_apps else 0},
            {"name": "app_qa_failed_apps", "value": len(failed_apps)},
            {"name": "app_qa_total_apps", "value": total_apps},
        ],
        "state": state_accum,
        "summary": (
            f"App Studio QA {overall}: {passed_apps}/{total_apps} healthy"
            + (f" (failed: {', '.join(failed_apps)})" if failed_apps else "")
        ),
    }


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_graph() -> Any:
    graph = StateGraph(QAState)
    graph.add_node("collect", node_collect)
    graph.add_node("check", node_check)
    graph.add_node("verdict", node_verdict)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "check")
    graph.add_edge("check", "verdict")
    graph.add_edge("verdict", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_qa", 1, compiled)
