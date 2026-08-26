"""Entity extraction and weighting tests."""

from __future__ import annotations

import pytest

from app.intelligence.entities import (
    GATE_WEIGHT,
    article_entities,
    extract_entities,
    has_conflicting_version,
    strongest_shared,
    weight,
    weighted_overlap,
)


def test_organisations_are_recognised() -> None:
    assert "org:openai" in extract_entities("OpenAI releases a new model")
    assert "org:google-deepmind" in extract_entities("Google DeepMind publishes a paper")


def test_organisation_aliases_map_to_one_key() -> None:
    assert extract_entities("AWS launches") & {"org:amazon"}
    assert extract_entities("Amazon launches") & {"org:amazon"}


def test_versioned_models_yield_family_and_version() -> None:
    entities = extract_entities("Introducing Gemini 3.5 Flash")

    assert "model:gemini" in entities
    assert "model:gemini-3.5" in entities


def test_unversioned_model_yields_only_the_family() -> None:
    entities = extract_entities("Claude can now use tools")

    assert "model:claude" in entities
    assert not any(entity.startswith("model:claude-") for entity in entities)


def test_ubiquitous_acronyms_are_ignored() -> None:
    """ "AI" and "LLM" in an AI feed identify nothing. On live data, "LLM" alone merged
    sixteen unrelated arXiv papers into one event."""
    entities = extract_entities("AI and LLM and ML advances")

    assert not any(entity.startswith("term:") for entity in entities)


def test_distinctive_acronyms_are_kept() -> None:
    assert "term:mmlu" in extract_entities("New MMLU results published")


def test_summary_supplements_the_title() -> None:
    entities = article_entities("A new model ships", "OpenAI announced it today.")

    assert "org:openai" in entities


def test_empty_text_yields_no_entities() -> None:
    assert extract_entities("") == frozenset()
    assert article_entities("Title", None) is not None


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        ("model:gemini-3.5", 1.0),
        ("product:chatgpt", 0.7),
        ("term:mmlu", 0.6),
        ("model:gemini", 0.6),
        ("org:openai", 0.25),
    ],
)
def test_weights_rank_specificity(entity: str, expected: float) -> None:
    assert weight(entity) == expected


def test_an_organisation_alone_cannot_pass_the_gate() -> None:
    """The rule that dissolved a twenty-two-article false cluster on live data."""
    assert weight("org:openai") < GATE_WEIGHT


def test_strongest_shared_reports_the_most_specific_overlap() -> None:
    left = frozenset({"org:google", "model:gemini", "model:gemini-3.5"})
    right = frozenset({"org:google", "model:gemini"})

    assert strongest_shared(left, right) == 0.6
    assert strongest_shared(left, frozenset({"org:google"})) == 0.25
    assert strongest_shared(left, frozenset({"org:meta"})) == 0.0


def test_weighted_overlap_favours_specific_matches() -> None:
    shared_version = weighted_overlap(
        frozenset({"org:google", "model:gemini-3.5"}),
        frozenset({"org:google", "model:gemini-3.5"}),
    )
    shared_org_only = weighted_overlap(
        frozenset({"org:google", "model:gemini-3.5"}),
        frozenset({"org:google", "model:gemma-4"}),
    )

    assert shared_version > shared_org_only


def test_weighted_overlap_of_empty_sets_is_zero() -> None:
    assert weighted_overlap(frozenset(), frozenset({"org:openai"})) == 0.0


def test_differing_versions_of_one_family_conflict() -> None:
    left = frozenset({"model:gemini", "model:gemini-3.5"})
    right = frozenset({"model:gemini", "model:gemini-4"})

    assert has_conflicting_version(left, right) is True


def test_a_missing_version_is_not_a_conflict() -> None:
    """A write-up that omits the version number is still about the same release."""
    left = frozenset({"model:gemini", "model:gemini-3.5"})
    right = frozenset({"model:gemini"})

    assert has_conflicting_version(left, right) is False


def test_different_families_do_not_conflict() -> None:
    left = frozenset({"model:gemini", "model:gemini-3.5"})
    right = frozenset({"model:llama", "model:llama-4"})

    assert has_conflicting_version(left, right) is False


def test_capitalised_words_are_not_treated_as_entities() -> None:
    """An attempt to mine distinctive capitalised words collapsed on live data.

    "Cowork", "Nemotron" and "SageMaker" are genuinely the words that identify a story,
    but so are "Desktop", "Search" and "Studio", and one shared common word was enough to
    merge a story about SpaceX shares with Anthropic's Cowork launch. Multi-source events
    went from 8 to 88, nearly all wrong. Recall here is deferred to the LLM pass in P5.
    """
    entities = extract_entities("Anthropic launches Cowork, a Claude desktop agent")

    assert entities == frozenset({"org:anthropic", "model:claude"})
