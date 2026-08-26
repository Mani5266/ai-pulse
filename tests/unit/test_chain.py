"""The provider chain: what advances it, and what deliberately does not."""

from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.llm.chain import ChainProvider
from app.llm.provider import LLMProvider, ScriptedProvider
from app.llm.schemas import ImpactScores

VALID = json.dumps(
    {
        "technical_impact": 6.0,
        "industry_impact": 5.0,
        "developer_impact": 7.0,
        "reasoning": "A capable release.",
    }
)


def spent(provider: LLMProvider) -> LLMProvider:
    """A provider whose daily allowance has already gone."""
    provider.stats.quota_exhausted = True
    return provider


def test_the_first_provider_is_used_while_it_has_allowance() -> None:
    first = ScriptedProvider([VALID], budget=5)
    second = ScriptedProvider([VALID], budget=5)

    result = ChainProvider([first, second]).structured("p", ImpactScores)

    assert result is not None
    assert first.stats.attempted == 1
    assert second.stats.attempted == 0


def test_an_exhausted_provider_hands_over_mid_run() -> None:
    """The whole point: a briefing finishes instead of publishing without prose."""
    first = spent(ScriptedProvider([], budget=5))
    second = ScriptedProvider([VALID], budget=5)

    result = ChainProvider([first, second]).structured("p", ImpactScores)

    assert result is not None
    assert second.stats.attempted == 1


def test_a_malformed_response_does_not_advance_the_chain() -> None:
    """Moving on would spend a second allowance on a problem the second provider shares."""
    first = ScriptedProvider(["not json", "still not json"], budget=5)
    second = ScriptedProvider([VALID], budget=5)

    result = ChainProvider([first, second]).structured("p", ImpactScores)

    assert result is None
    assert second.stats.attempted == 0


def test_the_chain_keeps_going_through_several_providers() -> None:
    first = spent(ScriptedProvider([], budget=5))
    second = spent(ScriptedProvider([], budget=5))
    third = ScriptedProvider([VALID], budget=5)

    result = ChainProvider([first, second, third]).structured("p", ImpactScores)

    assert result is not None
    assert third.stats.attempted == 1


def test_a_fully_spent_chain_returns_none_rather_than_raising() -> None:
    chain = ChainProvider([spent(ScriptedProvider([], budget=5)) for _ in range(3)])

    assert chain.structured("p", ImpactScores) is None
    assert chain.exhausted is True


def test_the_chain_does_not_reconsider_a_spent_provider() -> None:
    first = ScriptedProvider([VALID] * 3, budget=5)
    second = ScriptedProvider([VALID] * 3, budget=5)
    chain = ChainProvider([first, second])

    chain.structured("p", ImpactScores)
    first.stats.quota_exhausted = True
    chain.structured("p", ImpactScores)
    chain.structured("p", ImpactScores)

    assert first.stats.attempted == 1
    assert second.stats.attempted == 2


def test_statistics_are_summed_across_the_chain() -> None:
    first = ScriptedProvider([VALID], budget=5)
    second = ScriptedProvider([VALID], budget=5)
    chain = ChainProvider([first, second])

    chain.structured("p", ImpactScores)
    first.stats.quota_exhausted = True
    chain.structured("p", ImpactScores)

    assert chain.stats.attempted == 2
    assert chain.stats.succeeded == 2


def test_the_chain_reports_which_providers_it_holds() -> None:
    chain = ChainProvider([ScriptedProvider([], budget=1), ScriptedProvider([], budget=1)])

    assert chain.name.startswith("chain(")
    assert chain.name.count("scripted") == 2


def test_an_empty_chain_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ChainProvider([])


def test_remaining_counts_only_providers_not_yet_passed() -> None:
    first = spent(ScriptedProvider([], budget=10))
    second = ScriptedProvider([VALID], budget=7)
    chain = ChainProvider([first, second])

    chain.structured("p", ImpactScores)

    assert chain.remaining == 6


# --- configuration ------------------------------------------------------------


def settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {"_env_file": None}
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_the_chain_order_ignores_unknown_names() -> None:
    assert settings(llm_chain="groq, nonsense ,cerebras").chain_order() == ["groq", "cerebras"]


def test_a_tier_without_a_key_is_skipped() -> None:
    from app.llm.provider import build_provider

    provider = build_provider(
        settings(llm_provider="hosted", groq_api_key="gsk-test", llm_chain="groq,cerebras")
    )

    assert not isinstance(provider, ChainProvider)
    assert provider.name.startswith("groq:")


def test_two_keys_build_a_chain_in_the_configured_order() -> None:
    from app.llm.provider import build_provider

    provider = build_provider(
        settings(
            llm_provider="hosted",
            groq_api_key="gsk-test",
            cerebras_api_key="csk-test",
            llm_chain="cerebras,groq",
        )
    )

    assert isinstance(provider, ChainProvider)
    assert provider.name.index("cerebras") < provider.name.index("groq")


def test_the_single_key_configuration_still_works() -> None:
    """An existing deployment sets AI_PULSE_LLM_API_KEY and nothing else."""
    from app.llm.provider import build_provider

    provider = build_provider(settings(llm_provider="hosted", llm_api_key="sk-test"))

    assert not isinstance(provider, ChainProvider)


def test_a_per_tier_model_override_is_used() -> None:
    from app.llm.provider import build_provider

    provider = build_provider(
        settings(
            llm_provider="hosted",
            groq_api_key="gsk-test",
            groq_model="some-other-model",
            llm_chain="groq",
        )
    )

    assert provider.name == "groq:some-other-model"


# --- an unusable provider -----------------------------------------------------


def test_a_provider_that_refuses_the_run_hands_over() -> None:
    """A 402 second in the chain must not end a run the third could have finished."""

    class Unusable(ScriptedProvider):
        def _complete(self, prompt: str) -> str:
            from app.llm.provider import ProviderUnusableError

            raise ProviderUnusableError("cerebras: HTTP 402: Payment Required")

    first = Unusable([], budget=5)
    second = ScriptedProvider([VALID], budget=5)

    result = ChainProvider([first, second]).structured("p", ImpactScores)

    assert result is not None
    assert second.stats.attempted == 1
    assert first.stats.quota_exhausted is True


def test_an_unusable_provider_is_not_asked_twice() -> None:
    calls = 0

    class Unusable(ScriptedProvider):
        def _complete(self, prompt: str) -> str:
            nonlocal calls
            calls += 1
            from app.llm.provider import ProviderUnusableError

            raise ProviderUnusableError("openrouter: HTTP 401: invalid key")

    chain = ChainProvider([Unusable([], budget=5), ScriptedProvider([VALID] * 3, budget=5)])
    chain.structured("p", ImpactScores)
    chain.structured("p", ImpactScores)

    assert calls == 1
