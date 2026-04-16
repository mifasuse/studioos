"""CrossLister auto-list — stranded items get draft+publish automatically."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch
from typing import Any

from studioos.workflows.amz_crosslister import node_auto_list, AUTO_LIST_BATCH_MAX


def _stranded(n: int) -> list[dict[str, Any]]:
    return [
        {
            "asin": f"B00STRAND{i:02d}",
            "title": f"Stranded Item {i}",
            "amazon_price": 25.0 + i,
            "fulfillable_quantity": 3,
            "sku": f"SKU-S{i}",
            "priority": "stranded",
            "ebay_target_price": round((25.0 + i) * 1.175, 2),
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_auto_list_respects_batch_max() -> None:
    items = _stranded(10)
    state = {
        "agent_id": "amz-crosslister",
        "studio_id": "amz",
        "run_id": "test-1",
        "new_finds": items,
        "goals": {"auto_list_stranded": True},
    }
    with patch(
        "studioos.workflows.amz_crosslister.invoke_from_state",
        new_callable=AsyncMock,
    ) as mock_invoke:
        mock_invoke.return_value = {
            "status": "ok",
            "data": {"listing_id": 999, "status": "draft"},
        }
        result = await node_auto_list(state)
    listed = result.get("auto_listed") or []
    assert len(listed) == AUTO_LIST_BATCH_MAX
    assert len(listed) <= 5


@pytest.mark.asyncio
async def test_auto_list_skips_non_stranded() -> None:
    items = [
        {"asin": "B00NORMAL01", "priority": "normal", "amazon_price": 30.0},
        {"asin": "B00STRAND01", "priority": "stranded", "amazon_price": 30.0,
         "title": "Test", "sku": "S1", "ebay_target_price": 35.25},
    ]
    state = {
        "agent_id": "amz-crosslister",
        "studio_id": "amz",
        "run_id": "test-2",
        "new_finds": items,
        "goals": {"auto_list_stranded": True},
    }
    with patch(
        "studioos.workflows.amz_crosslister.invoke_from_state",
        new_callable=AsyncMock,
    ) as mock_invoke:
        mock_invoke.return_value = {
            "status": "ok",
            "data": {"listing_id": 999, "status": "draft"},
        }
        result = await node_auto_list(state)
    listed = result.get("auto_listed") or []
    assert len(listed) == 1
    assert listed[0]["asin"] == "B00STRAND01"


@pytest.mark.asyncio
async def test_auto_list_disabled_by_default() -> None:
    state = {
        "agent_id": "amz-crosslister",
        "studio_id": "amz",
        "run_id": "test-3",
        "new_finds": _stranded(3),
        "goals": {},
    }
    result = await node_auto_list(state)
    assert result == {}
