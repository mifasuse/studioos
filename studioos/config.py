"""StudioOS configuration — env + defaults via Pydantic Settings."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Loaded from environment variables prefixed with STUDIOOS_."""

    model_config = SettingsConfigDict(
        env_prefix="STUDIOOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Core
    env: str = "dev"
    log_level: str = "INFO"

    # Database
    database_url: str = (
        "postgresql+asyncpg://studioos:studioos@localhost:5433/studioos"
    )
    # Separate database used by the pytest suite so `drop_all` + `create_all`
    # never touches production state. conftest.py swaps settings.database_url
    # to this value before any studioos module reads it.
    test_database_url: str = (
        "postgresql+asyncpg://studioos:studioos@localhost:5433/studioos_test"
    )

    # LLM — default provider
    llm_default_provider: str = "minimax"  # minimax | anthropic | openai

    # LLM — MiniMax
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimax.io/v1"
    minimax_model: str = "MiniMax-M2.7-highspeed"
    minimax_cost_input_per_1k_cents: float = 1.0
    minimax_cost_output_per_1k_cents: float = 4.0

    # LLM — Anthropic (Claude)
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    anthropic_model: str = "claude-haiku-4-5-20251001"
    anthropic_cost_input_per_1k_cents: float = 0.1   # haiku 4.5 input
    anthropic_cost_output_per_1k_cents: float = 0.4  # haiku 4.5 output

    # LLM — OpenAI
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"
    openai_cost_input_per_1k_cents: float = 0.04
    openai_cost_output_per_1k_cents: float = 0.16

    # LLM — Anthropic (strategic)
    anthropic_api_key: str = ""

    # LLM — OpenAI (coding)
    openai_api_key: str = ""

    # Event bus
    redis_url: str = "redis://localhost:6379/0"
    bus_backend: str = "inproc"  # inproc | redis
    bus_stream: str = "studioos:events"
    bus_dlq_stream: str = "studioos:events:dlq"
    bus_max_delivery_attempts: int = 5
    bus_claim_idle_ms: int = 60_000
    bus_read_block_ms: int = 1000

    # Runtime
    scheduler_tick_seconds: float = 1.0  # dispatcher tick (legacy name)
    outbox_poll_seconds: float = 0.5
    run_timeout_seconds: int = 300
    agent_scheduler_tick_seconds: float = 15.0  # cadence scheduler tick

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # AMZ tool services (M6)
    pricefinder_url: str = ""
    pricefinder_username: str = ""
    pricefinder_password: str = ""
    pricefinder_timeout_seconds: float = 15.0
    # Direct read-only DB access for batch lookups
    pricefinder_db_url: str = ""

    # BuyBoxPricer (M12)
    buyboxpricer_db_url: str = ""
    buyboxpricer_api_url: str = "https://buyboxpricer.mifasuse.com/api/v1"
    buyboxpricer_username: str = ""
    buyboxpricer_password: str = ""

    # Notifications (M11)
    telegram_bot_token: str = ""
    telegram_default_chat_id: str = ""

    # Slack notify (M20)
    slack_bot_token: str = ""
    slack_default_channel: str = ""

    # MCP HTTP tool servers (M21)
    # Format: comma-separated "prefix=url" entries, e.g.
    #   playwright=https://pw.example/mcp,gh=https://gh.example/mcp
    mcp_http_servers: str = ""


settings = Settings()
