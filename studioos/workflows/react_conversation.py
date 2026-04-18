"""ReAct conversation workflow — M33 Task 2.

Flow:
  START → load_context → think → [route_after_think]
                                    ├─ "execute_tool"   → execute_tool → think (loop)
                                    ├─ "format_response" → format_response → END
                                    └─ "force_respond"   → force_respond → format_response → END
"""
from __future__ import annotations

import asyncio
import json
import re

import httpx
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

MAX_ITERATIONS = 25
MAX_PARSE_RETRIES = 2

# Markers that indicate LLM tried to emit a tool call but parser couldn't
# extract valid JSON. When seen, we retry with a corrective prompt rather
# than sending broken content to Slack.
_TOOL_INTENT_MARKERS = ("[TOOL_CALL]", '{"tool"', '"tool":')


def _looks_like_broken_tool_call(text: str) -> bool:
    """Detect unparseable tool call attempts in LLM output."""
    if not text:
        return False
    return any(m in text for m in _TOOL_INTENT_MARKERS)

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


def _repair_json(raw: str) -> str:
    """Append missing closing braces/brackets + fix unquoted keys."""
    opens = raw.count("{") - raw.count("}")
    open_brackets = raw.count("[") - raw.count("]")
    fixed = raw.rstrip()
    # Strip trailing extra closing braces (LLM sometimes adds stray })
    while opens < 0 and fixed.endswith("}"):
        fixed = fixed[:-1].rstrip()
        opens += 1
    # Close arrays first, then objects (inner-to-outer)
    fixed += "]" * max(0, open_brackets)
    fixed += "}" * max(0, opens)
    # Quote unquoted keys: {tool: "x"} → {"tool": "x"}
    fixed = re.sub(
        r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)(\s*:)',
        r'\1"\2"\3',
        fixed,
    )
    return fixed


