"""amz_analyst workflow — Milestone 6 phase 2.

Triggered by `amz.price.anomaly_detected`. Pulls product context from the
PriceFinder read-only replica, asks MiniMax to classify the anomaly
(accept/reject/uncertain), and emits either:

  - amz.opportunity.confirmed — analyst endorses the opportunity
  - amz.opportunity.rejected  — analyst dismisses as noise
  - (uncertain)               — parks the run with an approval row

LLM contract: the model must reply with a JSON object containing
  { verdict, confidence, rationale, recommended_action }
where verdict ∈ {"accept", "reject", "uncertain"}. We fall back to
"uncertain" on parse failure.
"""
from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


SYSTEM_PROMPT = """You are an Amazon arbitrage analyst for a TR→US
retail arbitrage operation. The trader sources products in Turkey
and resells them on Amazon.com.

You will receive a single price-anomaly observation for one ASIN, a
product context block, and recent memories. Decide whether the anomaly
represents a genuine arbitrage opportunity worth acting on, plain
market noise, or a case that needs human review.

Reply with a JSON object only (no prose, no markdown fences) with
exactly these keys:

  verdict:             "accept" | "reject" | "uncertain"
  confidence:          float in [0, 1]
  rationale:           <= 280 characters, why
  recommended_action:  short string or null

Signal interpretation:
  - estimated_profit_usd, profit_margin_pct, roi_pct come from
    PriceFinder's arbitrage calculator. If present and positive,
    the trade is economically viable in principle — treat them as
    primary signals.
  - A current-price drop on a product that already shows a healthy
    estimated_profit widens the margin further → stronger accept.
  - sales_rank / monthly_sold are often null on Amazon for niche
    B2B / long-tail items; null does NOT mean no demand. Do not
    reject solely for missing rank — weight the profit metrics more.
  - new_offer_count > 0 proves the listing is live. Only an exact 0
    (stockout) is a red flag.
  - Price drops are usually real when validated by positive
    estimated_profit and an active listing.

Accept at confidence ≥ 0.5. Prefer accept when the profit metrics
already justify the trade, even if rank data is thin.
"""


class AnalystState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    recent_memories: list[dict[str, Any]]
    kpis: list[dict[str, Any]]
    # populated during run
    event_kind: str  # "anomaly" | "discovery"
    anomaly: dict[str, Any]
    product: dict[str, Any] | None
    verdict: dict[str, Any]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    summary: str


async def node_load_context(state: AnalystState) -> dict[str, Any]:
    event = state.get("input") or {}
    payload = event.get("payload") or {}
    event_type = event.get("event_type") or ""

    if event_type == "amz.opportunity.discovered":
        kind = "discovery"
        anomaly = {
            "asin": payload.get("asin"),
            "marketplace": payload.get("marketplace", "US"),
            "previous_price": None,
            "current_price": payload.get("buybox_price_usd"),
            "delta_pct": None,
            "direction": None,
            "threshold_pct": None,
        }
    else:
        kind = "anomaly"
        anomaly = {
            "asin": payload.get("asin"),
            "marketplace": payload.get("marketplace", "US"),
            "previous_price": payload.get("previous_price"),
            "current_price": payload.get("current_price"),
            "delta_pct": payload.get("delta_pct"),
            "direction": payload.get("direction"),
            "threshold_pct": payload.get("threshold_pct"),
        }

    product: dict[str, Any] | None = None
    asin = anomaly.get("asin")
    if asin:
        lookup = await invoke_from_state(
            state, "pricefinder.db.lookup_asins", {"asins": [asin]}
        )
        if lookup["status"] == "ok":
            items = (lookup["data"] or {}).get("items") or []
            if items:
                product = items[0]

    return {"event_kind": kind, "anomaly": anomaly, "product": product}


def _build_messages(
    state: AnalystState,
) -> list[dict[str, str]]:
    anomaly = state.get("anomaly") or {}
    product = state.get("product") or {}
    memories = state.get("recent_memories") or []
    kind = state.get("event_kind") or "anomaly"

    memory_lines = []
    for m in memories[:5]:
        memory_lines.append(f"- {m.get('content', '')[:200]}")
    memory_block = "\n".join(memory_lines) if memory_lines else "(none)"

    label = "Newly discovered opportunity" if kind == "discovery" else "Anomaly"
    user = f"""{label}:
{json.dumps(anomaly, ensure_ascii=False)}

Product context:
{json.dumps(product or {}, ensure_ascii=False, default=str)[:1500]}

Recent related memories:
{memory_block}

Reply with the JSON object only."""

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


