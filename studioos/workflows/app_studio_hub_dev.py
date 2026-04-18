"""app_studio_hub_dev — Hub service health pulse for App Studio (6h cadence).

Identical pattern to app_studio_dev but monitors the Hub repository and
queries agent-run failures for studio_id="app-studio".

Workflow: START → collect → report → END
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import desc, func, select

from studioos.db import session_scope
from studioos.logging import get_logger
from studioos.models import AgentRun
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class HubDevState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    snapshot: dict[str, Any]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


async def node_collect(state: HubDevState) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=6)
    async with session_scope() as session:
        fail_rows = (
            (
                await session.execute(
                    select(AgentRun.agent_id, AgentRun.error)
                    .where(AgentRun.studio_id == "app-studio")
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
        total_runs = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AgentRun)
                    .where(AgentRun.studio_id == "app-studio")
                    .where(AgentRun.created_at >= since)
                )
            ).scalar_one()
        )

    failures = [
        {
            "agent_id": agent_id,
            "type": (err or {}).get("type"),
            "message": (err or {}).get("message", "")[:200],
        }
        for agent_id, err in fail_rows
    ]

    # Git status pulse for Hub repo(s) listed in goals.
    goals = state.get("goals") or {}
    repos = goals.get("repos") or []
    repo_state: list[dict[str, Any]] = []
    for repo in repos:
        result = await invoke_from_state(
            state, "exec.git_status", {"repo": repo}
        )
        if result["status"] != "ok":
            repo_state.append(
                {"repo": repo, "ok": False, "error": result.get("error")}
            )
            continue
        d = result["data"] or {}
        repo_state.append(
            {
                "repo": repo,
                "ok": True,
                "clean": d.get("clean"),
                "change_count": d.get("change_count"),
                "changes": d.get("changes", [])[:5],
            }
        )

    return {
        "snapshot": {
            "window_minutes": 360,
            "total_runs": total_runs,
            "failures": failures,
            "repos": repo_state,
        }
    }


async def node_report(state: HubDevState) -> dict[str, Any]:
    snap = state.get("snapshot") or {}
    failures = snap.get("failures") or []
    total = snap.get("total_runs", 0)
    repos = snap.get("repos") or []

    # Separate real errors from noise (None type = transient/empty errors)
    real_failures = [f for f in failures if f.get("type")]
    noise_failures = [f for f in failures if not f.get("type")]
    failure_rate = len(failures) / max(1, total)

    # Dirty/errored repos also warrant notification
    dirty_repos = [r for r in repos if r.get("ok") and not r.get("clean")]
    repo_errors = [r for r in repos if not r.get("ok")]

    # Only notify on real failures, high failure rate, or dirty repos
    should_notify = bool(real_failures) or failure_rate > 0.10 or bool(dirty_repos or repo_errors)

    if not failures:
        head = (
            f"*🔧 Hub Dev pulse* — son 6s: {total} run, *0 hata*. Hub sağlıklı."
        )
    else:
        lines = [
            f"*🔧 Hub Dev pulse* — son 6s: {total} run, "
            f"*{len(real_failures)} hata*"
        ]
        if noise_failures:
            lines[0] += f" (+{len(noise_failures)} geçici)"
        for f in real_failures[:5]:
            lines.append(
                f"• `{f['agent_id']}` — {f['type']}: {f['message'][:60]}"
            )
        head = "\n".join(lines)

    repo_lines: list[str] = []
    if dirty_repos or repo_errors:
        repo_lines.append("\n*Repo durumu:*")
        for r in repo_errors:
            repo_lines.append(
                f"• `{r['repo']}` — _err_ {(r.get('error') or '')[:60]}"
            )
        for r in dirty_repos:
            repo_lines.append(
                f"• `{r['repo']}` — ✗ {r.get('change_count', 0)} change"
            )

    text = head
    if repo_lines:
        text += "\n" + "\n".join(repo_lines)

    notify_tg = {"status": "skipped"}
    notify_slack = {"status": "skipped"}
    if should_notify:
        notify_tg = await invoke_from_state(
            state,
            "telegram.notify",
            {
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )
        notify_slack = await invoke_from_state(
            state,
            "slack.notify",
            {"text": text, "mrkdwn": True},
        )

    state_accum = dict(state.get("state") or {})
    state_accum["pulses_total"] = int(state_accum.get("pulses_total", 0)) + 1

    return {
        "memories": [
            {
                "content": (
                    f"Hub Dev pulse: {total} runs / {len(real_failures)} real failures last 6h"
                    + (f", {len(dirty_repos)} dirty repo(s)" if dirty_repos else "")
                ),
                "tags": ["app-studio", "hub", "dev", "pulse"],
                "importance": 0.6 if real_failures or dirty_repos else 0.2,
            }
        ],
        "kpi_updates": [
            {"name": "hub_dev_failures_last_6h", "value": len(real_failures)}
        ],
        "state": state_accum,
        "summary": (
            f"{total} runs, {len(real_failures)} real + {len(noise_failures)} transient"
            + (" (notified)" if should_notify else "")
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(HubDevState)
    graph.add_node("collect", node_collect)
    graph.add_node("report", node_report)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "report")
    graph.add_edge("report", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_hub_dev", 1, compiled)
