# M33: Agentic Reasoning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace fixed pipeline execution with a ReAct (Reason + Act) loop so agents can think, use tools, observe results, and respond conversationally — especially for Slack mentions.

**Architecture:** Single shared workflow `react_conversation` with LangGraph conditional edges. Agent identity from persona registry, tool scope enforced from DB. Think → tool → observe loop, max 5 iterations, response routed to Slack thread or Telegram based on trigger type.

**Tech Stack:** Python 3.12, LangGraph StateGraph + conditional edges, `invoke_from_state`, `llm.chat`

---

### Task 1: Persona Registry + Tool Scope Helper

**Files:**
- Create: `studioos/workflows/personas.py`
- Create: `tests/test_personas.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_personas.py`:

```python
"""Persona registry + tool scope helper."""
from __future__ import annotations


def test_known_agent_has_persona() -> None:
    from studioos.workflows.personas import get_persona
    persona = get_persona("amz-pricer")
    assert "pricer" in persona.lower() or "fiyat" in persona.lower()
    assert len(persona) > 20


def test_unknown_agent_gets_default() -> None:
    from studioos.workflows.personas import get_persona
    persona = get_persona("nonexistent-agent")
    assert "StudioOS" in persona
    assert len(persona) > 10


def test_tool_description_list() -> None:
    from studioos.workflows.personas import format_tool_list
    tools = ["buyboxpricer.db.lost_buybox", "pricefinder.db.scout_candidates"]
    result = format_tool_list(tools)
    assert "buyboxpricer.db.lost_buybox" in result
    assert "pricefinder.db.scout_candidates" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/ntopcugil/Documents/Projects/Amz/studioos && uv run pytest tests/test_personas.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Create `studioos/workflows/personas.py`**

```python
"""Agent persona registry — system prompts for ReAct conversations.

Each agent has a personality + role description used when the ReAct
workflow needs to reason on behalf of that agent. Unknown agents
get a generic StudioOS assistant prompt.
"""
from __future__ import annotations

from studioos.tools.registry import get_tool

PERSONAS: dict[str, str] = {
    "amz-monitor": (
        "Sen AMZ Monitor — Amazon fiyat izleme ajanısın. "
        "PriceFinder'dan ASIN fiyatlarını takip eder, anomali tespit edersin."
    ),
    "amz-scout": (
        "Sen AMZ Scout — arbitraj fırsat avcısısın. "
        "PriceFinder'dan yüksek ROI'li TR→US fırsatlarını bulursun. "
        "ROI, sales rank, monthly sold verilerini analiz edersin."
    ),
    "amz-analyst": (
        "Sen AMZ Analyst — veri analistisin. "
        "Fırsatları derinlemesine analiz eder, risk skoru hesaplar, "
        "GÜÇLÜ AL/AL/İZLE/GEÇ kararı verirsin."
    ),
    "amz-pricer": (
        "Sen AMZ Pricer — Amazon fiyat stratejistisin. "
        "BuyBoxPricer verilerine bakarak Buy Box kaybı tespit eder, "
        "repricing stratejisi (buy_box_win/profit_max/stock_bleed) önerirsin."
    ),
    "amz-crosslister": (
        "Sen AMZ CrossLister — eBay kanal yöneticisisin. "
        "Amazon FBA envanterini eBay'de cross-list eder, "
        "stranded inventory ve fiyat fırsatlarını bulursun."
    ),
    "amz-admanager": (
        "Sen AMZ AdManager — reklam yöneticisisin. "
        "Amazon PPC kampanyalarını izler, ACOS optimize eder, "
        "bütçe tier'ları (high/medium/low) belirlersin."
    ),
    "amz-ceo": (
        "Sen AMZ CEO — arbitraj operasyonu direktörüsün. "
        "Stratejik kararlar verir, ajanları koordine eder, "
        "haftalık P&L takibi yapar, ROI>%30 hedefini izlersin."
    ),
    "amz-qa": (
        "Sen AMZ QA — test ve kalite kontrol ajanısın. "
        "Servislerin health durumunu kontrol eder, PASS/FAIL verdict verirsin."
    ),
    "amz-dev": (
        "Sen AMZ Dev — platform mühendisisin. "
        "4 servisin (PriceFinder, BuyBoxPricer, AdsOptimizer, EbayCrossLister) "
        "bakımını yapar, deployment durumunu izlersin."
    ),
    "app-studio-ceo": (
        "Sen App Studio CEO — mobil uygulama portföyü direktörüsün. "
        "MRR, ROI, churn optimizasyonu yaparsın. "
        "Max 2 haftalık karar: pricing + acquisition."
    ),
    "app-studio-growth-intel": (
        "Sen Growth Intelligence — pazar araştırma ve funnel analiz ajanısın. "
        "Hub API'den metrik çeker, anomali tespit eder, haftalık rapor yazarsın."
    ),
    "app-studio-pricing": (
        "Sen Pricing — fiyatlama stratejistisin. "
        "Ülke bazlı ARPU analizi, WTP hesabı, A/B test planı yaparsın."
    ),
    "app-studio-marketing": (
        "Sen Marketing — UA ve ASO yöneticisisin. "
        "Apple Search Ads kampanyalarını izler, ülke bazlı ROI analizi yaparsın."
    ),
    "app-studio-dev": (
        "Sen App Studio Dev — uygulama geliştirme koordinatörüsün. "
        "Repo durumunu izler, build süreçlerini takip edersin."
    ),
    "app-studio-qa": (
        "Sen App Studio QA — uygulama kalite kontrol ajanısın. "
        "Hub API'den app sağlığını kontrol eder, PASS/FAIL verdict verirsin."
    ),
}

