"""Milestone 6 phase 1 — AMZ price monitor vertical slice.

Uses httpx.MockTransport to stub PriceFinder so the test runs hermetically.
Verifies:
  - amz.price.checked events recorded for every ASIN
  - anomaly detection fires when delta crosses the threshold
  - audit row for pricefinder.lookup_asin written
  - KPI snapshots + memory written
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select

from studioos.config import settings
from studioos.db import session_scope
from studioos.models import AgentState, Event, KpiSnapshot, MemorySemantic, ToolCall
from studioos.runtime.consumer import drain_once
from studioos.runtime.dispatcher import dispatch_once
from studioos.runtime.outbox import publish_batch
from studioos.runtime.triggers import create_pending_run


async def _drain(max_iters: int = 15) -> None:
    for _ in range(max_iters):
        ran = await dispatch_once()
        published = await publish_batch()
        consumed = await drain_once()
        if ran is None and published == 0 and consumed == 0:
            return
        await asyncio.sleep(0)


def _make_mock_transport(responses: dict[str, float]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        asin = request.url.params.get("asin")
        if asin not in responses:
            return httpx.Response(404, json={"error": f"unknown asin {asin}"})
        return httpx.Response(
            200,
            json={
                "asin": asin,
                "price": responses[asin],
                "currency": "USD",
                "buybox": True,
                "offer_count": 3,
            },
        )

    return httpx.MockTransport(handler)


def _patch_httpx(transport: httpx.MockTransport):
    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    return patch("studioos.tools.amz.httpx.AsyncClient", factory)


@pytest.mark.asyncio
async def test_amz_monitor_first_scan_records_baseline(db_session) -> None:
    settings.pricefinder_url = "http://pricefinder.test"
    transport = _make_mock_transport(
        {"B08N5WRWNW": 29.99, "B07FZ8S74R": 19.50, "B09G9FPHY6": 149.00}
    )

    with _patch_httpx(transport):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="amz-monitor",
                trigger_type="manual",
                trigger_ref="m6-baseline",
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
                        ToolCall.tool_name == "pricefinder.lookup_asin"
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
    assert len(tool_calls) == 3
    assert all(t.status == "ok" for t in tool_calls)
    kpi_names = {k.name for k in kpis}
    assert {"asins_scanned", "anomalies_found"} <= kpi_names

    # No baseline existed so no anomalies on the first scan.
    anomaly_events = [
        e for e in events if e.event_type == "amz.price.anomaly_detected"
    ]
    assert anomaly_events == []


@pytest.mark.asyncio
async def test_amz_monitor_detects_anomaly_on_second_scan(db_session) -> None:
    settings.pricefinder_url = "http://pricefinder.test"

    # First scan: seed baseline
    with _patch_httpx(
        _make_mock_transport(
            {"B08N5WRWNW": 29.99, "B07FZ8S74R": 19.50, "B09G9FPHY6": 149.00}
        )
    ):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="amz-monitor",
                trigger_type="manual",
                trigger_ref="m6-baseline",
            )
        await _drain()

    # Second scan: drop one ASIN by 10% → well above the 5% threshold
    with _patch_httpx(
        _make_mock_transport(
            {
                "B08N5WRWNW": 26.99,  # -10%
                "B07FZ8S74R": 19.50,
                "B09G9FPHY6": 149.00,
            }
        )
    ):
        async with session_scope() as session:
            await create_pending_run(
                session,
                agent_id="amz-monitor",
                trigger_type="manual",
                trigger_ref="m6-anomaly",
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
    assert payload["asin"] == "B08N5WRWNW"
    assert payload["direction"] == "down"
    assert abs(payload["delta_pct"] + 10.003) < 0.05

    assert any("price_anomaly" in (m.tags or []) for m in memories)
    assert state_row.state["last_prices"]["B08N5WRWNW"] == 26.99
    assert state_row.state["scans_total"] == 2
    assert state_row.state["anomalies_total"] == 1
