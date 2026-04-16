"""ReAct conversation workflow — M33 Task 2.

Flow:
  START → load_context → think → [route_after_think]
                                    ├─ "execute_tool"   → execute_tool → think (loop)
                                    ├─ "format_response" → format_response → END
                                    └─ "force_respond"   → force_respond → format_response → END
"""
from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state
from studioos.tools.workflow_helper import context_from_state
from studioos.tools.invoker import invoke_tool
from studioos.workflows.personas import build_system_prompt

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_ITERATIONS = 5

# Infrastructure tools the ReAct workflow needs regardless of agent's
# tool_scope. These bypass the per-agent allow-list enforcement.
_INFRA_TOOLS = {"llm.chat", "memory.search", "slack.notify", "telegram.notify"}


async def _invoke_unguarded(state: dict, name: str, args: dict) -> dict:
    """Call a tool bypassing the agent's tool_scope enforcement.

    Used for infrastructure calls (LLM, memory, notifications) that
    the ReAct workflow itself needs, not the agent's domain tools.
    """
    ctx = context_from_state(state)
    return await invoke_tool(name, args, ctx, enforce_allow_list=False)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class ReactState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    tool_scope: list[str]
    system_prompt: str
    user_message: str
    thread_ts: str | None
    channel: str | None
    messages: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    iteration: int
    final_response: str | None
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def parse_llm_response(text: str) -> dict[str, Any]:
    """Parse LLM output as tool call or plain text response.

    Tool call: {"tool": "name", "args": {...}} (optionally in ```json fence)
    Anything else → plain text response.
    """
    candidate = text.strip()

    # Unwrap markdown json fence if present
    fence_match = _JSON_FENCE_RE.search(candidate)
    if fence_match:
        candidate = fence_match.group(1).strip()

    # Try JSON parse
    try:
        obj = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return {"type": "response", "text": text}

    # Must be an object with a "tool" key to be a tool call
    if isinstance(obj, dict) and "tool" in obj:
        return {
            "type": "tool_call",
            "tool": obj["tool"],
            "args": obj.get("args", {}),
        }

    # Valid JSON but not a tool call → plain response
    return {"type": "response", "text": text}


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def node_load_context(state: ReactState) -> dict[str, Any]:
    """Extract user message, load tool_scope from DB, build system prompt, load memories."""
    inp = state.get("input") or {}
    payload = inp.get("payload") or inp  # nested payload or flat

    # Determine user message — slack_mention has "text", plain events may differ
    trigger_type = state.get("trigger_type", "")
    if trigger_type == "slack_mention":
        user_message = payload.get("text", "")
        thread_ts = payload.get("thread_ts", "")
        channel = payload.get("channel", "")
    else:
        user_message = payload.get("text") or payload.get("description") or payload.get("title") or ""
        thread_ts = payload.get("thread_ts", "")
        channel = payload.get("channel", "")

    # Load agent from DB to get tool_scope
    agent_id = state.get("agent_id", "")
    tool_scope: list[str] = []
    try:
        from studioos.db import session_scope
        from studioos.models import Agent
        from sqlalchemy import select

        async with session_scope() as session:
            result = await session.execute(
                select(Agent).where(Agent.id == agent_id)
            )
            agent = result.scalar_one_or_none()
            if agent and agent.tool_scope:
                tool_scope = list(agent.tool_scope)
    except Exception as exc:
        log.warning("react_conversation.load_context.db_error", error=str(exc))
        # Fall back to empty scope — still functional for unauthenticated tests
        tool_scope = []

    # Build system prompt
    system_prompt = build_system_prompt(agent_id, tool_scope)

    # Load recent memories
    memories: list[dict[str, Any]] = []
    try:
        mem_result = await _invoke_unguarded(
            state, "memory.search", {"query": user_message, "limit": 5}
        )
        if mem_result.get("status") == "ok":
            memories = (mem_result.get("data") or {}).get("items", [])
    except Exception as exc:
        log.warning("react_conversation.load_context.memory_error", error=str(exc))

    # Build initial messages list
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
    ]
    if memories:
        mem_text = "\n".join(
            f"- {m.get('content', '')}" for m in memories if m.get("content")
        )
        messages.append(
            {
                "role": "system",
                "content": f"Geçmiş hafıza:\n{mem_text}",
            }
        )
    messages.append({"role": "user", "content": user_message})

    return {
        "tool_scope": tool_scope,
        "system_prompt": system_prompt,
        "user_message": user_message,
        "thread_ts": thread_ts,
        "channel": channel,
        "messages": messages,
        "observations": [],
        "iteration": 0,
        "final_response": None,
        "events": [],
        "memories": memories,
        "kpi_updates": [],
    }


async def node_think(state: ReactState) -> dict[str, Any]:
    """Call LLM with current messages, parse response."""
    messages = list(state.get("messages") or [])
    observations = list(state.get("observations") or [])
    iteration = state.get("iteration") or 0

    # Call LLM
    try:
        llm_result = await _invoke_unguarded(
            state, "llm.chat", {"messages": messages}
        )
        if llm_result.get("status") != "ok":
            # LLM call failed — treat as plain text response
            raw_text = llm_result.get("error", "Yanıt alınamadı.")
        else:
            raw_text = (llm_result.get("data") or {}).get("content", "")
    except Exception as exc:
        log.warning("react_conversation.think.llm_error", error=str(exc))
        raw_text = "Bir hata oluştu, lütfen tekrar deneyin."

    parsed = parse_llm_response(raw_text)

    if parsed["type"] == "tool_call":
        # Append assistant message + increment iteration
        messages.append({"role": "assistant", "content": raw_text})
        observations.append(parsed)
        return {
            "messages": messages,
            "observations": observations,
            "iteration": iteration + 1,
            "final_response": None,
        }
    else:
        # Plain response — set final_response
        messages.append({"role": "assistant", "content": raw_text})
        return {
            "messages": messages,
            "observations": observations,
            "final_response": parsed["text"],
        }