_DEFAULT_PERSONA = (
    "Sen StudioOS platformunda çalışan bir otonom ajansın. "
    "Kullanıcının sorusunu yanıtla. Türkçe, kısa ve somut ol."
)

_REACT_SUFFIX = """

Kullanabildiğin araçlar:
{tool_list}

Bir araç çağırmak istersen SADECE şu JSON formatında yanıt ver (başka bir şey ekleme):
{{"tool": "tool_name", "args": {{"key": "value"}}}}

Araç çağırmak istemiyorsan düz metin olarak yanıt ver.
Türkçe yanıt ver. Kısa, somut, rakam odaklı ol."""


def get_persona(agent_id: str) -> str:
    """Return the base persona for an agent (without tool suffix)."""
    return PERSONAS.get(agent_id, _DEFAULT_PERSONA)


def build_system_prompt(agent_id: str, tool_scope: list[str]) -> str:
    """Build the full system prompt for a ReAct conversation."""
    base = get_persona(agent_id)
    tool_list = format_tool_list(tool_scope)
    return base + _REACT_SUFFIX.format(tool_list=tool_list)


def format_tool_list(tool_names: list[str]) -> str:
    """Format tool names + descriptions for the system prompt."""
    lines: list[str] = []
    for name in tool_names:
        tool = get_tool(name)
        if tool:
            desc = (tool.description or "")[:80]
            lines.append(f"- {name}: {desc}")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines) if lines else "(araç yok)"
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_personas.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add studioos/workflows/personas.py tests/test_personas.py
git commit -m "feat(M33): persona registry + tool list formatter

Per-agent system prompts for ReAct conversations. Unknown agents
get a generic StudioOS prompt. Tool list formatted from registry.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: ReAct Conversation Workflow

**Files:**
- Create: `studioos/workflows/react_conversation.py`
- Test: `tests/test_react_conversation.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_react_conversation.py`:

```python
"""ReAct conversation workflow — routing + iteration control."""
from __future__ import annotations

from studioos.workflows.react_conversation import (
    parse_llm_response,
    MAX_ITERATIONS,
)


def test_parse_tool_call() -> None:
    text = '{"tool": "buyboxpricer.db.lost_buybox", "args": {"limit": 5}}'
    result = parse_llm_response(text)
    assert result["type"] == "tool_call"
    assert result["tool"] == "buyboxpricer.db.lost_buybox"
    assert result["args"] == {"limit": 5}


def test_parse_plain_response() -> None:
    text = "Şu an 5 listing Buy Box kaybetmiş durumda."
    result = parse_llm_response(text)
    assert result["type"] == "response"
    assert result["text"] == text


def test_parse_json_in_markdown_fence() -> None:
    text = '```json\n{"tool": "hub.api.overview", "args": {"app_id": "quit_smoking"}}\n```'
    result = parse_llm_response(text)
    assert result["type"] == "tool_call"
    assert result["tool"] == "hub.api.overview"


def test_parse_invalid_json_is_response() -> None:
    text = '{"broken json'
    result = parse_llm_response(text)
    assert result["type"] == "response"


def test_max_iterations_constant() -> None:
    assert MAX_ITERATIONS == 5


def test_workflow_compiles() -> None:
    from studioos.workflows.react_conversation import build_graph
    graph = build_graph()
    assert graph is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_react_conversation.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Create `studioos/workflows/react_conversation.py`**

```python
"""ReAct conversation workflow — think → tool → observe → respond.

Shared workflow used by all agents when triggered by slack_mention
or task delegation events. The agent's identity (persona, tool scope)
is loaded from the persona registry + DB.

LangGraph conditional edge routes between think↔execute_tool until
the LLM produces a final text response or max iterations is reached.
"""
from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.db import session_scope
from studioos.logging import get_logger
from studioos.models import Agent
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state
from studioos.workflows.personas import build_system_prompt

