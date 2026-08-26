"""Provider tests: budget, retry, validation, and degradation."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.llm.provider import (
    BudgetExhaustedError,
    GroqProvider,
    LLMError,
    OllamaProvider,
    ScriptedProvider,
    build_provider,
    extract_json,
)
from app.llm.schemas import ImpactScores

VALID = json.dumps(
    {
        "technical_impact": 7.0,
        "industry_impact": 6.0,
        "developer_impact": 8.0,
        "reasoning": "A capable new open model.",
    }
)


def settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {"_env_file": None}
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


# --- validation ---------------------------------------------------------------


def test_a_valid_response_is_returned_as_a_model() -> None:
    provider = ScriptedProvider([VALID])

    result = provider.structured("prompt", ImpactScores)

    assert result is not None
    assert result.technical_impact == 7.0
    assert provider.stats.succeeded == 1


def test_markdown_fences_are_tolerated() -> None:
    """Packaging leniency, not content leniency: the result still has to validate."""
    provider = ScriptedProvider([f"```json\n{VALID}\n```"])

    assert provider.structured("prompt", ImpactScores) is not None


def test_preamble_before_the_json_is_tolerated() -> None:
    provider = ScriptedProvider([f"Here is the analysis:\n{VALID}"])

    assert provider.structured("prompt", ImpactScores) is not None


def test_extract_json_finds_the_object() -> None:
    assert json.loads(extract_json(f"noise {VALID} trailing"))["technical_impact"] == 7.0
    assert json.loads(extract_json(VALID))["industry_impact"] == 6.0


# --- retry and failure --------------------------------------------------------


def test_a_malformed_response_is_retried_once_then_succeeds() -> None:
    provider = ScriptedProvider(["not json at all", VALID])

    result = provider.structured("prompt", ImpactScores)

    assert result is not None
    assert provider.stats.attempted == 2
    assert provider.stats.retried == 1


def test_two_failures_give_up_and_return_none() -> None:
    """A model that fails twice will not succeed on a third attempt, and each costs budget."""
    provider = ScriptedProvider(["nope", "still nope"])

    assert provider.structured("prompt", ImpactScores) is None
    assert provider.stats.failed == 1
    assert provider.stats.attempted == 2


def test_a_transport_failure_is_recoverable() -> None:
    provider = ScriptedProvider([])  # runs out immediately, raising inside _complete

    assert provider.structured("prompt", ImpactScores) is None


# --- budget -------------------------------------------------------------------


def test_the_budget_is_enforced_by_the_provider() -> None:
    """A bug in a loop must not be able to exhaust a free tier overnight."""
    provider = ScriptedProvider([VALID] * 10, budget=3)

    for _ in range(3):
        provider.structured("prompt", ImpactScores)

    with pytest.raises(BudgetExhaustedError, match="budget of 3"):
        provider.structured("prompt", ImpactScores)


def test_remaining_budget_is_reported() -> None:
    provider = ScriptedProvider([VALID] * 5, budget=5)

    provider.structured("prompt", ImpactScores)

    assert provider.remaining == 4


def test_retries_consume_budget() -> None:
    provider = ScriptedProvider(["bad", VALID], budget=5)

    provider.structured("prompt", ImpactScores)

    assert provider.remaining == 3


# --- Ollama -------------------------------------------------------------------


def test_ollama_calls_localhost_only() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"message": {"content": VALID}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OllamaProvider(settings(llm_model="qwen3:4b"), client=client)

    assert provider.structured("prompt", ImpactScores) is not None
    assert seen == ["http://localhost:11434/api/chat"]
    assert provider.name == "ollama:qwen3:4b"


def test_ollama_requests_json_format() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": VALID}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    OllamaProvider(settings(), client=client).structured("prompt", ImpactScores)

    assert captured["format"] == "json"
    assert captured["stream"] is False


def test_an_empty_ollama_response_is_a_failure_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "   "}})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert OllamaProvider(settings(), client=client).structured("p", ImpactScores) is None


# --- Groq ---------------------------------------------------------------------


def test_groq_requires_an_api_key() -> None:
    with pytest.raises(LLMError, match="AI_PULSE_LLM_API_KEY"):
        GroqProvider(settings(llm_provider="hosted", llm_api_key=None))


def test_groq_sends_the_key_and_asks_for_json() -> None:
    captured: dict[str, object] = {}
    headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        headers.update(dict(request.headers))
        return httpx.Response(200, json={"choices": [{"message": {"content": VALID}}]})

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer sk-test"},
    )
    provider = GroqProvider(
        settings(llm_provider="hosted", llm_api_key="sk-test", llm_model="llama-3.3-70b"),
        client=client,
    )

    assert provider.structured("prompt", ImpactScores) is not None
    assert captured["response_format"] == {"type": "json_object"}
    assert headers["authorization"] == "Bearer sk-test"


def test_groq_rate_limiting_degrades_rather_than_crashing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GroqProvider(settings(llm_provider="hosted", llm_api_key="sk-test"), client=client)

    assert provider.structured("prompt", ImpactScores) is None


def test_a_server_error_degrades_rather_than_crashing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GroqProvider(settings(llm_provider="hosted", llm_api_key="sk-test"), client=client)

    assert provider.structured("prompt", ImpactScores) is None


# --- selection ----------------------------------------------------------------


def test_the_configured_provider_is_built() -> None:
    assert isinstance(build_provider(settings(llm_provider="ollama")), OllamaProvider)
    assert isinstance(
        build_provider(settings(llm_provider="hosted", llm_api_key="sk-test")), GroqProvider
    )


def test_call_stats_are_reported() -> None:
    provider = ScriptedProvider(["bad", VALID, VALID])

    provider.structured("prompt", ImpactScores)
    provider.structured("prompt", ImpactScores)

    stats = provider.stats.as_dict()

    assert stats["attempted"] == 3
    assert stats["succeeded"] == 2
    assert stats["retried"] == 1


def test_ollama_disables_thinking_by_default() -> None:
    """Measured: 227s per call with thinking on, 8s with it off, same task."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": VALID}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    OllamaProvider(settings(), client=client).structured("prompt", ImpactScores)

    assert captured["think"] is False


