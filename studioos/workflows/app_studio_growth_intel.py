"""app_studio_growth_intel — weekly growth intelligence for App Studio (M29).

Workflow: START → collect → analyze → report → END

collect:  For each tracked_app, fetches hub.api.overview + hub.api.metrics
          (conversion, retention).
analyze:  Runs detect_anomalies() pure function, emits
          app.growth.anomaly_detected events for any detected anomalies.
report:   LLM summary (Turkish), sends Slack + Telegram notifications,
          emits app.growth.weekly_report events, records KPI snapshots.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


SYSTEM_PROMPT = (
    "Sen bir mobil uygulama büyüme analistisin. "
    "Sana her hafta uygulama metrikleri ve anomaliler verilir. "
    "Türkçe, net ve öz bir haftalık büyüme raporu yaz. "
    "Rapor şu bölümleri içermeli: "
    "1) Özet (temel metrikler), "
    "2) Anomaliler ve uyarılar, "
    "3) Öneriler. "
    "Maksimum 400 kelime kullan."
)


# ---------------------------------------------------------------------------
# Pure function — no I/O, fully testable
# ---------------------------------------------------------------------------

def detect_anomalies(
    app_id: str,
    overview: dict[str, Any],
    conversion: dict[str, Any],
    thresholds: dict[str, Any],
) -> list[dict[str, Any]]:
    """Detect growth anomalies from app metrics.

    Rules:
      - trial_starts == 0          → critical
      - ROI < min_roi               → warning
      - churn_rate > max_churn_rate → warning
      - retention_d7 < min_retention_d7 → warning

    Returns a list of anomaly dicts (may be empty).
    """
    anomalies: list[dict[str, Any]] = []

    min_roi: float = float(thresholds.get("min_roi", 1.0))
    max_churn_rate: float = float(thresholds.get("max_churn_rate", 15.0))
    min_retention_d7: float = float(thresholds.get("min_retention_d7", 20.0))

    # trial_starts == 0 → critical
    trial_starts = overview.get("trial_starts")
    if trial_starts is not None and int(trial_starts) == 0:
        anomalies.append(
            {
                "app_id": app_id,
                "anomaly_type": "critical",
                "metric_name": "trial_starts",
                "current_value": float(trial_starts),
                "previous_value": None,
                "delta_pct": None,
                "severity": "critical",
            }
        )

    # ROI < min_roi → warning
    roi = overview.get("roi")
    if roi is not None and float(roi) < min_roi:
        anomalies.append(
            {
                "app_id": app_id,
                "anomaly_type": "warning",
                "metric_name": "roi",
                "current_value": float(roi),
                "previous_value": None,
                "delta_pct": None,
                "severity": "warning",
            }
        )

    # churn_rate > max_churn_rate → warning (can be in overview or conversion)
    churn_rate = overview.get("churn_rate") if overview.get("churn_rate") is not None else conversion.get("churn_rate")
    if churn_rate is not None and float(churn_rate) > max_churn_rate:
        anomalies.append(
            {
                "app_id": app_id,
                "anomaly_type": "warning",
                "metric_name": "churn_rate",
                "current_value": float(churn_rate),
                "previous_value": None,
                "delta_pct": None,
                "severity": "warning",
            }
        )

    # retention_d7 < min_retention_d7 → warning (can be in overview or conversion)
    retention_d7 = overview.get("retention_d7") if overview.get("retention_d7") is not None else conversion.get("retention_d7")
    if retention_d7 is not None and float(retention_d7) < min_retention_d7:
        anomalies.append(
            {
                "app_id": app_id,
                "anomaly_type": "warning",
                "metric_name": "retention_d7",
                "current_value": float(retention_d7),
                "previous_value": None,
                "delta_pct": None,
                "severity": "warning",
            }
        )

    return anomalies


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

class GrowthIntelState(TypedDict, total=False):
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
    app_data: dict[str, dict[str, Any]]   # app_id → {overview, conversion, retention}
    all_anomalies: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def node_collect(state: GrowthIntelState) -> dict[str, Any]:
    """Fetch overview + conversion + retention for each tracked app."""
    goals = state.get("goals") or {}
    tracked_apps: list[str] = goals.get("tracked_apps") or []
    thresholds: dict[str, Any] = goals.get("thresholds") or {
        "min_roi": 1.0,
        "max_churn_rate": 15.0,
        "min_retention_d7": 20.0,
        "max_mrr_drop_pct": 20.0,
    }

    app_data: dict[str, dict[str, Any]] = {}

    for app_id in tracked_apps:
        overview_result = await invoke_from_state(
            state, "hub.api.overview", {"app_id": app_id, "days": 7}
        )
        overview = (overview_result.get("data") or {}) if overview_result.get("status") == "ok" else {}

        conversion_result = await invoke_from_state(
            state, "hub.api.metrics", {"app_id": app_id, "metric": "conversion", "days": 7}
        )
        conversion = (conversion_result.get("data") or {}) if conversion_result.get("status") == "ok" else {}

        retention_result = await invoke_from_state(
            state, "hub.api.metrics", {"app_id": app_id, "metric": "retention", "days": 7}
        )
        retention = (retention_result.get("data") or {}) if retention_result.get("status") == "ok" else {}

        app_data[app_id] = {
            "overview": overview,
            "conversion": conversion,
            "retention": retention,
            "thresholds": thresholds,
        }

    return {"app_data": app_data}


async def node_analyze(state: GrowthIntelState) -> dict[str, Any]:
    """Run detect_anomalies for each app and emit anomaly events."""
    app_data = state.get("app_data") or {}
    all_anomalies: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for app_id, data in app_data.items():
        thresholds = data.get("thresholds") or {}
        overview = data.get("overview") or {}
        conversion = data.get("conversion") or {}

        anomalies = detect_anomalies(app_id, overview, conversion, thresholds)
        all_anomalies.extend(anomalies)

        for anomaly in anomalies:
            events.append(
                {
                    "event_type": "app.growth.anomaly_detected",
                    "event_version": 1,
                    "payload": {
                        "app_id": anomaly["app_id"],
                        "anomaly_type": anomaly["anomaly_type"],
                        "metric_name": anomaly["metric_name"],
                        "current_value": anomaly.get("current_value"),
                        "previous_value": anomaly.get("previous_value"),
                        "delta_pct": anomaly.get("delta_pct"),
                        "severity": anomaly.get("severity", "warning"),
                    },
                    "idempotency_key": (
                        f"growth_intel:{state.get('run_id', '')}:"
                        f"anomaly:{app_id}:{anomaly['metric_name']}"
                    ),
                }
            )

    return {"all_anomalies": all_anomalies, "events": events}


async def node_report(state: GrowthIntelState) -> dict[str, Any]:
    """Generate LLM summary, notify Slack + Telegram, emit weekly report events."""
    app_data = state.get("app_data") or {}
    all_anomalies = state.get("all_anomalies") or []
    existing_events = state.get("events") or []

    # Build LLM prompt
    import json

    metrics_text = json.dumps(
        {
            app_id: {
                "overview": data.get("overview", {}),
                "conversion": data.get("conversion", {}),
            }
            for app_id, data in app_data.items()
        },
        ensure_ascii=False,
        default=str,
    )[:3000]

    anomalies_text = json.dumps(all_anomalies, ensure_ascii=False)[:1000]

    user_message = (
        f"Uygulama metrikleri:\n{metrics_text}\n\n"
        f"Tespit edilen anomaliler:\n{anomalies_text}\n\n"
        "Lütfen haftalık büyüme raporunu yaz."
    )

    llm_result = await invoke_from_state(
        state,
        "llm.chat",
        {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": 1000,
            "temperature": 0.3,
        },
    )

    llm_summary = ""
    if llm_result.get("status") == "ok":
        llm_summary = (llm_result.get("data") or {}).get("content", "")
    else:
        log.warning(
            "app_studio_growth_intel.llm_failed",
            error=llm_result.get("error"),
        )
        llm_summary = f"[LLM hatası — {llm_result.get('error', 'bilinmeyen hata')}]"

    # Notify Slack
    slack_text = f"*App Studio Haftalık Büyüme Raporu*\n\n{llm_summary[:2000]}"
    slack_result = await invoke_from_state(
        state,
        "slack.notify",
        {"text": slack_text},
    )
    if slack_result.get("status") != "ok":
        log.warning(
            "app_studio_growth_intel.slack_failed",
            error=slack_result.get("error"),
        )

    # Notify Telegram
    tg_result = await invoke_from_state(
        state,
        "telegram.notify",
        {
            "text": slack_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
    )
    if tg_result.get("status") != "ok":
        log.warning(
            "app_studio_growth_intel.telegram_failed",
            error=tg_result.get("error"),
        )

    # Emit weekly report events + build KPI snapshots
    new_events = list(existing_events)
    kpi_updates: list[dict[str, Any]] = []

    for app_id, data in app_data.items():
        overview = data.get("overview") or {}
        conversion = data.get("conversion") or {}

        app_anomalies = [a for a in all_anomalies if a.get("app_id") == app_id]

        new_events.append(
            {
                "event_type": "app.growth.weekly_report",
                "event_version": 1,
                "payload": {
                    "app_id": app_id,
                    "period_days": 7,
                    "mrr": overview.get("mrr"),
                    "active_subs": overview.get("active_subs"),
                    "roi": overview.get("roi"),
                    "trial_starts": overview.get("trial_starts"),
                    "churn_rate": overview.get("churn_rate") or conversion.get("churn_rate"),
                    "retention_d7": overview.get("retention_d7") or conversion.get("retention_d7"),
                    "anomalies": app_anomalies,
                    "summary": llm_summary[:500],
                },
                "idempotency_key": (
                    f"growth_intel:{state.get('run_id', '')}:weekly_report:{app_id}"
                ),
            }
        )

        # KPI snapshots
        if overview.get("mrr") is not None:
            kpi_updates.append({"name": f"mrr.{app_id}", "value": overview["mrr"]})
        if overview.get("roi") is not None:
            kpi_updates.append({"name": f"roi.{app_id}", "value": overview["roi"]})
        if overview.get("active_subs") is not None:
            kpi_updates.append({"name": f"active_subs.{app_id}", "value": overview["active_subs"]})

    kpi_updates.append({"name": "anomalies_total", "value": len(all_anomalies)})

    # Memory
    memories: list[dict[str, Any]] = [
        {
            "content": (
                f"Haftalık büyüme raporu: {len(app_data)} uygulama, "
                f"{len(all_anomalies)} anomali. {llm_summary[:200]}"
            ),
            "tags": ["app-studio", "growth", "weekly-report"],
            "importance": 0.6 if all_anomalies else 0.4,
        }
    ]

    state_accum = dict(state.get("state") or {})
    state_accum["reports_total"] = int(state_accum.get("reports_total", 0)) + 1
    state_accum["anomalies_last_run"] = len(all_anomalies)

    return {
        "events": new_events,
        "memories": memories,
        "kpi_updates": kpi_updates,
        "state": state_accum,
        "summary": (
            f"Growth report: {len(app_data)} apps, {len(all_anomalies)} anomalies. "
            f"{llm_summary[:120]}"
        ),
    }


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_graph() -> Any:
    graph = StateGraph(GrowthIntelState)
    graph.add_node("collect", node_collect)
    graph.add_node("analyze", node_analyze)
    graph.add_node("report", node_report)

    graph.add_edge(START, "collect")
    graph.add_edge("collect", "analyze")
    graph.add_edge("analyze", "report")
    graph.add_edge("report", END)
    return graph.compile()


compiled = build_graph()

register_workflow("app_studio_growth_intel", 1, compiled)
