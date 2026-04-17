"""LLM tool — multi-provider chat completion (MiniMax / Anthropic / OpenAI).

The tool name is `llm.chat`; callers don't change. A new `provider`
argument routes the request:

  - "minimax"   → MiniMax M2.7 OpenAI-compatible /chat/completions (default)
  - "anthropic" → Anthropic Messages API (Claude Haiku 4.5 by default)
  - "openai"    → OpenAI /chat/completions (gpt-4.1-mini by default)

Provider selection precedence: explicit args.provider →
state.goals.llm_provider (passed via input) → settings.llm_default_provider.

The response shape is normalized across providers so workflows don't
care which one ran:

    {
      "content": "...",
      "parsed_json": {...} | None,
      "model": "...",
      "provider": "...",
      "finish_reason": "...",
      "usage": {prompt_tokens, completion_tokens, total_tokens}
    }
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from studioos.config import settings
from studioos.logging import get_logger

from .base import ToolContext, ToolError, ToolResult
from .registry import register_tool

log = get_logger(__name__)

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    """MiniMax (and some other models) emit visible <think>...</think> chain
    of thought before the actual answer. Strip it so downstream parsers see
    clean output."""
    if not text:
        return text
    return _THINK_TAG_RE.sub("", text).strip()


def _cents(prompt_tokens: int, completion_tokens: int, rate_in: float, rate_out: float) -> int:
    raw = (prompt_tokens * rate_in + completion_tokens * rate_out) / 1000.0
    if raw <= 0 and (prompt_tokens + completion_tokens) > 0:
        return 1
    return max(0, int(raw + 0.999))


def _cost_for(provider: str, prompt_tokens: int, completion_tokens: int) -> int:
    if provider == "anthropic":
        return _cents(
            prompt_tokens,
            completion_tokens,
            settings.anthropic_cost_input_per_1k_cents,
            settings.anthropic_cost_output_per_1k_cents,
        )
    if provider == "openai":
        return _cents(
            prompt_tokens,
            completion_tokens,
            settings.openai_cost_input_per_1k_cents,
            settings.openai_cost_output_per_1k_cents,
        )
    return _cents(
        prompt_tokens,
        completion_tokens,
        settings.minimax_cost_input_per_1k_cents,
        settings.minimax_cost_output_per_1k_cents,
    )


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


async def _call_minimax(args: dict[str, Any]) -> dict[str, Any]:
    base_url = (settings.minimax_base_url or "").rstrip("/")
    api_key = settings.minimax_api_key
    if not base_url or not api_key:
        raise ToolError(
            "STUDIOOS_MINIMAX_BASE_URL and STUDIOOS_MINIMAX_API_KEY must be set"
        )
    model = args.get("model") or settings.minimax_model
    body: dict[str, Any] = {"model": model, "messages": args["messages"]}
    if "max_tokens" in args:
        body["max_tokens"] = int(args["max_tokens"])
    if "temperature" in args:
        body["temperature"] = float(args["temperature"])
    if args.get("response_format") == "json_object":
        body["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"minimax http: {exc}") from exc
    if resp.status_code >= 400:
        raise ToolError(f"minimax {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    choices = payload.get("choices") or []
    if not choices:
        raise ToolError(f"minimax empty: {str(payload)[:200]}")
    msg = (choices[0] or {}).get("message") or {}
    raw = msg.get("content", "") or ""
    content = _strip_reasoning(raw)
    finish = choices[0].get("finish_reason")
    usage = payload.get("usage") or {}
    return {
        "content": content,
        "model": payload.get("model") or model,
        "finish_reason": finish,
        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
        "completion_tokens": int(usage.get("completion_tokens", 0)),
    }


async def _call_anthropic(args: dict[str, Any]) -> dict[str, Any]:
    api_key = settings.anthropic_api_key
    base_url = (settings.anthropic_base_url or "").rstrip("/")
    if not api_key:
        raise ToolError("STUDIOOS_ANTHROPIC_API_KEY must be set")
    model = args.get("model") or settings.anthropic_model

    # Anthropic separates system from messages.
    system_chunks: list[str] = []
    messages: list[dict[str, str]] = []
    for m in args["messages"]:
        if m["role"] == "system":
            system_chunks.append(m["content"])
        else:
            messages.append({"role": m["role"], "content": m["content"]})

    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": int(args.get("max_tokens", 1024)),
    }
    if system_chunks:
        body["system"] = "\n\n".join(system_chunks)
    if "temperature" in args:
        body["temperature"] = float(args["temperature"])

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(
                f"{base_url}/messages",
                json=body,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"anthropic http: {exc}") from exc
    if resp.status_code >= 400:
        raise ToolError(f"anthropic {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    blocks = payload.get("content") or []
    text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    content = "".join(text_parts).strip()
    usage = payload.get("usage") or {}
    return {
        "content": content,
        "model": payload.get("model") or model,
        "finish_reason": payload.get("stop_reason"),
        "prompt_tokens": int(usage.get("input_tokens", 0)),
        "completion_tokens": int(usage.get("output_tokens", 0)),
    }


async def _call_openai(args: dict[str, Any]) -> dict[str, Any]:
    api_key = settings.openai_api_key
    base_url = (settings.openai_base_url or "").rstrip("/")
    if not api_key:
        raise ToolError("STUDIOOS_OPENAI_API_KEY must be set")
    model = args.get("model") or settings.openai_model
    body: dict[str, Any] = {"model": model, "messages": args["messages"]}
    if "max_tokens" in args:
        body["max_tokens"] = int(args["max_tokens"])
    if "temperature" in args:
        body["temperature"] = float(args["temperature"])
    if args.get("response_format") == "json_object":
        body["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"openai http: {exc}") from exc
    if resp.status_code >= 400:
        raise ToolError(f"openai {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    choices = payload.get("choices") or []
    if not choices:
        raise ToolError(f"openai empty: {str(payload)[:200]}")
    msg = (choices[0] or {}).get("message") or {}
    content = (msg.get("content") or "").strip()
    finish = choices[0].get("finish_reason")
    usage = payload.get("usage") or {}
    return {
        "content": content,
        "model": payload.get("model") or model,
        "finish_reason": finish,
        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
        "completion_tokens": int(usage.get("completion_tokens", 0)),
    }


_PROVIDERS = {
    "minimax": _call_minimax,
    "anthropic": _call_anthropic,
    "openai": _call_openai,
}


@register_tool(
    "llm.chat",
    description=(
        "Send a chat-completions request to the configured LLM "
        "(MiniMax / Anthropic / OpenAI) and return the assistant message. "
        "Token-based cost charged to the agent's budget."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["role", "content"],
                    "additionalProperties": False,
                },
            },
            "model": {"type": "string"},
            "provider": {"type": "string"},
            "max_tokens": {"type": "integer"},
            "temperature": {"type": "number"},
            "response_format": {"type": "string"},
        },
        "required": ["messages"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="llm",
    cost_cents=0,
    cost_fn=lambda args, data: _cost_for(
        data.get("provider", "minimax"),
        int((data.get("usage") or {}).get("prompt_tokens", 0)),
        int((data.get("usage") or {}).get("completion_tokens", 0)),
    ),
)
async def llm_chat(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    provider = (
        args.get("provider")
        or (ctx.extra.get("llm_provider") if ctx.extra else None)
        or settings.llm_default_provider
        or "minimax"
    ).lower()
    if provider not in _PROVIDERS:
        raise ToolError(f"unknown LLM provider: {provider}")

    raw = await _PROVIDERS[provider](args)
    content = raw["content"]

    parsed_json: Any = None
    if args.get("response_format") == "json_object" and content:
        try:
            parsed_json = json.loads(content)
        except ValueError:
            parsed_json = None

    return ToolResult(
        data={
            "content": content,
            "parsed_json": parsed_json,
            "model": raw["model"],
            "provider": provider,
            "finish_reason": raw.get("finish_reason"),
            "usage": {
                "prompt_tokens": raw["prompt_tokens"],
                "completion_tokens": raw["completion_tokens"],
                "total_tokens": raw["prompt_tokens"] + raw["completion_tokens"],
            },
        }
    )
