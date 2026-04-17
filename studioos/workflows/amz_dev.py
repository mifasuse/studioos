"""amz_dev workflow — engineering pulse for the AMZ services.

The OpenClaw amz-dev role drives builds/deploys via shell exec —
that's a wide attack surface and not safe to port wholesale into
StudioOS yet. Phase 1 of the port is a thin git-status pulse:
ask the QA tool's healthcheck data + scan for any failing runs in
the last hour and report them. Real code-mutation tools (git pull,
docker compose up, alembic upgrade) come later behind explicit
approval.
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
from studioos.workflows.amz_dev_tech_map import (
    SERVICES as TECH_MAP_SERVICES,
    tech_map_memories,
)

log = get_logger(__name__)


class DevState(TypedDict, total=False):
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


async def node_collect(state: DevState) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=1)
    async with session_scope() as session:
        fail_rows = (
            (
                await session.execute(
                    select(AgentRun.agent_id, AgentRun.error)
                    .where(AgentRun.studio_id == "amz")
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
                    .where(AgentRun.studio_id == "amz")
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

    # Best-effort git status pulse for every allow-listed repo. Tool
    # call may fail if the allow-list is empty or git isn't available;
    # we never block the run on it.
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

    # Alembic drift — poll each service's /alembic/current endpoint
    # if exposed. Failures are non-fatal (most services don't yet
    # expose it; we log the shape DEV can add it to as a follow-up).
    alembic_status: list[dict[str, Any]] = []
    for svc in TECH_MAP_SERVICES:
        url = f"{svc['backend_internal']}/api/v1/alembic/current"
        r = await invoke_from_state(
            state, "http.get_json", {"url": url, "timeout_seconds": 5}
        )
        if r["status"] == "ok":
            body = (r.get("data") or {}).get("body") or {}
            alembic_status.append(
                {
                    "service": svc["name"],
                    "revision": body.get("revision"),
                    "head": body.get("head"),
                    "ok": body.get("revision") == body.get("head"),
                }
            )

    return {
        "snapshot": {
            "window_minutes": 60,
            "total_runs": total_runs,
            "failures": failures,
            "repos": repo_state,
            "alembic": alembic_status,
        }
    }


def _seed_tech_map_if_needed(state: DevState) -> list[dict[str, Any]]:
    """Return tech-map memories if not yet seeded (first run only)."""
    state_accum = state.get("state") or {}
    if state_accum.get("tech_map_seeded"):
        return []
    return tech_map_memories()


async def node_report(state: DevState) -> dict[str, Any]:
    snap = state.get("snapshot") or {}
    failures = snap.get("failures") or []
    total = snap.get("total_runs", 0)
    repos = snap.get("repos") or []
    alembic = snap.get("alembic") or []

    if not failures:
        head = (
            f"*🛠 AMZ Dev pulse* — son 60dk: {total} run, *0 hata*. Sistem yeşil."
        )
    else:
        lines = [
            f"*🛠 AMZ Dev pulse* — son 60dk: {total} run, "
            f"*{len(failures)} hata*"
        ]
        for f in failures[:5]:
            lines.append(
                f"• `{f['agent_id']}` — {f['type']}: {f['message'][:60]}"
            )
        head = "\n".join(lines)

    repo_lines: list[str] = []
    if repos:
        repo_lines.append("\n*Repo durumu:*")
        for r in repos:
            if not r.get("ok"):
                repo_lines.append(
                    f"• `{r['repo']}` — _err_ {(r.get('error') or '')[:60]}"
                )
                continue
            mark = "✓ clean" if r.get("clean") else f"✗ {r.get('change_count', 0)} change"
            repo_lines.append(f"• `{r['repo']}` — {mark}")

    alembic_lines: list[str] = []
    drifted = [a for a in alembic if not a.get("ok")]
    if drifted:
        alembic_lines.append("\n*Alembic drift:*")
        for a in drifted:
            alembic_lines.append(
                f"• `{a['service']}` — {a.get('revision')} ≠ {a.get('head')}"
            )

    text = head
    if repo_lines:
        text += "\n" + "\n".join(repo_lines)
    if alembic_lines:
        text += "\n" + "\n".join(alembic_lines)

    # Only notify on failures — silent when all green
    if failures:
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
            state, "slack.notify", {"text": text, "mrkdwn": True}
        )

    state_accum = dict(state.get("state") or {})
    state_accum["pulses_total"] = int(state_accum.get("pulses_total", 0)) + 1

    # Seed tech-map memories on first run.
    extra_mems = _seed_tech_map_if_needed(state)
    if extra_mems:
        state_accum["tech_map_seeded"] = True

    return {
        "memories": [
            {
                "content": (
                    f"Dev pulse: {total} runs / {len(failures)} failures last 60m"
                ),
                "tags": ["amz", "dev", "pulse"],
                "importance": 0.6 if failures else 0.2,
            },
            *extra_mems,
        ],
        "kpi_updates": [
            {"name": "dev_failures_last_60m", "value": len(failures)}
        ],
        "state": state_accum,
        "summary": (
            f"{total} runs, {len(failures)} failures"
            + (
                " (notified)"
                if notify_tg["status"] == "ok" or notify_slack["status"] == "ok"
                else ""
            )
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(DevState)
    graph.add_node("collect", node_collect)
    graph.add_node("report", node_report)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "report")
    graph.add_edge("report", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_dev", 1, compiled)
