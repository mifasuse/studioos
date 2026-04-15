"""amz_repricer workflow — Milestone 15: first conditional write agent.

Subscribes to `amz.reprice.recommended` events emitted by amz-pricer.
Two execution modes:

  - dry_run (default for tonight): create an approval row + Telegram
    notify, then complete. No BuyBoxPricer mutation. Safe to run
    unattended.
  - live: same approval gate, but on approval the workflow re-runs
    and detects the granted approval, then calls
    buyboxpricer.api.run_single_repricing for real.

Re-run detection: when a parked run goes back to pending after an
approval is granted, the workflow checks the approvals table for THIS
run_id. If a granted approval exists, it skips the gate and proceeds
to action. This is the first place in StudioOS we close the
approval → action loop end-to-end.
"""
from __future__ import annotations

from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from studioos.db import session_scope
from studioos.logging import get_logger
from studioos.models import Approval
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


class RepricerState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    # populated during run
    recommendation: dict[str, Any]
    already_granted: bool
    action_result: dict[str, Any]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    summary: str


async def _existing_granted_approval(run_id: str) -> bool:
    """Has any approval for this run already been granted?"""
    try:
        rid = UUID(run_id)
    except (TypeError, ValueError):
        return False
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Approval)
                .where(Approval.run_id == rid)
                .where(Approval.state == "approved")
                .limit(1)
            )
        ).scalar_one_or_none()
        return row is not None


async def node_intake(state: RepricerState) -> dict[str, Any]:
    event = state.get("input") or {}
    payload = event.get("payload") or {}
    rec = {
        "asin": payload.get("asin"),
        "sku": payload.get("sku"),
        "listing_id": payload.get("listing_id"),
        "current_price": payload.get("current_price"),
        "proposed_price": payload.get("proposed_price"),
        "buy_box_price": payload.get("buy_box_price"),
        "buybox_seller_name": payload.get("buybox_seller_name"),
        "delta": payload.get("delta"),
        "clamped_to_floor": payload.get("clamped_to_floor", False),
    }
    granted = await _existing_granted_approval(state.get("run_id") or "")
    return {"recommendation": rec, "already_granted": granted}


def _format_approval_msg(rec: dict[str, Any]) -> str:
    return (
        f"reprice {rec.get('asin')} ({rec.get('sku')}): "
        f"${rec.get('current_price')} → ${rec.get('proposed_price')} "
        f"(buybox ${rec.get('buy_box_price')} @ "
        f"{rec.get('buybox_seller_name', '?')})"
    )


async def node_decide(state: RepricerState) -> dict[str, Any]:
    rec = state.get("recommendation") or {}
    granted = state.get("already_granted", False)
    goals = state.get("goals") or {}
    dry_run = bool(goals.get("dry_run", True))

    if granted:
        # The human approved a previous run; this is the post-approval
        # rerun. Proceed straight to the action node.
        return {"approvals": []}

    # First pass: park the run with an approval row.
    text_blob = _format_approval_msg(rec)
    return {
        "approvals": [
            {
                "reason": (
                    f"Repricer would {('DRY-RUN ' if dry_run else '')}{text_blob}"
                ),
                "payload": {
                    "recommendation": rec,
                    "dry_run": dry_run,
                },
                "expires_in_seconds": 60 * 60 * 12,
            }
        ]
    }


async def node_act(state: RepricerState) -> dict[str, Any]:
    rec = state.get("recommendation") or {}
    granted = state.get("already_granted", False)
    goals = state.get("goals") or {}
    dry_run = bool(goals.get("dry_run", True))

    # Only act if the approval has been cleared (post-approval rerun).
    if not granted:
        return {}

    listing_id = rec.get("listing_id")
    asin = rec.get("asin")
    summary_parts: list[str] = []
    memories: list[dict[str, Any]] = []
    kpi_updates: list[dict[str, Any]] = []
    action_result: dict[str, Any] = {}

    state_accum = dict(state.get("state") or {})
    state_accum["acted_total"] = int(state_accum.get("acted_total", 0)) + 1

    if dry_run or not listing_id:
        action_result = {
            "ok": True,
            "dry_run": True,
            "would_have_called": (
                f"buyboxpricer.api.run_single_repricing(listing_id={listing_id})"
            ),
        }
        summary_parts.append(f"DRY-RUN reprice ack {asin}")
        memories.append(
            {
                "content": (
                    f"DRY-RUN repricer acted for {asin} (listing {listing_id}); "
                    f"would have called BBP run-single."
                ),
                "tags": ["amz", "repricer", "dry_run", asin or "?"],
                "importance": 0.4,
            }
        )
    else:
        result = await invoke_from_state(
            state,
            "buyboxpricer.api.run_single_repricing",
            {"listing_id": int(listing_id)},
        )
        action_result = result
        if result["status"] == "ok":
            summary_parts.append(f"Repriced {asin} via BBP")
            memories.append(
                {
                    "content": (
                        f"Repriced {asin} (listing {listing_id}) via BBP; "
                        f"engine result: "
                        f"{(result.get('data') or {}).get('result')}"
                    ),
                    "tags": ["amz", "repricer", "live", asin or "?"],
                    "importance": 0.7,
                }
            )
        else:
            summary_parts.append(
                f"Repricer FAILED {asin}: {result.get('error', '?')[:80]}"
            )
            memories.append(
                {
                    "content": (
                        f"Repricer FAILED for {asin}: "
                        f"{result.get('error', '?')[:200]}"
                    ),
                    "tags": ["amz", "repricer", "failed", asin or "?"],
                    "importance": 0.8,
                }
            )

    # Telegram digest of the action.
    notify_text = (
        f"*💸 AMZ Repricer*\n"
        f"`{asin}` ({rec.get('sku')})\n"
        f"${rec.get('current_price')} → ${rec.get('proposed_price')} "
        f"(-${rec.get('delta')})\n"
        f"_{'DRY-RUN' if dry_run else 'LIVE'}_ · "
        f"{action_result.get('status', 'ok')}"
    )
    notify = await invoke_from_state(
        state,
        "telegram.notify",
        {
            "text": notify_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
    )
    if notify["status"] != "ok":
        log.warning(
            "amz_repricer.notify_failed",
            error=notify.get("error"),
        )

    kpi_updates.append(
        {"name": "repricer_acted_total", "value": state_accum["acted_total"]}
    )

    return {
        "action_result": action_result,
        "memories": memories,
        "kpi_updates": kpi_updates,
        "state": state_accum,
        "summary": " · ".join(summary_parts) or "no-op",
    }


def build_graph() -> Any:
    graph = StateGraph(RepricerState)
    graph.add_node("intake", node_intake)
    graph.add_node("decide", node_decide)
    graph.add_node("act", node_act)
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "decide")
    graph.add_edge("decide", "act")
    graph.add_edge("act", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_repricer", 1, compiled)
