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
from studioos.workflows.amz_analyst_scoring import (
    VERDICT_TO_ANALYST,
    compute_profit,
    compute_risk,
    decide,
    verdict_confidence,
)

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
    pf_settings: dict[str, float]
    profit: dict[str, Any]
    risk: dict[str, int]
    scoring_verdict: str
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

    pf_settings: dict[str, float] = {}
    gs = await invoke_from_state(state, "pricefinder.db.global_settings", {})
    if gs["status"] == "ok":
        pf_settings = gs["data"] or {}

    profit = compute_profit(product or {}, pf_settings)
    risk = compute_risk(product or {})
    scoring_verdict = decide(
        risk["total"],
        profit.get("roi_pct"),
        (product or {}).get("monthly_sold"),
    )

    return {
        "event_kind": kind,
        "anomaly": anomaly,
        "product": product,
        "pf_settings": pf_settings,
        "profit": dict(profit),
        "risk": dict(risk),
        "scoring_verdict": scoring_verdict,
    }


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
    profit = state.get("profit") or {}
    risk = state.get("risk") or {}
    scoring_verdict = state.get("scoring_verdict") or "?"
    user = f"""{label}:
{json.dumps(anomaly, ensure_ascii=False)}

Product context:
{json.dumps(product or {}, ensure_ascii=False, default=str)[:1500]}

Deterministic analysis (already computed — do not recompute, just cite
and add rationale text):
- profit: {json.dumps(profit, ensure_ascii=False)}
- risk (1-5 each, total of 5): {json.dumps(risk, ensure_ascii=False)}
- matrix verdict: {scoring_verdict}
  (GUCLU_AL/AL → accept, IZLE → uncertain, GEC → reject)

Recent related memories:
{memory_block}

Reply with the JSON object only — verdict must match the matrix
unless you can explicitly justify overriding it (e.g. the product
context reveals a hidden risk the 5-dim score missed)."""

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


def _f(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def node_decide(state: AnalystState) -> dict[str, Any]:
    anomaly = state.get("anomaly") or {}
    verdict = state.get("verdict") or {}
    decision = verdict.get("verdict", "uncertain")
    confidence = float(verdict.get("confidence", 0.0))
    asin = anomaly.get("asin")
    kind = state.get("event_kind") or "anomaly"
    profit = state.get("profit") or {}
    risk = state.get("risk") or {}
    scoring_verdict = state.get("scoring_verdict") or "GEC"

    # Deterministic fallback: if the LLM gave up, trust the matrix.
    if decision == "uncertain" and scoring_verdict in VERDICT_TO_ANALYST:
        det_decision = VERDICT_TO_ANALYST[scoring_verdict]
        det_conf = verdict_confidence(
            scoring_verdict, int(risk.get("total", 25)), profit.get("roi_pct")
        )
        if det_decision != "uncertain" or det_conf >= 0.5:
            decision = det_decision
            confidence = det_conf
            verdict = {
                **verdict,
                "verdict": decision,
                "confidence": confidence,
                "rationale": (
                    verdict.get("rationale")
                    or f"deterministic matrix: {scoring_verdict} "
                    f"(risk={risk.get('total')}, roi={profit.get('roi_pct')}%)"
                )[:280],
            }

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
                    "previous_price": _f(anomaly.get("previous_price")),
                    "current_price": _f(anomaly.get("current_price")),
                    "delta_pct": _f(anomaly.get("delta_pct")),
                    "direction": anomaly.get("direction"),
                    "source": kind,
                    "verdict": "accept",
                    "confidence": confidence,
                    "rationale": verdict.get("rationale", ""),
                    "recommended_action": verdict.get("recommended_action"),
                    "matrix_verdict": scoring_verdict,
                    "net_profit_usd": profit.get("net_profit_usd"),
                    "roi_pct": profit.get("roi_pct"),
                    "margin_pct": profit.get("margin_pct"),
                    "risk_total": risk.get("total"),
                    "risk_breakdown": {
                        "price": risk.get("price"),
                        "demand": risk.get("demand"),
                        "fx": risk.get("fx"),
                        "category": risk.get("category"),
                        "quality": risk.get("quality"),
                    },
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
                    "delta_pct": _f(anomaly.get("delta_pct")),
                    "direction": anomaly.get("direction"),
                    "source": kind,
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
