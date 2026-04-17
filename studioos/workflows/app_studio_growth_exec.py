"""app_studio_growth_exec — experiment design and launch for App Studio (M29).

Workflow: START → intake → propose → gate → END

intake:   Read event payload (app.growth.weekly_report trigger).
propose:  LLM call to propose 1-3 experiments, classify each lane.
gate:     Fast Lane → emit app.experiment.launched directly.
          CEO Lane  → approval row + emit app.experiment.proposed.
          Notify Slack + Telegram.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


SYSTEM_PROMPT = (
    "Sen bir mobil uygulama büyüme stratejistisin. "
    "Sana haftalık büyüme raporu verilir. "
    "Türkçe düşün, ama deneyleri JSON olarak öner. "
    "1-3 deney öner. Her deney için şu alanları doldur: "
    "experiment_id (uuid), app_id, hypothesis (str), variants (list[dict]), "
    "traffic_split (str, örn '50/50'), duration_days (int), "
    "metrics (list[str]), is_pricing (bool), is_paywall (bool), "
    "reversible (bool), days_to_implement (float), user_impact_pct (float 0-100). "
    "Yanıtı YALNIZCA bir JSON dizisi olarak ver, başka metin ekleme. "
    "Örnek: [{\"experiment_id\": \"...\", \"app_id\": \"...\", ...}]"
)


# ---------------------------------------------------------------------------
# Pure function — no I/O, fully testable
# ---------------------------------------------------------------------------

def classify_lane(exp: dict[str, Any]) -> str:
    """Classify an experiment into 'ceo' or 'fast' lane.

    Rules:
      - is_pricing or is_paywall → ceo
      - user_impact_pct > 20    → ceo
      - days_to_implement > 1   → ceo
      - not reversible          → ceo
      - else                    → fast
    """
    if exp.get("is_pricing") or exp.get("is_paywall"):
        return "ceo"
    if float(exp.get("user_impact_pct", 0)) > 20:
        return "ceo"
    if float(exp.get("days_to_implement", 0)) > 1:
        return "ceo"
    if not exp.get("reversible", True):
        return "ceo"
    return "fast"


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

class GrowthExecState(TypedDict, total=False):
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
    report_payload: dict[str, Any]
    experiments: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def node_intake(state: GrowthExecState) -> dict[str, Any]:
    """Read event payload from the trigger."""
    inp = state.get("input") or {}
    # Trigger is app.growth.weekly_report; payload is under inp
    report_payload = inp.get("payload") or inp
    return {"report_payload": report_payload}


async def node_propose(state: GrowthExecState) -> dict[str, Any]:
    """LLM call to propose 1-3 experiments; classify each lane."""
    report_payload = state.get("report_payload") or {}

    user_message = (
        "Haftalık büyüme raporu:\n"
        + json.dumps(report_payload, ensure_ascii=False, default=str)[:4000]
        + "\n\nLütfen 1-3 deney öner (JSON dizisi)."
    )

    llm_result = await invoke_from_state(
        state,
        "llm.chat",
        {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": 1200,
            "temperature": 0.4,
        },
    )

    experiments: list[dict[str, Any]] = []

    if llm_result.get("status") == "ok":
        content = (llm_result.get("data") or {}).get("content", "")
        try:
            raw = json.loads(content)
            if isinstance(raw, list):
                experiments = raw
            elif isinstance(raw, dict) and "experiments" in raw:
                experiments = raw["experiments"]
        except (ValueError, TypeError):
            log.warning("app_studio_growth_exec.parse_failed", content=content[:200])
    else:
        log.warning("app_studio_growth_exec.llm_failed", error=llm_result.get("error"))

    # Ensure each experiment has a valid id, app_id, and lane
    report_app_id = report_payload.get("app_id", "unknown")
    for exp in experiments:
        if not exp.get("experiment_id"):
            exp["experiment_id"] = str(uuid.uuid4())
        if not exp.get("app_id"):
            exp["app_id"] = report_app_id
        exp["lane"] = classify_lane(exp)

    return {"experiments": experiments}


async def node_gate(state: GrowthExecState) -> dict[str, Any]:
    """Route experiments to Fast or CEO lane, emit events, notify."""
    experiments = state.get("experiments") or []
    run_id = state.get("run_id") or str(uuid.uuid4())
    launched_at = datetime.now(UTC).isoformat()

    events_out: list[dict[str, Any]] = []
    fast_exps: list[dict[str, Any]] = []
    ceo_exps: list[dict[str, Any]] = []

    for exp in experiments:
        lane = exp.get("lane", "ceo")
        exp_id = exp.get("experiment_id", str(uuid.uuid4()))
        app_id = exp.get("app_id", "unknown")

        if lane == "fast":
            fast_exps.append(exp)
            events_out.append(
                {
                    "event_type": "app.experiment.launched",
                    "event_version": 1,
                    "payload": {
                        "experiment_id": exp_id,
                        "app_id": app_id,
                        "lane": "fast",
                        "launched_at": launched_at,
                    },
                    "idempotency_key": (
                        f"growth_exec:{run_id}:launched:{exp_id}"
                    ),
                }
            )
        else:
            ceo_exps.append(exp)
            events_out.append(
                {
                    "event_type": "app.experiment.proposed",
                    "event_version": 1,
                    "payload": {
                        "experiment_id": exp_id,
                        "app_id": app_id,
                        "hypothesis": exp.get("hypothesis", ""),
                        "variants": exp.get("variants") or [],
                        "traffic_split": exp.get("traffic_split", "50/50"),
                        "duration_days": exp.get("duration_days", 14),
                        "lane": "ceo",
                        "metrics": exp.get("metrics") or [],
                    },
                    "idempotency_key": (
                        f"growth_exec:{run_id}:proposed:{exp_id}"
                    ),
                }
            )

    # Only notify if there are actual experiments to report
    if fast_exps or ceo_exps:
        lines = [f"*App Studio Deney Kapısı — {launched_at[:10]}*"]
        if fast_exps:
            lines.append(f"\nFast Lane ({len(fast_exps)} deney) — doğrudan başlatıldı:")
            for e in fast_exps:
                lines.append(f"  • [{e.get('app_id')}] {e.get('hypothesis', '')[:80]}")
        if ceo_exps:
            lines.append(f"\nCEO Lane ({len(ceo_exps)} deney) — onay gerekli:")
            for e in ceo_exps:
                lines.append(f"  • [{e.get('app_id')}] {e.get('hypothesis', '')[:80]}")
        notify_text = "\n".join(lines)
        await invoke_from_state(state, "slack.notify", {"text": notify_text})
        await invoke_from_state(state, "telegram.notify", {"text": notify_text})

    state_accum = dict(state.get("state") or {})
    state_accum["experiments_proposed"] = int(
        state_accum.get("experiments_proposed", 0)
    ) + len(ceo_exps)
    state_accum["experiments_launched"] = int(
        state_accum.get("experiments_launched", 0)
    ) + len(fast_exps)

    return {
        "events": events_out,
        "memories": [
            {
                "content": (
                    f"Deney kapısı: {len(fast_exps)} fast lane, "
                    f"{len(ceo_exps)} CEO lane."
                ),
                "tags": ["app-studio", "growth-exec", "experiment"],
                "importance": 0.6,
            }
        ],
        "kpi_updates": [
            {
                "name": "experiments_launched_total",
                "value": state_accum["experiments_launched"],
            },
            {
                "name": "experiments_proposed_total",
                "value": state_accum["experiments_proposed"],
            },
        ],
        "state": state_accum,
        "summary": (
            f"Growth exec: {len(fast_exps)} launched, {len(ceo_exps)} proposed"
        ),
    }


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_graph() -> Any:
    graph = StateGraph(GrowthExecState)
    graph.add_node("intake", node_intake)
    graph.add_node("propose", node_propose)
    graph.add_node("gate", node_gate)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "propose")
    graph.add_edge("propose", "gate")
    graph.add_edge("gate", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_growth_exec", 1, compiled)
