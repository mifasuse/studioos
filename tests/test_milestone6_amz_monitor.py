"""Milestone 6 phase 1 — AMZ price monitor vertical slice.

The workflow calls the batch DB tool `pricefinder.db.lookup_asins`. We
stub the tool handler directly so the test runs hermetically without
needing a live PriceFinder DB. A separate smoke test covers the real
asyncpg path in prod.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select

from studioos.db import session_scope
from studioos.models import AgentState, Event, KpiSnapshot, MemorySemantic, ToolCall
from studioos.runtime.consumer import drain_once
from studioos.runtime.dispatcher import dispatch_once
from studioos.runtime.outbox import publish_batch
from studioos.runtime.triggers import create_pending_run
from studioos.tools.base import ToolResult


async def _drain(max_iters: int = 15) -> None:
    for _ in range(max_iters):
        ran = await dispatch_once()
        published = await publish_batch()
        consumed = await drain_once()
        if ran is None and published == 0 and consumed == 0:
            return
        await asyncio.sleep(0)


def _mock_batch_handler(prices: dict[str, float]):
    """Return a handler that mimics pricefinder.db.lookup_asins."""

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


class _ToolPatch:
    """Swap the registered Tool with one wrapping a stub handler."""

    def __init__(self, name: str, handler: Any) -> None:
        self._name = name
        self._handler = handler
        self._original: Any = None

    def __enter__(self) -> None:
        from dataclasses import replace

        from studioos.tools.registry import _REGISTRY

        self._original = _REGISTRY[self._name]
        _REGISTRY[self._name] = replace(self._original, handler=self._handler)

    def __exit__(self, *exc: Any) -> None:
        from studioos.tools.registry import _REGISTRY

        _REGISTRY[self._name] = self._original


def _patch_db_tool(prices: dict[str, float]) -> _ToolPatch:
    return _ToolPatch(
        "pricefinder.db.lookup_asins", _mock_batch_handler(prices)
    )


@pytest.mark.asyncio
async def test_amz_monitor_first_scan_records_baseline(db_session) -> None:
    with _patch_db_tool(
        {"B00MFMV6S6": 29.99, "B001JYN1IE": 19.50, "B015LJPJUU": 149.00}
    ):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="amz-monitor",
                trigger_type="manual",
                trigger_ref="m6-baseline",
                input_snapshot={
                    "watchlist": [
                        "B00MFMV6S6",
                        "B001JYN1IE",
                        "B015LJPJUU",
                    ]
                },
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
        tool_calls = (
            (
                await session.execute(
                    select(ToolCall).where(
                        ToolCall.tool_name == "pricefinder.db.lookup_asins"
                    )
                )
            )
            .scalars()
            .all()
        )
        kpis = (
            (
                await session.execute(
                    select(KpiSnapshot).where(
                        KpiSnapshot.agent_id == "amz-monitor"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(events) == 3
    assert len(tool_calls) == 1  # batch = single call for all ASINs
    assert tool_calls[0].status == "ok"
    kpi_names = {k.name for k in kpis}
    assert {"asins_scanned", "anomalies_found"} <= kpi_names

    anomaly_events = [
        e for e in events if e.event_type == "amz.price.anomaly_detected"
    ]
    assert anomaly_events == []


@pytest.mark.asyncio
async def test_amz_monitor_detects_anomaly_on_second_scan(db_session) -> None:
    with _patch_db_tool(
        {"B00MFMV6S6": 29.99, "B001JYN1IE": 19.50, "B015LJPJUU": 149.00}
    ):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="amz-monitor",
                trigger_type="manual",
                trigger_ref="m6-baseline",
                input_snapshot={
                    "watchlist": [
                        "B00MFMV6S6",
                        "B001JYN1IE",
                        "B015LJPJUU",
                    ]
                },
            )
        await _drain()

    with _patch_db_tool(
        {"B00MFMV6S6": 26.99, "B001JYN1IE": 19.50, "B015LJPJUU": 149.00}
    ):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="amz-monitor",
                trigger_type="manual",
                trigger_ref="m6-anomaly",
                input_snapshot={
                    "watchlist": [
                        "B00MFMV6S6",
                        "B001JYN1IE",
                        "B015LJPJUU",
                    ]
                },
            )
        await _drain()

    async with session_scope() as session:
        anomalies = (
            (
                await session.execute(
                    select(Event).where(
                        Event.event_type == "amz.price.anomaly_detected"
                    )
                )
            )
            .scalars()
            .all()
        )
        memories = (
            (
                await session.execute(
                    select(MemorySemantic).where(
                        MemorySemantic.agent_id == "amz-monitor"
                    )
                )
            )
            .scalars()
            .all()
        )
        state_row = (
            await session.execute(
                select(AgentState).where(AgentState.agent_id == "amz-monitor")
            )
        ).scalar_one()

    assert len(anomalies) == 1
    payload = anomalies[0].payload
    assert payload["asin"] == "B00MFMV6S6"
    assert payload["direction"] == "down"
    assert abs(payload["delta_pct"] + 10.003) < 0.05

    assert any("price_anomaly" in (m.tags or []) for m in memories)
    assert state_row.state["last_prices"]["B00MFMV6S6"] == 26.99
    assert state_row.state["scans_total"] == 2
    assert state_row.state["anomalies_total"] == 1
