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
    llm_model: str = "qwen3:4b"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    ollama_host: str = "http://localhost:11434"
    ollama_think: bool = False
    """Whether to let a reasoning model think before answering.

    Off by default, and the measurement is stark: qwen3:4b took 227 seconds per call with
    thinking on — long enough that the first attempt hit the timeout and only the retry
    succeeded — against 8 seconds with it off. Filling a fixed JSON schema from supplied
    text is extraction, not reasoning, so the deliberation buys nothing here."""
    llm_call_budget: int = Field(default=40, ge=1, le=200)
    """Hard ceiling on model calls per run, enforced by the provider.

    A nominal run spends 25: one per shortlisted event, plus one per briefing story. The
    rest is headroom for the single retry each call is allowed, so a day of flaky responses
    degrades gracefully instead of stopping halfway through."""
    llm_timeout: float = Field(default=120.0, gt=0)
    """Per-call timeout. Generous: a 4B model on a laptop GPU is not fast."""

    # --- Delivery ---
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # --- Ingestion ---
    http_connect_timeout: float = Field(default=5.0, gt=0)
    http_read_timeout: float = Field(default=15.0, gt=0)
    http_max_response_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    http_max_redirects: int = Field(default=3, ge=0, le=10)
    max_article_chars: int = Field(default=20_000, ge=500, le=200_000)
    """Cap on stored article text. Bounds both repository growth and prompt size."""

    # --- Recency ---
    first_run_days: int = Field(default=2, ge=1, le=30)
    """How far back a first run looks, with no previous briefing to anchor to."""

    briefing_lookback_hours: int = Field(default=36, ge=1, le=336)
    """How far back the *briefing* reports, independent of what this run ingested.

    These are two different questions and conflating them is a bug. Ingestion asks "what
    have I not seen yet", which is correctly anchored to the last run. The briefing asks
    "what should this reader know now", which is not: a re-run three minutes after the
    last one has nothing new to ingest, and would otherwise produce an empty briefing and
    replace a good one with it."""

    max_catchup_days: int = Field(default=7, ge=1, le=90)
    """Cap on the catch-up window. After a long gap, "everything since" is thousands of
    articles and a briefing nobody reads."""

    # --- Deduplication ---
    dedup_memory_days: int = Field(default=7, ge=1, le=90)
    """How far back deduplication looks. A feed that still lists last week's post must
    not present it as news again."""

    dedup_title_threshold: float = Field(default=0.90, ge=0.5, le=1.0)
    """Title similarity above which two articles are the same article. Deliberately
    high; looser grouping is event clustering's job, not deduplication's."""

    # --- Clustering ---
    cluster_threshold: float = Field(default=0.45, ge=0.1, le=1.0)
    """Blended entity-and-title score above which an article joins an existing event."""

    event_memory_days: int = Field(default=14, ge=1, le=180)
    """How far back a running event stays open to new articles. Longer than the
    deduplication window: a story can develop for weeks."""

    # --- Ranking ---
    max_events_per_category: int = Field(default=4, ge=1, le=20)
    """Cap on how much of the shortlist one category may take. Without it, research
    fills the briefing: eighty papers a day outnumber every other category combined."""

    # --- Pipeline ---
    max_events_to_llm: int = Field(default=20, ge=1, le=100)
    stories_per_briefing: int = Field(default=5, ge=1, le=20)
    article_text_retention_days: int = Field(default=14, ge=1)

    # --- Runtime ---
    data_dir: Path = Path("data")
    site_dir: Path = Path("site")
    """Generated static site. Gitignored; GitHub Actions publishes it to Pages."""
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
