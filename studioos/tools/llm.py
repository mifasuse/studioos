"""LLM tool — calls MiniMax via its OpenAI-compatible chat/completions API.

Single provider for now: MiniMax-M2.7-highspeed configured from
`settings.minimax_*`. A future M12 milestone may introduce a multi-provider
router behind the same tool name; workflows don't need to change.

Cost model: MiniMax bills per-token. We compute a rough integer-cents
cost from the usage counts returned on the response. The default rates
below are deliberate ceilings — tune `minimax_cost_*` in settings when
the real contract is confirmed.
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    """MiniMax emits visible <think>...</think> chain-of-thought before the
    actual answer. Strip it so downstream parsers see clean output.
    """
    if not text:
        return text
    return _THINK_TAG_RE.sub("", text).strip()

from studioos.config import settings
from studioos.logging import get_logger

from .base import ToolContext, ToolError, ToolResult
from .registry import register_tool

log = get_logger(__name__)


def _minimax_cost_cents(
    input_tokens: int, output_tokens: int
) -> int:
    """Compute integer-cent cost from usage. Ceiling-rounded.

    Defaults assume a conservative ~1¢ per 1k input, ~4¢ per 1k output.
    Override via STUDIOOS_MINIMAX_COST_INPUT_PER_1K_CENTS and
    STUDIOOS_MINIMAX_COST_OUTPUT_PER_1K_CENTS if you know the real rates.
    """
    rate_in = settings.minimax_cost_input_per_1k_cents
    rate_out = settings.minimax_cost_output_per_1k_cents
    raw = (input_tokens * rate_in + output_tokens * rate_out) / 1000.0
    # Ceiling to 1¢ minimum for any non-empty call.
    if raw <= 0 and (input_tokens + output_tokens) > 0:
        return 1
    return max(0, int(raw + 0.999))


@register_tool(
    "llm.chat",
    description=(
        "Send a chat-completions request to the configured LLM "
        "(MiniMax by default) and return the assistant message. "
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
            "max_tokens": {"type": "integer"},
            "temperature": {"type": "number"},
            "response_format": {"type": "string"},
        },
        "required": ["messages"],
        "additionalProperties": False,
    },
    requires_network=True,
    category="llm",
    cost_cents=0,  # real cost computed dynamically below
    cost_fn=lambda args, data: _minimax_cost_cents(
        int((data.get("usage") or {}).get("prompt_tokens", 0)),
        int((data.get("usage") or {}).get("completion_tokens", 0)),
    ),
)
async def llm_chat(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    base_url = (settings.minimax_base_url or "").rstrip("/")
    api_key = settings.minimax_api_key
    if not base_url or not api_key:
        raise ToolError(
            "STUDIOOS_MINIMAX_BASE_URL and STUDIOOS_MINIMAX_API_KEY must be set"
        )

    model = args.get("model") or settings.minimax_model
    body: dict[str, Any] = {
        "model": model,
        "messages": args["messages"],
    }
    if "max_tokens" in args:
        body["max_tokens"] = int(args["max_tokens"])
    if "temperature" in args:
        body["temperature"] = float(args["temperature"])
    if args.get("response_format") == "json_object":
        body["response_format"] = {"type": "json_object"}

    url = f"{base_url}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        raise ToolError(f"minimax http error: {exc}") from exc

    if resp.status_code >= 400:
        raise ToolError(f"minimax {resp.status_code}: {resp.text[:300]}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise ToolError(f"minimax non-json: {exc}") from exc

    choices = payload.get("choices") or []
    if not choices:
        raise ToolError(f"minimax returned no choices: {str(payload)[:200]}")
    message = (choices[0] or {}).get("message") or {}
    raw_content = message.get("content", "") or ""
    content = _strip_reasoning(raw_content)
    finish_reason = choices[0].get("finish_reason")
    usage = payload.get("usage") or {}

    # Optional: try to parse a JSON object out of the content when the
    # caller asked for one — convenient for workflows expecting structured
    # output. We never raise on parse failure, just leave `parsed_json`
    # absent so the workflow can fall back.
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
            "model": payload.get("model") or model,
            "finish_reason": finish_reason,
            "usage": {
                "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                "completion_tokens": int(usage.get("completion_tokens", 0)),
                "total_tokens": int(usage.get("total_tokens", 0)),
            },
        }
    )
