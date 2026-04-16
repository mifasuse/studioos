# M28: Low-Risk Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make StudioOS autonomously reprice Amazon listings and auto-list stranded inventory on eBay — the first real write operations without human approval.

**Architecture:** Two independent changes: (1) flip repricer from approval-gated dry-run to direct execution, (2) add `create_draft` tool + wire crosslister to auto-list stranded items with a batch cap of 5.

**Tech Stack:** Python 3.12, LangGraph workflows, httpx, pytest

---

### Task 1: Live Repricing — remove approval gate

**Files:**
- Modify: `studioos/workflows/amz_repricer.py`
- Modify: `studioos/studios/amz/studio.yaml`
- Test: `tests/test_repricer_live.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_repricer_live.py`:

```python
"""Repricer live mode — no approval gate, direct execution."""
from __future__ import annotations
from unittest.mock import AsyncMock, patch
from typing import Any
from studioos.workflows.amz_repricer import node_decide, node_act


def _state(**over: Any) -> dict:
    base: dict[str, Any] = {
        "agent_id": "amz-repricer",
        "studio_id": "amz",
        "run_id": "test-run-1",
        "goals": {"dry_run": False},
        "recommendation": {
            "asin": "B00TEST0001",
            "sku": "SKU-1",
            "listing_id": 101,
            "current_price": 50.0,
            "proposed_price": 45.0,
            "buy_box_price": 44.0,
            "delta": 5.0,
            "clamped_to_floor": False,
        },
        "already_granted": False,
        "state": {},
    }
    base.update(over)
    return base


def test_decide_skips_approval_when_not_dry_run() -> None:
    """With dry_run=False, node_decide should NOT create approval rows."""
    result = node_decide(_state())
    approvals = result.get("approvals") or []
    assert len(approvals) == 0


def test_decide_still_creates_approval_in_dry_run() -> None:
    """With dry_run=True, node_decide still gates with an approval."""
    result = node_decide(_state(goals={"dry_run": True}))
    approvals = result.get("approvals") or []
    assert len(approvals) == 1
    assert "DRY-RUN" in approvals[0]["reason"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/ntopcugil/Documents/Projects/Amz/studioos && uv run pytest tests/test_repricer_live.py -v`

Expected: `test_decide_skips_approval_when_not_dry_run` FAILS (current code always creates an approval row on first pass regardless of dry_run).

- [ ] **Step 3: Modify node_decide to skip approval gate when dry_run=False**

In `studioos/workflows/amz_repricer.py`, replace the `node_decide` function:

```python
async def node_decide(state: RepricerState) -> dict[str, Any]:
    rec = state.get("recommendation") or {}
    granted = state.get("already_granted", False)
    goals = state.get("goals") or {}
    dry_run = bool(goals.get("dry_run", True))

    if granted:
        return {"approvals": []}

    # Live mode: skip approval, proceed directly to action.
    if not dry_run:
        return {"approvals": []}

    # Dry-run mode: park with approval row as before.
    text_blob = _format_approval_msg(rec)
    return {
        "approvals": [
            {
                "reason": f"Repricer would DRY-RUN {text_blob}",
                "payload": {
                    "recommendation": rec,
                    "dry_run": dry_run,
                },
                "expires_in_seconds": 60 * 60 * 12,
            }
        ]
    }
```

- [ ] **Step 4: Modify node_act to execute directly when not dry_run (no granted check)**

In `studioos/workflows/amz_repricer.py`, update the guard at the top of `node_act`:

```python
async def node_act(state: RepricerState) -> dict[str, Any]:
    rec = state.get("recommendation") or {}
    granted = state.get("already_granted", False)
    goals = state.get("goals") or {}
    dry_run = bool(goals.get("dry_run", True))

    # Act if: live mode (no approval needed) OR approved rerun.
    if dry_run and not granted:
        return {}

    # ... rest of function unchanged ...
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_repricer_live.py -v`
Expected: 2 passed

- [ ] **Step 6: Update studio.yaml — set dry_run: false**