def route_after_think(state: ReactState) -> str:
    """Conditional routing after think node."""
    final_response = state.get("final_response")
    iteration = state.get("iteration") or 0
    observations = state.get("observations") or []

    if final_response is not None:
        return "format_response"

    if iteration >= MAX_ITERATIONS:
        return "force_respond"

    # Last observation is a tool_call
    if observations and observations[-1].get("type") == "tool_call":
        return "execute_tool"

    # Fallback — shouldn't happen, but go to format_response
    return "format_response"


async def node_execute_tool(state: ReactState) -> dict[str, Any]:
    """Check tool scope, invoke the tool, append result to messages."""
    observations = list(state.get("observations") or [])
    messages = list(state.get("messages") or [])
    tool_scope = state.get("tool_scope") or []

    if not observations:
        return {}

    last_obs = observations[-1]
    tool_name = last_obs.get("tool", "")
    args = last_obs.get("args", {})

    # Enforce tool scope — reject if not allowed
    if tool_scope and tool_name not in tool_scope:
        result_text = f"Araç sonucu ({tool_name}): Hata — bu araç bu ajan için izinli değil."
        log.warning(
            "react_conversation.tool_scope_rejected",
            tool=tool_name,
            scope=tool_scope,
        )
    else:
        try:
            result = await invoke_from_state(state, tool_name, args)
            if result.get("status") == "ok":
                data = result.get("data") or {}
                result_text = f"Araç sonucu ({tool_name}): {json.dumps(data, ensure_ascii=False)}"
            else:
                error = result.get("error", "bilinmeyen hata")
                result_text = f"Araç sonucu ({tool_name}): Hata — {error}"
        except Exception as exc:
            log.warning(
                "react_conversation.execute_tool.error",
                tool=tool_name,
                error=str(exc),
            )
            result_text = f"Araç sonucu ({tool_name}): Hata — {exc}"

    messages.append({"role": "user", "content": result_text})

    return {"messages": messages}


def node_force_respond(state: ReactState) -> dict[str, Any]:
    """Set final_response when max iterations reached."""
    return {
        "final_response": (
            f"Maksimum adım sayısına ({MAX_ITERATIONS}) ulaşıldı. "
            "Şu ana kadar toplanan bilgilere göre özet yanıtım: "
            "Yeterli bilgiye ulaşamadım, lütfen sorunuzu daha spesifik hale getirin."
        )
    }


async def node_format_response(state: ReactState) -> dict[str, Any]:
    """Send reply via Slack or Telegram, emit event, save memory, set summary."""
    final_response = state.get("final_response") or ""
    trigger_type = state.get("trigger_type", "")
    thread_ts = state.get("thread_ts")
    channel = state.get("channel")
    agent_id = state.get("agent_id", "")
    events = list(state.get("events") or [])
    memories = list(state.get("memories") or [])

    # Send notification
    if trigger_type == "slack_mention":
        try:
            notify_args: dict[str, Any] = {"text": final_response}
            if channel:
                notify_args["channel"] = channel
            if thread_ts:
                notify_args["thread_ts"] = thread_ts
            await _invoke_unguarded(state, "slack.notify", notify_args)
        except Exception as exc:
            log.warning("react_conversation.format_response.slack_error", error=str(exc))
    else:
        try:
            await _invoke_unguarded(state, "telegram.notify", {"text": final_response})
        except Exception as exc:
            log.warning(
                "react_conversation.format_response.telegram_error", error=str(exc)
            )

    # Emit event
    events.append(
        {
            "event_type": "slack.mention.responded",
            "event_version": 1,
            "payload": {
                "agent_id": agent_id,
                "response": final_response[:200],
                "trigger_type": trigger_type,
            },
        }
    )

    # Save memory
    user_message = state.get("user_message", "")
    if user_message and final_response:
        memories.append(
            {
                "content": f"Kullanıcı: {user_message}\nAjan: {final_response}",
                "importance": 0.5,
            }
        )

    summary = f"ReAct tamamlandı. Yanıt: {final_response[:100]}"

    return {
        "events": events,
        "memories": memories,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_graph() -> Any:
    graph = StateGraph(ReactState)

    graph.add_node("load_context", node_load_context)
    graph.add_node("think", node_think)
    graph.add_node("execute_tool", node_execute_tool)
    graph.add_node("force_respond", node_force_respond)
    graph.add_node("format_response", node_format_response)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "think")
    graph.add_conditional_edges(
        "think",
        route_after_think,
        {
            "execute_tool": "execute_tool",
            "format_response": "format_response",
            "force_respond": "force_respond",
        },
    )
    graph.add_edge("execute_tool", "think")
    graph.add_edge("force_respond", "format_response")
    graph.add_edge("format_response", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Module-level registration
# ---------------------------------------------------------------------------

compiled = build_graph()

register_workflow("react_conversation", 1, compiled)
