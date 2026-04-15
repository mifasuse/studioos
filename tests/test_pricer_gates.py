"""amz_pricer gating rules — OpenClaw PRICER.md lines 36–41.

  - Buy Box kaybında 15 dk bekle
  - Günde max 2 fiyat değişikliği
  - fba_offer_count > 10 + fiyat savaşı → CEO eskalasyon

We call node_recommend directly so the DB-backed scan is mocked out.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from studioos.workflows.amz_pricer import (
    DAILY_REPRICE_CAP,
    LOST_BUYBOX_WAIT_MINUTES,
    node_recommend,
)


def _listing(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "listing_id": 101,
        "asin": "B00TEST0001",
        "sku": "SKU-101",
        "current_price": 50.0,
        "buy_box_price": 45.0,
        "min_price": 30.0,
        "max_price": 80.0,
        "has_buybox": False,
        "competitor_count": 4,
        "age_days": 10,
    }
    base.update(over)
    return base


def _run(lost: list[dict], state: dict | None = None) -> dict:
    return node_recommend(
        {
            "lost": lost,
            "aging": [],
            "goals": {},
            "state": state or {},
        }  # type: ignore[arg-type]
    )


def test_fresh_lost_buybox_waits_15min_on_first_sight() -> None:
    out = _run([_listing()])
    assert out["recommendations"] == []
    assert out["state"]["lost_since"]["101"]  # recorded
    assert out["state"]["last_gated_counts"]["wait_15min"] == 1


def test_lost_buybox_recommends_after_wait_elapsed() -> None:
    past = (datetime.now(UTC) - timedelta(minutes=LOST_BUYBOX_WAIT_MINUTES + 1)).isoformat()
    state = {"lost_since": {"101": past}}
    out = _run([_listing()], state)
    assert len(out["recommendations"]) == 1
    rec = out["recommendations"][0]
    assert rec["strategy"] == "buy_box_win"
    assert rec["proposed_price"] < rec["current_price"]


def test_daily_rate_limit_blocks_third_reprice() -> None:
    past = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    recent_ts = [
        (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        (datetime.now(UTC) - timedelta(hours=5)).isoformat(),
    ]
    state = {
        "lost_since": {"101": past},
        "reprice_log": {"101": recent_ts},
    }
    out = _run([_listing()], state)
    assert out["recommendations"] == []
    assert out["state"]["last_gated_counts"]["rate_limited"] == 1
    # Log untouched (still 2 entries).
    assert len(out["state"]["reprice_log"]["101"]) == DAILY_REPRICE_CAP


def test_price_war_escalates_to_ceo_approval() -> None:
    past = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    one_recent = [(datetime.now(UTC) - timedelta(hours=2)).isoformat()]
    state = {
        "lost_since": {"101": past},
        "reprice_log": {"101": one_recent},
    }
    # 12 competitors + already 1 reprice in last 24h = price war.
    out = _run([_listing(competitor_count=12)], state)
    assert out["recommendations"] == []
    assert len(out["approvals"]) == 1
    assert "price-war" in out["approvals"][0]["reason"]
    assert out["state"]["last_gated_counts"]["price_war_escalation"] == 1