In `studioos/studios/amz/studio.yaml`, change the `amz-repricer` goals:

```yaml
  - id: amz-repricer
    template_id: amz_repricer
    template_version: 1
    display_name: "AMZ Repricer"
    mode: normal
    heartbeat_config: {}
    goals:
      dry_run: false
    tool_scope:
      - buyboxpricer.api.run_single_repricing
      - telegram.notify
      - memory.search
```

- [ ] **Step 7: Run all tests**

Run: `uv run pytest tests/test_repricer_live.py tests/test_pricer_gates.py tests/test_analyst_scoring.py -q`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add studioos/workflows/amz_repricer.py studioos/studios/amz/studio.yaml tests/test_repricer_live.py
git commit -m "feat(M28): live repricing — remove approval gate, dry_run=false

Repricer now executes buyboxpricer.api.run_single_repricing directly
when dry_run=false (new default). Floor price + 2/day cap + price-war
escalation still protect against bad reprices. Dry-run mode preserved
as opt-in for testing.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Stranded Auto-List — create_draft tool

**Files:**
- Modify: `studioos/tools/ebaycrosslister.py`
- Test: `tests/test_ebay_create_draft.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ebay_create_draft.py`:

```python
"""ebaycrosslister.api.create_draft tool — smoke test the registration."""
from __future__ import annotations

from studioos.tools.registry import get_tool


def test_create_draft_tool_registered() -> None:
    tool = get_tool("ebaycrosslister.api.create_draft")
    assert tool is not None
    assert tool.name == "ebaycrosslister.api.create_draft"
    schema = tool.input_schema
    assert "title" in schema["properties"]
    assert "price" in schema["properties"]
    assert "quantity" in schema["properties"]
    assert "title" in schema["required"]
    assert "price" in schema["required"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ebay_create_draft.py -v`
Expected: FAIL — tool not registered

- [ ] **Step 3: Add create_draft tool**

In `studioos/tools/ebaycrosslister.py`, add before the `publish_listing` tool:

```python
@register_tool(
    "ebaycrosslister.api.create_draft",
    description=(
        "Create a draft eBay listing via POST /listings/. "
        "Requires title, price, quantity. Returns the new listing_id. "
        "Authenticated; must publish separately to go live."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "price": {"type": "number"},
            "quantity": {"type": "integer"},
            "condition": {"type": "string"},
            "asin": {"type": "string"},
            "sku": {"type": "string"},
        },
        "required": ["title", "price"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="amz",
    cost_cents=2,
)
async def ebaycrosslister_api_create_draft(
    args: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    base = settings.ebaycrosslister_api_url.rstrip("/")
    url = f"{base}/listings/"
    body = {
        "title": args["title"],
        "price": float(args["price"]),
        "quantity": int(args.get("quantity", 1)),
        "condition": args.get("condition", "new"),
    }
    if args.get("asin"):
        body["asin"] = args["asin"]
    if args.get("sku"):
        body["sku"] = args["sku"]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token = await _ebay_token(client)
            resp = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 401:
                token = await _ebay_token(client, force=True)
                resp = await client.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
    except httpx.HTTPError as exc:
        raise ToolError(f"ebaycrosslister http error: {exc}") from exc
    if resp.status_code >= 400:
        raise ToolError(
            f"ebaycrosslister {resp.status_code}: {resp.text[:300]}"
        )
    try:
        result = resp.json()
    except ValueError as exc:
        raise ToolError(f"ebaycrosslister non-json: {exc}") from exc
    return ToolResult(
        data={
            "listing_id": result.get("id"),
            "status": "draft",
            "result": result,
        }
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ebay_create_draft.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add studioos/tools/ebaycrosslister.py tests/test_ebay_create_draft.py
git commit -m "feat(M28): ebaycrosslister.api.create_draft tool

POST /listings/ to create a draft eBay listing with title, price,
quantity. Returns listing_id for subsequent publish_listing call.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Stranded Auto-List — wire crosslister workflow

**Files:**
- Modify: `studioos/workflows/amz_crosslister.py`
- Modify: `studioos/studios/amz/studio.yaml`
- Test: `tests/test_crosslister_autolist.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_crosslister_autolist.py`:

```python
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
    """Only AUTO_LIST_BATCH_MAX items are listed per run."""
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
    """Only priority=stranded items are auto-listed."""
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_crosslister_autolist.py -v`
Expected: FAIL — `node_auto_list` does not exist

- [ ] **Step 3: Add node_auto_list to crosslister workflow**

In `studioos/workflows/amz_crosslister.py`, add after `node_diff` and before `_format_digest`:

```python
AUTO_LIST_BATCH_MAX = 5