log = get_logger(__name__)

MAX_ITERATIONS = 5


class ReactState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    # populated during run
    tool_scope: list[str]
    system_prompt: str
    user_message: str
    thread_ts: str
    channel: str
    messages: list[dict[str, str]]
    observations: list[dict[str, Any]]
    iteration: int
    final_response: str
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


def parse_llm_response(text: str) -> dict[str, Any]:
    """Parse LLM output as either a tool call or a plain response.

    Tool call format:  {"tool": "name", "args": {...}}
    Optionally wrapped in ```json ... ``` fences.
    Anything else is treated as a plain text response.
    """
    cleaned = text.strip()
    # Strip markdown fence
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    # Try JSON parse
    if cleaned.startswith("{"):
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict) and "tool" in data:
                return {
                    "type": "tool_call",
                    "tool": data["tool"],
                    "args": data.get("args") or {},
                }
        except (json.JSONDecodeError, ValueError):
            pass
    return {"type": "response", "text": text.strip()}


async def node_load_context(state: ReactState) -> dict[str, Any]:
    """Load agent persona, tool scope, and extract the user's message."""
    agent_id = state.get("agent_id") or ""
    inp = state.get("input") or {}
    payload = inp.get("payload") or {}
    trigger = state.get("trigger_type") or ""

    # Extract user message + thread context
    if trigger == "slack_mention":
        user_message = payload.get("text") or ""
        thread_ts = payload.get("thread_ts") or ""
        channel = payload.get("channel") or ""
    else:
        user_message = payload.get("description") or payload.get("title") or ""
        thread_ts = ""
        channel = ""

    # Load tool scope from DB
    tool_scope: list[str] = []
    try:
        async with session_scope() as session:
            agent = await session.get(Agent, agent_id)
            if agent and agent.tool_scope:
                tool_scope = list(agent.tool_scope)
    except Exception:
        tool_scope = list((state.get("goals") or {}).get("tool_scope") or [])

    # Ensure llm.chat and slack.notify are available
    for required in ("llm.chat", "slack.notify", "telegram.notify"):
        if required not in tool_scope:
            tool_scope.append(required)

    system_prompt = build_system_prompt(agent_id, tool_scope)

    # Load recent memories for context
    memories_result = await invoke_from_state(
        state, "memory.search",
        {"query": user_message[:200], "limit": 5},
    )
    memory_context = ""
    if memories_result["status"] == "ok":
        items = (memories_result.get("data") or {}).get("results") or []
        if items:
            memory_lines = [f"- {m.get('content', '')[:150]}" for m in items[:3]]
            memory_context = "\nİlgili hafıza:\n" + "\n".join(memory_lines)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message + memory_context},
    ]

    return {
        "tool_scope": tool_scope,
        "system_prompt": system_prompt,
        "user_message": user_message,
        "thread_ts": thread_ts,
        "channel": channel,
        "messages": messages,
        "observations": [],
        "iteration": 0,
        "final_response": "",
    }


async def node_think(state: ReactState) -> dict[str, Any]:
    """Ask LLM what to do next — returns tool call or final response."""
    messages = list(state.get("messages") or [])
    iteration = state.get("iteration", 0)

    result = await invoke_from_state(
        state, "llm.chat",
        {
            "messages": messages,
            "max_tokens": 1500,
            "temperature": 0.3,
        },
    )

    if result["status"] != "ok":
        return {
            "final_response": f"LLM hatası: {result.get('error', '?')[:100]}",
            "iteration": iteration + 1,
        }

    content = ((result.get("data") or {}).get("content") or "").strip()
    parsed = parse_llm_response(content)

    if parsed["type"] == "tool_call":
        # Add assistant message showing the tool call
        messages.append({"role": "assistant", "content": content})
        return {
            "messages": messages,
            "observations": list(state.get("observations") or []) + [parsed],
            "iteration": iteration + 1,
        }

    # Final response
    return {
        "final_response": parsed["text"],
        "iteration": iteration + 1,
    }


