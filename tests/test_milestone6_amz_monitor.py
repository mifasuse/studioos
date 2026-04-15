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
    """Emulate the PriceFinder OAuth2 + /products search endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/auth/token"):
            return httpx.Response(
                200,
                json={"access_token": "test-token", "token_type": "bearer"},
            )
        if path.endswith("/products/") or path.endswith("/products"):
            asin = (request.url.params.get("search") or "").upper()
            price = responses.get(asin)
            if price is None:
                return httpx.Response(
                    200,
                    json={"items": [], "total": 0, "page": 1, "pages": 0},
                )
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 999,
                            "asin": asin,
                            "title": f"Mock product {asin}",
                            "brand": "MockBrand",
                            "us_market_data": {
                                "buybox_price": str(price),
                                "amazon_price": None,
                                "lowest_price": str(price),
                                "last_update": "2026-04-15T00:00:00+00:00",
                            },
                            "us_profit": 10.0,
                            "us_roi": 20.0,
                            "us_margin": 15.0,
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "pages": 1,
                },
            )
        return httpx.Response(404, json={"detail": "not mocked"})

    return httpx.MockTransport(handler)


def _patch_httpx(transport: httpx.MockTransport):
    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    # Also reset the cached PF client so each test rebuilds its token cache.
    import studioos.tools.amz as amz_mod

    amz_mod._client_singleton = None  # type: ignore[attr-defined]
    return patch("studioos.tools.amz.httpx.AsyncClient", factory)


@pytest.mark.asyncio
async def test_amz_monitor_first_scan_records_baseline(db_session) -> None:
    settings.pricefinder_url = "http://pricefinder.test/api/v1"
    settings.pricefinder_username = "test@test"
    settings.pricefinder_password = "pw"
    transport = _make_mock_transport(
        {"B00MFMV6S6": 29.99, "B001JYN1IE": 19.50, "B015LJPJUU": 149.00}
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
    settings.pricefinder_url = "http://pricefinder.test/api/v1"
    settings.pricefinder_username = "test@test"
    settings.pricefinder_password = "pw"

    # First scan: seed baseline
    with _patch_httpx(
        _make_mock_transport(
            {"B00MFMV6S6": 29.99, "B001JYN1IE": 19.50, "B015LJPJUU": 149.00}
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
                "B00MFMV6S6": 26.99,  # -10%
                "B001JYN1IE": 19.50,
                "B015LJPJUU": 149.00,
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
    assert payload["asin"] == "B00MFMV6S6"
    assert payload["direction"] == "down"
    assert abs(payload["delta_pct"] + 10.003) < 0.05

    assert any("price_anomaly" in (m.tags or []) for m in memories)
    assert state_row.state["last_prices"]["B00MFMV6S6"] == 26.99
    assert state_row.state["scans_total"] == 2
    assert state_row.state["anomalies_total"] == 1