def test_ollama_thinking_can_be_enabled() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": VALID}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    OllamaProvider(settings(ollama_think=True), client=client).structured("p", ImpactScores)

    assert captured["think"] is True


def test_rate_limiting_waits_instead_of_burning_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live failure: 14 of 25 hosted calls failed because a token-per-minute limit was
    retried against immediately instead of waited out."""
    slept: list[float] = []
    monkeypatch.setattr("app.llm.provider.time.sleep", slept.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"x-ratelimit-reset-tokens": "12.5s"}, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": VALID}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GroqProvider(settings(llm_provider="hosted", llm_api_key="sk-test"), client=client)

    assert provider.structured("prompt", ImpactScores) is not None
    assert slept == [12.5]
    # The refused attempt is refunded: waiting is not a failed try.
    assert provider.stats.attempted == 1
    assert provider.stats.rate_limited == 1


def test_the_backoff_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A twenty-minute reset must not stall the run for twenty minutes."""
    slept: list[float] = []
    monkeypatch.setattr("app.llm.provider.time.sleep", slept.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"x-ratelimit-reset-requests": "20m9.6s"}, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": VALID}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GroqProvider(settings(llm_provider="hosted", llm_api_key="sk-test"), client=client)
    provider.structured("prompt", ImpactScores)

    assert slept == [30.0]


def test_persistent_rate_limiting_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.provider.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "1"}, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GroqProvider(settings(llm_provider="hosted", llm_api_key="sk-test"), client=client)

    assert provider.structured("prompt", ImpactScores) is None
    assert provider.remaining > 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [("43.455s", 43.455), ("2m30s", 150.0), ("20m9.6s", 1209.6), ("12", 12.0)],
)
def test_reset_durations_are_parsed(value: str, expected: float) -> None:
    from app.llm.provider import _parse_duration

    assert _parse_duration(value) == expected


def test_unparseable_reset_durations_fall_back() -> None:
    from app.llm.provider import _parse_duration

    assert _parse_duration("bogus") is None
    assert _parse_duration("") is None
