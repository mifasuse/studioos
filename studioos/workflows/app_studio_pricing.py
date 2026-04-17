"""app_studio_pricing — country-based pricing analysis for App Studio (M29).

Workflow: START → collect → analyze → recommend → END

collect:    Hub API countries + conversion + mrr_history per app.
analyze:    LLM for WTP (willingness-to-pay) analysis.
recommend:  Emit app.pricing.recommendation + approval gate + Slack notify.
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
    "Sen bir mobil uygulama fiyatlandırma uzmanısın. "
    "Sana ülke bazlı dönüşüm, MRR geçmişi ve kullanıcı verileri verilir. "
    "Willingness-to-pay (WTP) analizi yap ve fiyat önerisi sun. "
    "Türkçe düşün, önerileri JSON olarak ver. "
    "Şu alanları doldur: "
    "app_id (str), current_price (str), recommended_price (str), "
    "rationale (str, max 300 karakter), ab_test_plan (dict). "
    "Yanıtı YALNIZCA bir JSON nesnesi olarak ver."
)


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

class PricingState(TypedDict, total=False):
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
    task_payload: dict[str, Any]
    pricing_data: dict[str, Any]
    recommendation: dict[str, Any]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def node_collect(state: PricingState) -> dict[str, Any]:
    """Fetch Hub API countries, conversion, and mrr_history for the target app."""
    inp = state.get("input") or {}
    task_payload = inp.get("payload") or inp
    # Try multiple sources for app_id:
    # 1. Direct in payload (from direct trigger)
    # 2. In task description (CEO delegation: "quit_smoking fiyat analizi")
    # 3. First tracked app from goals
    # 4. Default quit_smoking
    app_id: str = task_payload.get("app_id") or ""
    if not app_id:
        desc = (task_payload.get("description") or task_payload.get("title") or "").lower()
        for candidate in ("quit_smoking", "sms_forward"):
            if candidate.replace("_", " ") in desc or candidate in desc:
                app_id = candidate
                break
    if not app_id:
        tracked = (state.get("goals") or {}).get("tracked_apps") or []
        app_id = tracked[0] if tracked else "quit_smoking"

    countries_result = await invoke_from_state(
        state, "hub.api.overview", {"app_id": app_id, "days": 30}
    )
    countries_data = (
        (countries_result.get("data") or {})
        if countries_result.get("status") == "ok"
        else {}
    )

    conversion_result = await invoke_from_state(
        state,
        "hub.api.metrics",
        {"app_id": app_id, "metric": "conversion", "days": 30},
    )
    conversion_data = (
        (conversion_result.get("data") or {})
        if conversion_result.get("status") == "ok"
        else {}
    )

    mrr_result = await invoke_from_state(
        state,
        "hub.api.metrics",
        {"app_id": app_id, "metric": "mrr_history", "days": 90},
    )
    mrr_data = (
        (mrr_result.get("data") or {})
        if mrr_result.get("status") == "ok"
        else {}
    )

    pricing_data = {
        "app_id": app_id,
        "overview": countries_data,
        "conversion": conversion_data,
        "mrr_history": mrr_data,
    }

    return {"task_payload": task_payload, "pricing_data": pricing_data}


async def node_analyze(state: PricingState) -> dict[str, Any]:
    """Run LLM WTP analysis on the collected data."""
    pricing_data = state.get("pricing_data") or {}
    app_id = pricing_data.get("app_id", "unknown")

    user_message = (
        f"Uygulama: {app_id}\n\n"
        "Veri:\n"
        + json.dumps(pricing_data, ensure_ascii=False, default=str)[:4000]
        + "\n\nLütfen fiyatlandırma önerisi yap (JSON)."
    )

    llm_result = await invoke_from_state(
        state,
        "llm.chat",
        {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": 800,
            "temperature": 0.3,
        },
    )

    recommendation: dict[str, Any] = {"app_id": app_id}

    if llm_result.get("status") == "ok":
        content = (llm_result.get("data") or {}).get("content", "").strip()
        parsed = False
        # Try direct JSON parse
        try:
            raw = json.loads(content)
            if isinstance(raw, dict):
                recommendation.update(raw)
                parsed = True
        except (ValueError, TypeError):
            pass
        # Try extracting JSON from markdown fence or mixed text
        if not parsed:
            import re
            fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
            if fence:
                try:
                    raw = json.loads(fence.group(1))
                    if isinstance(raw, dict):
                        recommendation.update(raw)
                        parsed = True
                except (ValueError, TypeError):
                    pass
        if not parsed:
            # Find {"app_id" or {"current_price" in text
            idx = content.find('{"')
            if idx >= 0:
                tail = content[idx:]
                for end in range(len(tail), 0, -1):
                    if not tail[:end].rstrip().endswith("}"):
                        continue
                    try:
                        raw = json.loads(tail[:end])
                        if isinstance(raw, dict):
                            recommendation.update(raw)
                            parsed = True
                            break
                    except (ValueError, TypeError):
                        continue
        if not parsed:
            log.warning("app_studio_pricing.parse_failed", content=content[:200])
            recommendation["rationale"] = content[:300]
    else:
        log.warning("app_studio_pricing.llm_failed", error=llm_result.get("error"))
        recommendation["rationale"] = f"LLM hatası: {llm_result.get('error', '')}"

    return {"recommendation": recommendation}


async def node_recommend(state: PricingState) -> dict[str, Any]:
    """Emit pricing recommendation event, approval gate, and Slack notify."""
    recommendation = state.get("recommendation") or {}
    run_id = state.get("run_id") or str(uuid.uuid4())
    app_id = recommendation.get("app_id", "unknown")
    today = datetime.now(UTC).date().isoformat()

    event = {
        "event_type": "app.pricing.recommendation",
        "event_version": 1,
        "payload": {
            "app_id": app_id,
            "current_price": recommendation.get("current_price", ""),
            "recommended_price": recommendation.get("recommended_price", ""),
            "rationale": (recommendation.get("rationale") or "")[:300],
            "ab_test_plan": recommendation.get("ab_test_plan") or {},
        },
        "idempotency_key": f"pricing:{run_id}:{app_id}",
    }

    notify_text = (
        f"*App Studio Fiyatlandırma Önerisi — {today}*\n\n"
        f"Uygulama: `{app_id}`\n"
        f"Mevcut fiyat: {recommendation.get('current_price', '—')}\n"
        f"Önerilen fiyat: {recommendation.get('recommended_price', '—')}\n"
        f"Gerekçe: {(recommendation.get('rationale') or '')[:200]}\n\n"
        "_Onay için CEO lansmanı bekleniyor._"
    )

    slack_result = await invoke_from_state(
        state, "slack.notify", {"text": notify_text}
    )
    if slack_result.get("status") != "ok":
        log.warning(
            "app_studio_pricing.slack_failed", error=slack_result.get("error")
        )

    state_accum = dict(state.get("state") or {})
    state_accum["recommendations_total"] = (
        int(state_accum.get("recommendations_total", 0)) + 1
    )

    return {
        "events": [event],
        "memories": [
            {
                "content": (
                    f"Fiyat önerisi — {app_id}: "
                    f"{recommendation.get('current_price')} → "
                    f"{recommendation.get('recommended_price')}. "
                    f"{(recommendation.get('rationale') or '')[:150]}"
                ),
                "tags": ["app-studio", "pricing", app_id],
                "importance": 0.7,
            }
        ],
        "kpi_updates": [
            {
                "name": "pricing_recommendations_total",
                "value": state_accum["recommendations_total"],
            }
        ],
        "state": state_accum,
        "summary": (
            f"Pricing recommendation for {app_id}: "
            f"{recommendation.get('current_price')} → "
            f"{recommendation.get('recommended_price')} "
            f"(slack={slack_result.get('status')})"
        ),
    }


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_graph() -> Any:
    graph = StateGraph(PricingState)
    graph.add_node("collect", node_collect)
    graph.add_node("analyze", node_analyze)
    graph.add_node("recommend", node_recommend)

    graph.add_edge(START, "collect")
    graph.add_edge("collect", "analyze")
    graph.add_edge("analyze", "recommend")
    graph.add_edge("recommend", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_pricing", 1, compiled)