async def node_auto_list(state: CrossState) -> dict[str, Any]:
    """Auto-list stranded items on eBay: create_draft → publish.

    Only processes items with priority='stranded'. Caps at
    AUTO_LIST_BATCH_MAX per run to avoid flooding eBay.
    """
    goals = state.get("goals") or {}
    if not goals.get("auto_list_stranded", False):
        return {}

    new_finds = state.get("new_finds") or []
    stranded = [f for f in new_finds if f.get("priority") == "stranded"]
    batch = stranded[:AUTO_LIST_BATCH_MAX]

    auto_listed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for item in batch:
        asin = item.get("asin") or "?"
        title = (item.get("title") or asin)[:80]
        price = item.get("ebay_target_price") or (
            (item.get("amazon_price") or 0) * 1.175
        )
        qty = item.get("fulfillable_quantity") or 1
        sku = item.get("sku")

        # Step 1: create draft
        draft = await invoke_from_state(
            state,
            "ebaycrosslister.api.create_draft",
            {
                "title": title,
                "price": round(price, 2),
                "quantity": int(qty),
                "condition": "new",
                "asin": asin,
                "sku": sku or "",
            },
        )
        if draft["status"] != "ok":
            failed.append({"asin": asin, "step": "create_draft", "error": draft.get("error")})
            continue

        listing_id = (draft.get("data") or {}).get("listing_id")
        if not listing_id:
            failed.append({"asin": asin, "step": "create_draft", "error": "no listing_id"})
            continue

        # Step 2: publish
        pub = await invoke_from_state(
            state,
            "ebaycrosslister.api.publish_listing",
            {"listing_id": int(listing_id)},
        )
        if pub["status"] != "ok":
            failed.append({"asin": asin, "step": "publish", "error": pub.get("error")})
            continue

        auto_listed.append({
            "asin": asin,
            "listing_id": listing_id,
            "price": round(price, 2),
        })

    return {
        "auto_listed": auto_listed,
        "auto_list_failed": failed,
    }
```

- [ ] **Step 4: Wire node_auto_list into the graph**

In `studioos/workflows/amz_crosslister.py`, update `build_graph`:

```python
def build_graph() -> Any:
    graph = StateGraph(CrossState)
    graph.add_node("scan", node_scan)
    graph.add_node("scan_rules", node_scan_rules)
    graph.add_node("diff", node_diff)
    graph.add_node("auto_list", node_auto_list)
    graph.add_node("emit", node_emit)
    graph.add_edge(START, "scan")
    graph.add_edge("scan", "scan_rules")
    graph.add_edge("scan_rules", "diff")
    graph.add_edge("diff", "auto_list")
    graph.add_edge("auto_list", "emit")
    graph.add_edge("emit", END)
    return graph.compile()
```

Also add `auto_listed` and `auto_list_failed` to `CrossState`:

```python
class CrossState(TypedDict, total=False):
    # ... existing fields ...
    auto_listed: list[dict[str, Any]]
    auto_list_failed: list[dict[str, Any]]
