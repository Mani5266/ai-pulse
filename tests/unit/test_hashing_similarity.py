"""Hashing and title-similarity tests."""

from __future__ import annotations

import pytest

from app.ingestion.hashing import article_id, content_hash, normalize_text
from app.intelligence.similarity import (
    dice,
    identity_signature,
    is_near_duplicate,
    signatures_agree,
    title_similarity,
    trigrams,
)


def test_normalisation_removes_punctuation_case_and_spacing_noise() -> None:
    assert normalize_text("OpenAI's  new model — GPT-X!") == "openais new model gptx"


def test_normalisation_is_unicode_stable() -> None:
    """A smart quote and a straight quote must not produce different hashes."""
    smart_quote = "OpenAI" + chr(0x2019) + "s model"  # U+2019, not an ASCII quote

    assert normalize_text(smart_quote) == normalize_text("OpenAI's model")


def test_normalisation_handles_empty_input() -> None:
    assert normalize_text(None) == ""
    assert normalize_text("") == ""
    assert normalize_text("   ") == ""


def test_article_id_is_stable_and_short() -> None:
    first = article_id("https://example.com/a")

    assert first == article_id("https://example.com/a")
    assert len(first) == 16
    assert first != article_id("https://example.com/b")


def test_content_hash_ignores_cosmetic_differences() -> None:
    assert content_hash("OpenAI releases GPT-X", "It is fast.") == content_hash(
        "OpenAI  releases  GPT-X!", "It is fast"
    )


def test_content_hash_separates_title_from_summary() -> None:
    """Joining without a separator would let ('ab', 'c') and ('a', 'bc') collide."""
    assert content_hash("ab", "c") != content_hash("a", "bc")


def test_content_hash_changes_with_the_summary() -> None:
    assert content_hash("Same title", "one") != content_hash("Same title", "two")


def test_identical_titles_are_fully_similar() -> None:
    assert title_similarity("OpenAI releases GPT-X", "OpenAI releases GPT-X") == 1.0


def test_reworded_headline_scores_high_but_below_the_dedup_threshold() -> None:
    """Measured at 0.74.

    That is well above an unrelated pair and well below the 0.90 deduplication
    threshold, which is the intended behaviour: two publishers describing one event with
    different wording are not the same *article*. Grouping them is event clustering's
    job in P3, and deleting one of them here would throw away a source.
    """
    score = title_similarity(
        "OpenAI releases GPT-X",
        "OpenAI has released GPT-X",
    )

    assert 0.70 < score < 0.90


def test_unrelated_headlines_score_low() -> None:
    score = title_similarity(
        "OpenAI releases GPT-X",
        "EU passes new data protection rules",
    )

    assert score < 0.3


def test_different_stories_about_one_company_are_not_duplicates() -> None:
    """The threshold must not merge two genuinely different announcements."""
    score = title_similarity(
        "OpenAI releases GPT-X",
        "OpenAI announces enterprise pricing changes",
    )

    assert score < 0.9


def test_similarity_is_symmetric() -> None:
    left, right = "Model X ships today", "Model X ships"

    assert title_similarity(left, right) == title_similarity(right, left)


def test_empty_titles_are_never_similar() -> None:
    assert title_similarity("", "anything") == 0.0
    assert title_similarity("", "") == 0.0


def test_trigrams_of_empty_text_are_empty() -> None:
    assert trigrams("") == frozenset()
    assert trigrams("   ") == frozenset()


def test_dice_handles_empty_sets() -> None:
    assert dice(frozenset(), frozenset({"abc"})) == 0.0
    assert dice(frozenset({"abc"}), frozenset()) == 0.0


def test_dice_of_disjoint_sets_is_zero() -> None:
    assert dice(frozenset({"abc"}), frozenset({"xyz"})) == 0.0


@pytest.mark.parametrize("threshold", [0.5, 0.9, 1.0])
def test_identical_titles_pass_every_threshold(threshold: float) -> None:
    assert is_near_duplicate("Same headline", "Same headline", threshold) is True


def test_identity_signature_extracts_versions_dates_and_months() -> None:
    assert identity_signature("sqlite-utils 4.2.1") == ("4.2.1",)
    assert identity_signature("Gemini 3.5 Flash and 3.5 Flash-Lite") == ("3.5", "3.5")
    assert identity_signature("The latest AI news from June 2026") == ("2026", "june")
    assert identity_signature("No numbers here") == ()


def test_signatures_agree_only_when_identity_tokens_match() -> None:
    assert signatures_agree("sqlite-utils 4.2", "sqlite-utils 4.2") is True
    assert signatures_agree("sqlite-utils 4.2", "sqlite-utils 4.2.1") is False
    assert signatures_agree("News from June 2026", "News from July 2026") is False
    assert signatures_agree("Model X ships", "Model X ships today") is True
