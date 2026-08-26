"""Typed configuration, loaded from the environment and an optional .env file.

Nothing in the application reads ``os.environ`` directly; everything goes through
:func:`get_settings`. That keeps configuration testable and makes the full set of knobs
discoverable in one place.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProviderName = Literal["ollama", "hosted"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """Application settings.

    Every field is prefixed with ``AI_PULSE_`` in the environment, so
    ``llm_provider`` is read from ``AI_PULSE_LLM_PROVIDER``.
    """

    model_config = SettingsConfigDict(
        env_prefix="AI_PULSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM ---
    llm_provider: LLMProviderName = "ollama"
    llm_model: str = "llama3.1:8b"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    ollama_host: str = "http://localhost:11434"
    llm_call_budget: int = Field(default=25, ge=1, le=200)

    # --- Delivery ---
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # --- Ingestion ---
    http_connect_timeout: float = Field(default=5.0, gt=0)
    http_read_timeout: float = Field(default=15.0, gt=0)
    http_max_response_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    http_max_redirects: int = Field(default=3, ge=0, le=10)

    # --- Pipeline ---
    max_events_to_llm: int = Field(default=20, ge=1, le=100)
    stories_per_briefing: int = Field(default=5, ge=1, le=20)
    article_text_retention_days: int = Field(default=14, ge=1)

    # --- Runtime ---
    data_dir: Path = Path("data")
    log_level: LogLevel = "INFO"

    @field_validator("llm_api_key", "llm_base_url", "telegram_bot_token", "telegram_chat_id")
    @classmethod
    def _empty_string_is_none(cls, value: str | None) -> str | None:
        """Treat an empty environment variable as unset.

        ``.env.example`` ships these keys blank, and a blank value should not be
        mistaken for a configured one.
        """
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @property
    def telegram_enabled(self) -> bool:
        """True when both Telegram credentials are present."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    Cached so that the .env file is read once. Tests that need different values should
    construct ``Settings(...)`` directly rather than mutating the cached instance.
    """
    return Settings()