async def node_ask_llm(state: AnalystState) -> dict[str, Any]:
    messages = _build_messages(state)
    result = await invoke_from_state(
        state,
        "llm.chat",
        {
            "messages": messages,
            "max_tokens": 1500,
            "temperature": 0.0,
            "response_format": "json_object",
        },
    )
    if result["status"] != "ok":
        log.warning(
            "amz_analyst.llm_failed",
            status=result["status"],
            error=result.get("error"),
        )
        return {
            "verdict": {
                "verdict": "uncertain",
                "confidence": 0.0,
                "rationale": f"llm call failed: {result.get('error', '')[:200]}",
                "recommended_action": None,
            }
        }

    data = result["data"]
    parsed = data.get("parsed_json")
    if not isinstance(parsed, dict):
        try:
            parsed = json.loads(data.get("content", ""))
        except Exception:
            parsed = None

    if not isinstance(parsed, dict):
        return {
            "verdict": {
                "verdict": "uncertain",
                "confidence": 0.0,
                "rationale": "llm returned non-json",
                "recommended_action": None,
            }
        }

    verdict = str(parsed.get("verdict", "uncertain")).lower()
    if verdict not in ("accept", "reject", "uncertain"):
        verdict = "uncertain"
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "verdict": {
            "verdict": verdict,
            "confidence": confidence,
            "rationale": str(parsed.get("rationale", ""))[:280],
            "recommended_action": parsed.get("recommended_action"),
        }
    }


def node_decide(state: AnalystState) -> dict[str, Any]:
    anomaly = state.get("anomaly") or {}
    verdict = state.get("verdict") or {}
    decision = verdict.get("verdict", "uncertain")
    confidence = float(verdict.get("confidence", 0.0))
    asin = anomaly.get("asin")

    events: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    kpi_updates: list[dict[str, Any]] = []

    state_accum = dict(state.get("state") or {})
    state_accum["evaluated_total"] = (
        int(state_accum.get("evaluated_total", 0)) + 1
    )
    state_accum[f"{decision}_total"] = (
        int(state_accum.get(f"{decision}_total", 0)) + 1
    )

    if decision == "accept" and confidence >= 0.5 and asin:
        events.append(
            {
                "event_type": "amz.opportunity.confirmed",
                "event_version": 1,
                "payload": {
                    "asin": asin,
                    "marketplace": anomaly.get("marketplace", "US"),
                    "previous_price": float(anomaly.get("previous_price", 0)),
                    "current_price": float(anomaly.get("current_price", 0)),
                    "delta_pct": float(anomaly.get("delta_pct", 0)),
                    "direction": anomaly.get("direction", "down"),
                    "verdict": "accept",
                    "confidence": confidence,
                    "rationale": verdict.get("rationale", ""),
                    "recommended_action": verdict.get("recommended_action"),
                },
                "idempotency_key": (
                    f"amz_analyst:{state['run_id']}:confirmed:{asin}"
                ),
            }
        )
        memories.append(
            {
                "content": (
                    f"Confirmed opportunity on {asin}: {verdict.get('rationale', '')}"
                ),
                "tags": ["amz", "opportunity", "confirmed", asin],
                "importance": min(1.0, 0.5 + confidence / 2.0),
            }
        )
        summary = f"Confirmed opportunity on {asin} (conf={confidence:.2f})"
    elif decision == "reject" and confidence >= 0.5 and asin:
        events.append(
            {
                "event_type": "amz.opportunity.rejected",
                "event_version": 1,
                "payload": {
                    "asin": asin,
                    "marketplace": anomaly.get("marketplace", "US"),
                    "delta_pct": float(anomaly.get("delta_pct", 0)),
                    "direction": anomaly.get("direction", "down"),
                    "verdict": "reject",
                    "confidence": confidence,
                    "rationale": verdict.get("rationale", ""),
                },
                "idempotency_key": (
                    f"amz_analyst:{state['run_id']}:rejected:{asin}"
                ),
            }
        )
        memories.append(
            {
                "content": (
                    f"Rejected anomaly on {asin}: {verdict.get('rationale', '')}"
                ),
                "tags": ["amz", "opportunity", "rejected", asin],
                "importance": 0.3,
            }
        )
        summary = f"Rejected anomaly on {asin} (conf={confidence:.2f})"
    else:
        approvals.append(
            {
                "reason": (
                    f"amz-analyst uncertain about {asin or '<unknown>'}: "
                    f"{verdict.get('rationale', '')}"
                ),
                "payload": {
                    "asin": asin,
                    "anomaly": anomaly,
                    "verdict": verdict,
                },
                "expires_in_seconds": 60 * 60 * 24,
            }
        )
        summary = (
            f"Uncertain on {asin or '<unknown>'} "
            f"(conf={confidence:.2f}) — awaiting approval"
        )

    kpi_updates.append(
        {"name": "evaluated_total", "value": state_accum["evaluated_total"]}
    )
    kpi_updates.append(
        {
            "name": "accept_rate",
            "value": round(
                int(state_accum.get("accept_total", 0))
                / max(1, state_accum["evaluated_total"]),
                4,
            ),
        }
    )

    return {
        "events": events,
        "memories": memories,
        "kpi_updates": kpi_updates,
        "approvals": approvals,
        "state": state_accum,
        "summary": summary,
    }


def build_graph() -> Any:
    graph = StateGraph(AnalystState)
    graph.add_node("load_context", node_load_context)
    graph.add_node("ask_llm", node_ask_llm)
    graph.add_node("decide", node_decide)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "ask_llm")
    graph.add_edge("ask_llm", "decide")
    graph.add_edge("decide", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_analyst", 1, compiled)