def _try_parse_all_toolcalls(text: str) -> dict[str, Any] | None:
    """Find the first valid tool call in any [TOOL_CALL] block."""
    # Match all [TOOL_CALL]...[/TOOL_CALL] blocks
    for match in re.finditer(
        r'\[TOOL_CALL\](.*?)(?:\[/TOOL_CALL\]|$)', text, re.DOTALL
    ):
        raw = match.group(1).strip()
        for attempt in (raw, _repair_json(raw)):
            try:
                obj = json.loads(attempt)
                if isinstance(obj, dict) and "tool" in obj:
                    return {
                        "type": "tool_call",
                        "tool": obj["tool"],
                        "args": obj.get("args", {}),
                    }
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def parse_llm_response(text: str) -> dict[str, Any]:
    """Parse LLM output as tool call or plain text response.

    Tool call formats handled:
      1. Pure JSON: {"tool": "name", "args": {...}}
      2. Markdown fence: ```json\n{"tool": ...}\n```
      3. Mixed text + JSON: "some text\n{"tool": ...}"
    Anything else → plain text response.
    """
    candidate = text.strip()

    # Unwrap markdown json fence if present
    fence_match = _JSON_FENCE_RE.search(candidate)
    if fence_match:
        candidate = fence_match.group(1).strip()

    # Try pure JSON parse first
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict) and "tool" in obj:
            return {
                "type": "tool_call",
                "tool": obj["tool"],
                "args": obj.get("args", {}),
            }
    except (json.JSONDecodeError, ValueError):
        pass

    # Try fixing truncated JSON — LLM sometimes omits closing braces
    if '{"tool"' in candidate:
        tool_part = candidate[candidate.find('{"tool"'):]
        open_braces = tool_part.count("{") - tool_part.count("}")
        if open_braces > 0:
            candidate = candidate + "}" * open_braces

    # Handle [TOOL_CALL]...[/TOOL_CALL] format (MiniMax style).
    # Try each block; return first valid tool call.
    toolcall_result = _try_parse_all_toolcalls(candidate)
    if toolcall_result:
        return toolcall_result

    # Try finding JSON object embedded in text (LLM sometimes adds
    # prose before the JSON: "Kontrol ediyorum...\n{"tool": ...}")
    tool_idx = candidate.find('{"tool"')
    if tool_idx >= 0:
        tail = candidate[tool_idx:]
        # Try full tail first, then progressively shorter (find closing brace)
        for end_offset in range(len(tail), 0, -1):
            chunk = tail[:end_offset]
            if not chunk.endswith("}"):
                continue
            try:
                obj = json.loads(chunk)
                if isinstance(obj, dict) and "tool" in obj:
                    return {
                        "type": "tool_call",
                        "tool": obj["tool"],
                        "args": obj.get("args", {}),
                    }
            except (json.JSONDecodeError, ValueError):
                continue

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
    elif trigger_type == "telegram_message":
        user_message = payload.get("text", "")
        thread_ts = ""
        channel = payload.get("chat_id", "")  # reuse channel field for chat_id
    else:
        user_message = payload.get("text") or payload.get("description") or payload.get("title") or ""
        thread_ts = payload.get("thread_ts", "")
        channel = payload.get("channel", "")

    # tool_scope is injected by the runner from agent.tool_scope (no DB query needed)
    agent_id = state.get("agent_id", "")
    tool_scope = list(state.get("tool_scope") or [])

    # Build system prompt with goals context
    system_prompt = build_system_prompt(agent_id, tool_scope)
    goals = state.get("goals") or {}
    if goals:
        import json as _json
        goals_str = _json.dumps(goals, ensure_ascii=False, default=str)[:1000]
        system_prompt += f"\n\n## Mevcut Görev Ayarları\n{goals_str}"

    # Load thread history for multi-turn context (Slack only)
    thread_history: list[dict[str, str]] = []
    if trigger_type == "slack_mention" and thread_ts and channel:
        try:
            from studioos.config import settings as _cfg
            token = _cfg.slack_bot_token
            if token:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(
                        "https://slack.com/api/conversations.replies",
                        headers={"Authorization": f"Bearer {token}"},
                        params={"channel": channel, "ts": thread_ts, "limit": 10},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        bot_id = None
                        for msg in (data.get("messages") or [])[:-1]:  # exclude current
                            msg_text = msg.get("text", "")
                            if msg.get("bot_id"):
                                thread_history.append({"role": "assistant", "content": msg_text[:500]})
                                bot_id = msg.get("bot_id")
                            else:
                                # Strip mention from user messages
                                clean = re.sub(r"<@[UW][A-Z0-9_]+>\s*", "", msg_text).strip()
                                if clean:
                                    thread_history.append({"role": "user", "content": clean[:500]})
        except Exception as exc:
            log.warning("react_conversation.thread_history_error", error=str(exc)[:100])

    # Load recent memories — timeout protects against DB connection pool exhaustion
    memories: list[dict[str, Any]] = []
    try:
        mem_result = await asyncio.wait_for(
            _invoke_unguarded(state, "memory.search", {"query": user_message or agent_id, "limit": 5}),
            timeout=10.0,
        )
        if mem_result.get("status") == "ok":
            memories = (mem_result.get("data") or {}).get("results") or (mem_result.get("data") or {}).get("items") or []
    except (asyncio.TimeoutError, Exception) as exc:
        log.warning("react_conversation.load_context.memory_skip", error=str(exc)[:100])

    # Strategy performance stats from agent state (learning feedback loop)
    agent_state = state.get("state") or {}
    strategy_stats = agent_state.get("strategy_stats") or {}
    auto_adjustments = agent_state.get("auto_adjustments") or {}
    stats_context = ""
    if strategy_stats or auto_adjustments:
        lines = []
        if strategy_stats:
            lines.append("Geçmiş performansın:")
            for strategy, data in strategy_stats.items():
                rate = data.get("rate", 0)
                total = data.get("total", 0)
                success = data.get("success", 0)
                lines.append(f"- {strategy}: %{int(rate*100)} başarı ({success}/{total})")
        if auto_adjustments:
            lines.append("\nOtomatik ayarlamalar (öğrenme sistemi tarafından önerildi):")
            for key, val in auto_adjustments.items():
                lines.append(f"- {key}: {val}")
        lines.append("\nBu verilere dayanarak karar ver.")
        stats_context = "\n\n" + "\n".join(lines)

    # Build initial messages list
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt + stats_context},
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
    # Thread history for multi-turn context (previous messages in thread)
    if thread_history:
        messages.extend(thread_history)
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
    """Call LLM with current messages, parse response.

    Uses low temperature (0.1) for deterministic tool call formatting.
    On parse failure, retries up to MAX_PARSE_RETRIES with a corrective
    system message before giving up.
    """
    messages = list(state.get("messages") or [])
    observations = list(state.get("observations") or [])
    iteration = state.get("iteration") or 0

    # Call LLM with low temperature for stable tool call formatting
    try:
        llm_result = await _invoke_unguarded(
            state,
            "llm.chat",
            {"messages": messages, "temperature": 0.1},
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

    # If LLM tried to call a tool but we couldn't parse it, retry with a
    # corrective system message (up to MAX_PARSE_RETRIES) before giving up.
    if parsed["type"] == "response" and _looks_like_broken_tool_call(raw_text):
        for retry in range(MAX_PARSE_RETRIES):
            log.warning(
                "react_conversation.think.parse_retry",
                attempt=retry + 1,
                raw_sample=raw_text[:150],
            )
            correction_msg = {
                "role": "user",
                "content": (
                    "Tool call JSON bozuk geldi. Lütfen SADECE şu formatı "
                    'kullan (başka metin, CLI-flag, dict list ekleme):\n'
                    '{"tool": "tool_name", "args": {"param": "value"}}\n'
                    "args alanındaki tüm değerler ÇIFT TIRNAKLI string "
                    "olmalı. Cevabın sadece tek satır JSON olsun."
                ),
            }
            retry_messages = messages + [
                {"role": "assistant", "content": raw_text},
                correction_msg,
            ]
            try:
                retry_result = await _invoke_unguarded(
                    state,
                    "llm.chat",
                    {"messages": retry_messages, "temperature": 0.0},
                )
                if retry_result.get("status") == "ok":
                    raw_text = (retry_result.get("data") or {}).get("content", "")
                    parsed = parse_llm_response(raw_text)
                    if parsed["type"] == "tool_call":
                        break
            except Exception as exc:
                log.warning("react_conversation.think.retry_error", error=str(exc))
                continue

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
    elif trigger_type == "telegram_message":
        try:
            tg_args: dict[str, Any] = {"text": final_response, "parse_mode": ""}
            if channel:  # channel holds chat_id for telegram
                tg_args["chat_id"] = channel
            await _invoke_unguarded(state, "telegram.notify", tg_args)
        except Exception as exc:
            # Retry without markdown if parse fails
            try:
                tg_args_plain: dict[str, Any] = {"text": final_response, "parse_mode": ""}
                if channel:
                    tg_args_plain["chat_id"] = channel
                await _invoke_unguarded(state, "telegram.notify", tg_args_plain)
            except Exception:
                log.warning("react_conversation.format_response.telegram_error", error=str(exc))
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

    # Agent-to-agent chaining: detect "@agent_name task" in response
    chained: list[str] = []
    if trigger_type == "slack_mention" and channel and thread_ts:
        from studioos.slack_routing import (
            _AGENT_SHORT_NAMES,
            _KNOWN_AGENTS,
            check_cascade,
        )
        # Look for @short_name patterns in response
        # Determine source studio prefix for cross-studio protection
        source_prefix = "amz-" if agent_id.startswith("amz-") else "app-studio-" if agent_id.startswith("app-studio-") else ""
        for match in re.finditer(r"@(\w[\w-]*)", final_response):
            target_short = match.group(1).lower()
            # Prefer same-studio match: "amz-ceo" + "@qa" → "amz-qa"
            # Short-name map has last-write-wins collision so try prefixed first.
            target_agent: str | None = None
            if source_prefix:
                candidate = f"{source_prefix}{target_short}"
                if candidate in _KNOWN_AGENTS:
                    target_agent = candidate
            if target_agent is None:
                target_agent = _AGENT_SHORT_NAMES.get(target_short)
            # Cross-studio protection: only chain to agents in same studio
            if target_agent and target_agent != agent_id and (
                not source_prefix or target_agent.startswith(source_prefix)
            ):
                if check_cascade(thread_ts, target_agent, responding_agent_id=agent_id):
                    # Pass the FULL response as context + the specific task after mention
                    after = final_response[match.end():].strip()
                    task_line = after.split("\n")[0][:200] if after else ""
                    # Include full response so the target agent has all data
                    full_context = (
                        f"{agent_id} diyor ki:\n"
                        f"{final_response[:1500]}\n\n"
                        f"Görev: {task_line or 'yukarıdaki verileri analiz et'}"
                    )
                    try:
                        from studioos.runtime.trigger import trigger_run
                        await trigger_run(
                            agent_id=target_agent,
                            trigger_type="slack_mention",
                            trigger_ref=thread_ts,
                            input_data={
                                "event_type": "slack.mention.received",
                                "payload": {
                                    "agent_id": target_agent,
                                    "studio_id": state.get("studio_id", ""),
                                    "text": full_context,
                                    "user": agent_id,
                                    "channel": channel,
                                    "thread_ts": thread_ts,
                                },
                            },
                            workflow_override="react_conversation",
                        )
                        chained.append(target_agent)
                        log.info(
                            "react_conversation.chained",
                            source=agent_id,
                            target=target_agent,
                            task=task_line[:60],
                        )
                    except Exception as exc:
                        log.warning("react_conversation.chain_error", error=str(exc)[:100])

    summary = f"ReAct tamamlandı. Yanıt: {final_response[:100]}"
    if chained:
        summary += f" → chained: {','.join(chained)}"

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
