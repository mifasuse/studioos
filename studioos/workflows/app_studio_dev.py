"""app_studio_dev workflow — engineering pulse for App Studio.

Git-status pulse for App Studio repos + scan for failing runs in the last
hour. Reports to Slack #build and Telegram.
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


class AppDevState(TypedDict, total=False):
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


async def node_collect(state: AppDevState) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=1)
    async with session_scope() as session:
        fail_rows = (
            (
                await session.execute(
                    select(AgentRun.agent_id, AgentRun.error)
                    .where(AgentRun.studio_id == "app")
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
                    .where(AgentRun.studio_id == "app")
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

    # Best-effort git status pulse for every allow-listed repo.
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
            "window_minutes": 60,
            "total_runs": total_runs,
            "failures": failures,
            "repos": repo_state,
        }
    }


async def node_report(state: AppDevState) -> dict[str, Any]:
    snap = state.get("snapshot") or {}
    failures = snap.get("failures") or []
    total = snap.get("total_runs", 0)
    repos = snap.get("repos") or []

    # Separate real errors from noise (None type = transient/empty errors)
    real_failures = [f for f in failures if f.get("type")]
    noise_failures = [f for f in failures if not f.get("type")]
    failure_rate = len(failures) / max(1, total)

    # Only notify on real errors or high failure rate (>10%)
    should_notify = bool(real_failures) or failure_rate > 0.10

    if not failures:
        head = (
            f"*🛠 App Studio Dev pulse* — son 60dk: {total} run, *0 hata*. Sistem yeşil."
        )
    else:
        lines = [
            f"*🛠 App Studio Dev pulse* — son 60dk: {total} run, "
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
    dirty_repos_display = [r for r in repos if r.get("ok") and not r.get("clean")]
    repo_errors = [r for r in repos if not r.get("ok")]
    if dirty_repos_display or repo_errors:
        repo_lines.append("\n*Repo durumu:*")
        for r in repo_errors:
            repo_lines.append(
                f"• `{r['repo']}` — _err_ {(r.get('error') or '')[:60]}"
            )
        for r in dirty_repos_display:
            repo_lines.append(
                f"• `{r['repo']}` — ✗ {r.get('change_count', 0)} change"
            )

    text = head
    if repo_lines:
        text += "\n" + "\n".join(repo_lines)

    # Only notify on real failures or high failure rate — silent otherwise
    if should_notify:
        await invoke_from_state(
            state,
            "telegram.notify",
            {
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )
        await invoke_from_state(
            state,
            "slack.notify",
            {"text": text, "mrkdwn": True},
        )

    state_accum = dict(state.get("state") or {})
    state_accum["pulses_total"] = int(state_accum.get("pulses_total", 0)) + 1

    dirty_repos = [r for r in repos if r.get("ok") and not r.get("clean")]

    return {
        "memories": [
            {
                "content": (
                    f"App Studio Dev pulse: {total} runs / {len(failures)} failures last 60m"
                    + (f", {len(dirty_repos)} dirty repo(s)" if dirty_repos else "")
                ),
                "tags": ["app", "dev", "pulse"],
                "importance": 0.6 if failures or dirty_repos else 0.2,
            }
        ],
        "kpi_updates": [
            {"name": "app_dev_failures_last_60m", "value": len(failures)}
        ],
        "state": state_accum,
        "summary": (
            f"{total} runs, {len(real_failures)} real + "
            f"{len(noise_failures)} transient failures"
            + (" (notified)" if should_notify else "")
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(AppDevState)
    graph.add_node("collect", node_collect)
    graph.add_node("report", node_report)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "report")
    graph.add_edge("report", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_dev", 1, compiled)