def route_after_think(state: ReactState) -> str:
    """Conditional edge: tool_call → execute_tool, response → format_response."""
    if state.get("final_response"):
        return "format_response"
    if state.get("iteration", 0) >= MAX_ITERATIONS:
        return "force_respond"
    observations = state.get("observations") or []
    if observations and observations[-1].get("type") == "tool_call":
        return "execute_tool"
    return "format_response"


async def node_execute_tool(state: ReactState) -> dict[str, Any]:
    """Execute the tool the LLM requested."""
    observations = list(state.get("observations") or [])
    tool_scope = state.get("tool_scope") or []
    messages = list(state.get("messages") or [])

    if not observations:
        return {}

    last = observations[-1]
    tool_name = last.get("tool", "")
    tool_args = last.get("args") or {}

    # Tool scope enforcement
    if tool_name not in tool_scope:
        obs_text = f"Hata: '{tool_name}' aracına erişim yetkin yok."
        messages.append({"role": "user", "content": f"Araç sonucu: {obs_text}"})
        return {"messages": messages}

    result = await invoke_from_state(state, tool_name, tool_args)
    if result["status"] == "ok":
        obs_text = json.dumps(result.get("data") or {}, ensure_ascii=False, default=str)[:2000]
    else:
        obs_text = f"Hata: {result.get('error', '?')[:200]}"

    messages.append({"role": "user", "content": f"Araç sonucu ({tool_name}):\n{obs_text}"})

    return {"messages": messages}


async def node_force_respond(state: ReactState) -> dict[str, Any]:
    """Force a response after max iterations."""
    observations = state.get("observations") or []
    obs_summary = ", ".join(
        o.get("tool", "?") for o in observations if o.get("type") == "tool_call"
    )
    return {
        "final_response": (
            f"Araç çağrıları tamamlandı ({obs_summary}) ama "
            f"kesin bir yanıt oluşturulamadı. Lütfen sorunuzu "
            f"daha spesifik sorun."
        ),
    }


async def node_format_response(state: ReactState) -> dict[str, Any]:
    """Send the final response to the appropriate channel."""
    response = state.get("final_response") or "Yanıt oluşturulamadı."
    trigger = state.get("trigger_type") or ""
    agent_id = state.get("agent_id") or ""
    thread_ts = state.get("thread_ts") or ""
    channel = state.get("channel") or ""

    events: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []

    if trigger == "slack_mention" and channel:
        notify_args: dict[str, Any] = {
            "text": response[:4000],
            "mrkdwn": True,
        }
        if channel:
            notify_args["channel"] = channel
        if thread_ts:
            notify_args["thread_ts"] = thread_ts
        await invoke_from_state(state, "slack.notify", notify_args)
        events.append({
            "event_type": "slack.mention.responded",
            "event_version": 1,
            "payload": {
                "agent_id": agent_id,
                "text": response[:500],
                "channel": channel,
                "thread_ts": thread_ts,
            },
            "idempotency_key": f"react:{state.get('run_id')}:responded",
        })
    else:
        await invoke_from_state(state, "telegram.notify", {
            "text": response[:3500],
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        })

    user_msg = state.get("user_message") or ""
    memories.append({
        "content": f"Conversation: Q={user_msg[:100]} A={response[:200]}",
        "tags": [state.get("studio_id") or "?", agent_id, "conversation"],
        "importance": 0.5,
    })

    return {
        "events": events,
        "memories": memories,
        "summary": f"ReAct response to '{user_msg[:40]}' ({state.get('iteration', 0)} iterations)",
    }


def build_graph() -> Any:
    graph = StateGraph(ReactState)
    graph.add_node("load_context", node_load_context)
    graph.add_node("think", node_think)
    graph.add_node("execute_tool", node_execute_tool)
    graph.add_node("format_response", node_format_response)
    graph.add_node("force_respond", node_force_respond)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "think")
    graph.add_conditional_edges("think", route_after_think)
    graph.add_edge("execute_tool", "think")  # loop back
    graph.add_edge("force_respond", "format_response")
    graph.add_edge("format_response", END)

    return graph.compile()


compiled = build_graph()

register_workflow("react_conversation", 1, compiled)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_react_conversation.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add studioos/workflows/react_conversation.py tests/test_react_conversation.py
git commit -m "feat(M33): ReAct conversation workflow — think/tool/respond loop

LangGraph conditional edges: think → tool_call → execute → think (loop)
or think → final response → format + send. Max 5 iterations.
Tool scope enforced. Slack thread + Telegram output.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Wire Slack Mentions to ReAct + Deploy

**Files:**
- Modify: `studioos/api/slack_events.py`
- Modify: `studioos/studios/amz/studio.yaml`
- Modify: `studioos/studios/app_studio/studio.yaml`