```

- [ ] **Step 5: Update node_emit to include auto-list results in notification**

In `node_emit`, after the existing Telegram notification block, add auto-list digest:

```python
    # Auto-list digest
    auto_listed = state.get("auto_listed") or []
    auto_failed = state.get("auto_list_failed") or []
    if auto_listed:
        al_lines = [f"\n*📦 Auto-listed {len(auto_listed)} stranded on eBay:*"]
        for al in auto_listed:
            al_lines.append(f"  {al['asin']} → listing #{al['listing_id']} @ ${al['price']}")
        al_text = "\n".join(al_lines)
        await invoke_from_state(
            state,
            "telegram.notify",
            {"text": al_text, "parse_mode": "Markdown", "disable_web_page_preview": True},
        )
        memories.append({
            "content": f"Auto-listed {len(auto_listed)} stranded items on eBay: {', '.join(a['asin'] for a in auto_listed)}",
            "tags": ["amz", "crosslister", "auto_listed"],
            "importance": 0.7,
        })
    if auto_failed:
        memories.append({
            "content": f"Auto-list failed for {len(auto_failed)} items: {auto_failed}",
            "tags": ["amz", "crosslister", "auto_list_failed"],
            "importance": 0.8,
        })
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_crosslister_autolist.py -v`
Expected: 2 passed

- [ ] **Step 7: Update studio.yaml — add tool + enable auto_list_stranded**

In `studioos/studios/amz/studio.yaml`, update `amz-crosslister`:

```yaml
  - id: amz-crosslister
    template_id: amz_crosslister
    template_version: 1
    display_name: "AMZ CrossLister"
    mode: normal
    heartbeat_config: {}
    schedule_cron: "@every 6h"
    goals:
      scan_limit: 30
      auto_list_stranded: true
    tool_scope:
      - ebaycrosslister.db.listable_items
      - ebaycrosslister.db.stranded_inventory
      - ebaycrosslister.db.low_stock_listings
      - ebaycrosslister.api.create_draft
      - ebaycrosslister.api.publish_listing
      - pricefinder.db.crosslist_candidates
      - telegram.notify
      - slack.notify
      - memory.search
```

- [ ] **Step 8: Run all tests**

Run: `uv run pytest tests/test_repricer_live.py tests/test_ebay_create_draft.py tests/test_crosslister_autolist.py tests/test_pricer_gates.py tests/test_analyst_scoring.py -q`
Expected: all pass

- [ ] **Step 9: Commit**

```bash
git add studioos/workflows/amz_crosslister.py studioos/studios/amz/studio.yaml tests/test_crosslister_autolist.py
git commit -m "feat(M28): stranded auto-list — create_draft + publish on eBay

CrossLister now auto-lists stranded Amazon inventory on eBay:
create_draft → publish, batch max 5 per run, Telegram digest.
Only items with priority=stranded are auto-listed.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Final integration — push and verify

**Files:** none (deploy + verify)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -q --ignore=tests/test_milestone6_amz_analyst.py --ignore=tests/test_milestone6_amz_monitor.py --ignore=tests/test_milestone7_scheduler.py --ignore=tests/test_milestone1.py --ignore=tests/test_milestone2_memory.py --ignore=tests/test_milestone3_bus.py --ignore=tests/test_milestone9_status.py --ignore=tests/test_milestone10_dynamic_watchlist.py --ignore=tests/test_milestone5_budget_approvals.py`

Expected: all pass (DB-dependent milestone tests excluded — they need postgres 5433)

- [ ] **Step 2: Push to main**

```bash
git push origin main
```

GHA deploy triggers automatically.

- [ ] **Step 3: Verify on prod**

After deploy completes (~2 min), verify:

```bash
# Check repricer next run picks up dry_run=false
ssh -i deployer_new_key deployer@168.119.15.239 \
  "cd /home/deployer/studioos && docker compose logs studioos --tail 20 2>&1 | grep repricer"

# Check crosslister has create_draft in scope
ssh -i deployer_new_key deployer@168.119.15.239 \
  "cd /home/deployer/studioos && docker compose exec -T studioos uv run python -c \"
from studioos.tools.registry import get_tool
print('create_draft:', get_tool('ebaycrosslister.api.create_draft') is not None)
print('publish:', get_tool('ebaycrosslister.api.publish_listing') is not None)
\""
```

Expected: both tools registered, repricer running with `dry_run=false`.
