"""amz_monitor workflow — Milestone 6 phase 1 (read-only).

Behavior:
  START → scan_watchlist → detect_anomalies → END

For each ASIN in the agent's goals.watchlist the monitor calls
`pricefinder.lookup_asin`, compares the current price to the last
observed price kept in agent_state, and emits:
  - `amz.price.checked` per ASIN
  - `amz.price.anomaly_detected` whenever |delta_pct| > anomaly_threshold_pct
  - one semantic memory per anomaly
  - KPI snapshots: asins_scanned, anomalies_found, last_scan_epoch

Nothing is written outside of StudioOS — no repricing, no ad changes.
This agent exists to validate that the AMZ studio works in prod end-to-end
without touching OpenClaw.
"""
from __future__ import annotations

import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class MonitorState(TypedDict, total=False):
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
    observations: list[dict[str, Any]]
    anomalies: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


def _watchlist(state: MonitorState) -> list[str]:
    goals = state.get("goals") or {}
    watchlist = goals.get("watchlist") or []
    # Allow the trigger payload to override for one-off manual runs.
    trigger_watchlist = (state.get("input") or {}).get("watchlist")
    if isinstance(trigger_watchlist, list) and trigger_watchlist:
        return list(trigger_watchlist)
    return list(watchlist)


async def node_scan_watchlist(state: MonitorState) -> dict[str, Any]:
    asins = _watchlist(state)
    if not asins:
        return {"observations": []}

    result = await invoke_from_state(
        state, "pricefinder.db.lookup_asins", {"asins": asins}
    )
    if result["status"] != "ok":
        log.warning(
            "amz_monitor.batch_lookup_failed",
            status=result["status"],
            error=result.get("error"),
        )
        return {"observations": []}

    data = result["data"]
    if data.get("missing"):
        log.info(
            "amz_monitor.missing_asins",
            missing=data["missing"],
            found=data.get("found", 0),
        )

    observations: list[dict[str, Any]] = []
    for item in data.get("items", []):
        observations.append(
            {
                "asin": item["asin"],
                "marketplace": "US",
                "price": float(item["price"]),
                "currency": item.get("currency", "USD"),
            }
        )
    return {"observations": observations}


def node_detect_anomalies(state: MonitorState) -> dict[str, Any]:
    observations = state.get("observations") or []
    goals = state.get("goals") or {}
    threshold_pct = float(goals.get("anomaly_threshold_pct", 5.0))

    previous = dict((state.get("state") or {}).get("last_prices") or {})
    next_prices: dict[str, float] = dict(previous)

    events: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []

    for obs in observations:
        asin = obs["asin"]
        price = float(obs["price"])
        events.append(
            {
                "event_type": "amz.price.checked",
                "event_version": 1,
                "payload": {
                    "asin": asin,
                    "marketplace": obs["marketplace"],
                    "price": price,
                    "currency": obs["currency"],
                    "source": "pricefinder",
                },
                "idempotency_key": (
                    f"amz_monitor:{state['run_id']}:checked:{asin}"
                ),
            }
        )

        prev = previous.get(asin)
        if prev is not None and prev > 0:
            delta_pct = ((price - prev) / prev) * 100.0
            if abs(delta_pct) >= threshold_pct:
                direction = "up" if delta_pct > 0 else "down"
                anomaly = {
                    "asin": asin,
                    "previous_price": prev,
                    "current_price": price,
                    "delta_pct": round(delta_pct, 4),
                    "direction": direction,
                }
                anomalies.append(anomaly)
                events.append(
                    {
                        "event_type": "amz.price.anomaly_detected",
                        "event_version": 1,
                        "payload": {
                            "asin": asin,
                            "marketplace": obs["marketplace"],
                            "previous_price": prev,
                            "current_price": price,
                            "delta_pct": round(delta_pct, 4),
                            "threshold_pct": threshold_pct,
                            "direction": direction,
                        },
                        "idempotency_key": (
                            f"amz_monitor:{state['run_id']}:anomaly:{asin}"
                        ),
                    }
                )
                memories.append(
                    {
                        "content": (
                            f"Price anomaly on {asin}: {prev} → {price} "
                            f"({delta_pct:+.2f}%, threshold {threshold_pct}%)"
                        ),
                        "tags": [
                            "amz",
                            "price_anomaly",
                            direction,
                            asin,
                        ],
                        "importance": min(
                            1.0, 0.5 + abs(delta_pct) / 100.0
                        ),
                    }
                )

        next_prices[asin] = price

    existing_state = dict(state.get("state") or {})
    existing_state["last_prices"] = next_prices
    existing_state["last_scan_epoch"] = int(time.time())
    existing_state["scans_total"] = int(
        existing_state.get("scans_total", 0)
    ) + 1
    existing_state["anomalies_total"] = int(
        existing_state.get("anomalies_total", 0)
    ) + len(anomalies)

    kpi_updates: list[dict[str, Any]] = [
        {"name": "asins_scanned", "value": len(observations)},
        {"name": "anomalies_found", "value": len(anomalies)},
        {
            "name": "anomalies_total",
            "value": existing_state["anomalies_total"],
        },
    ]

    summary = (
        f"Scanned {len(observations)} ASIN(s), "
        f"{len(anomalies)} anomaly(ies)"
    )

    return {
        "anomalies": anomalies,
        "events": events,
        "memories": memories,
        "kpi_updates": kpi_updates,
        "state": existing_state,
        "summary": summary,
    }


def build_graph() -> Any:
    graph = StateGraph(MonitorState)
    graph.add_node("scan_watchlist", node_scan_watchlist)
    graph.add_node("detect_anomalies", node_detect_anomalies)
    graph.add_edge(START, "scan_watchlist")
    graph.add_edge("scan_watchlist", "detect_anomalies")
    graph.add_edge("detect_anomalies", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_monitor", 1, compiled)
