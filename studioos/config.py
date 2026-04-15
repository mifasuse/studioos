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

    # LLM — MiniMax (default)
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimax.io/v1"
    minimax_model: str = "MiniMax-M2.7-highspeed"

    # LLM — Anthropic (strategic)
    anthropic_api_key: str = ""

    # LLM — OpenAI (coding)
    openai_api_key: str = ""

    # Runtime
    scheduler_tick_seconds: float = 1.0
    outbox_poll_seconds: float = 0.5
    run_timeout_seconds: int = 300

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000


settings = Settings()
