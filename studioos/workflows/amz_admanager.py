"""amz_admanager workflow — Amazon PPC ad campaign candidates.

Reads pricefinder.db.ad_candidates: ASINs that are economically
worth running PPC ads on (high monthly_sold, decent reviews +
rating, manageable competition). Sends digest. No actual ad
campaign creation — that lives in AdsOptimizer and is the
next milestone, gated by approval.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


# ADMANAGER.md lines 27–36 — budget tier + ACOS pause rules.
BUDGET_TIERS = {
    "high": {"daily_budget_usd": 30.0, "target_acos_pct": 25.0},
    "medium": {"daily_budget_usd": 15.0, "target_acos_pct": 30.0},
    "low": {"daily_budget_usd": 5.0, "target_acos_pct": 35.0},
    "none": {"daily_budget_usd": 0.0, "target_acos_pct": None},
}
ACOS_PAUSE_THRESHOLD_PCT = 50.0
ACOS_PAUSE_GRACE_HOURS = 48


def classify_budget_tier(
    monthly_sold: float | int | None,
    rating: float | None,
) -> str:
    """ADMANAGER.md 27–30: map (monthly_sold, rating) → tier.

      monthly_sold > 200 & rating > 4.0  → high
      monthly_sold 50–200 & rating > 3.5 → medium
      monthly_sold < 50                  → low / none
    """
    ms = float(monthly_sold or 0)
    rt = float(rating or 0)
    if ms > 200 and rt > 4.0:
        return "high"
    if 50 <= ms <= 200 and rt > 3.5:
        return "medium"
    if ms < 50:
        return "none"
    return "low"


class AdState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    candidates: list[dict[str, Any]]
    active_campaigns: list[dict[str, Any]]
    new_finds: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


async def node_scan(state: AdState) -> dict[str, Any]:
    goals = state.get("goals") or {}
    pf = await invoke_from_state(
        state,
        "pricefinder.db.ad_candidates",
        {
            "limit": int(goals.get("scan_limit", 15)),
            "min_monthly_sold": int(goals.get("min_monthly_sold", 50)),
            "min_reviews": int(goals.get("min_reviews", 50)),
            "min_rating": float(goals.get("min_rating", 4.0)),
            "max_competitors": int(goals.get("max_competitors", 15)),
        },
    )
    candidates = (
        (pf["data"] or {}).get("items") or [] if pf["status"] == "ok" else []
    )
    if pf["status"] != "ok":
        log.warning("amz_admanager.scan_failed", error=pf.get("error"))

    # Pull existing AdsOptimizer campaigns so we can flag ASINs that
    # already have one running. The current AdsOptimizer schema doesn't
    # carry actual ACOS metrics, so the `over_target_acos_pause` logic
    # the OpenClaw playbook describes is structurally not implementable
    # here yet — we surface enabled campaigns instead and let the
    # human decide.
    camps = await invoke_from_state(
        state,
        "adsoptimizer.db.list_campaigns",
        {"limit": 200, "state": "enabled"},
    )
    active_campaigns = (
        (camps["data"] or {}).get("items") or []
        if camps["status"] == "ok"
        else []
    )

    return {
        "candidates": candidates,
        "active_campaigns": active_campaigns,
    }


def node_diff(state: AdState) -> dict[str, Any]:
    cands = state.get("candidates") or []
    existing = dict(state.get("state") or {})
    seen = set(existing.get("ad_candidates_seen") or [])
    new_finds: list[dict[str, Any]] = []
    tier_counts = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for c in cands:
        asin = c.get("asin")
        if not asin or asin in seen:
            continue
        tier = classify_budget_tier(c.get("monthly_sold"), c.get("rating"))
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        if tier == "none":
            # Skip entirely — doesn't warrant ad spend per playbook.
            seen.add(asin)
            continue
        tier_cfg = BUDGET_TIERS[tier]
        new_finds.append(
            {
                **c,
                "budget_tier": tier,
                "suggested_daily_budget_usd": tier_cfg["daily_budget_usd"],
                "suggested_target_acos_pct": tier_cfg["target_acos_pct"],
            }
        )
        seen.add(asin)
    if len(seen) > 500:
        seen = set(list(seen)[-500:])
    existing["ad_candidates_seen"] = sorted(seen)
    existing["scans_total"] = int(existing.get("scans_total", 0)) + 1
    existing["last_tier_counts"] = tier_counts
    return {"new_finds": new_finds, "state": existing}


def _format_digest(items: list[dict[str, Any]]) -> str:
    lines = [f"*📣 AMZ AdManager — {len(items)} reklam adayı*"]
    for c in items[:10]:
        asin = c.get("asin", "?")
        ms = c.get("monthly_sold")
        rc = c.get("review_count")
        rating = c.get("rating")
        bb = c.get("buybox_usd")
        comp = c.get("fba_offer_count")
        title = (c.get("title") or "")[:50]
        lines.append(
            f"• `{asin}` ${bb:.0f} · {ms}/mo · {rc} rev · ⭐{rating} · "
            f"comp={comp}\n  {title}"
            if isinstance(bb, (int, float))
            else f"• `{asin}` · {ms}/mo · {rc} rev · ⭐{rating}\n  {title}"
        )
    if len(items) > 10:
        lines.append(f"\n_+{len(items) - 10} more_")
    return "\n".join(lines)


async def node_emit(state: AdState) -> dict[str, Any]:
    new_finds = state.get("new_finds") or []
    events: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    kpi_updates: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []

    # ADMANAGER.md: ACOS > 50% → 48 hours later pause.
    # We don't have real ACOS metrics yet (AdsOptimizer bid-optimization
    # is placeholder per DEV.md), so the best proxy is the campaign's
    # own target_acos: if someone set a target > 50% the campaign is
    # structurally unprofitable and needs review.
    acos_paused = 0
    for camp in state.get("active_campaigns") or []:
        target_acos = camp.get("target_acos")
        try:
            target_acos_f = float(target_acos) if target_acos is not None else None
        except (TypeError, ValueError):
            target_acos_f = None
        if target_acos_f is None or target_acos_f <= ACOS_PAUSE_THRESHOLD_PCT:
            continue
        approvals.append(
            {
                "reason": (
                    f"ADMANAGER: pause campaign '{camp.get('name')}' — "
                    f"target_acos {target_acos_f:.1f}% > "
                    f"{ACOS_PAUSE_THRESHOLD_PCT:.0f}%"
                ),
                "payload": {
                    "kind": "acos_pause",
                    "campaign_id": camp.get("id"),
                    "amazon_campaign_id": camp.get("amazon_campaign_id"),
                    "name": camp.get("name"),
                    "target_acos": target_acos_f,
                    "grace_hours": ACOS_PAUSE_GRACE_HOURS,
                },
                "expires_in_seconds": 60 * 60 * ACOS_PAUSE_GRACE_HOURS,
            }
        )
        acos_paused += 1

    for c in new_finds:
        events.append(
            {
                "event_type": "amz.ad.candidate",
                "event_version": 1,
                "payload": c,
                "idempotency_key": (
                    f"amz_admanager:{state['run_id']}:{c.get('asin')}"
                ),
            }
        )

    notified = False
    if new_finds:
        text = _format_digest(new_finds)
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
            state, "slack.notify", {"text": text, "mrkdwn": True}
        )
        notified = (
            notify_tg["status"] == "ok" or notify_slack["status"] == "ok"
        )
        memories.append(
            {
                "content": (
                    f"AdManager surfaced {len(new_finds)} new ad candidates; "
                    f"top {new_finds[0].get('asin')} "
                    f"({new_finds[0].get('monthly_sold')}/mo)"
                ),
                "tags": ["amz", "admanager", "ppc"],
                "importance": 0.5,
            }
        )

    kpi_updates.append(
        {"name": "ad_candidates_new", "value": len(new_finds)}
    )
    kpi_updates.append(
        {"name": "ad_acos_pause_flags", "value": acos_paused}
    )

    return {
        "events": events,
        "memories": memories,
        "kpi_updates": kpi_updates,
        "approvals": approvals,
        "summary": (
            f"{len(state.get('candidates') or [])} candidates, "
            f"{len(new_finds)} new, {acos_paused} acos-pause"
            + (" (notified)" if notified else "")
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(AdState)
    graph.add_node("scan", node_scan)
    graph.add_node("diff", node_diff)
    graph.add_node("emit", node_emit)
    graph.add_edge(START, "scan")
    graph.add_edge("scan", "diff")
    graph.add_edge("diff", "emit")
    graph.add_edge("emit", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_admanager", 1, compiled)
