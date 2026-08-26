"""Tests for configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    """Build settings from explicit values, ignoring any developer .env file."""
    defaults: dict[str, object] = {"_env_file": None}
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_defaults_are_usable_without_any_environment() -> None:
    settings = _settings()

    assert settings.llm_provider == "ollama"
    assert settings.llm_call_budget == 25
    assert settings.max_events_to_llm == 20
    assert settings.stories_per_briefing == 5
    assert settings.data_dir == Path("data")


def test_blank_env_values_are_treated_as_unset() -> None:
    """`.env.example` ships secrets blank; blank must not read as configured."""
    settings = _settings(telegram_bot_token="", telegram_chat_id="   ", llm_api_key="")

    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None
    assert settings.llm_api_key is None
    assert settings.telegram_enabled is False


def test_telegram_enabled_requires_both_credentials() -> None:
    assert _settings(telegram_bot_token="t").telegram_enabled is False
    assert _settings(telegram_chat_id="c").telegram_enabled is False
    assert _settings(telegram_bot_token="t", telegram_chat_id="c").telegram_enabled is True


def test_env_prefix_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_PULSE_LLM_PROVIDER", "hosted")
    monkeypatch.setenv("AI_PULSE_LLM_MODEL", "some-hosted-model")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.llm_provider == "hosted"
    assert settings.llm_model == "some-hosted-model"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_call_budget", 0),
        ("http_connect_timeout", 0.0),
        ("http_max_response_bytes", 0),
        ("http_max_redirects", 11),
        ("max_events_to_llm", 0),
        ("stories_per_briefing", 0),
        ("article_text_retention_days", 0),
    ],
)
def test_out_of_range_values_are_rejected(field: str, value: object) -> None:
    """Bounds exist so a typo cannot silently blow the free-tier quota."""
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(llm_provider="openai")
