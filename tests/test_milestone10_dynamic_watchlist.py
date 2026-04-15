"""Milestone 10 — dynamic watchlist driven by top_opportunities.

Exercises the full chain with the agent's goals.watchlist_strategy set to
`top_opportunities`: the monitor must consult pricefinder.db.top_opportunities
(stubbed) to pick ASINs, then pass those through the normal lookup + anomaly
detection pipeline.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select

from studioos.db import session_scope
from studioos.models import Agent, AgentState, Event
from studioos.runtime.consumer import drain_once
from studioos.runtime.dispatcher import dispatch_once
from studioos.runtime.outbox import publish_batch
from studioos.runtime.triggers import create_pending_run
from studioos.tools.base import ToolResult
from studioos.tools.registry import _REGISTRY


async def _drain(max_iters: int = 15) -> None:
    for _ in range(max_iters):
        ran = await dispatch_once()
        published = await publish_batch()
        consumed = await drain_once()
        if ran is None and published == 0 and consumed == 0:
            return
        await asyncio.sleep(0)


class _Patch:
    def __init__(self, name: str, handler: Any) -> None:
        self._name = name
        self._handler = handler
        self._original: Any = None

    def __enter__(self) -> None:
        self._original = _REGISTRY[self._name]
        _REGISTRY[self._name] = replace(self._original, handler=self._handler)

    def __exit__(self, *exc: Any) -> None:
        _REGISTRY[self._name] = self._original


def _top_opps_handler(asins: list[str]):
    async def handler(args: dict[str, Any], ctx: Any) -> ToolResult:
        items = [
            {
                "opportunity_id": 1000 + i,
                "asin": asin,
                "title": f"Mock opp {asin}",
                "brand": "MockBrand",
                "source_price": 5.0,
                "target_price": 40.0,
                "estimated_profit": 25.0,
                "profit_margin_percent": 60.0,
                "roi_percent": 500.0,
                "monthly_sold": 12,
                "competition_level": 2,
                "found_at": "2026-04-15T00:00:00",
            }
            for i, asin in enumerate(asins)
        ]
        return ToolResult(
            data={"items": items, "count": len(items), "filters": args}
        )

    return handler


def _lookup_handler(prices: dict[str, float]):
    async def handler(args: dict[str, Any], ctx: Any) -> ToolResult:
        requested = [a.upper() for a in args["asins"]]
        items = []
        missing = []
        for asin in requested:
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
                    "fba_offer_count": 0,
                    "new_offer_count": 1,
                    "sales_rank": None,
                    "last_update": "2026-04-15T00:00:00",
                    "tr_price": None,
                }
            )
        return ToolResult(
            data={"items": items, "found": len(items), "missing": missing}
        )

    return handler


@pytest.mark.asyncio
async def test_monitor_scans_top_opportunities(db_session) -> None:
    """Monitor reads from top_opportunities, not the static list."""
    picked = ["B01TESTAAA", "B01TESTBBB", "B01TESTCCC"]
    with (
        _Patch("pricefinder.db.top_opportunities", _top_opps_handler(picked)),
        _Patch(
            "pricefinder.db.lookup_asins",
            _lookup_handler({a: 10.0 + i for i, a in enumerate(picked)}),
        ),
    ):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="amz-monitor",
                trigger_type="manual",
                trigger_ref="m10-top",
            )
        await _drain()

    # Every picked ASIN should have produced exactly one amz.price.checked
    # event — proving the monitor walked the dynamic watchlist.
    async with session_scope() as session:
        events = (
            (
                await session.execute(
                    select(Event).where(Event.event_type == "amz.price.checked")
                )
            )
            .scalars()
            .all()
        )
    event_asins = {e.payload["asin"] for e in events}
    assert event_asins == set(picked)


@pytest.mark.asyncio
async def test_trigger_watchlist_override_beats_strategy(db_session) -> None:
    """A trigger payload `watchlist` takes precedence over the agent strategy."""

    async def _explode(*_a: Any, **_k: Any) -> ToolResult:
        raise AssertionError("top_opportunities must not be called")

    with (
        _Patch("pricefinder.db.top_opportunities", _explode),
        _Patch("pricefinder.db.lookup_asins", _lookup_handler({"B02OVERRID": 50.0})),
    ):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="amz-monitor",
                trigger_type="manual",
                trigger_ref="m10-override",
                input_snapshot={"watchlist": ["B02OVERRID"]},
            )
        await _drain()

    async with session_scope() as session:
        events = (
            (
                await session.execute(
                    select(Event).where(Event.event_type == "amz.price.checked")
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].payload["asin"] == "B02OVERRID"


@pytest.mark.asyncio
async def test_empty_top_opportunities_short_circuits(db_session) -> None:
    """Monitor gracefully produces zero observations when the pool is empty."""
    with _Patch("pricefinder.db.top_opportunities", _top_opps_handler([])):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="amz-monitor",
                trigger_type="manual",
                trigger_ref="m10-empty",
            )
        await _drain()

    async with session_scope() as session:
        events = (
            (
                await session.execute(
                    select(Event).where(Event.event_type == "amz.price.checked")
                )
            )
            .scalars()
            .all()
        )
        state = (
            await session.execute(
                select(AgentState).where(AgentState.agent_id == "amz-monitor")
            )
        ).scalar_one()
    assert events == []
    # last_scan_epoch still updated, scans_total incremented.
    assert state.state.get("scans_total", 0) == 1
