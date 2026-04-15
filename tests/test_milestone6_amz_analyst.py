"""Milestone 6 phase 2 — LLM-backed AMZ analyst.

Stubs both the pricefinder DB tool and the llm.chat tool so the full
monitor → anomaly → analyst chain runs hermetically. The key observations:

  - analyst accepts a high-confidence 'accept' verdict  → amz.opportunity.confirmed
  - analyst rejects a high-confidence 'reject' verdict → amz.opportunity.rejected
  - analyst parks the run on 'uncertain'               → approval row
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select

from studioos.db import session_scope
from studioos.models import Approval, Event
from studioos.runtime.consumer import drain_once
from studioos.runtime.dispatcher import dispatch_once
from studioos.runtime.outbox import publish_batch
from studioos.runtime.triggers import create_pending_run
from studioos.tools.base import ToolResult
from studioos.tools.registry import _REGISTRY


async def _drain(max_iters: int = 30) -> None:
    for _ in range(max_iters):
        ran = await dispatch_once()
        published = await publish_batch()
        consumed = await drain_once()
        if ran is None and published == 0 and consumed == 0:
            return
        await asyncio.sleep(0)


class _StubPatch:
    def __init__(self, name: str, handler: Any) -> None:
        self._name = name
        self._handler = handler
        self._original: Any = None

    def __enter__(self) -> None:
        self._original = _REGISTRY[self._name]
        _REGISTRY[self._name] = replace(self._original, handler=self._handler)

    def __exit__(self, *exc: Any) -> None:
        _REGISTRY[self._name] = self._original


def _pf_handler(prices: dict[str, float]):
    async def handler(args: dict[str, Any], ctx: Any) -> ToolResult:
        asins = [a.upper() for a in args["asins"]]
        items = []
        missing = []
        for asin in asins:
            price = prices.get(asin)
            if price is None:
                missing.append(asin)
                continue
            items.append(
                {
                    "asin": asin,
                    "product_id": 999,
                    "title": f"Mock {asin}",
                    "brand": "MockBrand",
                    "price": price,
                    "currency": "USD",
                    "price_source": "buybox",
                    "fba_offer_count": 3,
                    "new_offer_count": 5,
                    "sales_rank": 12345,
                    "last_update": "2026-04-15T00:00:00",
                    "tr_price": 45.0,
                }
            )
        return ToolResult(
            data={"items": items, "found": len(items), "missing": missing}
        )

    return handler


def _llm_handler(verdict: str, confidence: float, rationale: str = "mock"):
    async def handler(args: dict[str, Any], ctx: Any) -> ToolResult:
        body = json.dumps(
            {
                "verdict": verdict,
                "confidence": confidence,
                "rationale": rationale,
                "recommended_action": "buy" if verdict == "accept" else None,
            }
        )
        return ToolResult(
            data={
                "content": body,
                "parsed_json": json.loads(body),
                "model": "mock-model",
                "finish_reason": "stop",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "total_tokens": 140,
                },
            }
        )

    return handler


async def _run_monitor_then_anomaly() -> None:
    # First scan establishes baseline, second scan drops price 10% → anomaly.
    with _StubPatch(
        "pricefinder.db.lookup_asins",
        _pf_handler({"B00MFMV6S6": 29.99}),
    ):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="amz-monitor",
                trigger_type="manual",
                trigger_ref="m6.2-baseline",
                input_snapshot={"watchlist": ["B00MFMV6S6"]},
            )
        await _drain()


async def _run_drop_scan() -> None:
    with _StubPatch(
        "pricefinder.db.lookup_asins",
        _pf_handler({"B00MFMV6S6": 26.99}),
    ):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="amz-monitor",
                trigger_type="manual",
                trigger_ref="m6.2-drop",
                input_snapshot={"watchlist": ["B00MFMV6S6"]},
            )
        await _drain()


@pytest.mark.asyncio
async def test_analyst_confirms_opportunity(db_session) -> None:
    await _run_monitor_then_anomaly()

    with (
        _StubPatch(
            "pricefinder.db.lookup_asins",
            _pf_handler({"B00MFMV6S6": 26.99}),
        ),
        _StubPatch(
            "llm.chat",
            _llm_handler("accept", 0.92, "sustained drop, healthy rank"),
        ),
    ):
        await _run_drop_scan()

    async with session_scope() as session:
        confirmed = (
            (
                await session.execute(
                    select(Event).where(
                        Event.event_type == "amz.opportunity.confirmed"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(confirmed) == 1
    payload = confirmed[0].payload
    assert payload["asin"] == "B00MFMV6S6"
    assert payload["verdict"] == "accept"
    assert payload["confidence"] == 0.92
    assert payload["recommended_action"] == "buy"


@pytest.mark.asyncio
async def test_analyst_rejects_noise(db_session) -> None:
    await _run_monitor_then_anomaly()

    with (
        _StubPatch(
            "pricefinder.db.lookup_asins",
            _pf_handler({"B00MFMV6S6": 26.99}),
        ),
        _StubPatch(
            "llm.chat",
            _llm_handler("reject", 0.88, "single-tick blip, no trend"),
        ),
    ):
        await _run_drop_scan()

    async with session_scope() as session:
        rejected = (
            (
                await session.execute(
                    select(Event).where(
                        Event.event_type == "amz.opportunity.rejected"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rejected) == 1
    assert rejected[0].payload["verdict"] == "reject"


@pytest.mark.asyncio
async def test_analyst_uncertain_creates_approval(db_session) -> None:
    await _run_monitor_then_anomaly()

    with (
        _StubPatch(
            "pricefinder.db.lookup_asins",
            _pf_handler({"B00MFMV6S6": 26.99}),
        ),
        _StubPatch(
            "llm.chat",
            _llm_handler("uncertain", 0.3, "thin context, cannot decide"),
        ),
    ):
        await _run_drop_scan()

    async with session_scope() as session:
        approvals = (
            (
                await session.execute(
                    select(Approval).where(Approval.agent_id == "amz-analyst")
                )
            )
            .scalars()
            .all()
        )
    assert len(approvals) == 1
    assert approvals[0].state == "pending"
    assert "uncertain" in approvals[0].reason.lower()
