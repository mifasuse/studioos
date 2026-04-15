"""amz_qa workflow — health smoke tests for the AMZ infra services.

Mirrors the OpenClaw amz-qa smoke test routine: hit the health
endpoints of pricefinder, buyboxpricer, adsoptimizer, ebaycrosslister
via the studioos-net + traefik-public networks. Report PASS/FAIL
to Slack + Telegram. No state mutation.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


_SERVICES = [
    {"name": "pricefinder", "url": "https://pricefinder.mifasuse.com/api/health"},
    {"name": "buyboxpricer", "url": "https://buyboxpricer.mifasuse.com/api/v1/health"},
    {"name": "adsoptimizer", "url": "https://adsoptimizer.mifasuse.com/api/health"},
    {
        "name": "ebaycrosslister",
        "url": "https://ebaycrosslister.mifasuse.com/api/health",
    },
]


class QAState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    results: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


async def node_check(state: QAState) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for svc in _SERVICES:
        r = await invoke_from_state(
            state,
            "http.get_json",
            {"url": svc["url"], "timeout_seconds": 8},
        )
        results.append(
            {
                "service": svc["name"],
                "url": svc["url"],
                "ok": r["status"] == "ok",
                "error": r.get("error"),
                "status_code": (r.get("data") or {}).get("status_code"),
            }
        )
    return {"results": results}


async def node_report(state: QAState) -> dict[str, Any]:
    results = state.get("results") or []
    failed = [r for r in results if not r["ok"]]
    overall = "PASS" if not failed else "FAIL"
    icon = "✅" if not failed else "🚨"

    lines = [f"{icon} *AMZ QA — Smoke {overall}*"]
    for r in results:
        sym = "✓" if r["ok"] else "✗"
        line = f"{sym} `{r['service']}`"
        if not r["ok"]:
            line += f" — {(r.get('error') or '')[:80]}"
        lines.append(line)
    text = "\n".join(lines)

    notify = await invoke_from_state(
        state,
        "telegram.notify",
        {
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
    )
    slack = await invoke_from_state(
        state,
        "slack.notify",
        {"text": text, "mrkdwn": True, "unfurl_links": False},
    )

    state_accum = dict(state.get("state") or {})
    state_accum["smoke_runs_total"] = (
        int(state_accum.get("smoke_runs_total", 0)) + 1
    )
    if failed:
        state_accum["last_fail_count"] = len(failed)

    events: list[dict[str, Any]] = []
    if failed:
        events.append(
            {
                "event_type": "amz.qa.smoke_failed",
                "event_version": 1,
                "payload": {
                    "failed_services": [r["service"] for r in failed],
                    "details": failed,
                },
                "idempotency_key": (
                    f"amz_qa:{state['run_id']}:smoke"
                ),
            }
        )

    return {
        "events": events,
        "memories": [
            {
                "content": (
                    f"Smoke {overall}: "
                    + ", ".join(
                        f"{r['service']}={'ok' if r['ok'] else 'FAIL'}"
                        for r in results
                    )
                ),
                "tags": ["amz", "qa", "smoke", overall.lower()],
                "importance": 0.8 if failed else 0.3,
            }
        ],
        "kpi_updates": [
            {"name": "smoke_pass", "value": 1 if not failed else 0},
            {"name": "smoke_failed_services", "value": len(failed)},
        ],
        "state": state_accum,
        "summary": (
            f"{overall} ({len(results) - len(failed)}/{len(results)})"
            + (" notified" if notify["status"] == "ok" or slack["status"] == "ok" else "")
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(QAState)
    graph.add_node("check", node_check)
    graph.add_node("report", node_report)
    graph.add_edge(START, "check")
    graph.add_edge("check", "report")
    graph.add_edge("report", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_qa", 1, compiled)
