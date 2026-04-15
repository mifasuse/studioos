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

    # LLM — MiniMax (default, single provider for now)
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimax.io/v1"
    minimax_model: str = "MiniMax-M2.7-highspeed"
    # Rough per-1k-token cost in integer cents — conservative defaults,
    # override in .env when real contract is confirmed.
    minimax_cost_input_per_1k_cents: float = 1.0
    minimax_cost_output_per_1k_cents: float = 4.0

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
    scheduler_tick_seconds: float = 1.0
    outbox_poll_seconds: float = 0.5
    run_timeout_seconds: int = 300

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


settings = Settings()