- [ ] **Step 1: Update slack_events.py to use react_conversation**

In `studioos/api/slack_events.py`, update `_process_mention` to set the trigger to use `react_conversation` workflow instead of the agent's default pipeline:

Find the `trigger_run` call and change it to specify `react_conversation` as the workflow:

```python
async def _process_mention(event: dict[str, Any]) -> None:
    """Route an app_mention event to the correct agent run."""
    if event.get("type") != "app_mention":
        return

    text = event.get("text", "")
    channel = event.get("channel", "")
    agent_id = resolve_agent_from_mention(text, channel=channel)
    if not agent_id:
        log.debug("slack_events.no_agent", text=text[:100])
        return

    thread_ts = event.get("thread_ts") or event.get("ts", "")
    message_ts = event.get("ts", "")
    user = event.get("user", "")
    studio_id = _studio_for_agent(agent_id)
    clean_text = clean_mention_text(text)

    log.info(
        "slack_events.mention",
        agent_id=agent_id,
        user=user,
        channel=channel,
        text=clean_text[:80],
    )

    from studioos.runtime.trigger import trigger_run

    await trigger_run(
        agent_id=agent_id,
        trigger_type="slack_mention",
        trigger_ref=message_ts,
        input_data={
            "event_type": "slack.mention.received",
            "payload": {
                "agent_id": agent_id,
                "studio_id": studio_id,
                "text": clean_text,
                "user": user,
                "channel": channel,
                "thread_ts": thread_ts,
                "message_ts": message_ts,
            },
        },
        workflow_override="react_conversation",
    )
```

- [ ] **Step 2: Update trigger_run to support workflow_override**

In `studioos/runtime/trigger.py`, add `workflow_override` parameter. Check the existing function and add the parameter that tells the runner to use a different workflow than the agent's default:

Read the file first, then add `workflow_override: str | None = None` parameter. If set, store it in `input_snapshot` so the runner picks it up:

```python
async def trigger_run(
    *,
    agent_id: str,
    trigger_type: str = "api",
    trigger_ref: str = "",
    input_data: dict[str, Any] | None = None,
    priority: int = 30,
    workflow_override: str | None = None,
) -> str:
    """Create a pending run for an agent. Returns the run_id."""
    # ... existing code ...
    # Add workflow_override to input if specified
    if workflow_override and input_data:
        input_data["_workflow_override"] = workflow_override
    # ... rest of function
```

- [ ] **Step 3: Update the runner to check for workflow_override**

In `studioos/runtime/runner.py`, find where the workflow is loaded (by template_id). Add a check: if `input_snapshot.get("_workflow_override")` exists, use that workflow instead.

Read the runner file first to find the exact location, then add:

```python
# Near where workflow is resolved:
override = (run.input_snapshot or {}).get("_workflow_override")
if override:
    workflow = get_workflow(override, 1)
else:
    workflow = get_workflow(agent.template_id, agent.template_version)
```

- [ ] **Step 4: Add react_conversation template to both studio.yaml files**

In `studioos/studios/amz/studio.yaml`, add to templates section:
```yaml
  - id: react_conversation
    version: 1
    display_name: "ReAct Conversation"
    description: "Shared conversational workflow — think/tool/respond loop for Slack mentions"
    workflow_ref: react_conversation
```

In `studioos/studios/app_studio/studio.yaml`, add the same template.

- [ ] **Step 5: Run all tests**

Run: `uv run pytest tests/test_personas.py tests/test_react_conversation.py tests/test_slack_routing.py tests/test_slack_webhook.py -q`
Expected: all pass

- [ ] **Step 6: Smoke import**

Run: `uv run python -c "from studioos.workflows.react_conversation import compiled; print('ok')"`
Expected: `ok`

- [ ] **Step 7: Commit**

```bash
git add studioos/api/slack_events.py studioos/runtime/trigger.py studioos/runtime/runner.py studioos/studios/amz/studio.yaml studioos/studios/app_studio/studio.yaml
git commit -m "feat(M33): wire Slack mentions to ReAct workflow

Slack mention → react_conversation workflow instead of agent's
default pipeline. workflow_override in trigger_run + runner.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 8: Push and deploy**

```bash
gh auth switch --user mifasuse
git push origin main
```

- [ ] **Step 9: Test on prod**

In `#amz-hq`, type: `@StudioOS pricer Buy Box kaybeden listingleri göster`

Expected: amz-pricer agent runs `react_conversation`, calls `buyboxpricer.db.lost_buybox`, LLM formats the response, replies in the same Slack thread.
